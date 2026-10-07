"""
The graph endpoint behind the circles-and-lines view.

Only *semantic* nodes are drawn. Episodic nodes — the individual thoughts —
outnumber them by roughly 50 to 1 and would turn the canvas into static;
they're what you see when you click a circle, not what's on it.

Worth knowing before you wire a canvas to this: below roughly 200-300
semantic nodes a force-directed graph tells you less than the timeline
does. The endpoint works from day one, but the view is worth building when
the data is there, not before.
"""
import math

from django.db.models import Count
from rest_framework.decorators import api_view, permission_classes
from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Edge, Node
from .serializers import NodeDetailSerializer

# Radius floor/ceiling in px. sqrt scaling keeps one obsessive night from
# doubling a circle, and keeps Project CB from swallowing the screen while
# gym stays a legible dot.
MIN_RADIUS = 4.0
RADIUS_SCALE = 3.2
MAX_RADIUS = 48.0


def _radius(mention_count):
    return min(MAX_RADIUS, MIN_RADIUS + RADIUS_SCALE * math.sqrt(mention_count or 0))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def graph_data(request):
    try:
        limit = min(int(request.query_params.get("limit", 500)), 2000)
    except ValueError:
        limit = 500

    nodes = list(
        Node.objects.filter(
            user=request.user,
            kind__in=Node.SEMANTIC_KINDS,
            state=Node.State.ACTIVE,
            merged_into__isnull=True,
        )
        .order_by("-mention_count")[:limit]
        .values("id", "kind", "title", "mention_count", "last_mentioned_at")
    )

    node_ids = [n["id"] for n in nodes]

    # Edges only between two drawn nodes — an edge to a node that fell
    # outside the limit would render as a line into empty space.
    edges = (
        Edge.objects.filter(
            from_node_id__in=node_ids,
            to_node_id__in=node_ids,
            # Edge reuses Node.State rather than declaring its own — the
            # lifecycle is identical and two parallel enums would drift.
            state=Node.State.ACTIVE,
        )
        .values("from_node_id", "to_node_id", "relation")
        .annotate(weight=Count("id"))
    )

    return Response(
        {
            "nodes": [
                {
                    "id": str(n["id"]),
                    "title": n["title"],
                    "kind": n["kind"],
                    "mention_count": n["mention_count"],
                    "r": round(_radius(n["mention_count"]), 2),
                    "last_mentioned_at": n["last_mentioned_at"],
                }
                for n in nodes
            ],
            "edges": [
                {
                    "from": str(e["from_node_id"]),
                    "to": str(e["to_node_id"]),
                    "relation": e["relation"],
                    "weight": e["weight"],
                }
                for e in edges
            ],
        }
    )


class NodeDetailView(RetrieveAPIView):
    """What you get when you click a circle: the node, plus every thought
    edged to it, newest first."""

    serializer_class = NodeDetailSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Node.objects.filter(user=self.request.user).prefetch_related(
            "outgoing_edges__to_node", "incoming_edges__from_node"
        )
