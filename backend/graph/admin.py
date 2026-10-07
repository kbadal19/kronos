from django.contrib import admin

from .models import Digest, Edge, Embedding, Node, ReviewItem


@admin.register(Node)
class NodeAdmin(admin.ModelAdmin):
    list_display = ["title", "kind", "state", "mention_count", "occurred_at", "provenance"]
    list_filter = ["kind", "state", "provenance", "pinned_by_user"]
    search_fields = ["title", "body", "summary", "aliases"]
    readonly_fields = ["id", "ingested_at", "capture", "segment", "extraction_run"]
    date_hierarchy = "captured_at"


@admin.register(Edge)
class EdgeAdmin(admin.ModelAdmin):
    list_display = ["from_node", "relation", "to_node", "confidence", "state"]
    list_filter = ["relation", "state", "provenance"]
    readonly_fields = ["id", "created_at", "extraction_run"]
    raw_id_fields = ["from_node", "to_node"]


@admin.register(ReviewItem)
class ReviewItemAdmin(admin.ModelAdmin):
    """The middle confidence band. If this list grows without bound, the
    bands in settings need retuning — not more reviewing."""

    list_display = ["kind", "state", "created_at", "resolved_at"]
    list_filter = ["kind", "state"]
    readonly_fields = ["id", "created_at"]


@admin.register(Digest)
class DigestAdmin(admin.ModelAdmin):
    list_display = ["covers_date", "kind", "created_at"]
    list_filter = ["kind"]
    date_hierarchy = "covers_date"


@admin.register(Embedding)
class EmbeddingAdmin(admin.ModelAdmin):
    list_display = ["node", "model", "created_at"]
    list_filter = ["model"]
    # The vector itself is 768 floats — rendering it in a form is pointless
    # and slow.
    exclude = ["vector"]
    raw_id_fields = ["node"]
