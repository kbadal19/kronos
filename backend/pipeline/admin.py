from django.contrib import admin

from .models import CorrectionPair, EntityMerge, ExtractionRun, TranscriptionProfile


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = [
        "created_at", "stage", "status", "pipeline_version", "model", "capture",
    ]
    list_filter = ["stage", "status", "pipeline_version", "model"]
    readonly_fields = [f.name for f in ExtractionRun._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False


@admin.register(EntityMerge)
class EntityMergeAdmin(admin.ModelAdmin):
    """Where you come when the graph has decided two unrelated things are
    the same thing. Every row here is reversible by design."""

    list_display = ["absorbed", "canonical", "confidence", "approved_by_user", "reverted_at"]
    list_filter = ["approved_by_user"]
    raw_id_fields = ["canonical", "absorbed"]
    readonly_fields = ["id", "created_at", "moved_edge_ids", "moved_aliases"]


@admin.register(CorrectionPair)
class CorrectionPairAdmin(admin.ModelAdmin):
    list_display = ["wrong", "right", "hits", "user"]
    list_filter = ["user"]
    search_fields = ["wrong", "right"]


@admin.register(TranscriptionProfile)
class TranscriptionProfileAdmin(admin.ModelAdmin):
    """Where a new client's languages and speaking context get set."""

    list_display = ["user", "languages", "script", "recognizer_language", "updated_at"]
