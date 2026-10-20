"""
Answering questions from your notes and knowledge base.

Never by handing a model everything. Each question is:

  1. planned   a cheap call turns it into search terms (English and
               Hinglish) and a date range ("last week" -> real dates)
  2. searched  four ways at once, each catching what the others miss:
                 keyword   Postgres full-text over every transcript
                 meaning   note embeddings — crosses languages
                 entity    names in the question, matched by sound
                           (retrieval.py), then the notes that mention them
                 time      a hard filter when the question has a range
  3. fused     reciprocal rank fusion -> the top notes, plus cards for the
               entities involved
  4. answered  from that context only, citing notes as [n]; "I don't have
               that" when they don't say

Cost per question stays flat however large the knowledge base grows, and
every question is logged (models.Question) as a free eval set.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db.models import Q
from django.utils import timezone
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_NOTES = 12
MAX_ENTITIES = 15
MAX_NAMED = 6
NOTE_CHARS = 2000
PER_LIST = 25
RRF_K = 60  # the standard constant; dampens the head of each list


class AskError(RuntimeError):
    pass


class _Plan(BaseModel):
    standalone: str
    keywords: list[str]
    start: str | None = None
    end: str | None = None


class _Answer(BaseModel):
    answer: str
    cited: list[int]


PLAN_PROMPT = """\
You turn the latest question in a conversation about someone's own voice
notes into a search plan. The notes mix Hindi (written in Roman script)
and English.

Today is {today}.
{history}
Return JSON:
- "standalone": the latest question as a complete English sentence that
  makes sense on its own. Resolve every reference from the conversation:
  "and who's their designer?" after talking about Stracomm becomes "Who is
  the designer at Stracomm?"; "what about last week?" carries over the
  topic. Name people and companies explicitly.
- "keywords": 2-8 search terms: names, places, topics. Include Hinglish
  forms where natural ("website", "site"; "shoot"; "payment", "paisa").
  No filler words.
- "start", "end": an inclusive date range as YYYY-MM-DD when the question
  implies one ("yesterday", "last week", "in September", "on teachers'
  day"), else null. Don't invent a range the question doesn't imply.
"""

ANSWER_PROMPT = """\
You are SANS (Spoken Archive & Neural System), the speaker's personal assistant. You know their life from
their own voice notes and knowledge base, and you talk with them about it
— like a sharp assistant who's been taking notes for them, not like a
search engine.
{speaker}
How to answer:
1. Facts about their life — people, clients, what happened, what they
   said, decided or planned — come only from the notes and knowledge
   below. Never guess or fill gaps there: if the notes don't say, tell
   them plainly, and say what you do know that's close.
2. Everything else is yours to bring: advice, ideas, explanations, how to
   approach something, what they may be missing. When they ask what to
   do or for suggestions, actually advise, like an experienced
   consultant who knows their situation — build on what the notes say,
   then add what you'd recommend and why. Never just read their own
   notes back to them as the answer. Keep it clear which parts are their
   notes (cited) and which are your suggestions.
3. Cite the notes you used inline as [n]. Knowledge-base facts (who
   someone is, how people connect) need no citation.
4. Be conversational: natural, warm, direct. Answer the question first,
   then add what's useful around it — the person behind a company, a
   pending task, when they last mentioned it. No preamble like "Based on
   the notes provided".
5. For broad questions ("tell me everything about X", "what's going on
   with Y") give a well-organised overview: who or what it is, the people
   involved, what the notes say over time, anything open or pending.
   Otherwise keep it short.
6. This is a conversation: use the earlier turns to understand what
   "they", "him", "that client" refer to, and don't repeat what you've
   already told them unless asked.
7. Resolve dates against the note dates and today ({today}); say
   "yesterday", "last Tuesday" where that's clearer.
8. Reply in the language and style of the question — English or Hinglish.

Return JSON: "answer" (text with [n] citations) and "cited" (the note
numbers you used).
"""


@dataclass
class Result:
    answer: str
    sources: list = field(default_factory=list)  # [(n, Capture)]
    entities: list = field(default_factory=list)  # [Node]
    question_id: str = ""
    conversation_id: str = ""


# Models that 400 on any thinking config (flash-lite doesn't think, and
# says so by rejecting the field). Learned on first contact, per process.
_NO_THINKING_CONFIG = set()


def _generate(prompt, system, schema, models, thinking=None):
    """One structured Gemini call, falling through to the next model on
    overload or quota (same policy as normalize). `thinking` is a level
    ("low"), a token budget ("0"), or None to leave it to the model."""
    from .gemini import client as shared_client
    from google.genai import errors, types

    client = shared_client()
    failures = []
    for model in models:
        configured = thinking is not None and model not in _NO_THINKING_CONFIG
        try:
            response = _call(client, types, model, prompt, system, schema, thinking if configured else None)
        except errors.ServerError as exc:
            failures.append(f"{model}: {exc.code}")
            continue
        except errors.ClientError as exc:
            if exc.code == 429:
                failures.append(f"{model}: rate limited")
                continue
            if exc.code == 400 and configured:
                _NO_THINKING_CONFIG.add(model)
                try:
                    response = _call(client, types, model, prompt, system, schema, None)
                except errors.APIError as retry_exc:
                    failures.append(f"{model}: {retry_exc.code}")
                    continue
            else:
                raise AskError(f"Gemini rejected the request: {exc}") from exc
        parsed = response.parsed or schema.model_validate_json(response.text or "{}")
        return parsed, model, response.usage_metadata
    raise AskError("Gemini unavailable: " + "; ".join(failures))


def _thinking_config(types, thinking):
    if thinking is None:
        return None
    if str(thinking).isdigit():
        return types.ThinkingConfig(thinking_budget=int(thinking))
    return types.ThinkingConfig(thinking_level=thinking)


def _call(client, types, model, prompt, system, schema, thinking):
    return client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=_thinking_config(types, thinking),
        ),
    )


def _models(primary):
    return list(dict.fromkeys(m for m in (primary, settings.GEMINI_FAST_MODEL, settings.GEMINI_SMART_MODEL) if m))


def _history_text(history, answer_chars=600):
    if not history:
        return ""
    turns = "\n".join(f"Q: {q}\nA: {a[:answer_chars]}" for q, a in history)
    return f"Conversation so far:\n{turns}\n"


def plan(question, today, history=()):
    """Search terms, a date range, and the question made standalone using
    the conversation so far. Falls back to the question's own words on any
    failure — a plan is an optimisation, never a blocker."""
    try:
        parsed, _, _ = _generate(
            f"Latest question: {question}",
            PLAN_PROMPT.format(today=f"{today:%A %d %B %Y}", history=_history_text(history)),
            _Plan,
            _models(settings.GEMINI_FAST_MODEL),
            thinking=settings.ASK_PLAN_THINKING,
        )
        out = parsed.model_dump()
    except AskError:
        logger.warning("ask: planner failed, using raw question")
        out = {"standalone": question, "keywords": [], "start": None, "end": None}
    for key in ("start", "end"):
        try:
            out[key] = date.fromisoformat(out[key]).isoformat() if out[key] else None
        except ValueError:
            out[key] = None
    if not out["keywords"]:
        out["keywords"] = [w for w in re.findall(r"\w{3,}", question.lower())][:8]
    return out


def _date_bounds(p):
    tz = timezone.get_current_timezone()
    start = end = None
    if p.get("start"):
        start = timezone.make_aware(datetime.combine(date.fromisoformat(p["start"]), datetime.min.time()), tz)
    if p.get("end"):
        end = timezone.make_aware(
            datetime.combine(date.fromisoformat(p["end"]) + timedelta(days=1), datetime.min.time()), tz
        )
    return start, end


def _fuse(*ranked_lists):
    scores = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank + 1)
    return [item for item, _ in sorted(scores.items(), key=lambda kv: -kv[1])]


def search(user_id, question, p, query_vector=None):
    """(note ids best first, entity ids best first) for a planned question.

    `query_vector`, if given, returns the question's embedding (or raises
    EmbeddingError) — ask() starts it alongside the plan rather than
    waiting for the plan to finish."""
    from capture.models import Capture, NoteEmbedding, Transcript
    from graph.models import Embedding, Node
    from pgvector.django import CosineDistance

    from .embeddings import EmbeddingError, embed_texts
    from .retrieval import RELATED_MAX_DISTANCE, match_text

    if query_vector is None:
        def query_vector():
            [vec] = embed_texts([p["standalone"] or question], task="RETRIEVAL_QUERY")
            return vec

    notes = Capture.objects.filter(user_id=user_id, transcripts__isnull=False).distinct()
    start, end = _date_bounds(p)
    if start:
        notes = notes.filter(captured_at__gte=start)
    if end:
        notes = notes.filter(captured_at__lt=end)
    # A subquery, not a list: at 20k notes an IN (...) of every id is the
    # slow part. Postgres plans this against the indexes instead.
    note_ids = notes.values("id")
    if not notes.exists():
        return [], []

    # Keyword: any search term, ranked.
    terms = [t for t in p["keywords"] if t.strip()] or [question]
    query = None
    for term in terms:
        q = SearchQuery(term, search_type="websearch", config="english")
        query = q if query is None else query | q
    vector = SearchVector("text", config="english")  # must match transcript_fts_idx
    by_keyword = list(dict.fromkeys(
        Transcript.objects.filter(capture__user_id=user_id)
        .annotate(document=vector)
        .filter(document=query)  # the @@ match the GIN index serves
        .filter(capture_id__in=note_ids)
        .annotate(rank=SearchRank(vector, query))
        .order_by("-rank")
        .values_list("capture_id", flat=True)[: PER_LIST * 3]
    ))[:PER_LIST]

    # Meaning: question vector vs note vectors (and entity vectors).
    by_meaning, entities_by_meaning = [], []
    try:
        qvec = query_vector()
        by_meaning = list(
            NoteEmbedding.objects.filter(capture_id__in=note_ids, model=settings.GEMINI_EMBEDDING_MODEL)
            .order_by(CosineDistance("vector", qvec))
            .values_list("capture_id", flat=True)[:PER_LIST]
        )
        entities_by_meaning = list(
            Embedding.objects.filter(
                node__user_id=user_id, node__state=Node.State.ACTIVE, model=settings.GEMINI_EMBEDDING_MODEL,
            )
            .annotate(distance=CosineDistance("vector", qvec))
            .filter(distance__lt=RELATED_MAX_DISTANCE)
            .order_by("distance")
            .values_list("node_id", flat=True)[:5]
        )
        p["meaning_search"] = True
    except EmbeddingError:
        # Recorded on the Question: an answer that missed something is
        # judged differently when this leg was down.
        p["meaning_search"] = False
        logger.warning("ask: embedding unavailable, searching without meaning")

    # Entity: names in the question by sound/spelling, then notes naming them.
    # The standalone form carries names resolved from the conversation
    # ("their designer" -> "the designer at Stracomm").
    hits = match_text(user_id, " ".join([p.get("standalone") or "", question, *terms]))
    named = [h.node_id for h in sorted(hits.values(), key=lambda h: -h.score)][:MAX_NAMED]
    # One hop out: "who owns Rudraksh?" names Rudraksh, but the answer
    # lives on Dr. Rajeev Ranjan's card, linked to it.
    entity_ids = list(dict.fromkeys([*named, *map(str, entities_by_meaning), *_neighbours(named)]))[:MAX_ENTITIES]
    by_entity = []
    if named:
        mentions = Q()
        for node in Node.objects.filter(pk__in=named[:MAX_ENTITIES]).only("title", "aliases"):
            for name in (node.title, *(node.aliases or [])):
                spoken = re.sub(r"\(.*?\)", "", name).strip()
                if len(spoken) >= 3:
                    mentions |= Q(text__icontains=spoken)
        if mentions:
            by_entity = list(dict.fromkeys(
                Transcript.objects.filter(mentions, capture_id__in=note_ids)
                .order_by("-capture__captured_at")
                .values_list("capture_id", flat=True)[: PER_LIST * 3]
            ))[:PER_LIST]

    # A pure time question ("what did I do yesterday?") has nothing to rank
    # on but time itself.
    by_time = []
    if start or end:
        by_time = list(notes.order_by("-captured_at").values_list("id", flat=True)[:PER_LIST])

    fused = _fuse(by_keyword, by_meaning, by_entity, by_time)
    return fused[:MAX_NOTES], entity_ids


def _neighbours(node_ids):
    """Entities one part_of edge away from these, busiest first."""
    from graph.models import Edge, Node

    if not node_ids:
        return []
    live = dict(state=Node.State.ACTIVE, merged_into__isnull=True)
    out = Node.objects.filter(incoming_edges__from_node_id__in=node_ids, **live)
    back = Node.objects.filter(outgoing_edges__to_node_id__in=node_ids, **live)
    linked = (out | back).exclude(pk__in=node_ids).distinct().order_by("-mention_count")
    return [str(i) for i in linked.values_list("id", flat=True)[:MAX_ENTITIES]]


def _entity_cards(entity_ids):
    from graph.models import Edge, Node

    nodes = {str(n.id): n for n in Node.objects.filter(pk__in=entity_ids)}
    lines = []
    for nid in entity_ids:
        n = nodes.get(str(nid))
        if n is None:
            continue
        links = [e.to_node.title for e in Edge.objects.filter(from_node=n).select_related("to_node")[:6]]
        links += [e.from_node.title for e in Edge.objects.filter(to_node=n).select_related("from_node")[:6]]
        card = f"- {n.title} ({n.kind}"
        if n.aliases:
            card += f"; also {', '.join(n.aliases)}"
        card += ")"
        if n.summary:
            card += f": {n.summary}"
        if n.body:
            card += f" Notes: {n.body}"
        if links:
            card += f" Linked to: {', '.join(dict.fromkeys(links))}."
        lines.append(card)
    return lines


HISTORY_TURNS = 4

# Network-only work (no DB) run alongside the plan call.
_side = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ask")


def _embed_question(text):
    from . import embeddings

    [vec] = embeddings.embed_texts([text], task="RETRIEVAL_QUERY")
    return vec


class _Stopwatch:
    """Wall-clock ms per stage, for Question.timings."""

    def __init__(self, timings):
        self.timings = timings
        self.mark = time.monotonic()

    def lap(self, stage):
        now = time.monotonic()
        self.timings[stage] = int((now - self.mark) * 1000)
        self.mark = now


def ask(user, question, conversation_id=None, spoken=False, timings=None):
    """The whole loop. Logs a Question row whatever happens. Pass the
    conversation_id from a previous answer to continue that chat; `spoken`
    marks a question that was heard rather than typed. `timings` carries
    stages measured before this (e.g. hearing the clip) into the log."""
    import uuid

    from capture.models import Capture

    from .models import Question, TranscriptionProfile

    started = time.monotonic()
    timings = dict(timings or {})
    watch = _Stopwatch(timings)
    today = timezone.localdate()
    conversation_id = conversation_id or uuid.uuid4()
    history = [
        (q.text, q.answer)
        for q in reversed(
            Question.objects.filter(user=user, conversation_id=conversation_id)
            .exclude(answer="")
            .order_by("-created_at")[:HISTORY_TURNS]
        )
    ]
    log = Question(user=user, text=question, conversation_id=conversation_id, spoken=spoken)
    result = None

    try:
        # A first question is embedded as asked, while the plan is being
        # made: the two calls are each ~1s and neither needs the other.
        # A follow-up ("and their designer?") means nothing on its own, so
        # it waits for the plan's standalone form instead.
        query_vector = None
        if not history and settings.GEMINI_API_KEY:
            pending = _side.submit(_embed_question, question)
            query_vector = pending.result
        watch.lap("prepare")
        p = plan(question, today, history)
        log.plan = p
        watch.lap("plan")
        note_ids, entity_ids = search(user.id, question, p, query_vector)
        watch.lap("search")
        log.note_ids = [str(i) for i in note_ids]
        log.entity_ids = [str(i) for i in entity_ids]

        captures = {
            c.id: c for c in Capture.objects.filter(pk__in=note_ids).prefetch_related("transcripts")
        }
        numbered = [(i, captures[nid]) for i, nid in enumerate(note_ids, 1) if nid in captures]

        if not numbered and not entity_ids:
            when = f" between {p['start']} and {p['end']}" if p.get("start") else ""
            log.answer = f"I don't have any notes{when} that cover this."
            result = Result(answer=log.answer)
            return result

        blocks = []
        for n, c in numbered:
            shown = c.display_transcript()
            stamp = timezone.localtime(c.captured_at)
            blocks.append(f"[{n}] {stamp:%a %d %b %Y, %H:%M}: {shown.text[:NOTE_CHARS] if shown else ''}")
        cards = _entity_cards(entity_ids)

        notes = TranscriptionProfile.for_user(user.id).speaker_notes.strip()
        system = ANSWER_PROMPT.format(
            speaker=f"About the speaker: {notes}\n" if notes else "",
            today=f"{today:%A %d %B %Y}",
        )
        prompt = (
            _history_text(history)
            + ("\n" if history else "")
            + f"Question: {question}\n\n"
            + ("Notes:\n" + "\n\n".join(blocks) + "\n\n" if blocks else "Notes: none matched.\n\n")
            + ("Knowledge base:\n" + "\n".join(cards) if cards else "")
        )
        watch.lap("context")
        parsed, model, usage = _generate(
            prompt, system, _Answer, _models(settings.GEMINI_SMART_MODEL),
            thinking=settings.ASK_ANSWER_THINKING,
        )
        watch.lap("answer")

        cited = [n for n in dict.fromkeys(parsed.cited) if 1 <= n <= len(numbered)]
        by_n = dict(numbered)
        log.answer = parsed.answer.strip()
        log.cited_note_ids = [str(by_n[n].id) for n in cited]
        log.model = model
        log.input_tokens = getattr(usage, "prompt_token_count", None)
        log.output_tokens = getattr(usage, "candidates_token_count", None)

        from graph.models import Node

        nodes = {str(n.id): n for n in Node.objects.filter(pk__in=entity_ids)}
        result = Result(
            answer=log.answer,
            sources=[(n, by_n[n]) for n in cited],
            entities=[nodes[str(i)] for i in entity_ids if str(i) in nodes],
        )
        return result
    except AskError as exc:
        log.error = str(exc)
        raise
    finally:
        log.latency_ms = int((time.monotonic() - started) * 1000)
        log.timings = timings
        log.save()
        if result is not None:
            result.question_id = str(log.id)
            result.conversation_id = str(conversation_id)
