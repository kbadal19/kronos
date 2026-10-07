import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Sweeps audio whose retention window has expired, plus anything
    # stranded by a worker that died between upload and transcribe. The
    # per-capture delete in the pipeline is the primary mechanism; this is
    # the backstop that makes "no audio is kept" actually true.
    "sweep-expired-audio": {
        "task": "pipeline.tasks.sweep_expired_audio",
        "schedule": crontab(minute="*/30"),
    },
    # Re-queues captures that got stuck mid-chain (worker OOM, provider
    # outage, a bad deploy). Without this a failed 4am capture sits in
    # `uploaded` forever and you never find out.
    "retry-stalled-captures": {
        "task": "pipeline.tasks.retry_stalled_captures",
        "schedule": crontab(minute="*/15"),
    },
    # Backstop for meaning-based search: any note or entity whose embedding
    # was missed (outage, deploy mid-task) gets one. Unchanged text is
    # skipped by hash, so a quiet night costs nothing.
    "backfill-embeddings": {
        "task": "pipeline.tasks.backfill_embeddings",
        "schedule": crontab(hour=4, minute=15),
    },
    # Phase 3. Recomputes mention_count/last_mentioned_at for the graph
    # view and writes the morning digest.
    "nightly-consolidation": {
        "task": "pipeline.tasks.nightly_consolidation",
        "schedule": crontab(hour=5, minute=30),
    },
}
