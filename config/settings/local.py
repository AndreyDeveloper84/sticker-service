from .base import *  # noqa: F403,F401

DEBUG = True
ALLOWED_HOSTS = ["*"]
# Local dev and tests run without bot webhook secrets.
WEBHOOK_ALLOW_UNSIGNED = True
