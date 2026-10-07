"""
Django settings for Second Mind.

Single-user by design: no signup, no tenants, no organisations. Every
capture belongs to the one account created by `manage.py createsuperuser`.
"""
from datetime import timedelta
from importlib.util import find_spec
from pathlib import Path
import os
import sys

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-dev-only-replace-me")

DEBUG = os.getenv("DEBUG", "True") == "True"

ALLOWED_HOSTS = [h for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Needed for ArrayField (node aliases), SearchVectorField (the BM25
    # half of hybrid retrieval) and the pg_trgm / vector extension
    # migration operations.
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "capture",
    "graph",
    "pipeline",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *(["whitenoise.middleware.WhiteNoiseMiddleware"] if find_spec("whitenoise") else []),
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "secondmind"),
        "USER": os.getenv("DB_USER", "secondmind"),
        "PASSWORD": os.getenv("DB_PASSWORD", "secondmind"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

# The graph web UI (Phase 4) is the only browser client; the Expo app
# doesn't need CORS. Empty by default so nothing is allowed until you
# actually stand the graph page up.
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_TRACK_STARTED = True
# A capture that got accepted must eventually get processed, even if the
# worker dies mid-chain. late-ack + reject-on-worker-lost means a task
# killed halfway is redelivered rather than silently dropped; every stage
# in pipeline/tasks.py is written to be idempotent so redelivery is safe.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_MAX_TASKS_PER_CHILD = 50

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # The timeline is append-only and grows forever — at 20 captures a day
    # it's ~7,000 rows after a year, and an unpaginated list would ship all
    # of them, with transcripts, on every app launch.
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

SIMPLE_JWT = {
    # Deliberately long-lived compared to masterflow. This is a personal
    # device-bound token on a single phone: being logged out at 4am is a
    # worse failure than the marginal risk of a long refresh window, and
    # there is no admin who can re-issue it for you at that hour.
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=365),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# --- Capture / audio ----------------------------------------------------
# Audio is spooled to disk only between the upload request and a committed
# raw transcript, then unlinked. Nothing long-lived, nothing in object
# storage. AUDIO_RETENTION_HOURS=0 deletes it the instant the transcript
# commits; a small non-zero value keeps it just long enough to retry a
# transcription that came back empty or garbled.
#
# Set this to something generous (e.g. 336 = 14 days) during Phase 0 so you
# have real audio to benchmark STT providers against, then drop it to 0.
AUDIO_SPOOL_DIR = Path(os.getenv("AUDIO_SPOOL_DIR", BASE_DIR / "spool"))
AUDIO_RETENTION_HOURS = int(os.getenv("AUDIO_RETENTION_HOURS", "24"))
MAX_CAPTURE_BYTES = int(os.getenv("MAX_CAPTURE_BYTES", str(50 * 1024 * 1024)))

# The sideloaded Android app, served at /app/<token>/krono.apk so the phone
# can install updates from a link instead of a cable. Off unless a token
# is set; the token keeps the URL unguessable without needing a login the
# app can't do yet at install time. deploy/publish_apk.sh puts the file here.
APP_DOWNLOAD_DIR = Path(os.getenv("APP_DOWNLOAD_DIR", BASE_DIR / "downloads"))
APP_DOWNLOAD_TOKEN = os.getenv("APP_DOWNLOAD_TOKEN", "")

# --- Transcription ------------------------------------------------------
# Provider is config-driven from day one so swapping it is an env change,
# not a refactor. See pipeline/providers/ — run the bake-off on your own
# voice before committing to one.
TRANSCRIPTION_PROVIDER = os.getenv("TRANSCRIPTION_PROVIDER", "deepgram")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3")
# "multi" is Nova-3's code-switching mode (Hindi and English among its
# languages): it follows a switch mid-sentence instead of forcing one
# language on the whole recording. Set "en" to go back to English-only.
DEEPGRAM_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "multi")
# How many of your own entity names get passed to the recognizer as
# keyterms. This is the single biggest accuracy lever for proper nouns
# ("Project CB", "Credence", "RCA") — see plan §6.
TRANSCRIPTION_KEYTERM_LIMIT = int(os.getenv("TRANSCRIPTION_KEYTERM_LIMIT", "100"))
# Deepgram rejects a request whose keyterms total more than 500 tokens, so
# the list is also cut by an estimated token count, with headroom.
TRANSCRIPTION_KEYTERM_TOKEN_BUDGET = int(
    os.getenv("TRANSCRIPTION_KEYTERM_TOKEN_BUDGET", "450")
)

# How far a capture has to get before it counts as done. In Phase 0 the
# chain ends at `transcribed`, so without this the health endpoint would
# report every capture you have ever made as permanently pending. Bump to
# "extracted" when Phase 1's stages are wired in.
PIPELINE_TERMINAL_STATUS = os.getenv("PIPELINE_TERMINAL_STATUS", "transcribed")

# --- LLM ----------------------------------------------------------------
# Dedicated key on its own Google Cloud project — deliberately NOT the one
# masterflow uses. Separate billing, separate budget alert, separate blast
# radius.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Cheap model for the per-segment grind (normalize, segment, extract);
# stronger one for nightly consolidation and answering questions.
GEMINI_FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-flash-lite-latest")
GEMINI_SMART_MODEL = os.getenv("GEMINI_SMART_MODEL", "gemini-flash-latest")
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))

# The normalize pass: Gemini re-listens to the audio with the Deepgram
# draft, your vocabulary and your recent notes in hand, and writes the
# Hinglish transcript you actually read. Off until a Gemini key exists, so
# capture keeps working on Deepgram alone.
NORMALIZE_ENABLED = os.getenv(
    "NORMALIZE_ENABLED", "true" if GEMINI_API_KEY else "false"
).lower() in ("1", "true", "yes")
# Flash-Lite by default: on real Hinglish it fixed every word and name the
# recognizer missed, at ~1/10th of Flash's audio price (~$1/month at 15
# min/day). Swap to GEMINI_SMART_MODEL if your eval says otherwise.
NORMALIZE_MODEL = os.getenv("NORMALIZE_MODEL", GEMINI_FAST_MODEL)
# Tried only when the primary is overloaded (503) or out of quota (429) —
# limits are per model, so this is usually still available. Rare enough
# that the higher price doesn't matter.
NORMALIZE_FALLBACK_MODEL = os.getenv("NORMALIZE_FALLBACK_MODEL", GEMINI_SMART_MODEL)
# How many of your previous notes go in as topic context.
NORMALIZE_RECENT_CONTEXT = int(os.getenv("NORMALIZE_RECENT_CONTEXT", "5"))

# Confidence bands for edges the extractor proposes (plan §7). Above the
# high mark an edge is written live; between the two it lands in the review
# inbox; below the low mark it's discarded.
EDGE_CONFIDENCE_AUTO = float(os.getenv("EDGE_CONFIDENCE_AUTO", "0.90"))
EDGE_CONFIDENCE_REVIEW = float(os.getenv("EDGE_CONFIDENCE_REVIEW", "0.70"))

# Captures shorter than this get segmentation and extraction in one LLM
# call; longer ones get a dedicated segmentation pass first, which keeps
# each extraction call focused (plan §1).
SEGMENTATION_SPLIT_THRESHOLD_SECONDS = int(
    os.getenv("SEGMENTATION_SPLIT_THRESHOLD_SECONDS", "90")
)

if find_spec("sentry_sdk") and os.getenv("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        # Errors only. This is a one-user system; performance traces would
        # cost more attention than they're worth.
        traces_sample_rate=0.0,
        send_default_pii=False,
    )

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"

# Same reasoning as masterflow: USE_TZ keeps storage in UTC, but "what was
# I thinking last Tuesday" has to resolve against your calendar day, not
# UTC's. A 4am thought is the exact case that breaks under UTC.
TIME_ZONE = "Asia/Kolkata"

USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if find_spec("whitenoise")
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Behind a TLS-terminating proxy (Caddy) -----------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
    CSRF_TRUSTED_ORIGINS = [
        origin.strip()
        for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    # Caddy already redirects http->https, but this is a personal database
    # of everything you think — the cost of defence in depth here is zero.
    SECURE_SSL_REDIRECT = True
    # One year, and only after you've confirmed TLS works on every hostname
    # in ALLOWED_HOSTS. HSTS is hard to walk back: browsers cache the
    # policy, so a premature rollout locks you out of your own plain-HTTP
    # fallback for its full duration.
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# --- Test isolation ---------------------------------------------------------
# `manage.py test` must never reach a paid API or leave jobs in a real Redis
# queue, whatever backend/.env holds. (It did both once: tests read the real
# GEMINI_API_KEY and queued normalize jobs into the dev broker.) Tests that
# need a provider mock it and override these explicitly.
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
if TESTING:
    DEEPGRAM_API_KEY = ""
    GEMINI_API_KEY = ""
    NORMALIZE_ENABLED = False
    CELERY_TASK_ALWAYS_EAGER = True
