from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Edge, Node


class GraphEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.cb = Node.objects.create(
            user=self.user, kind=Node.Kind.PROJECT, title="Project CB", mention_count=64
        )
        self.rca = Node.objects.create(
            user=self.user, kind=Node.Kind.CONCEPT, title="RCA", mention_count=16
        )
        self.gym = Node.objects.create(
            user=self.user, kind=Node.Kind.HABIT, title="Gym", mention_count=1
        )
        Edge.objects.create(
            from_node=self.cb, relation=Edge.Relation.DEPENDS_ON, to_node=self.rca
        )

    def test_returns_semantic_nodes_with_sqrt_scaled_radius(self):
        response = self.client.get(reverse("graph-data"))
        self.assertEqual(response.status_code, 200)

        by_title = {n["title"]: n for n in response.data["nodes"]}
        self.assertEqual(set(by_title), {"Project CB", "RCA", "Gym"})

        # sqrt scaling: 64 mentions is 4x the count of 16 but only 2x the
        # radius contribution, so one obsessive night can't swallow the
        # canvas.
        self.assertGreater(by_title["Project CB"]["r"], by_title["RCA"]["r"])
        self.assertLess(
            by_title["Project CB"]["r"] - by_title["RCA"]["r"],
            by_title["RCA"]["r"] - by_title["Gym"]["r"] + 10,
        )

    def test_episodic_nodes_are_not_drawn(self):
        """Individual thoughts outnumber entities ~50:1 — drawing them
        turns the canvas into static."""
        Node.objects.create(
            user=self.user, kind=Node.Kind.IDEA, title="Make RCA the core", mention_count=5
        )

        response = self.client.get(reverse("graph-data"))

        titles = {n["title"] for n in response.data["nodes"]}
        self.assertNotIn("Make RCA the core", titles)

    def test_edges_only_between_drawn_nodes(self):
        """An edge to a node outside the limit would render as a line into
        empty space."""
        response = self.client.get(reverse("graph-data") + "?limit=1")

        self.assertEqual(len(response.data["nodes"]), 1)
        self.assertEqual(response.data["edges"], [])

    def test_merged_nodes_are_excluded(self):
        self.gym.merged_into = self.cb
        self.gym.save()

        response = self.client.get(reverse("graph-data"))

        titles = {n["title"] for n in response.data["nodes"]}
        self.assertNotIn("Gym", titles)

    def test_node_detail_lists_both_directions(self):
        response = self.client.get(reverse("node-detail", args=[self.rca.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["edges"]), 1)
        edge = response.data["edges"][0]
        self.assertEqual(edge["direction"], "in")
        self.assertEqual(edge["other"]["title"], "Project CB")

    def test_other_users_nodes_are_invisible(self):
        stranger = get_user_model().objects.create_user("stranger", password="x")
        Node.objects.create(
            user=stranger, kind=Node.Kind.PROJECT, title="Not mine", mention_count=99
        )

        response = self.client.get(reverse("graph-data"))

        titles = {n["title"] for n in response.data["nodes"]}
        self.assertNotIn("Not mine", titles)
