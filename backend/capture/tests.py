"""
Tests for the ingestion path.

Weighted deliberately towards "a thought must never be lost and never be
paid for twice", because that's the only failure in this system with no
recovery: everything downstream of the transcript can be recomputed, and a
capture that never arrived cannot.
"""
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from graph.models import Node
from pipeline.keyterms import get_keyterms, invalidate_keyterms
from pipeline.providers.base import (
    EmptyTranscriptError,
    TranscriptionError,
    TranscriptionResult,
)
from pipeline.tasks import retry_stalled_captures, sweep_expired_audio, transcribe

from .models import Capture, Transcript

SPOOL = Path(tempfile.mkdtemp(prefix="secondmind-test-spool-"))


def stub_result(text="I think RCA might actually be the product.", **kwargs):
    return TranscriptionResult(
        text=text, provider="stub", model="stub-1", confidence=0.97,
        duration_seconds=42.0, **kwargs,
    )


@override_settings(AUDIO_SPOOL_DIR=SPOOL, CELERY_TASK_ALWAYS_EAGER=True, NORMALIZE_ENABLED=False)
class CaptureUploadTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        # DRF's client, not Django's: the API is JWT-only by design (no
        # SessionAuthentication), so force_login would leave the request
        # anonymous and every test here would fail on a 401.
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("capture-upload")

    def _payload(self, capture_id=None):
        return {
            "id": str(capture_id or uuid.uuid4()),
            "audio": SimpleUploadedFile("t.m4a", b"fake-audio-bytes", "audio/m4a"),
            "captured_at": timezone.now().isoformat(),
            "duration_seconds": 42.0,
            "device": "pixel",
        }

    @mock.patch("pipeline.tasks.get_provider")
    def test_upload_accepts_and_runs_chain(self, get_provider):
        """End to end: upload -> spool -> transcribe -> transcript.

        captureOnCommitCallbacks because the view enqueues via
        transaction.on_commit — which is the correct production behaviour
        (a worker must not see the job before the row is visible) and which
        TestCase's rolled-back transaction would otherwise swallow.
        """
        get_provider.return_value.transcribe.return_value = stub_result()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, self._payload(), format="multipart")

        self.assertEqual(response.status_code, 202)
        capture = Capture.objects.get()
        self.assertEqual(capture.user, self.user)
        self.assertEqual(capture.audio_bytes, len(b"fake-audio-bytes"))
        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.TRANSCRIBED)
        self.assertIn("RCA", capture.transcripts.get().text)

    @mock.patch("pipeline.tasks.get_provider")
    def test_duplicate_upload_is_idempotent(self, get_provider):
        """The 4am case: upload succeeds, the ack is lost, the phone's
        outbox retries. It must collapse onto one row and must not pay the
        transcription bill twice."""
        get_provider.return_value.transcribe.return_value = stub_result()
        capture_id = uuid.uuid4()

        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(
                self.url, self._payload(capture_id), format="multipart"
            )
        with self.captureOnCommitCallbacks(execute=True):
            second = self.client.post(
                self.url, self._payload(capture_id), format="multipart"
            )

        self.assertEqual(first.status_code, 202)
        # 200, not 409 — tells the outbox "done, stop retrying" without it
        # needing to special-case an error status.
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Capture.objects.count(), 1)
        self.assertEqual(Transcript.objects.count(), 1)
        self.assertEqual(get_provider.return_value.transcribe.call_count, 1)

    def test_upload_rejects_empty_audio(self):
        payload = self._payload()
        payload["audio"] = SimpleUploadedFile("t.m4a", b"", "audio/m4a")

        response = self.client.post(self.url, payload, format="multipart")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Capture.objects.count(), 0)

    def test_upload_requires_auth(self):
        self.client.force_authenticate(None)
        response = self.client.post(self.url, self._payload(), format="multipart")
        self.assertIn(response.status_code, (401, 403))


@override_settings(AUDIO_SPOOL_DIR=SPOOL, AUDIO_RETENTION_HOURS=0, NORMALIZE_ENABLED=False)
class TranscribeTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        SPOOL.mkdir(parents=True, exist_ok=True)
        self.audio = SPOOL / f"{uuid.uuid4()}.m4a"
        self.audio.write_bytes(b"fake-audio-bytes")
        self.capture = Capture.objects.create(
            user=self.user,
            captured_at=timezone.now(),
            audio_path=str(self.audio),
            audio_bytes=16,
        )

    @mock.patch("pipeline.tasks.get_provider")
    def test_writes_transcript_then_deletes_audio(self, get_provider):
        get_provider.return_value.transcribe.return_value = stub_result()

        transcribe(str(self.capture.id))

        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.TRANSCRIBED)
        self.assertEqual(self.capture.duration_seconds, 42.0)

        transcript = self.capture.transcripts.get(kind=Transcript.Kind.RAW)
        self.assertIn("RCA", transcript.text)

        # The ordering guarantee: audio only goes once the transcript is
        # committed, because the audio is the one unrecoverable thing.
        self.assertFalse(self.audio.exists())
        self.assertIsNotNone(self.capture.audio_deleted_at)
        self.assertEqual(self.capture.audio_path, "")

    @override_settings(AUDIO_RETENTION_HOURS=24)
    @mock.patch("pipeline.tasks.get_provider")
    def test_retention_window_keeps_audio(self, get_provider):
        """During Phase 0 the window is deliberately open so there's real
        audio to benchmark STT providers against."""
        get_provider.return_value.transcribe.return_value = stub_result()

        transcribe(str(self.capture.id))

        self.capture.refresh_from_db()
        self.assertTrue(self.audio.exists())
        self.assertIsNone(self.capture.audio_deleted_at)

    @mock.patch("pipeline.tasks.get_provider")
    def test_rerun_does_not_double_bill(self, get_provider):
        """acks_late means a worker killed mid-task gets redelivered, so
        every stage has to be safe to run twice."""
        get_provider.return_value.transcribe.return_value = stub_result()

        transcribe(str(self.capture.id))
        transcribe(str(self.capture.id))

        self.assertEqual(self.capture.transcripts.count(), 1)
        self.assertEqual(get_provider.return_value.transcribe.call_count, 1)

    @mock.patch("pipeline.tasks.get_provider")
    def test_empty_transcript_fails_without_retrying(self, get_provider):
        """More attempts against silence produce more silence. Fail loudly
        while the audio is still on disk and could be re-run by hand."""
        get_provider.return_value.transcribe.side_effect = EmptyTranscriptError("silent")

        transcribe(str(self.capture.id))

        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.FAILED)
        self.assertIn("silent", self.capture.error)

    @mock.patch("pipeline.tasks.get_provider")
    def test_missing_audio_fails_cleanly(self, get_provider):
        self.audio.unlink()

        transcribe(str(self.capture.id))

        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.FAILED)
        get_provider.return_value.transcribe.assert_not_called()

    @mock.patch("pipeline.tasks.get_provider")
    def test_provider_outage_leaves_capture_retryable(self, get_provider):
        get_provider.return_value.transcribe.side_effect = TranscriptionError("502")

        with self.assertRaises(Exception):
            transcribe(str(self.capture.id))

        self.capture.refresh_from_db()
        # Back to UPLOADED, not FAILED — the sweeper will pick it up rather
        # than the thought being written off.
        self.assertEqual(self.capture.status, Capture.Status.UPLOADED)
        self.assertTrue(self.audio.exists())


@override_settings(AUDIO_SPOOL_DIR=SPOOL, AUDIO_RETENTION_HOURS=0)
class SweeperTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        SPOOL.mkdir(parents=True, exist_ok=True)

    def test_sweep_removes_expired_audio(self):
        audio = SPOOL / f"{uuid.uuid4()}.m4a"
        audio.write_bytes(b"x")
        capture = Capture.objects.create(
            user=self.user, captured_at=timezone.now(), audio_path=str(audio)
        )

        sweep_expired_audio()

        capture.refresh_from_db()
        self.assertFalse(audio.exists())
        self.assertIsNotNone(capture.audio_deleted_at)

    @mock.patch("pipeline.tasks.transcribe.delay")
    def test_stalled_capture_is_requeued(self, delay):
        audio = SPOOL / f"{uuid.uuid4()}.m4a"
        audio.write_bytes(b"x")
        capture = Capture.objects.create(
            user=self.user, captured_at=timezone.now(),
            audio_path=str(audio), status=Capture.Status.UPLOADED,
        )
        # auto_now means updated_at can't be set on create.
        Capture.objects.filter(pk=capture.pk).update(
            updated_at=timezone.now() - timedelta(hours=2)
        )

        self.assertEqual(retry_stalled_captures(), 1)
        delay.assert_called_once_with(str(capture.id))

    def test_exhausted_capture_is_marked_failed(self):
        audio = SPOOL / f"{uuid.uuid4()}.m4a"
        audio.write_bytes(b"x")
        capture = Capture.objects.create(
            user=self.user, captured_at=timezone.now(),
            audio_path=str(audio), status=Capture.Status.UPLOADED, attempts=9,
        )
        Capture.objects.filter(pk=capture.pk).update(
            updated_at=timezone.now() - timedelta(hours=2)
        )

        retry_stalled_captures()

        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.FAILED)


@override_settings(AUDIO_SPOOL_DIR=SPOOL, PIPELINE_TERMINAL_STATUS="transcribed")
class HealthTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_transcribed_is_not_pending_during_phase_0(self):
        """Phase 0's chain ends at `transcribed`. Without the configurable
        terminal status the app would show every capture ever made as
        permanently pending."""
        Capture.objects.create(
            user=self.user, captured_at=timezone.now(),
            status=Capture.Status.TRANSCRIBED,
        )
        Capture.objects.create(
            user=self.user, captured_at=timezone.now(), status=Capture.Status.UPLOADED
        )

        data = self.client.get(reverse("capture-health")).data

        self.assertEqual(data["total"], 2)
        self.assertEqual(data["pending"], 1)

    @override_settings(PIPELINE_TERMINAL_STATUS="extracted")
    def test_terminal_status_moves_with_the_pipeline(self):
        Capture.objects.create(
            user=self.user, captured_at=timezone.now(),
            status=Capture.Status.TRANSCRIBED,
        )

        data = self.client.get(reverse("capture-health")).data

        self.assertEqual(data["pending"], 1)


class SeedEntitiesTests(TestCase):
    """The keyterm flywheel can't start itself — biasing comes from
    entities you already have, and on day one there are none."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        invalidate_keyterms(self.user.id)

    def tearDown(self):
        invalidate_keyterms(self.user.id)

    def _seed(self, body, **opts):
        from django.core.management import call_command

        path = Path(tempfile.mkdtemp()) / "entities.txt"
        path.write_text(body)
        call_command("seed_entities", str(path), user="badal", verbosity=0, **opts)

    def test_seeds_entities_and_makes_them_keyterms(self):
        self._seed(
            "# comment\n\n"
            "project: Project CB | CB | the observability project\n"
            "company: Credence Marketings | Credence\n"
        )

        self.assertEqual(Node.objects.count(), 2)
        cb = Node.objects.get(title="Project CB")
        self.assertEqual(cb.aliases, ["CB", "the observability project"])
        # Hand-seeded, so it must survive any future reprocessing run.
        self.assertTrue(cb.pinned_by_user)

        terms = get_keyterms(self.user.id)
        self.assertIn("Project CB", terms)
        self.assertIn("Credence", terms)

    def test_rerunning_adds_aliases_without_duplicating(self):
        self._seed("project: Project CB | CB\n")
        self._seed("project: Project CB | the observability project\n")

        self.assertEqual(Node.objects.filter(title="Project CB").count(), 1)
        self.assertEqual(
            Node.objects.get(title="Project CB").aliases,
            ["CB", "the observability project"],
        )

    def test_rejects_episodic_kinds(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._seed("idea: Make RCA the core\n")

    def test_dry_run_changes_nothing(self):
        self._seed("project: Project CB | CB\n", dry_run=True)
        self.assertEqual(Node.objects.count(), 0)


class KeytermTests(TestCase):
    """The accuracy flywheel: entities you already have bias the next
    recognizer call, which is what keeps 'Project CB' from becoming
    'project sea bee'."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        invalidate_keyterms(self.user.id)

    def tearDown(self):
        invalidate_keyterms(self.user.id)

    def test_titles_and_aliases_ordered_by_mention_count(self):
        Node.objects.create(
            user=self.user, kind=Node.Kind.PROJECT, title="Project CB",
            aliases=["CB", "the observability project"], mention_count=40,
        )
        Node.objects.create(
            user=self.user, kind=Node.Kind.HABIT, title="Gym", mention_count=3,
        )

        terms = get_keyterms(self.user.id)

        self.assertEqual(terms[0], "Project CB")
        self.assertIn("the observability project", terms)
        self.assertIn("Gym", terms)

    def test_episodic_nodes_are_not_keyterms(self):
        """Biasing on 'I think RCA might be the product' would be noise —
        only the accumulating entities are worth weighting."""
        Node.objects.create(
            user=self.user, kind=Node.Kind.IDEA,
            title="Make RCA the core of Project CB", mention_count=99,
        )

        self.assertEqual(get_keyterms(self.user.id), [])

    def test_merged_entities_are_excluded(self):
        canonical = Node.objects.create(
            user=self.user, kind=Node.Kind.PROJECT, title="Project CB", mention_count=10
        )
        Node.objects.create(
            user=self.user, kind=Node.Kind.PROJECT, title="CB project",
            mention_count=5, merged_into=canonical,
        )

        self.assertEqual(get_keyterms(self.user.id), ["Project CB"])


class DeepgramParamsTests(TestCase):
    @override_settings(DEEPGRAM_API_KEY="k", DEEPGRAM_LANGUAGE="multi")
    @mock.patch("pipeline.providers.deepgram.httpx.post")
    def test_requests_code_switching(self, post):
        """Hindi-English code-switching only works in Nova-3's multi mode;
        `en` forces English onto every Hindi word."""
        from pipeline.providers.deepgram import DeepgramProvider

        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "results": {"channels": [{"alternatives": [{"transcript": "kal meeting hai"}]}]}
        }
        audio = SPOOL / f"{uuid.uuid4()}.m4a"
        SPOOL.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"x")

        DeepgramProvider().transcribe(str(audio), keyterms=["Project CB"])

        params = post.call_args.kwargs["params"]
        self.assertEqual(params["language"], "multi")
        self.assertEqual(params["keyterm"], ["Project CB"])


class KeytermBudgetTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        invalidate_keyterms(self.user.id)

    def tearDown(self):
        invalidate_keyterms(self.user.id)

    @override_settings(TRANSCRIPTION_KEYTERM_TOKEN_BUDGET=20)
    def test_list_is_cut_to_the_token_budget(self):
        """Deepgram fails the whole request past 500 keyterm tokens, so the
        list stops before the budget rather than after it."""
        for i in range(10):
            Node.objects.create(
                user=self.user, kind=Node.Kind.CONCEPT,
                title=f"a fairly long concept name {i}", mention_count=10 - i,
            )

        terms = get_keyterms(self.user.id)

        self.assertGreater(len(terms), 0)
        self.assertLess(len(terms), 10)
        self.assertEqual(terms[0], "a fairly long concept name 0")


def stub_normalized(text="Kal Project CB ka demo hai, RCA wala part pending hai."):
    from pipeline.normalize import NormalizationResult

    return NormalizationResult(
        text=text, corrections=[{"heard": "project sea bee", "written": "Project CB"}],
        model="gemini-stub", prompt_hash="h" * 64,
        raw_output={"text": text, "corrections": []},
        input_tokens=100, output_tokens=20, used_audio=True,
    )


@override_settings(
    AUDIO_SPOOL_DIR=SPOOL, AUDIO_RETENTION_HOURS=0, NORMALIZE_ENABLED=True,
    CELERY_TASK_ALWAYS_EAGER=True,
)
class NormalizeTaskTests(TestCase):
    RAW = "kal project sea bee ka demo hai RCA wala part pending hai"

    def setUp(self):
        from pipeline.tasks import normalize

        self.normalize = normalize
        self.user = get_user_model().objects.create_user("badal", password="x")
        SPOOL.mkdir(parents=True, exist_ok=True)
        self.audio = SPOOL / f"{uuid.uuid4()}.m4a"
        self.audio.write_bytes(b"fake-audio-bytes")
        self.capture = Capture.objects.create(
            user=self.user, captured_at=timezone.now(),
            audio_path=str(self.audio), audio_bytes=16,
        )

    def _with_raw(self):
        Transcript.objects.create(capture=self.capture, kind=Transcript.Kind.RAW, text=self.RAW)
        Capture.objects.filter(pk=self.capture.pk).update(status=Capture.Status.TRANSCRIBED)

    @mock.patch("pipeline.tasks.normalize.delay")
    @mock.patch("pipeline.tasks.get_provider")
    def test_transcribe_hands_off_to_normalize_and_keeps_audio(self, get_provider, delay):
        """Normalize listens to the audio, so transcribe must not delete it."""
        get_provider.return_value.transcribe.return_value = stub_result(text=self.RAW)

        transcribe(str(self.capture.id))

        delay.assert_called_once_with(str(self.capture.id))
        self.assertTrue(self.audio.exists())

    @mock.patch("pipeline.tasks.normalize_capture")
    def test_writes_normalized_then_deletes_audio(self, normalize_capture):
        from pipeline.models import ExtractionRun

        self._with_raw()
        normalize_capture.return_value = stub_normalized()

        self.normalize(str(self.capture.id))

        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.NORMALIZED)
        normalized = self.capture.transcripts.get(kind=Transcript.Kind.NORMALIZED)
        self.assertIn("Project CB", normalized.text)
        # RAW is the floor and stays byte for byte.
        self.assertEqual(self.capture.transcripts.get(kind=Transcript.Kind.RAW).text, self.RAW)
        self.assertFalse(self.audio.exists())

        run = ExtractionRun.objects.get(capture=self.capture)
        self.assertEqual(run.stage, ExtractionRun.Stage.NORMALIZE)
        self.assertEqual(run.status, ExtractionRun.Status.SUCCEEDED)
        self.assertEqual(run.input_tokens, 100)

    @override_settings(AUDIO_RETENTION_HOURS=24)
    @mock.patch("pipeline.tasks.normalize_capture")
    def test_rerun_is_idempotent_and_force_supersedes(self, normalize_capture):
        from pipeline.models import ExtractionRun

        self._with_raw()
        normalize_capture.return_value = stub_normalized()
        self.normalize(str(self.capture.id))
        self.normalize(str(self.capture.id))
        self.assertEqual(normalize_capture.call_count, 1)

        normalize_capture.return_value = stub_normalized(text="Kal Project CB ka demo hai.")
        self.normalize(str(self.capture.id), force=True)

        self.assertEqual(
            self.capture.transcripts.get(kind=Transcript.Kind.NORMALIZED).text,
            "Kal Project CB ka demo hai.",
        )
        statuses = sorted(ExtractionRun.objects.values_list("status", flat=True))
        self.assertEqual(statuses, ["succeeded", "superseded"])

    @mock.patch("pipeline.tasks.normalize_capture")
    def test_rejection_keeps_raw_and_still_deletes_audio(self, normalize_capture):
        from pipeline.models import ExtractionRun
        from pipeline.normalize import NormalizationRejected

        self._with_raw()
        normalize_capture.side_effect = NormalizationRejected("looks like a summary")

        self.normalize(str(self.capture.id))

        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.TRANSCRIBED)
        self.assertFalse(self.capture.transcripts.filter(kind=Transcript.Kind.NORMALIZED).exists())
        self.assertFalse(self.audio.exists())
        self.assertEqual(ExtractionRun.objects.get().status, ExtractionRun.Status.FAILED)

    @mock.patch("pipeline.tasks.normalize_capture")
    def test_correction_pairs_that_applied_gain_hits(self, normalize_capture):
        from pipeline.models import CorrectionPair

        self._with_raw()
        used = CorrectionPair.objects.create(user=self.user, wrong="project sea bee", right="Project CB")
        unused = CorrectionPair.objects.create(user=self.user, wrong="master flow", right="masterflow")
        normalize_capture.return_value = stub_normalized()

        self.normalize(str(self.capture.id))

        used.refresh_from_db()
        unused.refresh_from_db()
        self.assertEqual((used.hits, unused.hits), (1, 0))


class NormalizeGuardTests(TestCase):
    def test_summary_shaped_output_is_rejected(self):
        from pipeline.normalize import NormalizationRejected, _guard

        raw = "kal project CB ka demo hai aur RCA wala part abhi bhi pending hai " * 3
        with self.assertRaises(NormalizationRejected):
            _guard(raw, "Demo tomorrow.")
        _guard(raw, raw.capitalize())  # a real correction passes

    def test_context_carries_vocabulary_and_recent_notes(self):
        from pipeline.models import CorrectionPair
        from pipeline.normalize import build_context

        user = get_user_model().objects.create_user("badal", password="x")
        Node.objects.create(
            user=user, kind=Node.Kind.PERSON, title="Navjyoti", aliases=["Nav"], mention_count=5,
        )
        CorrectionPair.objects.create(user=user, wrong="master flow", right="masterflow")
        earlier = Capture.objects.create(
            user=user, captured_at=timezone.now() - timedelta(hours=2)
        )
        Transcript.objects.create(capture=earlier, kind=Transcript.Kind.RAW, text="log ingestion fix karna hai")
        later = Capture.objects.create(user=user, captured_at=timezone.now() + timedelta(hours=1))
        Transcript.objects.create(capture=later, kind=Transcript.Kind.RAW, text="from the future")
        current = Capture.objects.create(user=user, captured_at=timezone.now())

        context, _ = build_context(current, "Nav ko call karna hai about the master flow thing")

        self.assertIn("Navjyoti (also said as: Nav) [person]", context)
        self.assertIn("master flow -> masterflow", context)
        self.assertIn("log ingestion fix karna hai", context)
        self.assertNotIn("from the future", context)


class HealthChainTests(TestCase):
    def test_normalized_counts_as_settled_past_a_transcribed_terminal(self):
        user = get_user_model().objects.create_user("badal", password="x")
        client = APIClient()
        client.force_authenticate(user)
        Capture.objects.create(user=user, captured_at=timezone.now(), status=Capture.Status.NORMALIZED)

        data = client.get(reverse("capture-health")).data

        self.assertEqual(data["pending"], 0)


@override_settings(AUDIO_SPOOL_DIR=SPOOL, NORMALIZE_ENABLED=False)
class TenantIsolationTests(TestCase):
    """More than one person on the same backend: nothing about one user's
    vocabulary, corrections or language may reach another's transcripts."""

    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        invalidate_keyterms(self.alice.id)
        invalidate_keyterms(self.bob.id)

    def tearDown(self):
        invalidate_keyterms(self.alice.id)
        invalidate_keyterms(self.bob.id)

    def test_keyterm_cache_is_per_user(self):
        Node.objects.create(user=self.alice, kind=Node.Kind.PROJECT, title="Project CB")
        Node.objects.create(user=self.bob, kind=Node.Kind.PROJECT, title="Apollo")

        self.assertEqual(get_keyterms(self.alice.id), ["Project CB"])
        # Alice's list is now cached; Bob must not be served it.
        self.assertEqual(get_keyterms(self.bob.id), ["Apollo"])

    def test_corrections_and_vocabulary_stay_with_their_owner(self):
        from pipeline.models import CorrectionPair
        from pipeline.normalize import build_context

        CorrectionPair.objects.create(user=self.alice, wrong="navjoti", right="Navjyoti")
        Node.objects.create(user=self.alice, kind=Node.Kind.PERSON, title="Navjyoti")
        capture = Capture.objects.create(user=self.bob, captured_at=timezone.now())

        self.assertNotIn("Navjyoti", build_context(capture, "Navjyoti navjoti ko call karna")[0])

    @mock.patch("pipeline.tasks.get_provider")
    def test_recognizer_language_comes_from_the_profile(self, get_provider):
        from pipeline.models import TranscriptionProfile

        TranscriptionProfile.objects.create(
            user=self.bob, recognizer_language="ta", languages="Tamil and English",
        )
        SPOOL.mkdir(parents=True, exist_ok=True)
        calls = {}
        for user in (self.alice, self.bob):
            audio = SPOOL / f"{uuid.uuid4()}.m4a"
            audio.write_bytes(b"x")
            capture = Capture.objects.create(
                user=user, captured_at=timezone.now(), audio_path=str(audio),
            )
            get_provider.return_value.transcribe.return_value = stub_result()
            transcribe(str(capture.id))
            calls[user.username] = get_provider.return_value.transcribe.call_args.kwargs["language"]

        self.assertEqual(calls, {"alice": "multi", "bob": "ta"})

    def test_prompt_follows_the_profile(self):
        from pipeline.models import TranscriptionProfile
        from pipeline.normalize import render_system_prompt

        default = render_system_prompt(TranscriptionProfile.for_user(self.alice.id))
        self.assertIn("Hindi and English", default)
        self.assertIn("Never use a native script", default)

        custom = render_system_prompt(TranscriptionProfile(
            user=self.bob, languages="Tamil and English",
            script=TranscriptionProfile.Script.NATIVE, speaker_notes="Runs a bakery in Chennai.",
        ))
        self.assertIn("Tamil and English", custom)
        self.assertIn("native script of its language", custom)
        self.assertIn("Runs a bakery in Chennai.", custom)


@override_settings(NORMALIZE_ENABLED=True)
class EditTranscriptTests(TestCase):
    """Your hand fixes: shown first, never overwritten by the pipeline,
    and turned into suggested corrections."""

    SHOWN = "I have to call Shubham from Starcom. It is regarding credence. On my hand or ek kaam."
    FIXED = "I have to call Shubham from Stracomm. It is regarding Credence. On my hand aur ek kaam."

    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.capture = Capture.objects.create(
            user=self.user, captured_at=timezone.now(), status=Capture.Status.NORMALIZED,
        )
        Transcript.objects.create(capture=self.capture, kind=Transcript.Kind.RAW, text="raw draft")
        Transcript.objects.create(capture=self.capture, kind=Transcript.Kind.NORMALIZED, text=self.SHOWN)
        self.url = reverse("capture-edit", args=[self.capture.id])

    def test_edit_is_shown_first_and_suggests_corrections(self):
        data = self.client.put(self.url, {"text": self.FIXED}, format="json").data

        self.assertEqual(data["capture"]["text"], self.FIXED)
        self.assertTrue(data["capture"]["edited"])
        by_wrong = {s["wrong"]: s for s in data["suggestions"]}
        self.assertEqual(by_wrong["Starcom"]["right"], "Stracomm")
        self.assertTrue(by_wrong["Starcom"]["recommended"])
        # "or -> aur" is right here and wrong in English sentences: offered,
        # not pre-ticked.
        self.assertFalse(by_wrong["or"]["recommended"])

        listed = self.client.get(reverse("capture-list")).data["results"][0]
        self.assertEqual((listed["text"], listed["edited"]), (self.FIXED, True))

    def test_normalized_and_raw_survive_the_edit(self):
        self.client.put(self.url, {"text": self.FIXED}, format="json")
        kinds = dict(self.capture.transcripts.values_list("kind", "text"))
        self.assertEqual(kinds["normalized"], self.SHOWN)
        self.assertEqual(kinds["raw"], "raw draft")

    @mock.patch("pipeline.tasks.normalize_capture")
    def test_renormalizing_never_touches_your_edit(self, normalize_capture):
        from pipeline.tasks import normalize

        self.client.put(self.url, {"text": self.FIXED}, format="json")
        normalize_capture.return_value = stub_normalized(text="Model's new attempt at the note.")

        normalize(str(self.capture.id), force=True)

        self.capture.refresh_from_db()
        self.assertEqual(self.capture.display_transcript().text, self.FIXED)
        self.assertEqual(
            self.capture.transcripts.get(kind=Transcript.Kind.NORMALIZED).text,
            "Model's new attempt at the note.",
        )

    def test_known_corrections_are_not_suggested_again(self):
        from pipeline.models import CorrectionPair

        CorrectionPair.objects.create(user=self.user, wrong="Starcom", right="Stracomm")
        data = self.client.put(self.url, {"text": self.FIXED}, format="json").data
        self.assertNotIn("Starcom", [s["wrong"] for s in data["suggestions"]])

    def test_delete_reverts_to_the_model_version(self):
        self.client.put(self.url, {"text": self.FIXED}, format="json")
        data = self.client.delete(self.url).data
        self.assertEqual(data["capture"]["text"], self.SHOWN)
        self.assertFalse(data["capture"]["edited"])

    def test_other_users_notes_are_not_editable(self):
        other = get_user_model().objects.create_user("eve", password="x")
        client = APIClient()
        client.force_authenticate(other)
        self.assertEqual(client.put(self.url, {"text": "x"}, format="json").status_code, 404)

    @mock.patch("pipeline.tasks.normalize.delay")
    def test_renormalize_endpoint_queues_a_forced_run(self, delay):
        response = self.client.post(reverse("capture-renormalize", args=[self.capture.id]))
        self.assertEqual(response.status_code, 202)
        delay.assert_called_once_with(str(self.capture.id), force=True)


class SuggestCorrectionsTests(TestCase):
    def test_long_rewrites_and_punctuation_are_not_corrections(self):
        from capture.editing import suggest_corrections

        before = "So we should totally go with the first plan today, okay."
        after = "So I think the second approach is better for everyone involved here, okay!"
        self.assertEqual(suggest_corrections(before, after), [])
        self.assertEqual(suggest_corrections("Call Shubham.", "Call Shubham!"), [])


class VocabularyApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        invalidate_keyterms(self.user.id)

    def tearDown(self):
        invalidate_keyterms(self.user.id)

    def test_added_word_becomes_a_keyterm_and_misheard_forms_become_corrections(self):
        from pipeline.models import CorrectionPair

        response = self.client.post(
            reverse("vocabulary"),
            {"title": "Stracomm", "kind": "company", "misheard_as": ["Starcom", "Stra com"]},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        # The right name biases Deepgram; the wrong ones must not.
        self.assertEqual(get_keyterms(self.user.id), ["Stracomm"])
        self.assertEqual(
            set(CorrectionPair.objects.filter(user=self.user).values_list("wrong", "right")),
            {("Starcom", "Stracomm"), ("Stra com", "Stracomm")},
        )

    def test_adding_an_existing_name_merges_and_removing_hides_it(self):
        first = self.client.post(reverse("vocabulary"), {"title": "Credence", "kind": "company"}, format="json").data
        again = self.client.post(
            reverse("vocabulary"), {"title": "credence", "aliases": ["Credence Marketings"]}, format="json",
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["id"], first["id"])
        self.assertEqual(again.data["aliases"], ["Credence Marketings"])

        self.client.delete(reverse("vocabulary-detail", args=[first["id"]]))
        self.assertEqual(self.client.get(reverse("vocabulary")).data, [])
        self.assertEqual(get_keyterms(self.user.id), [])

    def test_bulk_corrections_skip_duplicates_and_stay_per_user(self):
        payload = {"pairs": [{"wrong": "or", "right": "aur"}, {"wrong": "or", "right": "aur"}]}
        self.client.post(reverse("corrections"), payload, format="json")
        self.assertEqual(len(self.client.get(reverse("corrections")).data), 1)

        other = APIClient()
        other.force_authenticate(get_user_model().objects.create_user("eve", password="x"))
        self.assertEqual(other.get(reverse("corrections")).data, [])


class LoadKnowledgeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("badal", password="x")
        invalidate_keyterms(self.user.id)

    def tearDown(self):
        invalidate_keyterms(self.user.id)

    def _load(self, data):
        import io
        import json
        from django.core.management import call_command

        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))):
            call_command("load_knowledge", "-", user="badal", stdout=io.StringIO())

    def test_entities_edges_clients_and_profile_land_and_rerun_is_safe(self):
        from graph.models import Edge
        from pipeline.models import CorrectionPair, TranscriptionProfile
        from pipeline.normalize import build_context

        data = {
            "speaker_notes": "Badal, developer at Credence.",
            "entities": [
                {"kind": "company", "title": "Credence Marketings", "aliases": ["Credence"], "weight": 9,
                 "summary": "Badal's employer", "body": "private note"},
                {"kind": "company", "title": "Stracomm", "misheard_as": ["Starcom"], "summary": "client"},
                {"kind": "person", "title": "Shubham", "summary": "owner of Stracomm"},
            ],
            "relations": [["Shubham", "Stracomm"]],
            "clients": ["Stracomm"],
            "clients_of": "Credence Marketings",
        }
        self._load(data)
        self._load(data)

        self.assertEqual(Node.objects.filter(user=self.user).count(), 3)
        self.assertEqual(Edge.objects.count(), 1)
        self.assertEqual(Node.objects.get(title="Stracomm").metadata["client_of"], "Credence Marketings")
        self.assertTrue(CorrectionPair.objects.filter(wrong="Starcom", right="Stracomm").exists())
        self.assertEqual(TranscriptionProfile.for_user(self.user.id).speaker_notes, "Badal, developer at Credence.")
        self.assertEqual(get_keyterms(self.user.id)[0], "Credence Marketings")

        context, _ = build_context(
            Capture.objects.create(user=self.user, captured_at=timezone.now()), "Shubham ko call karna hai"
        )
        self.assertIn("Shubham [person]: owner of Stracomm", context)
        # The body is notes, not prompt material.
        self.assertNotIn("private note", context)


class RetrievalTests(TestCase):
    """Per-note retrieval: the prompt gets the entities a draft plausibly
    names, never the whole knowledge base."""

    def setUp(self):
        from graph.models import Edge

        self.user = get_user_model().objects.create_user("badal", password="x")
        invalidate_keyterms(self.user.id)
        make = lambda kind, title, **kw: Node.objects.create(user=self.user, kind=kind, title=title, **kw)
        self.stracomm = make(Node.Kind.COMPANY, "Stracomm", summary="event company")
        self.shubham = make(Node.Kind.PERSON, "Shubham", summary="owner of Stracomm")
        self.maqsood = make(Node.Kind.PERSON, "Maqsood", summary="designer at Stracomm")
        self.doon = make(Node.Kind.COMPANY, "Doon Public School", aliases=["Doon"])
        self.navjyoti = make(Node.Kind.COMPANY, "Navjyoti Superspeciality Eye Hospital", aliases=["Navjyoti"])
        for person in (self.shubham, self.maqsood):
            Edge.objects.create(from_node=person, relation=Edge.Relation.PART_OF, to_node=self.stracomm)
        # Noise: a big vocabulary the note has nothing to do with.
        for i in range(300):
            make(Node.Kind.CONCEPT, f"Unrelated topic number {i}", mention_count=0)

    def tearDown(self):
        invalidate_keyterms(self.user.id)

    def _ids(self, text):
        from pipeline.retrieval import retrieve

        return {h.node_id: h for h in retrieve(self.user.id, text).candidates}

    def test_misheard_name_is_found_by_sound_and_brings_its_neighbours(self):
        hits = self._ids("Make a note that I have to call Shubham from Starcom regarding the ad")

        self.assertEqual(hits[str(self.stracomm.id)].reason, "sounds like")
        self.assertIn(str(self.shubham.id), hits)
        # Never named, but attached to what was.
        self.assertEqual(hits[str(self.maqsood.id)].reason, "linked")

    def test_devanagari_in_the_draft_matches_romanized_names(self):
        hits = self._ids("और एक दून का करना होगा")
        self.assertIn(str(self.doon.id), hits)

    def test_candidate_set_stays_bounded_however_big_the_vocabulary(self):
        from pipeline.retrieval import MAX_CANDIDATES

        hits = self._ids("Navjyoti ka GMB rank karna hai, Doon ka website, Shubham ko call")
        self.assertLessEqual(len(hits), MAX_CANDIDATES)
        self.assertIn(str(self.navjyoti.id), hits)
        named = [h for h in hits.values() if h.reason != "recent"]
        self.assertFalse(any("Unrelated" in Node.objects.get(pk=h.node_id).title for h in named))

    def test_mentions_move_the_recent_set_and_keyterms(self):
        from pipeline.retrieval import record_mentions

        record_mentions(self.user.id, [self.maqsood.id, self.doon.id], "Maqsood se baat karni hai", timezone.now())
        invalidate_keyterms(self.user.id)

        self.maqsood.refresh_from_db()
        self.doon.refresh_from_db()
        self.assertEqual(self.maqsood.mention_count, 1)
        self.assertIsNone(self.doon.last_mentioned_at)  # offered, not said
        self.assertEqual(get_keyterms(self.user.id)[0], "Maqsood")


@override_settings(AUDIO_SPOOL_DIR=SPOOL, AUDIO_RETENTION_HOURS=0, NORMALIZE_ENABLED=True)
class NormalizeMentionTests(TestCase):
    @mock.patch("pipeline.tasks.normalize_capture")
    def test_mentions_count_once_per_note_not_per_rerun(self, normalize_capture):
        from pipeline.tasks import normalize

        user = get_user_model().objects.create_user("badal", password="x")
        shubham = Node.objects.create(user=user, kind=Node.Kind.PERSON, title="Shubham", mention_count=1)
        capture = Capture.objects.create(user=user, captured_at=timezone.now(), status=Capture.Status.TRANSCRIBED)
        Transcript.objects.create(capture=capture, kind=Transcript.Kind.RAW, text="call shubham")
        result = stub_normalized(text="Call Shubham.")
        result.candidate_ids = [str(shubham.id)]
        normalize_capture.return_value = result

        normalize(str(capture.id))
        normalize(str(capture.id), force=True)

        shubham.refresh_from_db()
        self.assertEqual(shubham.mention_count, 2)
        invalidate_keyterms(user.id)


@override_settings(DEEPGRAM_API_KEY="k", AUDIO_SPOOL_DIR=SPOOL)
class DeepgramFailureTests(TestCase):
    """A note must never be lost to keyterm biasing, and a request Deepgram
    refuses must fail once with the reason — not retry blindly for half an
    hour behind a generic "gave up"."""

    OK = {"results": {"channels": [{"alternatives": [{"transcript": "call Shubham"}]}]}}

    def _audio(self):
        SPOOL.mkdir(parents=True, exist_ok=True)
        path = SPOOL / f"{uuid.uuid4()}.m4a"
        path.write_bytes(b"x")
        return path

    def _response(self, status, payload=None, text=""):
        r = mock.Mock(status_code=status, text=text)
        r.json.return_value = payload
        return r

    @mock.patch("pipeline.providers.deepgram.httpx.post")
    def test_keyterm_limit_retries_with_fewer_terms(self, post):
        from pipeline.providers.deepgram import DeepgramProvider

        limit = self._response(400, text='{"err_msg":"Keyterm limit exceeded. The maximum number of tokens across all keyterms is 500."}')
        post.side_effect = [limit, limit, self._response(200, self.OK)]

        result = DeepgramProvider().transcribe(str(self._audio()), keyterms=[f"name{i}" for i in range(80)])

        self.assertEqual(result.text, "call Shubham")
        sent = [len(c.kwargs["params"].get("keyterm", [])) for c in post.call_args_list]
        self.assertEqual(sent, [80, 40, 20])

    @mock.patch("pipeline.providers.deepgram.httpx.post")
    def test_still_transcribes_with_no_keyterms_at_all(self, post):
        from pipeline.providers.deepgram import DeepgramProvider

        limit = self._response(400, text="Keyterm limit exceeded")
        post.side_effect = [limit, limit, self._response(200, self.OK)]

        DeepgramProvider().transcribe(str(self._audio()), keyterms=["a", "b"])
        self.assertNotIn("keyterm", post.call_args_list[-1].kwargs["params"])

    @mock.patch("pipeline.providers.deepgram.httpx.post")
    def test_other_rejections_fail_the_capture_once_with_the_reason(self, post):
        user = get_user_model().objects.create_user("badal", password="x")
        capture = Capture.objects.create(user=user, captured_at=timezone.now(), audio_path=str(self._audio()))
        post.return_value = self._response(400, text="Bad Request: unsupported audio format")

        transcribe(str(capture.id))

        capture.refresh_from_db()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(capture.status, Capture.Status.FAILED)
        self.assertIn("unsupported audio format", capture.error)

    def test_estimate_errs_high_on_indian_names(self):
        from pipeline.keyterms import _estimated_tokens

        # Deepgram counted these well above len/3; the estimate must too.
        for name in ["Matrichhaya Ortho", "Dr. Dhananjay Ashturkar", "Khushboo ma'am"]:
            self.assertGreaterEqual(_estimated_tokens(name), len(name) // 2)


class AskFixture(TestCase):
    """Questions are answered from a small retrieved context, never the
    whole knowledge base, and never from another user's notes."""

    def setUp(self):
        from graph.models import Edge

        self.user = get_user_model().objects.create_user("badal", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        invalidate_keyterms(self.user.id)
        self.doon = Node.objects.create(
            user=self.user, kind=Node.Kind.COMPANY, title="Doon Public School", aliases=["Doon"],
            summary="client; school in Begusarai", body="Aayush relies on Badal for marketing.",
        )
        aayush = Node.objects.create(user=self.user, kind=Node.Kind.PERSON, title="Aayush Sawarn")
        Edge.objects.create(from_node=aayush, relation=Edge.Relation.PART_OF, to_node=self.doon)
        self.website = self._note("Doon ka website mein hero section ka background change karna hai.", days_ago=1)
        self.bill = self._note("Next time Anushka should pay the bill.", days_ago=10)

        other = get_user_model().objects.create_user("eve", password="x")
        self.secret = self._note("Doon website secret plans", days_ago=1, user=other)

    def tearDown(self):
        invalidate_keyterms(self.user.id)

    def _note(self, text, days_ago, user=None):
        c = Capture.objects.create(
            user=user or self.user, captured_at=timezone.now() - timedelta(days=days_ago),
            status=Capture.Status.NORMALIZED,
        )
        Transcript.objects.create(capture=c, kind=Transcript.Kind.NORMALIZED, text=text)
        return c

    def _gemini(self, plan, answer):
        from pipeline.ask import _Answer, _Plan

        def fake(prompt, system, schema, models):
            self.prompts.append(prompt)
            self.systems.append(system)
            return (_Plan(**plan) if schema is _Plan else _Answer(**answer)), "gemini-stub", None

        self.prompts, self.systems = [], []
        return mock.patch("pipeline.ask._generate", side_effect=fake)


class AskTests(AskFixture):
    def test_answers_from_retrieved_notes_with_citations_and_logs_it(self):
        from pipeline.models import Question

        with self._gemini(
            {"standalone": "What do I need to change on the Doon website?", "keywords": ["Doon", "website"]},
            {"answer": "Change the hero section background [1].", "cited": [1, 7]},
        ):
            data = self.client.post(reverse("ask"), {"question": "doon website pe kya change karna hai?"}, format="json").data

        self.assertIn("[1]", data["answer"])
        self.assertEqual([s["capture_id"] for s in data["sources"]], [str(self.website.id)])  # 7 is out of range
        self.assertEqual(data["entities"][0]["title"], "Doon Public School")
        answer_prompt = self.prompts[-1]
        self.assertIn("hero section", answer_prompt)
        self.assertIn("Aayush relies on Badal", answer_prompt)  # entity card, body included
        # Never named in the question; reached through the graph.
        self.assertIn("- Aayush Sawarn (person", answer_prompt)
        self.assertNotIn("secret plans", answer_prompt)  # other user's note

        q = Question.objects.get(user=self.user)
        self.assertEqual(q.cited_note_ids, [str(self.website.id)])
        self.assertIn(str(self.website.id), q.note_ids)

    def test_date_range_filters_hard(self):
        start = (timezone.localdate() - timedelta(days=12)).isoformat()
        end = (timezone.localdate() - timedelta(days=8)).isoformat()
        with self._gemini(
            {"standalone": "What did I note last week?", "keywords": [], "start": start, "end": end},
            {"answer": "Anushka should pay the bill next time [1].", "cited": [1]},
        ):
            data = self.client.post(reverse("ask"), {"question": "what did I note last week?"}, format="json").data

        self.assertEqual([s["capture_id"] for s in data["sources"]], [str(self.bill.id)])
        self.assertNotIn("hero section", self.prompts[-1])

    def test_nothing_in_range_says_so_without_calling_the_answer_model(self):
        with self._gemini(
            {"standalone": "x", "keywords": ["zzz"], "start": "2020-01-01", "end": "2020-01-31"},
            {"answer": "should not be called", "cited": []},
        ):
            data = self.client.post(reverse("ask"), {"question": "what happened in Jan 2020?"}, format="json").data
        self.assertIn("don't have any notes", data["answer"])
        self.assertEqual(len(self.prompts), 1)  # planner only

    def test_rejects_empty_questions(self):
        self.assertEqual(self.client.post(reverse("ask"), {"question": " "}, format="json").status_code, 400)


class EmbeddingTests(TestCase):
    @override_settings(GEMINI_API_KEY="k")
    @mock.patch("pipeline.embeddings.embed_texts")
    def test_notes_embed_once_and_again_only_when_text_changes(self, embed_texts):
        from pipeline.embeddings import embed_notes
        from django.conf import settings as s

        embed_texts.side_effect = lambda texts, task: [[0.1] * s.EMBEDDING_DIMENSIONS for _ in texts]
        user = get_user_model().objects.create_user("badal", password="x")
        c = Capture.objects.create(user=user, captured_at=timezone.now())
        Transcript.objects.create(capture=c, kind=Transcript.Kind.NORMALIZED, text="Doon ka website")

        self.assertEqual(embed_notes([c]), 1)
        self.assertEqual(embed_notes([c]), 0)  # unchanged: not sent again
        Transcript.objects.create(capture=c, kind=Transcript.Kind.EDITED, text="Doon ka naya website")
        c = Capture.objects.prefetch_related("transcripts").get(pk=c.pk)
        self.assertEqual(embed_notes([c]), 1)  # your edit is what gets embedded
        self.assertIn("naya website", embed_texts.call_args.args[0][0])


class RelatedRetrievalTests(TestCase):
    """The meaning leg: a draft that names nothing still surfaces what it's
    about, and an embedding outage costs only that leg."""

    @override_settings(GEMINI_API_KEY="k")
    def test_draft_about_an_entity_finds_it_without_naming_it(self):
        from django.conf import settings as s
        from graph.models import Embedding
        from pipeline.retrieval import retrieve

        user = get_user_model().objects.create_user("badal", password="x")
        doon = Node.objects.create(user=user, kind=Node.Kind.COMPANY, title="Doon Public School")
        far = Node.objects.create(user=user, kind=Node.Kind.COMPANY, title="Ananya Green Tractors")
        dims = s.EMBEDDING_DIMENSIONS
        Embedding.objects.create(node=doon, model=s.GEMINI_EMBEDDING_MODEL, vector=[1.0] + [0.0] * (dims - 1))
        Embedding.objects.create(node=far, model=s.GEMINI_EMBEDDING_MODEL, vector=[0.0, 1.0] + [0.0] * (dims - 2))

        with mock.patch("pipeline.embeddings.embed_texts", return_value=[[1.0] + [0.0] * (dims - 1)]):
            hits = {h.node_id: h for h in retrieve(user.id, "Begusarai shoot on teachers day").candidates}

        self.assertEqual(hits[str(doon.id)].reason, "related")
        self.assertNotEqual(hits.get(str(far.id), None) and hits[str(far.id)].reason, "related")
        invalidate_keyterms(user.id)

    @override_settings(GEMINI_API_KEY="k")
    def test_embedding_outage_still_retrieves_by_name(self):
        from pipeline.embeddings import EmbeddingError
        from pipeline.retrieval import retrieve

        user = get_user_model().objects.create_user("badal", password="x")
        shubham = Node.objects.create(user=user, kind=Node.Kind.PERSON, title="Shubham")
        with mock.patch("pipeline.embeddings.embed_texts", side_effect=EmbeddingError("down")):
            ids = retrieve(user.id, "call Shubham").ids()
        self.assertIn(str(shubham.id), ids)
        invalidate_keyterms(user.id)


class RetrievalPrecisionTests(TestCase):
    """At 20k entities, ordinary words collide with short names by sound.
    Reproduces the benchmark failure: "run" pulled in every Runa and Runi."""

    def test_common_words_do_not_match_crowds_of_short_names(self):
        from pipeline.retrieval import match_text

        user = get_user_model().objects.create_user("badal", password="x")
        for i in range(12):
            Node.objects.create(user=user, kind=Node.Kind.PERSON, title=f"Runa{'i' * (i % 3)}", aliases=[f"Runi {i}"])
        stracomm = Node.objects.create(user=user, kind=Node.Kind.COMPANY, title="Stracomm")
        invalidate_keyterms(user.id)

        hits = match_text(user.id, "I have to run the ad for Starcom, karna hai baar baar")

        self.assertEqual([h.node_id for h in hits.values()], [str(stracomm.id)])
        invalidate_keyterms(user.id)


class ConversationTests(AskFixture):
    """Follow-ups mean what they meant: "and who's their designer?" after a
    question about Stracomm is about Stracomm."""

    def test_follow_up_sees_the_conversation_and_continues_it(self):
        with self._gemini(
            {"standalone": "Tell me about Doon Public School", "keywords": ["Doon"]},
            {"answer": "Doon is a school client in Begusarai.", "cited": []},
        ):
            first = self.client.post(reverse("ask"), {"question": "tell me about doon"}, format="json").data

        with self._gemini(
            {"standalone": "What needs changing on the Doon Public School website?", "keywords": ["Doon", "website"]},
            {"answer": "The hero section background [1].", "cited": [1]},
        ):
            second = self.client.post(
                reverse("ask"),
                {"question": "and what's pending on their website?", "conversation_id": first["conversation_id"]},
                format="json",
            ).data

        self.assertEqual(second["conversation_id"], first["conversation_id"])
        # The planner rewrote with the earlier turn in view...
        self.assertIn("Q: tell me about doon", self.systems[0])
        # ...and the answerer saw it too.
        self.assertIn("A: Doon is a school client in Begusarai.", self.prompts[-1])

    def test_a_new_chat_and_another_users_chat_carry_no_history(self):
        with self._gemini({"standalone": "x", "keywords": ["Doon"]}, {"answer": "Secret answer.", "cited": []}):
            mine = self.client.post(reverse("ask"), {"question": "doon?"}, format="json").data

        eve = APIClient()
        eve.force_authenticate(get_user_model().objects.get(username="eve"))
        with self._gemini({"standalone": "y", "keywords": ["Doon"]}, {"answer": "ok", "cited": []}):
            theirs = eve.post(
                reverse("ask"), {"question": "what did we discuss?", "conversation_id": mine["conversation_id"]},
                format="json",
            ).data
        self.assertNotIn("Secret answer", self.systems[0])
        self.assertNotIn("Secret answer", self.prompts[-1])
        self.assertIsNotNone(theirs["conversation_id"])


class EmbeddingRetryTests(TestCase):
    @override_settings(GEMINI_API_KEY="k")
    @mock.patch("pipeline.embeddings.time.sleep")
    @mock.patch("google.genai.Client")
    def test_busy_is_retried_and_a_real_error_is_not(self, Client, sleep):
        from google.genai import errors
        from pipeline.embeddings import EmbeddingError, embed_texts

        busy = errors.ClientError(429, {"error": {"code": 429, "message": "busy", "status": "RESOURCE_EXHAUSTED"}})
        ok = mock.Mock(embeddings=[mock.Mock(values=[0.5])])
        Client.return_value.models.embed_content.side_effect = [busy, ok]
        self.assertEqual(embed_texts(["x"], task="RETRIEVAL_QUERY"), [[0.5]])
        self.assertEqual(sleep.call_count, 1)

        bad = errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})
        Client.return_value.models.embed_content.side_effect = [bad]
        with self.assertRaises(EmbeddingError):
            embed_texts(["x"], task="RETRIEVAL_QUERY")
        self.assertEqual(sleep.call_count, 1)  # no retry for a request that's simply wrong


class KnowledgeApiTests(TestCase):
    def setUp(self):
        from graph.models import Edge

        self.user = get_user_model().objects.create_user("badal", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        invalidate_keyterms(self.user.id)
        self.stracomm = Node.objects.create(
            user=self.user, kind=Node.Kind.COMPANY, title="Stracomm", summary="event company",
            metadata={"client_of": "Credence Marketings"}, mention_count=4,
        )
        self.shubham = Node.objects.create(user=self.user, kind=Node.Kind.PERSON, title="Shubham", summary="owner of Stracomm")
        self.patna = Node.objects.create(user=self.user, kind=Node.Kind.PLACE, title="Patna")
        Edge.objects.create(from_node=self.shubham, relation=Edge.Relation.PART_OF, to_node=self.stracomm)
        for text, days in [("Call Shubham from Stracomm about the ad.", 1), ("Stracomm ka Bandhan shoot.", 0), ("Nothing relevant.", 2)]:
            c = Capture.objects.create(user=self.user, captured_at=timezone.now() - timedelta(days=days))
            Transcript.objects.create(capture=c, kind=Transcript.Kind.NORMALIZED, text=text)
        other = get_user_model().objects.create_user("eve", password="x")
        self.foreign = Node.objects.create(user=other, kind=Node.Kind.COMPANY, title="Stracomm Rival")
        c = Capture.objects.create(user=other, captured_at=timezone.now())
        Transcript.objects.create(capture=c, kind=Transcript.Kind.NORMALIZED, text="Stracomm secret")

    def tearDown(self):
        invalidate_keyterms(self.user.id)

    def test_list_search_by_sound_and_filter_by_kind(self):
        found = self.client.get(reverse("knowledge"), {"q": "starcom"}).data
        self.assertEqual(found["results"][0]["title"], "Stracomm")
        self.assertEqual(found["results"][0]["client_of"], "Credence Marketings")
        self.assertNotIn("Stracomm Rival", [r["title"] for r in found["results"]])

        people = self.client.get(reverse("knowledge"), {"kind": "person"}).data
        self.assertEqual([r["title"] for r in people["results"]], ["Shubham"])

    def test_entity_page_has_links_and_only_your_mentions_newest_first(self):
        page = self.client.get(reverse("knowledge-detail", args=[self.stracomm.id])).data
        self.assertEqual([(l["title"], l["direction"]) for l in page["links"]], [("Shubham", "in")])
        self.assertEqual(page["mention_total"], 2)
        self.assertIn("Bandhan", page["mentions"][0]["snippet"])
        self.assertNotIn("secret", str(page["mentions"]))

    def test_edit_and_relink(self):
        url = reverse("knowledge-detail", args=[self.shubham.id])
        page = self.client.patch(url, {"summary": "owner of Stracomm; met at Exhibition Road", "body": "Prefers WhatsApp."}, format="json").data
        self.assertEqual(page["body"], "Prefers WhatsApp.")

        link = reverse("knowledge-link", args=[self.shubham.id, self.patna.id])
        self.assertEqual(self.client.post(link).status_code, 201)
        titles = [l["title"] for l in self.client.get(url).data["links"]]
        self.assertEqual(sorted(titles), ["Patna", "Stracomm"])

        self.client.delete(reverse("knowledge-link", args=[self.stracomm.id, self.shubham.id]))
        self.assertEqual([l["title"] for l in self.client.get(url).data["links"]], ["Patna"])

    def test_other_users_entities_are_unreachable(self):
        self.assertEqual(self.client.get(reverse("knowledge-detail", args=[self.foreign.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("knowledge-link", args=[self.shubham.id, self.foreign.id])).status_code, 404)
