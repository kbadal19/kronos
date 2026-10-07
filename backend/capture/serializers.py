from rest_framework import serializers

from .models import Capture, Segment, Transcript


class CaptureUploadSerializer(serializers.Serializer):
    """Validates one upload from the phone's outbox.

    `id` is required and comes from the *client*. That's what makes the
    endpoint idempotent: an upload that succeeded but whose response was
    lost gets retried with the same id and collapses onto the same row.
    """

    id = serializers.UUIDField()
    audio = serializers.FileField()
    captured_at = serializers.DateTimeField()
    duration_seconds = serializers.FloatField(required=False, allow_null=True)
    device = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def validate_audio(self, value):
        from django.conf import settings

        if value.size == 0:
            raise serializers.ValidationError("Audio file is empty.")
        if value.size > settings.MAX_CAPTURE_BYTES:
            raise serializers.ValidationError(
                f"Audio exceeds {settings.MAX_CAPTURE_BYTES} bytes."
            )
        return value


class TranscriptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transcript
        fields = [
            "id", "kind", "text", "provider", "model", "confidence", "created_at", "updated_at",
        ]


class SegmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Segment
        fields = ["id", "index", "start_char", "end_char", "text"]


class _DisplayTextMixin:
    def get_text(self, obj):
        shown = obj.display_transcript()
        return shown.text if shown else ""

    def get_edited(self, obj):
        shown = obj.display_transcript()
        return bool(shown and shown.kind == Transcript.Kind.EDITED)


class CaptureSerializer(_DisplayTextMixin, serializers.ModelSerializer):
    """One capture with every version of its transcript.

    `text` is what you read: your edit, else the normalized transcript,
    else raw — so the app has something to show the moment transcription
    lands rather than waiting on the whole chain.
    """

    text = serializers.SerializerMethodField()
    edited = serializers.SerializerMethodField()
    has_audio = serializers.BooleanField(read_only=True)
    transcripts = TranscriptSerializer(many=True, read_only=True)
    segments = SegmentSerializer(many=True, read_only=True)

    class Meta:
        model = Capture
        fields = [
            "id", "status", "captured_at", "uploaded_at", "duration_seconds",
            "device", "error", "text", "edited", "has_audio", "transcripts", "segments",
        ]


class CaptureListSerializer(_DisplayTextMixin, serializers.ModelSerializer):
    """Lighter payload for the timeline — no word timings, no full
    transcript objects."""

    text = serializers.SerializerMethodField()
    edited = serializers.SerializerMethodField()

    class Meta:
        model = Capture
        fields = ["id", "status", "captured_at", "duration_seconds", "text", "edited"]


class TranscriptEditSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=50_000, trim_whitespace=True)
