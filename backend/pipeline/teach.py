"""
Teaching SANS by voice: "Rahul is the new designer at Stracomm, he
handles their Instagram."

Two steps, and nothing changes in between:

  propose   the clip's transcript, plus the entries it plausibly names
            (retrieval.py, as for a note), go to one Gemini call that
            returns changes — new entries, or additions to existing ones.
  apply     you tick the ones that are right; only those are written.

The review step is not ceremony. A summary goes into every transcription
prompt and a title becomes a keyterm, so a wrongly heard name saved here
would be pushed back into the recognizer on every capture after it.
"""
import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel

logger = logging.getLogger(__name__)

TITLE_CHARS = 60
SUMMARY_CHARS = 160
NOTE_CHARS = 1000
BODY_CHARS = 4000  # VocabularySerializer's limit, so an entry stays editable
ALIAS_CHARS = 60
MAX_CHANGES = 8
CARD_BODY_CHARS = 300

TEACH_PROMPT = """\
You keep the knowledge base behind one person's voice notes: the people,
companies, places and projects in their life. They have just told you
something to remember. Turn it into changes to the knowledge base.

Today is {today}.
{speaker}
Rules:
1. Only what they said. Never add facts they didn't state, and never
   guess at what an existing entry should say.
2. Update an existing entry (give its "entity_id") rather than creating a
   duplicate. A name that sounds like an existing entry most likely is it.
3. "title": the name as it's normally written, without honorifics
   (bhaiya, di, sir, ma'am, ji). For an update, the entry's current title.
4. "summary": who or what it is in at most 12 words ("video editor at
   Credence"). It is read before every transcription, so keep it a label,
   not a story. For an existing entry, set it only when what they said
   changes who or what it is; otherwise null.
5. "note": everything else they said about it, as one or two short
   factual sentences in the language they used. Put a date on anything
   time-bound ("From Oct 2026, handles their Instagram"). Null if the
   summary already covers it.
6. "aliases": other real names for it that they used — nicknames, short
   forms, the name with an honorific. For an update, only new ones.
7. "part_of": titles of entries this belongs to — a person to their
   company, a project to its client. Use an existing entry's exact title
   when there is one.
8. "kind" is one of: {kinds}.
9. If they told you nothing to remember, return no changes.

Return JSON: "changes", each with "op" ("create" or "update"),
"entity_id" (update only, else null), "kind", "title", "aliases",
"summary", "note", "part_of".
"""


class TeachError(RuntimeError):
    pass


class _Change(BaseModel):
    op: str
    entity_id: str | None = None
    kind: str | None = None
    title: str
    aliases: list[str] = []
    summary: str | None = None
    note: str | None = None
    part_of: list[str] = []


class _Proposal(BaseModel):
    changes: list[_Change]


def _live(user_id):
    from graph.models import Node

    return Node.objects.filter(
        user_id=user_id, kind__in=Node.SEMANTIC_KINDS,
        state=Node.State.ACTIVE, merged_into__isnull=True,
    )


def _find(user_id, title):
    """An existing entry by title, then by alias — case-insensitively."""
    title = title.strip()
    if not title:
        return None
    qs = _live(user_id)
    node = qs.filter(title__iexact=title).order_by("-mention_count").first()
    if node:
        return node
    from django.db.models import CharField, F, Func, Value

    lowered = title.lower()
    joined = Func(F("aliases"), Value("|"), function="array_to_string", output_field=CharField())
    for candidate in qs.annotate(alias_text=joined).filter(alias_text__icontains=title).order_by("-mention_count"):
        if any(a.lower() == lowered for a in candidate.aliases or []):
            return candidate
    return None


def _cards(user_id, transcript):
    from graph.models import Edge

    from .retrieval import retrieve

    ids = retrieve(user_id, transcript).ids()
    nodes = {str(n.id): n for n in _live(user_id).filter(pk__in=ids)}
    lines = []
    for nid in ids:
        n = nodes.get(str(nid))
        if n is None:
            continue
        parents = Edge.objects.filter(from_node=n).select_related("to_node").values_list("to_node__title", flat=True)[:4]
        line = f"- entity_id={n.id} | {n.title} [{n.kind}]"
        if n.aliases:
            line += f" (also: {', '.join(n.aliases)})"
        if n.summary:
            line += f": {n.summary}"
        if n.body:
            line += f" Notes: {n.body[:CARD_BODY_CHARS]}"
        if parents:
            line += f" Part of: {', '.join(parents)}."
        lines.append(line)
    return lines


def _text(value, limit):
    value = re.sub(r"\s+", " ", (value or "")).strip()
    return value[:limit].rstrip() if value else ""


def clean_changes(user_id, raw):
    """Changes the model proposed, or the client sent back to apply, made
    safe: known kinds, owned entities, bounded lengths, nothing empty. An
    update whose id isn't yours is dropped; a create naming an existing
    entry becomes an update to it."""
    from graph.models import Node

    kinds = {k.value for k in Node.SEMANTIC_KINDS}
    out = []
    for item in raw[:MAX_CHANGES]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), TITLE_CHARS)
        if len(title) < 2:
            continue
        node = None
        if item.get("op") == "update" and item.get("entity_id"):
            node = _live(user_id).filter(pk=item["entity_id"]).first() if _is_uuid(item["entity_id"]) else None
            if node is None:
                continue
        if node is None:
            node = _find(user_id, title)

        aliases = [_text(a, ALIAS_CHARS) for a in item.get("aliases") or [] if isinstance(a, str)]
        known = {title.lower(), *(a.lower() for a in (node.aliases if node else []))}
        if node:
            known.add(node.title.lower())
        aliases = [a for a in dict.fromkeys(aliases) if len(a) >= 2 and a.lower() not in known]
        part_of = [_text(p, TITLE_CHARS) for p in item.get("part_of") or [] if isinstance(p, str)]
        part_of = [p for p in dict.fromkeys(part_of) if len(p) >= 2 and p.lower() != title.lower()]
        summary = _text(item.get("summary"), SUMMARY_CHARS) or None
        note = _text(item.get("note"), NOTE_CHARS) or None

        if node:
            if summary and summary == node.summary:
                summary = None
            if not (aliases or summary or note or part_of):
                continue
            out.append({
                "op": "update", "entity_id": str(node.id), "kind": node.kind, "title": node.title,
                "aliases": aliases, "summary": summary, "note": note, "part_of": part_of,
                "current_summary": node.summary,
            })
        else:
            kind = item.get("kind") if item.get("kind") in kinds else Node.Kind.CONCEPT.value
            out.append({
                "op": "create", "entity_id": None, "kind": kind, "title": title,
                "aliases": aliases, "summary": summary, "note": note, "part_of": part_of,
                "current_summary": "",
            })
    return out


def _is_uuid(value):
    import uuid

    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def propose(user, transcript):
    """(changes, model) for what was said. Raises TeachError."""
    from .gemini import client as shared_client
    from google.genai import errors, types

    from graph.models import Node

    from .models import TranscriptionProfile

    if not settings.GEMINI_API_KEY:
        raise TeachError("GEMINI_API_KEY is not set")

    today = timezone.localdate()
    notes = TranscriptionProfile.for_user(user.id).speaker_notes.strip()
    system = TEACH_PROMPT.format(
        today=f"{today:%A %d %B %Y}",
        speaker=f"About the speaker: {notes}\n" if notes else "",
        kinds=", ".join(sorted(k.value for k in Node.SEMANTIC_KINDS)),
    )
    cards = _cards(user.id, transcript)
    prompt = (
        ("Existing entries that may be relevant:\n" + "\n".join(cards) + "\n\n" if cards else "")
        + f"What they said:\n{transcript}"
    )

    client = shared_client()
    models = list(dict.fromkeys(m for m in (settings.GEMINI_SMART_MODEL, settings.GEMINI_FAST_MODEL) if m))
    failures = []
    for model in models:
        try:
            response = client.models.generate_content(
                model=model,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=_Proposal,
                ),
            )
        except errors.ServerError as exc:
            failures.append(f"{model}: {exc.code}")
            continue
        except errors.ClientError as exc:
            if exc.code == 429:
                failures.append(f"{model}: rate limited")
                continue
            raise TeachError(f"Gemini rejected the request: {exc}") from exc
        parsed = response.parsed
        if parsed is None:
            try:
                parsed = _Proposal.model_validate_json(response.text or "")
            except ValueError:
                failures.append(f"{model}: unparseable output")
                continue
        raw = [c.model_dump() for c in parsed.changes]
        return clean_changes(user.id, raw), model
    raise TeachError("Gemini unavailable: " + "; ".join(failures))


def apply(user, changes):
    """Write the changes you kept. Returns one row per change:
    {op, id, title, skipped?}. All or nothing."""
    from graph.models import Edge, Node

    from .keyterms import invalidate_keyterms
    from .tasks import embed_user_entities

    results, made = [], {}
    with transaction.atomic():
        for change in changes:
            if change["op"] == "create":
                node = _find(user.id, change["title"])  # added since it was proposed
                if node is None:
                    node = Node.objects.create(
                        user=user, kind=change["kind"], title=change["title"],
                        aliases=change["aliases"], summary=change["summary"] or "",
                        body=change["note"] or "",
                        # You said it and then approved it.
                        provenance=Node.Provenance.STATED, pinned_by_user=True,
                        mention_count=1,
                    )
                    made[node.title.lower()] = node
                    results.append({"op": "create", "id": str(node.id), "title": node.title})
                    continue
            else:
                node = _live(user.id).filter(pk=change["entity_id"]).first()
                if node is None:
                    results.append({"op": "update", "id": change["entity_id"], "title": change["title"],
                                    "skipped": "no longer exists"})
                    continue

            node.aliases = list(dict.fromkeys([*node.aliases, *change["aliases"]]))
            if change["summary"]:
                node.summary = change["summary"]
            skipped = None
            if change["note"]:
                body = "\n\n".join(p for p in (node.body.strip(), change["note"]) if p)
                if len(body) <= BODY_CHARS:
                    node.body = body
                else:
                    skipped = "notes are full — edit them to make room"
            node.pinned_by_user = True
            node.save(update_fields=["aliases", "summary", "body", "pinned_by_user"])
            made[node.title.lower()] = node
            row = {"op": "update", "id": str(node.id), "title": node.title}
            if skipped:
                row["skipped"] = skipped
            results.append(row)

        for change, row in zip(changes, results):
            if not row.get("id") or row.get("skipped") == "no longer exists":
                continue
            source = _live(user.id).filter(pk=row["id"]).first()
            unresolved = []
            for title in change["part_of"]:
                target = made.get(title.lower()) or _find(user.id, title)
                if target is None or target.pk == source.pk:
                    unresolved.append(title)
                    continue
                Edge.objects.get_or_create(
                    from_node=source, relation=Edge.Relation.PART_OF, to_node=target,
                    defaults={"provenance": Node.Provenance.STATED, "pinned_by_user": True},
                )
            if unresolved:
                row["unlinked"] = unresolved

    invalidate_keyterms(user.id)
    transaction.on_commit(lambda: embed_user_entities.delay(user.id))
    logger.info("teach: user %s applied %d changes", user.id, len(results))
    return results
