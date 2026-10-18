"""
Your vocabulary and your corrections — the two lists every transcription
is checked against. Both feed the next capture straight away: vocabulary
biases Deepgram (keyterms) and briefs Gemini; corrections go into the
normalize prompt.
"""
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from graph.models import Node

from .keyterms import invalidate_keyterms
from .models import CorrectionPair
from .serializers import CorrectionSerializer, VocabularySerializer
from .tasks import embed_user_entities


def _vocabulary(user):
    return Node.objects.filter(
        user=user,
        kind__in=Node.SEMANTIC_KINDS,
        state=Node.State.ACTIVE,
        merged_into__isnull=True,
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def vocabulary(request):
    if request.method == "GET":
        words = _vocabulary(request.user).order_by("title")
        return Response(VocabularySerializer(words, many=True).data)

    serializer = VocabularySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    # Same rule as seed_entities: a name you already have gains aliases
    # rather than duplicating. Includes one you removed earlier — adding it
    # back restores it.
    existing = (
        Node.objects.filter(
            user=request.user, kind__in=Node.SEMANTIC_KINDS,
            title__iexact=data["title"], merged_into__isnull=True,
        )
        .order_by("state")  # "active" sorts first
        .first()
    )
    if existing:
        existing.aliases = list(dict.fromkeys([*existing.aliases, *data["aliases"]]))
        existing.state = Node.State.ACTIVE
        existing.save(update_fields=["aliases", "state"])
        word, code = existing, status.HTTP_200_OK
    else:
        word = Node.objects.create(
            user=request.user,
            kind=data["kind"],
            title=data["title"],
            aliases=data["aliases"],
            summary=data.get("summary", ""),
            provenance=Node.Provenance.MANUAL,
            pinned_by_user=True,
            # 1, not 0: mention_count orders the keyterm list, and a word
            # you typed in should outrank one never heard at all.
            mention_count=1,
        )
        code = status.HTTP_201_CREATED

    for wrong in data["misheard_as"]:
        if wrong.lower() != word.title.lower():
            CorrectionPair.objects.get_or_create(user=request.user, wrong=wrong, right=word.title)

    invalidate_keyterms(request.user.id)
    transaction.on_commit(lambda: embed_user_entities.delay(request.user.id))
    return Response(VocabularySerializer(word).data, status=code)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def vocabulary_detail(request, pk):
    word = get_object_or_404(_vocabulary(request.user), pk=pk)

    if request.method == "DELETE":
        # Soft: nodes are never hard-deleted, and edges may point here.
        word.state = Node.State.REJECTED
        word.save(update_fields=["state"])
        invalidate_keyterms(request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = VocabularySerializer(word, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.validated_data.pop("misheard_as", None)
    serializer.save()
    invalidate_keyterms(request.user.id)
    transaction.on_commit(lambda: embed_user_entities.delay(request.user.id))
    return Response(serializer.data)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def corrections(request):
    """GET lists them. POST takes one {wrong, right} or {"pairs": [...]} —
    the latter is what the app sends after you tick suggestions from an
    edit. Pairs you already have are skipped, not errors."""
    if request.method == "GET":
        pairs = CorrectionPair.objects.filter(user=request.user).order_by("-hits", "wrong")
        return Response(CorrectionSerializer(pairs, many=True).data)

    items = request.data.get("pairs") if isinstance(request.data, dict) else None
    items = items if isinstance(items, list) else [request.data]

    serializer = CorrectionSerializer(data=items, many=True)
    serializer.is_valid(raise_exception=True)

    saved = []
    for pair in serializer.validated_data:
        try:
            with transaction.atomic():
                obj, _ = CorrectionPair.objects.get_or_create(user=request.user, **pair)
        except IntegrityError:
            obj = CorrectionPair.objects.get(user=request.user, **pair)
        saved.append(obj)
    return Response(CorrectionSerializer(saved, many=True).data, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def correction_detail(request, pk):
    get_object_or_404(CorrectionPair, pk=pk, user=request.user).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


def _conversation_id(request):
    """(uuid or None, error Response or None)."""
    import uuid

    raw = request.data.get("conversation_id") or None
    if not raw:
        return None, None
    try:
        return uuid.UUID(str(raw)), None
    except ValueError:
        return None, Response({"detail": "Bad conversation_id."}, status=status.HTTP_400_BAD_REQUEST)


def _answer(user, question, conversation_id, spoken=False):
    from django.utils import timezone

    from .ask import AskError, ask

    try:
        result = ask(user, question, conversation_id, spoken=spoken)
    except AskError as exc:
        # The question goes back too: when it was spoken, it's the only
        # place the app learns what was heard.
        return Response(
            {"detail": f"Couldn't answer right now — {exc}", "question": question},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    def snippet(capture):
        shown = capture.display_transcript()
        text = shown.text if shown else ""
        return text if len(text) <= 240 else text[:237].rsplit(" ", 1)[0] + "…"

    return Response({
        "id": result.question_id,
        "conversation_id": result.conversation_id,
        "question": question,
        "answer": result.answer,
        "sources": [
            {
                "n": n,
                "capture_id": str(c.id),
                "captured_at": timezone.localtime(c.captured_at).isoformat(),
                "snippet": snippet(c),
            }
            for n, c in result.sources
        ],
        "entities": [{"id": str(e.id), "title": e.title, "kind": e.kind, "summary": e.summary} for e in result.entities],
    })


def _clip(request, purpose):
    """(ClipResult, None) for the uploaded `audio`, or (None, error Response)."""
    from .spoken import MAX_CLIP_BYTES, ClipError, EmptyClip, transcribe_clip

    audio = request.FILES.get("audio")
    if audio is None:
        return None, Response({"detail": "No audio."}, status=status.HTTP_400_BAD_REQUEST)
    if audio.size > MAX_CLIP_BYTES:
        return None, Response({"detail": "Too long — keep it under a few minutes."},
                              status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    try:
        return transcribe_clip(request.user.id, audio.read(), audio.name, purpose), None
    except EmptyClip:
        return None, Response({"detail": "Didn't catch that — try again."},
                              status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    except ClipError as exc:
        return None, Response({"detail": f"Couldn't hear it right now — {exc}"},
                              status=status.HTTP_503_SERVICE_UNAVAILABLE)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ask_question(request):
    """Answer a question from your notes and knowledge base. Synchronous —
    one planning call and one answering call, a few seconds."""
    question = (request.data.get("question") or "").strip() if isinstance(request.data, dict) else ""
    if not question:
        return Response({"detail": "Ask something."}, status=status.HTTP_400_BAD_REQUEST)
    if len(question) > 1000:
        return Response({"detail": "Keep it under 1000 characters."}, status=status.HTTP_400_BAD_REQUEST)

    conversation_id, error = _conversation_id(request)
    if error:
        return error
    return _answer(request.user, question, conversation_id)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def ask_spoken(request):
    """The same, asked out loud: `audio` (and optionally `conversation_id`)
    as multipart. Heard and answered in one request, so the phone makes one
    round trip; the response's `question` is what was heard."""
    conversation_id, error = _conversation_id(request)
    if error:
        return error
    heard, error = _clip(request, "question")
    if error:
        return error
    return _answer(request.user, heard.text[:1000], conversation_id, spoken=True)


# --- Knowledge: browse, inspect, correct ------------------------------------

KNOWLEDGE_PAGE = 50
KIND_GROUPS = {
    "person": [Node.Kind.PERSON],
    "company": [Node.Kind.COMPANY],
    "place": [Node.Kind.PLACE],
    "project": [Node.Kind.PROJECT, Node.Kind.GOAL],
    "other": [Node.Kind.CONCEPT, Node.Kind.TECHNOLOGY, Node.Kind.RESOURCE, Node.Kind.HABIT],
}


def _list_row(n):
    return {
        "id": str(n.id), "title": n.title, "kind": n.kind, "aliases": n.aliases,
        "summary": n.summary, "mention_count": n.mention_count,
        "client_of": (n.metadata or {}).get("client_of"),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def knowledge_list(request):
    """Everything Krono knows, searchable. ?q= matches names, aliases and
    summaries — and names by sound, so "starcom" finds Stracomm. ?kind=
    is a group (person, company, place, project, other). ?page= from 1."""
    from django.db.models import CharField, F, Func, Value

    from .retrieval import match_text

    qs = _vocabulary(request.user)
    kind = request.query_params.get("kind")
    if kind in KIND_GROUPS:
        qs = qs.filter(kind__in=KIND_GROUPS[kind])

    q = (request.query_params.get("q") or "").strip()
    if q:
        aliases = Func(F("aliases"), Value(" "), function="array_to_string", output_field=CharField())
        text_hits = list(
            qs.annotate(alias_text=aliases)
            .filter(Q(title__icontains=q) | Q(alias_text__icontains=q) | Q(summary__icontains=q))
            .order_by("-mention_count", "title")[:200]
        )
        sound = sorted(match_text(request.user.id, q).values(), key=lambda h: -h.score)
        by_id = {str(n.id): n for n in qs.filter(pk__in=[h.node_id for h in sound])}
        ordered = [by_id[h.node_id] for h in sound if h.node_id in by_id]
        seen = {str(n.id) for n in ordered}
        ordered += [n for n in text_hits if str(n.id) not in seen]
        total = len(ordered)
        rows = ordered
    else:
        qs = qs.order_by("-mention_count", "title")
        total = qs.count()
        rows = qs

    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    start = (page - 1) * KNOWLEDGE_PAGE
    chunk = list(rows[start : start + KNOWLEDGE_PAGE])
    return Response({
        "count": total,
        "next": page + 1 if start + KNOWLEDGE_PAGE < total else None,
        "results": [_list_row(n) for n in chunk],
    })


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def knowledge_detail(request, pk):
    """One entity's page: what Krono knows, your notes on it, how it
    connects, and every note that mentions it. PATCH corrects it; DELETE
    removes it from what Krono knows (soft — nothing is hard-deleted)."""
    from .knowledge import links, mentions

    node = get_object_or_404(_vocabulary(request.user), pk=pk)

    if request.method == "DELETE":
        node.state = Node.State.REJECTED
        node.save(update_fields=["state"])
        invalidate_keyterms(request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    if request.method == "PATCH":
        serializer = VocabularySerializer(node, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.validated_data.pop("misheard_as", None)
        serializer.save()
        invalidate_keyterms(request.user.id)
        transaction.on_commit(lambda: embed_user_entities.delay(request.user.id))
        node.refresh_from_db()

    total, rows = mentions(node)
    return Response({
        **VocabularySerializer(node).data,
        "client_of": (node.metadata or {}).get("client_of"),
        "links": links(node),
        "mention_total": total,
        "mentions": rows,
    })


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def knowledge_link(request, pk, other):
    """Connect (POST) or disconnect (DELETE) two entities. Connections are
    part_of edges, from this entity to the other ("Shubham" -> "Stracomm");
    DELETE removes the edge in either direction."""
    from graph.models import Edge

    node = get_object_or_404(_vocabulary(request.user), pk=pk)
    target = get_object_or_404(_vocabulary(request.user), pk=other)
    if node.pk == target.pk:
        return Response({"detail": "An entity can't link to itself."}, status=status.HTTP_400_BAD_REQUEST)

    if request.method == "POST":
        Edge.objects.get_or_create(
            from_node=node, relation=Edge.Relation.PART_OF, to_node=target,
            defaults={"provenance": Node.Provenance.MANUAL, "pinned_by_user": True},
        )
        return Response(status=status.HTTP_201_CREATED)

    Edge.objects.filter(
        Q(from_node=node, to_node=target) | Q(from_node=target, to_node=node)
    ).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# --- Teaching by voice --------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def knowledge_teach(request):
    """Tell Krono something to remember: `audio` as multipart. Returns what
    was heard and the changes it would make — nothing is saved to the
    knowledge base until you apply them."""
    from .models import Teaching
    from .teach import TeachError, propose

    heard, error = _clip(request, "teach")
    if error:
        return error
    try:
        changes, model = propose(request.user, heard.text)
    except TeachError as exc:
        return Response(
            {"detail": f"Couldn't work that out right now — {exc}", "transcript": heard.text},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    teaching = Teaching.objects.create(
        user=request.user, transcript=heard.text, heard_by=heard.via, proposed=changes, model=model,
    )
    return Response({"id": str(teaching.id), "transcript": heard.text, "changes": changes})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def knowledge_teach_apply(request, pk):
    """Save the changes you kept: {"changes": [...]}, a subset of what
    teach proposed. Re-checked here, so a stale or edited list can't reach
    another user's entries or skip the length limits."""
    from django.utils import timezone

    from .models import Teaching
    from .teach import apply, clean_changes

    teaching = get_object_or_404(Teaching, pk=pk, user=request.user)
    if teaching.applied_at:
        return Response({"detail": "Already saved."}, status=status.HTTP_409_CONFLICT)
    raw = request.data.get("changes") if isinstance(request.data, dict) else None
    if not isinstance(raw, list):
        return Response({"detail": "Send the changes to keep."}, status=status.HTTP_400_BAD_REQUEST)

    changes = clean_changes(request.user.id, raw)
    results = apply(request.user, changes) if changes else []
    teaching.applied = results
    teaching.applied_at = timezone.now()
    teaching.save(update_fields=["applied", "applied_at"])
    return Response({"applied": results})
