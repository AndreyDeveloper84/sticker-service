import hmac
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def webhook_secret_ok(expected: str, provided: str | None, *, channel: str) -> bool:
    """Fail-closed check of a bot webhook's shared secret.

    An empty configured secret used to mean "accept anyone": a deploy that
    forgot the env var silently opened the webhook to forged updates. Now an
    empty secret rejects, unless WEBHOOK_ALLOW_UNSIGNED is set — and only the
    local settings (dev + tests) set it.
    """
    if not expected:
        if getattr(settings, "WEBHOOK_ALLOW_UNSIGNED", False):
            return True
        logger.error("%s webhook secret is not configured; rejecting the update", channel)
        return False
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
