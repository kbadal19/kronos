"""
The Celery chain. Each stage is its own task with its own retry policy and
its own replay entry point, rather than one long agent — so a failure at
extraction never costs you a transcription you already paid for.

    upload -> transcribe -> normalize -> [delete audio] -> segment
           -> extract -> resolve -> link -> embed

Normalize runs before the audio goes because it listens to the audio
itself — see pipeline/normalize.py. The chain is implemented up to it. The stages after it are
declared here but deliberately unimplemented: they should be written
against two weeks of *your real transcripts*, not imagined ones. Their
prompts are the part of this system most likely to be wrong if guessed.

Every task is idempotent. CELERY_TASK_ACKS_LATE means a worker killed
mid-task gets its message redelivered, so "run twice, same result" is a
correctness requirement here, not a nicety.
"""
import logging
import os
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Concat
from django.utils import timezone

from capture.models import Capture, Transcript

from .keyterms import get_keyterms, invalidate_keyterms
from .models import CorrectionPair, ExtractionRun, TranscriptionProfile
from .normalize import NormalizationError, NormalizationRejected, normalize_capture
from .retrieval import record_mentions
from .providers.base import (
    EmptyTranscriptError,
    TranscriptionError,
    TranscriptionRejected,
    get_provider,
)

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "0.2"


def _discard_audio(capture: Capture, *, force=False):
    """Unlink the spooled audio file.

    Called once the raw transcript is safely committed. With
    AUDIO_RETENTION_HOURS > 0 this defers to the sweeper instead, keeping
    the file just long enough to retry a transcription that came back
    garbled — the only window in which that's possible at all, since
    nothing else in the system ever stores audio.
    """
    if not capture.audio_path or capture.audio_deleted_at:
        return

    if settings.AUDIO_RETENTION_HOURS > 0 and not force:
        return

    try:
        os.unlink(capture.audio_path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("Could not unlink audio for capture %s", capture.id)
        return

    Capture.objects.filter(pk=capture.pk).update(
        audio_deleted_at=timezone.now(), audio_path=""
    )


@shared_task(
    bind=True,
    max_retries=5,
    # Provider hiccups and rate limits are the common case; back off rather
    # than burning the retry budget in the first ten seconds.
    default_retry_delay=60,
    retry_backoff=True,
    retry_jitter=True,
)
def transcribe(self, capture_id):
    capture = Capture.objects.filter(pk=capture_id).first()
    if capture is None:
        logger.warning("transcribe: capture %s vanished", capture_id)
        return

    # Idempotency: a redelivered message for an already-transcribed capture
    # must not pay for the same audio a second time.
    if capture.transcripts.filter(kind=Transcript.Kind.RAW).exists():
        logger.info("transcribe: capture %s already has a raw transcript", capture_id)
        return

    if not capture.audio_path or not os.path.exists(capture.audio_path):
        Capture.objects.filter(pk=capture.pk).update(
            status=Capture.Status.FAILED,
            error="Audio file missing before transcription — nothing to recover.",
        )
        logger.error("transcribe: audio missing for capture %s", capture_id)
        return

    Capture.objects.filter(pk=capture.pk).update(
        status=Capture.Status.TRANSCRIBING, attempts=capture.attempts + 1
    )

    provider = get_provider()
    keyterms = get_keyterms(capture.user_id)

    try:
        result = provider.transcribe(
            capture.audio_path,
            keyterms=keyterms,
            language=TranscriptionProfile.for_user(capture.user_id).deepgram_language,
        )
    except EmptyTranscriptError as exc:
        # Not retryable: more attempts against silence produce more
        # silence. Fail loudly while the audio is still on disk.
        Capture.objects.filter(pk=capture.pk).update(
            status=Capture.Status.FAILED, error=str(exc)
        )
        logger.error("transcribe: empty transcript for capture %s", capture_id)
        return
    except TranscriptionRejected as exc:
        # Same request, same refusal: fail once and say why, while the
        # audio is still on disk to retry after a fix.
        Capture.objects.filter(pk=capture.pk).update(
            status=Capture.Status.FAILED, error=str(exc)
        )
        logger.error("transcribe: provider rejected capture %s: %s", capture_id, exc)
        return
    except TranscriptionError as exc:
        Capture.objects.filter(pk=capture.pk).update(
            status=Capture.Status.UPLOADED, error=str(exc)
        )
        raise self.retry(exc=exc)

    with transaction.atomic():
        Transcript.objects.create(
            capture=capture,
            kind=Transcript.Kind.RAW,
            text=result.text,
            provider=result.provider,
            model=result.model,
            confidence=result.confidence,
            words=result.words,
        )
        Capture.objects.filter(pk=capture.pk).update(
            status=Capture.Status.TRANSCRIBED,
            duration_seconds=result.duration_seconds or capture.duration_seconds,
            error="",
        )

    logger.info(
        "transcribe: capture %s -> %d chars (%s/%s)",
        capture_id, len(result.text), result.provider, result.model,
    )

    # With normalize on, the audio has one more listener and normalize
    # discards it. Otherwise it goes now — only after the transcript is
    # committed, because the audio is the one thing that can't be regenerated.
    if settings.NORMALIZE_ENABLED:
        normalize.delay(str(capture_id))
    else:
        capture.refresh_from_db()
        _discard_audio(capture)
        embed_note.delay(str(capture_id))


def _bump_correction_hits(user_id, raw_text, text):
    """Count a correction pair as used when its wrong form was in the draft
    and its right form made it into the result. Keeps the ones that earn
    their place at the top of the next prompt."""
    raw_lower = raw_text.lower()
    for pair in CorrectionPair.objects.filter(user_id=user_id):
        if pair.wrong.lower() in raw_lower and pair.right in text:
            CorrectionPair.objects.filter(pk=pair.pk).update(hits=pair.hits + 1)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    retry_backoff=True,
    retry_jitter=True,
)
def normalize(self, capture_id, force=False):
    """Second, context-aware listen. Writes the NORMALIZED transcript.

    Never fails the capture: the raw transcript is already a usable answer,
    so a rejected or exhausted normalization just leaves raw standing.
    `force` replaces an existing NORMALIZED row (re-runs under a better
    prompt); RAW is never touched either way.
    """
    capture = Capture.objects.filter(pk=capture_id).first()
    if capture is None:
        logger.warning("normalize: capture %s vanished", capture_id)
        return

    existing = capture.transcripts.filter(kind=Transcript.Kind.NORMALIZED).first()
    if existing is not None and not force:
        logger.info("normalize: capture %s already normalized", capture_id)
        _discard_audio(capture)
        return

    raw = capture.transcripts.filter(kind=Transcript.Kind.RAW).first()
    if raw is None:
        logger.warning("normalize: capture %s has no raw transcript yet", capture_id)
        return

    run = ExtractionRun.objects.create(
        capture=capture,
        stage=ExtractionRun.Stage.NORMALIZE,
        pipeline_version=PIPELINE_VERSION,
        model=settings.NORMALIZE_MODEL,
    )

    def give_up(exc):
        ExtractionRun.objects.filter(pk=run.pk).update(
            status=ExtractionRun.Status.FAILED, error=str(exc), finished_at=timezone.now()
        )
        logger.error("normalize: capture %s keeps raw transcript: %s", capture_id, exc)
        capture.refresh_from_db()
        _discard_audio(capture)
        embed_note.delay(str(capture_id))

    try:
        result = normalize_capture(capture, raw.text)
    except NormalizationRejected as exc:
        give_up(exc)
        return
    except NormalizationError as exc:
        if self.request.retries >= self.max_retries:
            give_up(exc)
            return
        ExtractionRun.objects.filter(pk=run.pk).update(
            status=ExtractionRun.Status.FAILED, error=str(exc), finished_at=timezone.now()
        )
        raise self.retry(exc=exc)

    with transaction.atomic():
        if existing is not None:
            existing.delete()
            ExtractionRun.objects.filter(
                capture=capture,
                stage=ExtractionRun.Stage.NORMALIZE,
                status=ExtractionRun.Status.SUCCEEDED,
            ).update(status=ExtractionRun.Status.SUPERSEDED)
        Transcript.objects.create(
            capture=capture,
            kind=Transcript.Kind.NORMALIZED,
            text=result.text,
            provider="gemini",
            model=result.model,
        )
        ExtractionRun.objects.filter(pk=run.pk).update(
            status=ExtractionRun.Status.SUCCEEDED,
            prompt_hash=result.prompt_hash,
            raw_output=result.raw_output,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            finished_at=timezone.now(),
        )
        # Only move forward: a re-run on a capture that's further along
        # mustn't drag its status back.
        Capture.objects.filter(
            pk=capture.pk,
            status__in=[Capture.Status.TRANSCRIBED, Capture.Status.NORMALIZED],
        ).update(status=Capture.Status.NORMALIZED)

    _bump_correction_hits(capture.user_id, raw.text, result.text)

    # Counted once per note: a forced re-run of the same note mustn't make
    # everyone in it look twice as talked-about.
    if existing is None:
        named = record_mentions(
            capture.user_id, result.candidate_ids, result.text, capture.captured_at
        )
        if named:
            invalidate_keyterms(capture.user_id)

    capture.refresh_from_db()
    _discard_audio(capture)
    embed_note.delay(str(capture_id))

    logger.info(
        "normalize: capture %s -> %d chars, %d corrections (%s, audio=%s)",
        capture_id, len(result.text), len(result.corrections), result.model,
        result.used_audio,
    )


@shared_task(bind=True, max_retries=3, default_retry_delay=60, retry_backoff=True)
def embed_note(self, capture_id):
    """Index a note for meaning-based search once its text is final (or
    after you edit it). A failure here never touches the note itself; the
    nightly backfill catches anything that slipped through."""
    from .embeddings import EmbeddingError, embed_notes

    if not settings.GEMINI_API_KEY:
        return 0
    capture = Capture.objects.filter(pk=capture_id).prefetch_related("transcripts").first()
    if capture is None:
        return 0
    try:
        return embed_notes([capture])
    except EmbeddingError as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, retry_backoff=True)
def embed_user_entities(self, user_id):
    from .embeddings import EmbeddingError, embed_entities

    if not settings.GEMINI_API_KEY:
        return 0
    try:
        return embed_entities(user_id)
    except EmbeddingError as exc:
        raise self.retry(exc=exc)


@shared_task
def backfill_embeddings():
    """Nightly: anything that missed its embedding (an outage, a deploy
    mid-task) gets one. Cheap — unchanged text is skipped by hash."""
    from django.contrib.auth import get_user_model

    from .embeddings import EmbeddingError, embed_entities, embed_notes

    done = 0
    for user_id in get_user_model().objects.values_list("id", flat=True):
        try:
            done += embed_entities(user_id)
            captures = list(
                Capture.objects.filter(user_id=user_id).exclude(status=Capture.Status.UPLOADED)
                .prefetch_related("transcripts")
            )
            done += embed_notes(captures)
        except EmbeddingError:
            logger.exception("backfill_embeddings failed for user %s", user_id)
    return done


@shared_task
def sweep_expired_audio():
    """Delete spooled audio past its retention window.

    The per-capture delete in `transcribe` is the primary mechanism; this
    is the backstop that makes "no audio is kept" true even when a worker
    died between upload and transcription. It is also what enforces the
    retention window during Phase 0, when audio is deliberately kept for
    the provider bake-off.
    """
    cutoff = timezone.now() - timedelta(hours=settings.AUDIO_RETENTION_HOURS)

    stale = Capture.objects.filter(
        audio_deleted_at__isnull=True, uploaded_at__lt=cutoff
    ).exclude(audio_path="")

    deleted = 0
    for capture in stale.iterator():
        # force=True: the retention window is precisely what we just
        # checked, so _discard_audio shouldn't re-apply it and no-op.
        _discard_audio(capture, force=True)
        deleted += 1

    # Orphans: files on disk with no capture row pointing at them, left by
    # an upload that failed between write and commit.
    spool = settings.AUDIO_SPOOL_DIR
    if spool.exists():
        known = set(
            Capture.objects.exclude(audio_path="").values_list("audio_path", flat=True)
        )
        for path in spool.iterdir():
            if not path.is_file() or str(path) in known:
                continue
            if timezone.now().timestamp() - path.stat().st_mtime < 3600:
                continue  # might be an upload in flight
            try:
                path.unlink()
                deleted += 1
            except OSError:
                logger.exception("Could not unlink orphaned spool file %s", path)

    if deleted:
        logger.info("sweep_expired_audio: removed %d files", deleted)
    return deleted


@shared_task
def retry_stalled_captures():
    """Re-queue captures that got stuck mid-chain.

    Without this a capture orphaned by a worker OOM, a provider outage or a
    bad deploy sits in `uploaded` forever and you simply never find out —
    which for a system whose whole job is remembering things is the worst
    possible failure mode.
    """
    cutoff = timezone.now() - timedelta(minutes=20)

    stalled = Capture.objects.filter(
        status__in=[Capture.Status.UPLOADED, Capture.Status.TRANSCRIBING],
        updated_at__lt=cutoff,
        attempts__lt=8,
    ).exclude(audio_path="")

    requeued = 0
    for capture in stalled.iterator():
        logger.warning(
            "retry_stalled_captures: re-queueing %s (status=%s, attempts=%d)",
            capture.id, capture.status, capture.attempts,
        )
        transcribe.delay(str(capture.id))
        requeued += 1

    # Anything that exhausted its attempts needs a human, so stop pretending
    # it's in flight.
    Capture.objects.filter(
        status__in=[Capture.Status.UPLOADED, Capture.Status.TRANSCRIBING],
        updated_at__lt=cutoff,
        attempts__gte=8,
    ).update(
        status=Capture.Status.FAILED,
        # Keep the last real error: "gave up" alone is undebuggable.
        error=Concat(Value("Gave up after 8 transcription attempts. Last error: "), F("error")),
    )

    return requeued


@shared_task
def nightly_consolidation():
    """Phase 3. Clusters recent nodes, proposes concepts, detects
    contradictions and repetition, recomputes mention_count, writes the
    morning digest.

    Left unimplemented on purpose — it needs a populated graph to be worth
    anything, and its heuristics should be tuned against your real node
    distribution rather than a guess at one."""
    logger.info("nightly_consolidation: not implemented until Phase 3")
    return None
