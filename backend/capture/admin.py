"""Admin is the debugging surface for this project — with one user and no
dashboard, it's where you go when a capture stalls at 4am."""
from django.contrib import admin

from .models import Capture, Segment, Transcript


class TranscriptInline(admin.TabularInline):
    model = Transcript
    extra = 0
    # Raw transcripts are the immutable floor of the system; making them
    # editable here would be a footgun with no undo.
    readonly_fields = ["kind", "provider", "model", "confidence", "created_at"]
    fields = ["kind", "text", "provider", "model", "confidence", "created_at"]


class SegmentInline(admin.TabularInline):
    model = Segment
    extra = 0
    readonly_fields = ["index", "start_char", "end_char", "text"]


@admin.register(Capture)
class CaptureAdmin(admin.ModelAdmin):
    list_display = [
        "captured_at", "status", "duration_seconds", "attempts", "has_audio", "device",
    ]
    list_filter = ["status", "device"]
    date_hierarchy = "captured_at"
    search_fields = ["id", "transcripts__text"]
    readonly_fields = [
        "id", "uploaded_at", "updated_at", "audio_path", "audio_bytes",
        "audio_deleted_at", "attempts",
    ]
    inlines = [TranscriptInline, SegmentInline]

    @admin.display(boolean=True, description="Audio on disk")
    def has_audio(self, obj):
        return obj.has_audio
