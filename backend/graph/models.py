"""
The knowledge graph itself.

One `Node` table, not two. The alternative — separate `knowledge_items`
and `entities` tables — forces polymorphic edges (from_type/from_id pairs),
which throws away foreign keys and makes every traversal query a union.
Instead there's a single table with a `kind` discriminator, split
conceptually into two families that behave differently:

  episodic  things you actually said. One per Segment. Immutable,
            timestamped, never merged.
  semantic  things that persist and accumulate — Project CB, the gym,
            your father. These carry aliases, get merged, and are the
            circles in the graph view.

Nothing here is ever hard-deleted. Reprocessing writes new rows against a
new ExtractionRun and flips the old ones to SUPERSEDED, which is what lets
you re-run extraction over two years of thoughts without losing the
history of how the system used to read them.
"""
import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from pgvector.django import HalfVectorField, HnswIndex


class Node(models.Model):
    class Kind(models.TextChoices):
        # --- episodic: things you said ---
        THOUGHT = "thought", "Thought"
        IDEA = "idea", "Idea"
        DECISION = "decision", "Decision"
        TASK = "task", "Task"
        QUESTION = "question", "Question"
        OBSERVATION = "observation", "Observation"
        PROBLEM = "problem", "Problem"
        LEARNING = "learning", "Learning"
        EVENT = "event", "Event"
        # --- semantic: things that accumulate ---
        PERSON = "person", "Person"
        PROJECT = "project", "Project"
        COMPANY = "company", "Company"
        CONCEPT = "concept", "Concept"
        TECHNOLOGY = "technology", "Technology"
        RESOURCE = "resource", "Resource"
        PLACE = "place", "Place"
        HABIT = "habit", "Habit"
        GOAL = "goal", "Goal"

    EPISODIC_KINDS = frozenset({
        Kind.THOUGHT, Kind.IDEA, Kind.DECISION, Kind.TASK, Kind.QUESTION,
        Kind.OBSERVATION, Kind.PROBLEM, Kind.LEARNING, Kind.EVENT,
    })
    SEMANTIC_KINDS = frozenset({
        Kind.PERSON, Kind.PROJECT, Kind.COMPANY, Kind.CONCEPT, Kind.TECHNOLOGY,
        Kind.RESOURCE, Kind.PLACE, Kind.HABIT, Kind.GOAL,
    })

    class Provenance(models.TextChoices):
        STATED = "stated", "Stated"          # you said it, more or less literally
        INFERRED = "inferred", "Inferred"    # the model concluded it from what you said
        DERIVED = "derived", "Derived"       # the nightly job worked it out across captures
        MANUAL = "manual", "Manual"          # you typed or corrected it by hand

    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        SUGGESTED = "suggested", "Suggested"      # awaiting review
        SUPERSEDED = "superseded", "Superseded"   # replaced by a later extraction run
        REJECTED = "rejected", "Rejected"         # you said no

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="nodes"
    )

    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True)
    summary = models.TextField(blank=True)

    # --- three timestamps, deliberately ---
    # occurred_at   when the thought/event actually happened or refers to.
    #               "Last Tuesday I realised..." said at 4am on Friday
    #               resolves to Tuesday. This is what temporal queries use.
    # captured_at   when you spoke it.
    # ingested_at   when this row was written. Differs from captured_at on
    #               any reprocessing run.
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    captured_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    importance = models.FloatField(default=0.5)
    confidence = models.FloatField(default=1.0)
    provenance = models.CharField(
        max_length=20, choices=Provenance.choices, default=Provenance.STATED
    )
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.ACTIVE, db_index=True
    )

    # Semantic nodes only. "CB", "the observability project" and "Project
    # CB" all land on one row, and the aliases are what feed the
    # recognizer's keyterm list on the next capture.
    aliases = ArrayField(models.CharField(max_length=200), default=list, blank=True)

    # Set when this node lost a merge. Kept rather than deleted so an
    # over-eager merge can be walked back — see pipeline.models.EntityMerge.
    merged_into = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="absorbed"
    )

    # Provenance chain back to the words you actually spoke.
    capture = models.ForeignKey(
        "capture.Capture", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nodes",
    )
    segment = models.ForeignKey(
        "capture.Segment", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nodes",
    )
    extraction_run = models.ForeignKey(
        "pipeline.ExtractionRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nodes",
    )

    # Anything you edited or approved by hand survives reprocessing
    # untouched. Without this, improving the extractor would quietly
    # destroy your manual corrections.
    pinned_by_user = models.BooleanField(default=False)

    # Denormalised, maintained incrementally by the link stage and
    # recomputed nightly as a correctness backstop. Drives circle radius
    # (sqrt of this) and opacity in the graph view.
    mention_count = models.PositiveIntegerField(default=0, db_index=True)
    last_mentioned_at = models.DateTimeField(null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True)
    search_vector = SearchVectorField(null=True, blank=True)

    class Meta:
        ordering = ["-occurred_at", "-ingested_at"]
        indexes = [
            # The BM25 half of hybrid retrieval. Lexical search is not
            # optional here: "Project CB" and "RCA" are exactly the queries
            # pure vector search is worst at.
            GinIndex(fields=["search_vector"], name="node_search_vector_gin"),
            GinIndex(fields=["aliases"], name="node_aliases_gin"),
            models.Index(fields=["kind", "state", "-mention_count"], name="node_graph_view_idx"),
            models.Index(fields=["user", "-captured_at"], name="node_timeline_idx"),
        ]

    def __str__(self):
        return f"{self.title} [{self.kind}]"

    @property
    def is_semantic(self):
        return self.kind in self.SEMANTIC_KINDS


class Edge(models.Model):
    """A typed, directed, confidence-weighted link between two nodes.

    Twelve relation types, deliberately. The temptation is to add a
    thirteenth every time something doesn't quite fit; resist it for at
    least six months. A graph with 40 relation types is one nobody — model
    or human — can reason about consistently.
    """

    class Relation(models.TextChoices):
        MENTIONS = "mentions", "Mentions"
        ABOUT = "about", "About"
        PART_OF = "part_of", "Part of"
        DEPENDS_ON = "depends_on", "Depends on"
        CAUSES = "causes", "Causes"
        SUPPORTS = "supports", "Supports"
        CONTRADICTS = "contradicts", "Contradicts"
        EXPANDS_ON = "expands_on", "Expands on"
        SUPERSEDES = "supersedes", "Supersedes"
        ANSWERS = "answers", "Answers"
        SAME_AS = "same_as", "Same as"
        CO_OCCURS = "co_occurs", "Co-occurs"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    from_node = models.ForeignKey(
        Node, on_delete=models.CASCADE, related_name="outgoing_edges"
    )
    relation = models.CharField(max_length=20, choices=Relation.choices, db_index=True)
    to_node = models.ForeignKey(
        Node, on_delete=models.CASCADE, related_name="incoming_edges"
    )

    confidence = models.FloatField(default=1.0)
    provenance = models.CharField(
        max_length=20, choices=Node.Provenance.choices, default=Node.Provenance.INFERRED
    )
    state = models.CharField(
        max_length=20, choices=Node.State.choices, default=Node.State.ACTIVE, db_index=True
    )

    # The reason a bad prompt deploy is one UPDATE and not a cleanup
    # project: every edge knows which run invented it.
    extraction_run = models.ForeignKey(
        "pipeline.ExtractionRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="edges",
    )
    pinned_by_user = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["from_node", "relation", "to_node"], name="unique_edge"
            ),
            # A node related to itself is always an extraction bug, and one
            # that makes the force layout explode.
            models.CheckConstraint(
                condition=~models.Q(from_node=models.F("to_node")), name="no_self_edge"
            ),
        ]
        indexes = [
            models.Index(fields=["from_node", "relation"], name="edge_out_idx"),
            models.Index(fields=["to_node", "relation"], name="edge_in_idx"),
        ]

    def __str__(self):
        return f"{self.from_node_id} -{self.relation}-> {self.to_node_id}"


class Embedding(models.Model):
    """Kept in its own table on purpose.

    Two reasons. You will want to run a new embedding model alongside the
    old one to compare retrieval before committing, and you will want to
    re-embed everything without rewriting (and re-indexing) the node rows.
    A vector column on `node` makes both of those painful.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="embeddings")
    model = models.CharField(max_length=100)

    # halfvec rather than vector: half the storage and index size for a
    # retrieval-quality difference you cannot measure at this scale.
    vector = HalfVectorField(dimensions=settings.EMBEDDING_DIMENSIONS)
    # sha256 of the text embedded. Nodes have no updated_at, so this is how
    # a changed summary is noticed and re-embedded — and an unchanged one
    # never paid for twice.
    text_hash = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["node", "model"], name="one_embedding_per_node_per_model"
            )
        ]
        indexes = [
            HnswIndex(
                name="embedding_hnsw_cosine",
                fields=["vector"],
                m=16,
                ef_construction=64,
                opclasses=["halfvec_cosine_ops"],
            )
        ]

    def __str__(self):
        return f"{self.node_id} [{self.model}]"


class ReviewItem(models.Model):
    """The middle confidence band, queued for a human.

    Above EDGE_CONFIDENCE_AUTO the pipeline writes live; below
    EDGE_CONFIDENCE_REVIEW it discards. In between, it asks. This is the
    valve that stops the graph filling up with plausible-sounding
    machine-generated garbage that you never explicitly agreed to.
    """

    class Kind(models.TextChoices):
        EDGE = "edge", "Suggested edge"
        ENTITY_MERGE = "entity_merge", "Suggested entity merge"
        NEW_ENTITY = "new_entity", "New entity"
        CONTRADICTION = "contradiction", "Possible contradiction"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="review_items"
    )

    kind = models.CharField(max_length=20, choices=Kind.choices)
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.PENDING, db_index=True
    )

    # What's being proposed, in a shape the reviewer endpoint can act on
    # without re-deriving it. Keeps this table decoupled from whichever
    # pipeline stage produced the suggestion.
    payload = models.JSONField(default=dict)

    edge = models.ForeignKey(
        Edge, on_delete=models.CASCADE, null=True, blank=True, related_name="review_items"
    )
    node = models.ForeignKey(
        Node, on_delete=models.CASCADE, null=True, blank=True, related_name="review_items"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} ({self.state})"


class Digest(models.Model):
    """Output of the nightly consolidation job — the thing you actually read.

    Recurring themes, contradictions with past decisions, and the highest
    signal one: "you've raised this problem six times since March."
    """

    class Kind(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="digests"
    )

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.DAILY)
    covers_date = models.DateField(db_index=True)
    body = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-covers_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind", "covers_date"], name="one_digest_per_period"
            )
        ]

    def __str__(self):
        return f"{self.kind} digest {self.covers_date}"
