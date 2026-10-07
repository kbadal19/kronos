from rest_framework import serializers

from .models import Edge, Node


class NodeSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Node
        fields = ["id", "kind", "title", "summary", "occurred_at", "mention_count"]


class EdgeSerializer(serializers.ModelSerializer):
    other = serializers.SerializerMethodField()
    direction = serializers.SerializerMethodField()

    class Meta:
        model = Edge
        fields = ["id", "relation", "direction", "confidence", "provenance", "other"]

    def get_direction(self, obj):
        return "out" if obj.from_node_id == self.context.get("node_id") else "in"

    def get_other(self, obj):
        node = obj.to_node if obj.from_node_id == self.context.get("node_id") else obj.from_node
        return NodeSummarySerializer(node).data


class NodeDetailSerializer(serializers.ModelSerializer):
    edges = serializers.SerializerMethodField()

    class Meta:
        model = Node
        fields = [
            "id", "kind", "title", "body", "summary", "occurred_at", "captured_at",
            "importance", "confidence", "provenance", "state", "aliases",
            "mention_count", "last_mentioned_at", "capture", "segment", "edges",
        ]

    def get_edges(self, obj):
        context = {**self.context, "node_id": obj.id}
        edges = [
            *obj.outgoing_edges.filter(state=Node.State.ACTIVE),
            *obj.incoming_edges.filter(state=Node.State.ACTIVE),
        ]
        return EdgeSerializer(edges, many=True, context=context).data
