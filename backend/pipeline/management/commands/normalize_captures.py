"""
Run the normalize pass by hand and see what it changed.

This is the accuracy check: Phase 0 keeps audio for two weeks, so you can
re-run normalization on real notes after changing the prompt, the model or
the vocabulary, and read the draft against the result.

    python manage.py normalize_captures --all-with-audio --show
    python manage.py normalize_captures <capture-id> --force --show

Runs synchronously in this process, not through Celery. Celery's eager
retries ignore their countdown and would burn a free-tier quota in a
second, so this makes one attempt at a time and waits between them. Captures whose
audio is gone still get a text-only pass (vocabulary, corrections, script).
"""
import time

from django.core.management.base import BaseCommand, CommandError

from capture.models import Capture, Transcript
from pipeline.normalize import TRANSIENT_PREFIX
from pipeline.tasks import normalize

ATTEMPTS = 3
# Gemini's free tier counts requests per minute.
WAIT_SECONDS = 60


class Command(BaseCommand):
    help = "Run the Gemini normalize pass over captures and optionally print raw vs. normalized."

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="*", help="Capture ids")
        parser.add_argument(
            "--all-with-audio", action="store_true",
            help="Every transcribed capture whose audio is still on disk",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Replace an existing normalized transcript",
        )
        parser.add_argument("--show", action="store_true", help="Print raw and normalized text")

    def handle(self, *args, ids, all_with_audio, force, show, **options):
        if not ids and not all_with_audio:
            raise CommandError("Pass capture ids or --all-with-audio.")

        qs = Capture.objects.filter(transcripts__kind=Transcript.Kind.RAW)
        if ids:
            qs = qs.filter(pk__in=ids)
        if all_with_audio:
            qs = qs.filter(audio_deleted_at__isnull=True).exclude(audio_path="")
        captures = list(qs.distinct().order_by("captured_at"))

        if not captures:
            self.stdout.write("Nothing to normalize.")
            return

        for capture in captures:
            for attempt in range(1, ATTEMPTS + 1):
                # retries=max_retries makes the task give up on a transient
                # error instead of retrying instantly in-process.
                normalize.apply(
                    args=[str(capture.id)], kwargs={"force": force},
                    retries=normalize.max_retries,
                )
                run = capture.extraction_runs.filter(stage="normalize").first()
                transient = run and run.status == "failed" and run.error.startswith(TRANSIENT_PREFIX)
                if not transient or attempt == ATTEMPTS:
                    break
                self.stdout.write(f"  {run.error} — waiting {WAIT_SECONDS}s")
                time.sleep(WAIT_SECONDS)
            by_kind = {t.kind: t for t in capture.transcripts.all()}
            normalized = by_kind.get(Transcript.Kind.NORMALIZED)
            run = capture.extraction_runs.filter(stage="normalize").first()

            status = "normalized" if normalized else f"kept raw ({run.error if run else 'no run'})"
            self.stdout.write(self.style.SUCCESS(f"\n{capture.id}  {capture.captured_at:%Y-%m-%d %H:%M}  {status}"))
            if show:
                self.stdout.write(f"  RAW:        {by_kind[Transcript.Kind.RAW].text}")
                if normalized:
                    self.stdout.write(f"  NORMALIZED: {normalized.text}")
                if run and run.raw_output:
                    for c in run.raw_output.get("corrections", []):
                        self.stdout.write(f"    {c['heard']!r} -> {c['written']!r}")
