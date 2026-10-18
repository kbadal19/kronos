"""
Pipeline bookkeeping: what ran, with which prompt, producing what.

The point of ExtractionRun is reprocessability. Your prompts will be
mediocre at first and models will get better; in two years you'll want to
re-run extraction over everything you ever said. That's only possible if
extraction output is never the source of truth — the transcript is, and
every derived node and edge carries the run that produced it.
"""
import uuid

from django.conf import settings
from django.db import models


class ExtractionRun(models.Model):
    """One LLM pass over one capture, at one pipeline version.

    Re-running a capture creates a *new* run, writes new nodes/edges, and
    flips the previous run's output to SUPERSEDED. Nothing is deleted, so
    a regression is a diff rather than an archaeology problem.
    """

    class Stage(models.TextChoices):
        NORMALIZE = "normalize", "Normalize"
        SEGMENT = "segment", "Segment"
        EXTRACT = "extract", "Extract"
        CONSOLIDATE = "consolidate", "Consolidate"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SUPERSEDED = "superseded", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capture = models.ForeignKey(
        "capture.Capture", on_delete=models.CASCADE, null=True, blank=True,
        related_name="extraction_runs",
    )

    stage = models.CharField(max_length=20, choices=Stage.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RUNNING, db_index=True
    )

    # Bumped by hand whenever the pipeline's *shape* changes (a new stage,
    # a different chunking rule). Lets you ask "which captures haven't been
    # through v3 yet" without guessing from timestamps.
    pipeline_version = models.CharField(max_length=20)
    model = models.CharField(max_length=100, blank=True)

    # sha256 of the rendered prompt. Two runs with the same hash and the
    # same model should produce comparable output — which is what makes
    # the eval harness meaningful when you're A/B-ing prompt changes.
    prompt_hash = models.CharField(max_length=64, blank=True, db_index=True)

    # The model's response before any interpretation. When extraction does
    # something baffling six months from now, this is the only record of
    # what it actually said.
    raw_output = models.JSONField(null=True, blank=True)

    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)

    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["capture", "stage", "-created_at"], name="run_latest_idx"),
        ]

    def __str__(self):
        return f"{self.stage}/{self.pipeline_version} ({self.status})"


class EntityMerge(models.Model):
    """A record of two semantic nodes having been collapsed into one.

    You will over-merge at some point — "CB" is not always Project CB —
    and need to walk it back. Storing the absorbed node's edges here makes
    that a reversal rather than a reconstruction.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    canonical = models.ForeignKey(
        "graph.Node", on_delete=models.CASCADE, related_name="merges_won"
    )
    absorbed = models.ForeignKey(
        "graph.Node", on_delete=models.CASCADE, related_name="merges_lost"
    )

    # Edge ids that were repointed from `absorbed` to `canonical`, plus the
    # aliases that moved. Everything needed to undo this.
    moved_edge_ids = models.JSONField(default=list)
    moved_aliases = models.JSONField(default=list)

    confidence = models.FloatField(default=1.0)
    approved_by_user = models.BooleanField(default=False)
    reverted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.absorbed_id} -> {self.canonical_id}"


class CorrectionPair(models.Model):
    """Your personal misrecognition dictionary.

    Every time you fix a transcript by hand, the (wrong -> right) pair
    lands here and gets fed into the normalization prompt on subsequent
    captures. Twenty of these will fix most of what the recognizer
    routinely mangles about your particular vocabulary — and unlike a
    model upgrade, it costs nothing.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Per user: one client's corrections are names from their life, and
    # must never land in another client's prompt.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="correction_pairs"
    )
    wrong = models.CharField(max_length=200)
    right = models.CharField(max_length=200)
    hits = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-hits"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "wrong", "right"], name="unique_correction_pair_per_user"
            )
        ]

    def __str__(self):
        return f"{self.wrong} -> {self.right}"


class TranscriptionProfile(models.Model):
    """How one user speaks, so the pipeline needn't assume.

    Optional: a user without a row gets the defaults below, which are
    Hindi/English code-switching written as Hinglish. A new client who
    speaks, say, Tamil and English is a row in the admin, not a code change.
    """

    class Script(models.TextChoices):
        ROMAN = "roman", "Roman (e.g. Hinglish)"
        NATIVE = "native", "Each language in its own script"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="transcription_profile",
    )
    # Deepgram language code. Blank means settings.DEEPGRAM_LANGUAGE
    # ("multi" — code-switching across Nova-3's multilingual set).
    recognizer_language = models.CharField(max_length=20, blank=True)
    # In words, for the normalize prompt: "Hindi and English".
    languages = models.CharField(max_length=200, default="Hindi and English")
    script = models.CharField(max_length=10, choices=Script.choices, default=Script.ROMAN)
    # Free text the editor should know: who they are, their work, how they
    # talk. Goes into the normalize prompt verbatim.
    speaker_notes = models.TextField(blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} ({self.languages}, {self.script})"

    @property
    def deepgram_language(self):
        return self.recognizer_language or settings.DEEPGRAM_LANGUAGE

    @classmethod
    def for_user(cls, user_id):
        """The saved profile, or an unsaved one carrying the defaults."""
        return cls.objects.filter(user_id=user_id).first() or cls(user_id=user_id)


class Question(models.Model):
    """One question asked of the knowledge base, with everything needed to
    judge the answer later: what the planner made of it, which notes and
    entities retrieval surfaced, and which the answer actually cited.

    This is the eval set for retrieval, accumulating for free. "It said it
    didn't know" with the right note in `note_ids` is a model miss; without
    it, a retrieval miss — different fixes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="questions"
    )
    # Questions in one chat share this; the planner and the answerer see the
    # last few turns of it, which is what lets "and who's their designer?"
    # mean Stracomm's designer.
    conversation_id = models.UUIDField(null=True, blank=True, db_index=True)
    text = models.TextField()
    # Asked by voice: `text` is then what the recognizer heard, so a wrong
    # answer may be a mishearing rather than a retrieval or model miss.
    spoken = models.BooleanField(default=False)
    plan = models.JSONField(default=dict, blank=True)
    note_ids = models.JSONField(default=list, blank=True)
    entity_ids = models.JSONField(default=list, blank=True)
    answer = models.TextField(blank=True)
    cited_note_ids = models.JSONField(default=list, blank=True)
    model = models.CharField(max_length=100, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.text[:80]


class Teaching(models.Model):
    """Something you told Krono to remember, by voice, and what came of it.

    `proposed` is what the model made of `transcript`; `applied` is what you
    kept after reviewing it — the gap between the two is how the teach
    prompt gets judged. Nothing changes in the knowledge base until apply.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="teachings"
    )
    transcript = models.TextField()
    heard_by = models.CharField(max_length=100, blank=True)
    proposed = models.JSONField(default=list, blank=True)
    model = models.CharField(max_length=100, blank=True)
    applied = models.JSONField(default=list, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.transcript[:80]
