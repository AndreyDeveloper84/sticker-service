import os

from .production import *  # noqa: F403,F401
from .production import BASE_DIR

DEBUG = False

# Primary hostname of the staging deployment (e.g. "staging.example.com").
# Used to derive ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS when the explicit
# DJANGO_* variables are not set.
STAGING_DOMAIN = os.getenv("STAGING_DOMAIN", "").strip()

_allowed_hosts = [
    h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()
]
if STAGING_DOMAIN and STAGING_DOMAIN not in _allowed_hosts:
    _allowed_hosts.append(STAGING_DOMAIN)
if _allowed_hosts:
    ALLOWED_HOSTS = _allowed_hosts

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]
if STAGING_DOMAIN:
    _origin = f"https://{STAGING_DOMAIN}"
    if _origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_origin)

# collectstatic target; shared with nginx through the static_data volume.
STATIC_ROOT = os.getenv("STATIC_ROOT", str(BASE_DIR / "staticfiles"))

# Behind the nginx HTTPS reverse proxy (SECURE_PROXY_SSL_HEADER is already
# set by production.py); trust the forwarded host header from nginx.
USE_X_FORWARDED_HOST = True
