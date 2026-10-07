"""
Browsing the knowledge base: the entity list, and one entity's page.

The page is assembled live rather than stored: connections come from the
graph, and "where you've mentioned it" is every note whose text names it —
so the page grows the moment a note mentions it, before extraction exists
to turn notes into facts.
"""
import re

from django.db.models import Q
from django.utils import timezone

MENTIONS_LIMIT = 30
SNIPPET_CHARS = 90


def spoken_names(node):
    """Title and aliases as they'd appear in a transcript — without the
    parentheticals that only disambiguate in lists ("Ayush (video editor)")."""
    names = []
    for name in (node.title, *(node.aliases or [])):
        spoken = re.sub(r"\(.*?\)", "", name).strip()
        if len(spoken) >= 3:
            names.append(spoken)
    return list(dict.fromkeys(names))


def links(node):
    """Connections in both directions, labelled the way you'd say them."""
    from graph.models import Edge, Node

    live = dict(state=Node.State.ACTIVE, merged_into__isnull=True)
    out = Edge.objects.filter(from_node=node, state=Node.State.ACTIVE, **{f"to_node__{k}": v for k, v in live.items()})
    back = Edge.objects.filter(to_node=node, state=Node.State.ACTIVE, **{f"from_node__{k}": v for k, v in live.items()})
    rows = [
        {"id": str(e.to_node_id), "title": e.to_node.title, "kind": e.to_node.kind, "summary": e.to_node.summary,
         "direction": "out", "relation": e.relation}
        for e in out.select_related("to_node")
    ] + [
        {"id": str(e.from_node_id), "title": e.from_node.title, "kind": e.from_node.kind, "summary": e.from_node.summary,
         "direction": "in", "relation": e.relation}
        for e in back.select_related("from_node")
    ]
    return sorted(rows, key=lambda r: (r["direction"], r["kind"], r["title"].lower()))


def _snippet(text, names):
    lowered = text.lower()
    hit = min((i for i in (lowered.find(n.lower()) for n in names) if i >= 0), default=0)
    start = max(0, hit - SNIPPET_CHARS)
    end = min(len(text), hit + SNIPPET_CHARS * 2)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def mentions(node):
    """(total, [newest notes naming this entity, with a snippet around the
    name]). Matches any version of the transcript, shows the one you read."""
    from capture.models import Capture

    names = spoken_names(node)
    if not names:
        return 0, []
    named = Q()
    for name in names:
        named |= Q(transcripts__text__icontains=name)
    notes = Capture.objects.filter(named, user_id=node.user_id).distinct()
    total = notes.count()
    rows = []
    for capture in notes.order_by("-captured_at").prefetch_related("transcripts")[:MENTIONS_LIMIT]:
        shown = capture.display_transcript()
        if shown is None:
            continue
        rows.append({
            "capture_id": str(capture.id),
            "captured_at": timezone.localtime(capture.captured_at).isoformat(),
            "snippet": _snippet(shown.text, names),
        })
    return total, rows
