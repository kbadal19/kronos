from rest_framework import serializers

from graph.models import Node

from .models import CorrectionPair

# The kinds offered when you add a word by hand. Narrower than every
# semantic kind on purpose: these are the ones that get misheard.
VOCABULARY_KINDS = [
    Node.Kind.PERSON, Node.Kind.COMPANY, Node.Kind.PROJECT,
    Node.Kind.PLACE, Node.Kind.TECHNOLOGY, Node.Kind.CONCEPT,
]


class VocabularySerializer(serializers.ModelSerializer):
    """A word the recognizer and the normalize pass should know.

    `aliases` are other real names for it ("CB" for Project CB): they bias
    Deepgram toward hearing them. `misheard_as` is the opposite — wrong
    forms ("Starcom" for Stracomm) — so they're saved as corrections
    instead; as keyterms they'd teach the recognizer the mistake."""

    # Adding by hand offers VOCABULARY_KINDS; editing accepts any semantic
    # kind, since loaded knowledge includes goals, habits and the rest.
    kind = serializers.ChoiceField(
        choices=sorted(k.value for k in Node.SEMANTIC_KINDS), default=Node.Kind.CONCEPT,
    )
    aliases = serializers.ListField(
        child=serializers.CharField(max_length=60), required=False, default=list, max_length=20,
    )
    misheard_as = serializers.ListField(
        child=serializers.CharField(max_length=60), required=False, default=list,
        max_length=20, write_only=True,
    )

    class Meta:
        model = Node
        fields = [
            "id", "title", "kind", "aliases", "misheard_as", "summary", "body",
            "mention_count", "last_mentioned_at",
        ]
        read_only_fields = ["id", "mention_count", "last_mentioned_at"]
        extra_kwargs = {
            "title": {"max_length": 60},
            # summary goes into transcription prompts: keep it a line.
            "summary": {"max_length": 160, "required": False},
            # body is your private notes — for answers, never per-note prompts.
            "body": {"max_length": 4000, "required": False},
        }

    def validate_title(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("At least two characters.")
        return value

    def validate_aliases(self, value):
        return list(dict.fromkeys(a.strip() for a in value if a.strip()))

    def validate_misheard_as(self, value):
        return list(dict.fromkeys(a.strip() for a in value if a.strip()))


class CorrectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CorrectionPair
        fields = ["id", "wrong", "right", "hits", "created_at"]
        read_only_fields = ["id", "hits", "created_at"]
        extra_kwargs = {"wrong": {"max_length": 200}, "right": {"max_length": 200}}

    def validate(self, attrs):
        attrs["wrong"], attrs["right"] = attrs["wrong"].strip(), attrs["right"].strip()
        if not attrs["wrong"] or not attrs["right"] or attrs["wrong"] == attrs["right"]:
            raise serializers.ValidationError("Wrong and right must differ.")
        return attrs
