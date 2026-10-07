"""
The ingestion side: a recording, the text it produced, and the topical
slices that text was cut into.

The invariant that governs this whole app: the raw transcript is the
immutable floor of the system. Audio is transient (deleted once the raw
transcript commits), everything downstream of the transcript is derived
and regenerable. So `Transcript.kind == RAW` rows are never updated and
never deleted — if that row is wrong, the thought is lost.
"""
import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from pgvector.django import HalfVectorField, HnswIndex


class Capture(models.Model):
    """One press of the button. May contain many unrelated thoughts.

    The primary key is generated on the *phone*, not here, which is what
    makes upload idempotent: a capture that uploaded successfully but whose
    ack was lost to a dead 4am connection gets retried by the app with the
    same id and collapses into the same row instead of duplicating.
    """

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        TRANSCRIBING = "transcribing", "Transcribing"
        TRANSCRIBED = "transcribed", "Transcribed"
        NORMALIZED = "normalized", "Normalized"
        SEGMENTED = "segmented", "Segmented"
        EXTRACTED = "extracted", "Extracted"
        FAILED = "failed", "Failed"

    # Terminal-ish states once the whole chain exists. During Phase 0 the
    # chain stops at TRANSCRIBED, so what counts as "done" for the health
    # endpoint is settings.PIPELINE_TERMINAL_STATUS, not this.
    SETTLED_STATUSES = (Status.EXTRACTED, Status.FAILED)

    # The chain in order, so "at or beyond stage X" is answerable. FAILED
    # sits outside it.
    CHAIN = (
        Status.UPLOADED, Status.TRANSCRIBING, Status.TRANSCRIBED,
        Status.NORMALIZED, Status.SEGMENTED, Status.EXTRACTED,
    )

    @classmethod
    def statuses_from(cls, status):
        """`status` and every stage after it."""
        return cls.CHAIN[cls.CHAIN.index(status):]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="captures"
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.UPLOADED, db_index=True
    )

    # Where the audio is spooled between upload and transcription. Cleared
    # (and the file unlinked) once the raw transcript commits — see
    # pipeline.tasks.transcribe and sweep_expired_audio.
    audio_path = models.CharField(max_length=500, blank=True)
    audio_bytes = models.PositiveIntegerField(null=True, blank=True)
    audio_deleted_at = models.DateTimeField(null=True, blank=True)

    duration_seconds = models.FloatField(null=True, blank=True)

    # When you actually spoke, per the phone's clock. Distinct from
    # uploaded_at, which can be hours later if you were offline — and the
    # difference matters, because a 4am thought uploaded at 9am is still a
    # 4am thought.
    captured_at = models.DateTimeField(db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    device = models.CharField(max_length=100, blank=True)

    # Populated on failure so a stuck capture explains itself in the admin
    # rather than requiring a log dig.
    error = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-captured_at"]
        indexes = [
            # Drives the stall sweeper: "anything not settled, ordered by
            # how long it's been sitting there".
            models.Index(fields=["status", "updated_at"]),
        ]

    def __str__(self):
        return f"{self.captured_at:%Y-%m-%d %H:%M} ({self.status})"

    @property
    def has_audio(self):
        return bool(self.audio_path) and self.audio_deleted_at is None

    def display_transcript(self):
        """The version you read: yours, else the model's, else the
        recognizer's. Works off a prefetched `transcripts` set."""
        by_kind = {t.kind: t for t in self.transcripts.all()}
        for kind in Transcript.DISPLAY_ORDER:
            if kind in by_kind:
                return by_kind[kind]
        return None


class Transcript(models.Model):
    """What the recording said.

    RAW is what the recognizer returned, byte for byte, and is immutable.
    NORMALIZED is the cleaned-up version — punctuation, paragraph breaks,
    proper nouns corrected against your own entity list. Regenerating
    NORMALIZED is always safe; there is no path that rewrites RAW.
    EDITED is your own hand-corrected text. It outranks both for display,
    and no pipeline stage ever writes it — so re-running normalize can
    never undo a fix you made. Kept alongside NORMALIZED rather than
    replacing it, the pair is a free record of where the model went wrong.
    """

    class Kind(models.TextChoices):
        RAW = "raw", "Raw"
        NORMALIZED = "normalized", "Normalized"
        EDITED = "edited", "Edited by you"

    # Display order: the first kind present is what you read.
    DISPLAY_ORDER = (Kind.EDITED, Kind.NORMALIZED, Kind.RAW)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capture = models.ForeignKey(
        Capture, on_delete=models.CASCADE, related_name="transcripts"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)

    text = models.TextField()

    # Which recognizer/model produced this, so a provider switch is
    # visible in the data rather than inferred from dates. Blank on
    # NORMALIZED rows produced by the LLM pass (model field carries it).
    provider = models.CharField(max_length=50, blank=True)
    model = models.CharField(max_length=100, blank=True)
    confidence = models.FloatField(null=True, blank=True)

    # Word-level timings when the provider returns them. Unused in Phase 0,
    # but free to keep and impossible to recover later given audio is gone.
    words = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    # Only ever moves on EDITED rows; RAW and NORMALIZED are write-once.
    updated_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["capture", "kind"], name="one_transcript_per_kind_per_capture"
            )
        ]
        indexes = [
            # Keyword search for Ask. An expression index, so it must match
            # the query's expression exactly: SearchVector("text",
            # config="english") — see pipeline/ask.py.
            GinIndex(SearchVector("text", config="english"), name="transcript_fts_idx"),
        ]

    def __str__(self):
        return f"{self.capture_id} [{self.kind}]"


class Segment(models.Model):
    """One topically coherent slice of a normalized transcript.

    This is what makes "talk about five things in one session" work. A
    three-minute ramble that goes gym -> Project CB -> a personal thought
    becomes three segments, each of which becomes its own node.

    Boundaries are character offsets into the normalized transcript rather
    than copies, so a segment can always be traced back to the exact words
    (`text` is a denormalized convenience, not the source of truth).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capture = models.ForeignKey(
        Capture, on_delete=models.CASCADE, related_name="segments"
    )
    transcript = models.ForeignKey(
        Transcript, on_delete=models.CASCADE, related_name="segments"
    )

    index = models.PositiveSmallIntegerField()
    start_char = models.PositiveIntegerField()
    end_char = models.PositiveIntegerField()
    text = models.TextField()

    # Which run cut this boundary. Re-segmenting under a better prompt
    # writes new rows against a new run rather than mutating these.
    extraction_run = models.ForeignKey(
        "pipeline.ExtractionRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="segments",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["capture", "index"]
        constraints = [
            models.UniqueConstraint(
                fields=["capture", "extraction_run", "index"],
                name="unique_segment_index_per_run",
            )
        ]

    def __str__(self):
        return f"{self.capture_id}#{self.index}"


class NoteEmbedding(models.Model):
    """A capture's meaning as a vector, for "what did I say about…" when
    the words differ — including across languages: an English question
    finds a Hinglish note.

    Embeds the version you read (your edit, else normalized, else raw), so
    an edit re-embeds. `text_hash` is what makes that cheap: unchanged text
    is never sent again. Own table for the same reason as graph.Embedding —
    a second model can be run alongside for comparison.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capture = models.ForeignKey(Capture, on_delete=models.CASCADE, related_name="embeddings")
    model = models.CharField(max_length=100)
    text_hash = models.CharField(max_length=64)
    vector = HalfVectorField(dimensions=settings.EMBEDDING_DIMENSIONS)
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["capture", "model"], name="one_note_embedding_per_model")
        ]
        indexes = [
            HnswIndex(
                name="note_embedding_hnsw_cosine",
                fields=["vector"],
                m=16,
                ef_construction=64,
                opclasses=["halfvec_cosine_ops"],
            )
        ]
