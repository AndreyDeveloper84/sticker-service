import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-local-dev-key")
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "stickers"),
        "USER": os.getenv("POSTGRES_USER", "stickers"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "stickers"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# Background generation (async design 2026-09-20). Default off: the console
# runs the provider call inline (today's behaviour). When on, the console only
# creates the PENDING job and enqueues its id; a separate `generation_worker`
# process performs the provider call (RQ, Redis holds job ids only).
GENERATION_WORKER_ENABLED = os.getenv("GENERATION_WORKER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
GENERATION_QUEUE_NAME = os.getenv("GENERATION_QUEUE_NAME", "generation")
GENERATION_QUEUE_REDIS_URL = os.getenv("GENERATION_QUEUE_REDIS_URL", "") or REDIS_URL
# RQ job_timeout must exceed the worker's OpenAI read timeout (540 s) and stay
# below the stale-RUNNING guard (15 min): read 540 < job 560 < stale 900.
GENERATION_JOB_TIMEOUT_S = int(os.getenv("GENERATION_JOB_TIMEOUT_S", "560"))
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(BASE_DIR / "media")))
ORDER_PHOTO_MAX_BYTES = int(os.getenv("ORDER_PHOTO_MAX_BYTES", str(20 * 1024 * 1024)))
# Photo Suitability Gate (DRF-2164): decode + min side + aspect + blur before
# a photo is accepted (see services/photo_gate.py for the rationale).
PHOTO_GATE_ENABLED = os.getenv("PHOTO_GATE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
PHOTO_MIN_SIDE = int(os.getenv("PHOTO_MIN_SIDE", "512"))
PHOTO_MAX_ASPECT = float(os.getenv("PHOTO_MAX_ASPECT", "2.5"))
PHOTO_BLUR_MIN_VARIANCE = float(os.getenv("PHOTO_BLUR_MIN_VARIANCE", "30"))
# observe (default): a blurry photo is accepted, the metric is stored and a
# warning logged; enforce: rejected. Switch after >= 20 live photos are measured.
PHOTO_BLUR_MODE = os.getenv("PHOTO_BLUR_MODE", "observe").strip().lower()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
# Telegram outbound transport (DRF-1870): defaults are production-compatible
# direct api.telegram.org; set a Bot API relay and/or proxy on restricted
# networks. Credentials only via env, never in code.
TELEGRAM_API_ORIGIN = os.getenv("TELEGRAM_API_ORIGIN", "")
TELEGRAM_FILE_ORIGIN = os.getenv("TELEGRAM_FILE_ORIGIN", "")
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "")

# Centralized outbound proxy pool for geo-blocked upstreams (Telegram,
# OpenAI). Application-level only: nothing else on the host is proxied.
# Credentials only via env (.env.staging, mode 600), never in code/git.
OUTBOUND_PROXY_ENABLED = os.getenv("OUTBOUND_PROXY_ENABLED", "").lower() in ("1", "true", "yes", "on")
# JSON list so credentials may contain any characters:
# OUTBOUND_PROXY_URLS_JSON=["http://user:pass@proxy-a:3128","http://user:pass@proxy-b:3128"]
OUTBOUND_PROXY_URLS_JSON = os.getenv("OUTBOUND_PROXY_URLS_JSON", "")
OUTBOUND_PROXY_COOLDOWN_SECONDS = os.getenv("OUTBOUND_PROXY_COOLDOWN_SECONDS", "60")

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}
