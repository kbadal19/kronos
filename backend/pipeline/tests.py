"""
Tests for asking and teaching by voice.

The model calls are stubbed: what's tested is everything around them —
that a spoken question reaches the same answer path as a typed one, that
the fallback recognizer still gives Roman script, and that a proposed
change can't touch anything until it's applied, or reach another user's
entries when it is.
"""
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from graph.models import Edge, Node
from pipeline.keyterms import get_keyterms, invalidate_keyterms
from pipeline.providers.base import TranscriptionResult

from .ask import Result
from .models import Teaching
from .spoken import ClipError, ClipResult, EmptyClip, transcribe_clip
from .teach import apply, clean_changes


def clip(name="q.m4a"):
    return SimpleUploadedFile(name, b"fake-audio", "audio/m4a")


class SpokenAskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("ask-spoken")

    @mock.patch("pipeline.ask.ask")
    @mock.patch("pipeline.spoken.transcribe_clip")
    def test_heard_question_is_answered_and_returned(self, heard, ask):
        heard.return_value = ClipResult(text="Doon ka kya pending hai?", via="stub")
        ask.return_value = Result(answer="Website ka footer [1].", question_id="q", conversation_id="c")

        response = self.client.post(self.url, {"audio": clip()}, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["question"], "Doon ka kya pending hai?")
        self.assertEqual(response.data["answer"], "Website ka footer [1].")
        args, kwargs = ask.call_args
        self.assertEqual(args[1], "Doon ka kya pending hai?")
        self.assertTrue(kwargs["spoken"])

    @mock.patch("pipeline.ask.ask")
    @mock.patch("pipeline.spoken.transcribe_clip", side_effect=EmptyClip("nothing"))
    def test_silence_is_a_422_and_never_asks(self, heard, ask):
        response = self.client.post(self.url, {"audio": clip()}, format="multipart")
        self.assertEqual(response.status_code, 422)
        ask.assert_not_called()

    def test_missing_audio_is_rejected(self):
        response = self.client.post(self.url, {}, format="multipart")
        self.assertEqual(response.status_code, 400)


@override_settings(GEMINI_API_KEY="test", AUDIO_SPOOL_DIR=Path("/tmp/secondmind-test-clips"))
class ClipFallbackTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")

    @mock.patch("pipeline.providers.base.get_provider")
    @mock.patch("pipeline.spoken._gemini", side_effect=ClipError("503"))
    def test_deepgram_fallback_is_romanized_and_leaves_no_file(self, gemini, get_provider):
        seen = {}

        def transcribe(path, **kwargs):
            seen["path"] = path
            return TranscriptionResult(text="कल meeting है?", provider="deepgram", model="nova-3")

        get_provider.return_value.transcribe.side_effect = transcribe
        result = transcribe_clip(self.user.id, b"audio", "q.m4a", "question")

        self.assertNotRegex(result.text, r"[ऀ-ॿ]")
        self.assertIn("meeting", result.text)
        self.assertFalse(Path(seen["path"]).exists())


class AskGenerateTests(TestCase):
    @override_settings(GEMINI_API_KEY="k")
    @mock.patch("google.genai.Client")
    def test_model_that_rejects_thinking_config_is_retried_without_it(self, Client):
        from google.genai import errors

        from . import ask

        ask._NO_THINKING_CONFIG.clear()
        bad = errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})
        ok = mock.Mock(parsed=ask._Answer(answer="hi", cited=[]), usage_metadata=None)
        generate = Client.return_value.models.generate_content
        generate.side_effect = [bad, ok, ok]

        parsed, model, _ = ask._generate("q", "s", ask._Answer, ["lite"], thinking="low")
        self.assertEqual((parsed.answer, model), ("hi", "lite"))
        configs = [c.kwargs["config"].thinking_config for c in generate.call_args_list]
        self.assertEqual(configs[0].thinking_level, "LOW")
        self.assertIsNone(configs[1])

        ask._generate("q", "s", ask._Answer, ["lite"], thinking="low")  # remembered: no second 400
        self.assertIsNone(generate.call_args_list[2].kwargs["config"].thinking_config)
        self.assertEqual(generate.call_count, 3)
        ask._NO_THINKING_CONFIG.clear()


class TeachTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("badal", password="x")
        self.other = User.objects.create_user("client", password="x")
        self.stracomm = Node.objects.create(
            user=self.user, kind=Node.Kind.COMPANY, title="Stracomm", aliases=["Stracom"],
            summary="client; real estate", mention_count=5,
        )
        self.foreign = Node.objects.create(user=self.other, kind=Node.Kind.PERSON, title="Secret Person")
        invalidate_keyterms(self.user.id)

    def test_create_naming_an_alias_becomes_an_update(self):
        [change] = clean_changes(self.user.id, [
            {"op": "create", "kind": "company", "title": "stracom", "aliases": [], "note": "Pays late."},
        ])
        self.assertEqual(change["op"], "update")
        self.assertEqual(change["entity_id"], str(self.stracomm.id))

    def test_another_users_entry_is_never_offered(self):
        changes = clean_changes(self.user.id, [
            {"op": "update", "entity_id": str(self.foreign.id), "title": "Secret Person", "note": "x"},
        ])
        self.assertEqual(changes, [])

    def test_unknown_kind_and_empty_update_are_handled(self):
        changes = clean_changes(self.user.id, [
            {"op": "create", "kind": "thought", "title": "Lokesh", "aliases": []},
            {"op": "update", "entity_id": str(self.stracomm.id), "title": "Stracomm",
             "summary": "client; real estate"},
        ])
        self.assertEqual([c["title"] for c in changes], ["Lokesh"])
        self.assertEqual(changes[0]["kind"], "concept")

    def test_apply_creates_links_and_appends(self):
        changes = clean_changes(self.user.id, [
            {"op": "create", "kind": "person", "title": "Rahul", "aliases": ["Rahul bhaiya"],
             "summary": "designer at Stracomm", "note": "From Oct 2026, handles their Instagram.",
             "part_of": ["Stracomm"]},
            {"op": "update", "entity_id": str(self.stracomm.id), "title": "Stracomm", "note": "Pays late."},
        ])
        results = apply(self.user, changes)

        rahul = Node.objects.get(user=self.user, title="Rahul")
        self.assertEqual(rahul.summary, "designer at Stracomm")
        self.assertTrue(Edge.objects.filter(from_node=rahul, to_node=self.stracomm).exists())
        self.stracomm.refresh_from_db()
        self.assertTrue(self.stracomm.body.endswith("Pays late."))
        self.assertEqual(self.stracomm.summary, "client; real estate")
        self.assertIn("Rahul", get_keyterms(self.user.id))
        self.assertEqual([r["op"] for r in results], ["create", "update"])

    @mock.patch("pipeline.teach.propose")
    @mock.patch("pipeline.spoken.transcribe_clip")
    def test_nothing_changes_until_applied_and_only_once(self, heard, propose):
        heard.return_value = ClipResult(text="Lokesh is a client.", via="stub")
        propose.return_value = (
            [{"op": "create", "entity_id": None, "kind": "person", "title": "Lokesh", "aliases": [],
              "summary": "client", "note": None, "part_of": [], "current_summary": ""}],
            "stub",
        )
        client = APIClient()
        client.force_authenticate(self.user)

        proposed = client.post(reverse("knowledge-teach"), {"audio": clip()}, format="multipart")
        self.assertEqual(proposed.status_code, 200)
        self.assertFalse(Node.objects.filter(title="Lokesh").exists())

        url = reverse("knowledge-teach-apply", args=[proposed.data["id"]])
        applied = client.post(url, {"changes": proposed.data["changes"]}, format="json")
        self.assertEqual(applied.status_code, 200)
        self.assertTrue(Node.objects.filter(user=self.user, title="Lokesh").exists())
        self.assertIsNotNone(Teaching.objects.get(pk=proposed.data["id"]).applied_at)

        again = client.post(url, {"changes": proposed.data["changes"]}, format="json")
        self.assertEqual(again.status_code, 409)

        stranger = APIClient()
        stranger.force_authenticate(self.other)
        self.assertEqual(stranger.post(url, {"changes": []}, format="json").status_code, 404)
