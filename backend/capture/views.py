"""
The ingestion endpoint and the timeline.

The upload path is the most important code in the project. Everything else
can fail and be retried; a thought that never made it off the phone is
simply gone. Two properties carry that weight:

  idempotent   the client generates the capture id, so a retry after a
               lost response collapses onto the same row instead of
               duplicating a 4am thought.
  fast         the request writes a file and a row and returns 202. It
               never transcribes inline — a 20-second provider call inside
               the request is a 20-second window for a flaky connection to
               drop the upload you already paid to receive.
"""
import logging

from django.conf import settings
from django.db import transaction
from rest_framework import status
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from pipeline.models import CorrectionPair
from pipeline.tasks import embed_note, normalize, transcribe

from .editing import looks_like_a_name, suggest_corrections
from .models import Capture, Transcript
from .serializers import (
    CaptureListSerializer,
    CaptureSerializer,
    CaptureUploadSerializer,
    TranscriptEditSerializer,
)

logger = logging.getLogger(__name__)

# Whatever the phone recorded. Kept permissive on purpose: rejecting an
# upload because of a container mismatch would lose the thought, and the
# recognizer sniffs the format anyway.
DEFAULT_AUDIO_SUFFIX = ".m4a"
ALLOWED_SUFFIXES = {".m4a", ".mp4", ".aac", ".ogg", ".opus", ".webm", ".wav", ".mp3", ".flac"}


def _spool_path(capture_id, filename):
    suffix = ""
    if filename and "." in filename:
        candidate = "." + filename.rsplit(".", 1)[-1].lower()
        if candidate in ALLOWED_SUFFIXES:
            suffix = candidate
    return settings.AUDIO_SPOOL_DIR / f"{capture_id}{suffix or DEFAULT_AUDIO_SUFFIX}"


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_capture(request):
    serializer = CaptureUploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    capture_id = data["id"]

    # Idempotency. Returning 200 (not 409) tells the app's outbox "this one
    # is done, stop retrying" without it having to special-case an error.
    existing = Capture.objects.filter(pk=capture_id).first()
    if existing is not None:
        if existing.user_id != request.user.id:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )
        logger.info("upload_capture: duplicate upload for %s, ignoring", capture_id)
        return Response(CaptureSerializer(existing).data, status=status.HTTP_200_OK)

    settings.AUDIO_SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    path = _spool_path(capture_id, getattr(data["audio"], "name", ""))

    # Streamed in chunks rather than read whole — a long capture shouldn't
    # be able to push a 4GB box into swap.
    written = 0
    with open(path, "wb") as fh:
        for chunk in data["audio"].chunks():
            fh.write(chunk)
            written += len(chunk)

    try:
        with transaction.atomic():
            capture = Capture.objects.create(
                id=capture_id,
                user=request.user,
                status=Capture.Status.UPLOADED,
                audio_path=str(path),
                audio_bytes=written,
                duration_seconds=data.get("duration_seconds"),
                captured_at=data["captured_at"],
                device=data.get("device", ""),
            )
            # on_commit, not delay() inline: a worker is perfectly capable
            # of picking the job up before this transaction is visible,
            # then failing to find the capture it was handed.
            transaction.on_commit(lambda: transcribe.delay(str(capture.id)))
    except Exception:
        # Don't strand the audio if the row never landed.
        path.unlink(missing_ok=True)
        raise

    logger.info("upload_capture: accepted %s (%d bytes)", capture_id, written)
    return Response(CaptureSerializer(capture).data, status=status.HTTP_202_ACCEPTED)


class CaptureListView(ListAPIView):
    """The timeline. Time is the navigable spine of the whole system —
    ship this before anything visual."""

    serializer_class = CaptureListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            Capture.objects.filter(user=self.request.user)
            .prefetch_related("transcripts")
            .order_by("-captured_at")
        )


class CaptureDetailView(RetrieveAPIView):
    serializer_class = CaptureSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Capture.objects.filter(user=self.request.user).prefetch_related(
            "transcripts", "segments"
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def capture_health(request):
    """What the app polls to know the pipeline is alive.

    Surfaces stuck and failed counts rather than a bare 200, because the
    failure this system has to defend against is silent: captures that
    upload fine and then quietly never get processed.
    """
    qs = Capture.objects.filter(user=request.user)
    # At or past the terminal stage, not equal to it: with the terminal at
    # `transcribed`, a capture that went on to `normalized` is done too.
    settled = [
        *Capture.statuses_from(settings.PIPELINE_TERMINAL_STATUS),
        Capture.Status.FAILED,
    ]
    return Response(
        {
            "total": qs.count(),
            "pending": qs.exclude(status__in=settled).count(),
            "failed": qs.filter(status=Capture.Status.FAILED).count(),
            "awaiting_audio_cleanup": qs.filter(audio_deleted_at__isnull=True)
            .exclude(audio_path="")
            .count(),
        }
    )


def _own_capture(request, pk):
    return get_object_or_404(
        Capture.objects.prefetch_related("transcripts", "segments"),
        pk=pk, user=request.user,
    )


@api_view(["PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def edit_transcript(request, pk):
    """Save your own version of a transcript, or drop it (DELETE) to go
    back to the model's.

    PUT answers with the capture and `suggestions`: the short word swaps
    between what you were shown and what you saved, which the app offers
    to remember as corrections for every future note.
    """
    capture = _own_capture(request, pk)

    if request.method == "DELETE":
        capture.transcripts.filter(kind=Transcript.Kind.EDITED).delete()
        transaction.on_commit(lambda: embed_note.delay(str(pk)))
        capture = _own_capture(request, pk)
        return Response({"capture": CaptureSerializer(capture).data, "suggestions": []})

    serializer = TranscriptEditSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    text = serializer.validated_data["text"]

    shown = capture.display_transcript()
    if shown is None:
        return Response(
            {"detail": "Nothing to edit until this note is transcribed."},
            status=status.HTTP_409_CONFLICT,
        )
    before = shown.text

    Transcript.objects.update_or_create(
        capture=capture, kind=Transcript.Kind.EDITED,
        defaults={"text": text, "provider": "you"},
    )
    transaction.on_commit(lambda: embed_note.delay(str(pk)))

    known = set(
        CorrectionPair.objects.filter(user=request.user).values_list("wrong", "right")
    )
    suggestions = [
        {"wrong": wrong, "right": right, "recommended": looks_like_a_name(right)}
        for wrong, right in suggest_corrections(before, text)
        if (wrong, right) not in known
    ]

    capture = _own_capture(request, pk)
    return Response({"capture": CaptureSerializer(capture).data, "suggestions": suggestions})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def renormalize_capture(request, pk):
    """Run the Gemini pass again — typically after adding words to your
    vocabulary. With the audio still retained it re-listens; after that
    it works from the recognizer's text alone. Never touches your edit."""
    capture = _own_capture(request, pk)
    if not capture.transcripts.filter(kind=Transcript.Kind.RAW).exists():
        return Response(
            {"detail": "This note hasn't been transcribed yet."},
            status=status.HTTP_409_CONFLICT,
        )
    if not settings.NORMALIZE_ENABLED:
        return Response(
            {"detail": "Re-transcription is switched off on the server."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    normalize.delay(str(capture.id), force=True)
    return Response({"queued": True, "with_audio": capture.has_audio}, status=status.HTTP_202_ACCEPTED)
