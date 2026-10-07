"""
Vectors for notes and entities, so search can match on meaning.

Keyword search finds "Doon ka website" from "website"; it can't find it
from "what's pending for the school?". Embeddings can, and they cross the
language line — an English question lands on a Hinglish note — which for
a bilingual knowledge base is most of the point.

Everything here is incremental: each row stores a hash of the text it was
made from, and unchanged text is never sent again.
"""
import hashlib
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

BATCH = 100  # Gemini's embedContent accepts up to 100 inputs per call.
NOTE_CHARS = 6000
RETRIES = 2
BACKOFF_SECONDS = 1.5


class EmbeddingError(RuntimeError):
    pass


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def embed_texts(texts, *, task):
    """task: "RETRIEVAL_DOCUMENT" for what's stored, "RETRIEVAL_QUERY" for
    a question. Asymmetric on purpose — the model embeds them differently."""
    from google import genai
    from google.genai import errors, types

    if not settings.GEMINI_API_KEY:
        raise EmbeddingError("GEMINI_API_KEY is not set")
    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    vectors = []
    for start in range(0, len(texts), BATCH):
        chunk = texts[start : start + BATCH]
        for attempt in range(RETRIES + 1):
            try:
                result = client.models.embed_content(
                    model=settings.GEMINI_EMBEDDING_MODEL,
                    contents=chunk,
                    config=types.EmbedContentConfig(
                        task_type=task, output_dimensionality=settings.EMBEDDING_DIMENSIONS,
                    ),
                )
                break
            except errors.APIError as exc:
                # 429 here is Google's shared-capacity "busy", not a spent
                # quota: it clears in seconds. Without a retry, a question
                # asked in a busy moment silently loses meaning search.
                transient = exc.code == 429 or (exc.code or 0) >= 500
                if not transient or attempt == RETRIES:
                    raise EmbeddingError(f"Gemini embedding failed: {exc}") from exc
                time.sleep(BACKOFF_SECONDS * (2**attempt))
        vectors.extend(e.values for e in result.embeddings)
    return vectors


def note_text(capture):
    """What a note is embedded as: the version you read, dated, because
    "what was I doing on teachers' day" is a question about time too."""
    from django.utils import timezone

    shown = capture.display_transcript()
    if not shown or not shown.text.strip():
        return ""
    when = timezone.localtime(capture.captured_at)
    return f"{when:%A %d %B %Y}. {shown.text.strip()[:NOTE_CHARS]}"


def entity_text(node):
    """Name, aliases and what it is — the body too, since "the client who
    scolds us about Google ranking" is exactly the kind of question the
    body answers."""
    parts = [node.title]
    if node.aliases:
        parts.append(f"also called {', '.join(node.aliases)}")
    parts.append(f"({node.kind})")
    if node.summary:
        parts.append(node.summary)
    if node.body:
        parts.append(node.body)
    return ". ".join(parts)


def embed_notes(captures):
    """Embed whichever of these notes changed since last time. Returns the
    number embedded."""
    from capture.models import NoteEmbedding

    model = settings.GEMINI_EMBEDDING_MODEL
    existing = {
        e.capture_id: e
        for e in NoteEmbedding.objects.filter(capture__in=captures, model=model)
    }
    todo = []
    for capture in captures:
        text = note_text(capture)
        if not text:
            continue
        digest = _hash(text)
        row = existing.get(capture.id)
        if row is None or row.text_hash != digest:
            todo.append((capture, text, digest))
    if not todo:
        return 0

    vectors = embed_texts([t for _, t, _ in todo], task="RETRIEVAL_DOCUMENT")
    for (capture, _, digest), vector in zip(todo, vectors):
        NoteEmbedding.objects.update_or_create(
            capture=capture, model=model, defaults={"vector": vector, "text_hash": digest},
        )
    return len(todo)


def embed_entities(user_id):
    """Embed the user's active entities whose text changed. Returns the
    number embedded."""
    from graph.models import Embedding, Node

    model = settings.GEMINI_EMBEDDING_MODEL
    nodes = list(
        Node.objects.filter(
            user_id=user_id, kind__in=Node.SEMANTIC_KINDS,
            state=Node.State.ACTIVE, merged_into__isnull=True,
        )
    )
    existing = dict(
        Embedding.objects.filter(node__in=nodes, model=model).values_list("node_id", "text_hash")
    )
    todo = [(n, entity_text(n)) for n in nodes]
    todo = [(n, t, _hash(t)) for n, t in todo if existing.get(n.id) != _hash(t)]
    if not todo:
        return 0

    vectors = embed_texts([t for _, t, _ in todo], task="RETRIEVAL_DOCUMENT")
    for (node, _, digest), vector in zip(todo, vectors):
        Embedding.objects.update_or_create(
            node=node, model=model, defaults={"vector": vector, "text_hash": digest},
        )
    return len(todo)
