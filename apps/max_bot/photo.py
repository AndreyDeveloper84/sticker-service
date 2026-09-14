"""MAX photo attachment extraction + safe download.

Adapted (minimally) from the reference
``ai-bot-platform/apps/channels/max/photo.py``:

- extract the image URL from a MAX ``image`` attachment;
- download over HTTPS only, streamed, capped at 10 MiB, with a timeout;
- SSRF gate before any socket is opened: no http://, file://, localhost,
  loopback, private/link-local/reserved IPs (literal or DNS-resolved),
  ``169.254.169.254`` included;
- never log the full URL — MAX CDN URLs carry signed query parameters;
  log lines carry only the hostname.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Final
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES: Final[int] = 10 * 1024 * 1024  # 10 MiB
PHOTO_DOWNLOAD_TIMEOUT_S: Final[float] = 8.0
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"https"})


class PhotoTooLargeError(Exception):
    """Payload exceeded :data:`MAX_PHOTO_BYTES` mid-stream."""


class PhotoDownloadError(Exception):
    """Timeout, network error, 4xx/5xx, or an SSRF-class URL reject."""


def extract_photo_url(attachment: dict[str, Any]) -> str | None:
    """Return the image URL of one MAX ``image`` attachment, or None.

    Supported payload shapes:
      * ``payload.url`` — the current MAX wire format;
      * ``payload.photos`` (dict or list of variants) — legacy form kept
        because Sticker Service already accepted it; the last candidate
        wins (typically the largest variant).
    """
    if not isinstance(attachment, dict):
        return None
    payload = attachment.get("payload")
    if not isinstance(payload, dict):
        return None

    url = payload.get("url")
    if isinstance(url, str) and url:
        return url

    photos = payload.get("photos")
    if isinstance(photos, dict):
        candidates = [
            value.get("url")
            for value in photos.values()
            if isinstance(value, dict) and value.get("url")
        ]
        return candidates[-1] if candidates else None
    if isinstance(photos, list):
        candidates = [
            item.get("url")
            for item in photos
            if isinstance(item, dict) and item.get("url")
        ]
        return candidates[-1] if candidates else None
    return None


def safe_hostname(url: str) -> str:
    """Hostname for log lines — never the path/querystring."""
    try:
        host = urlparse(url).hostname
    except Exception:  # log helper must not raise
        host = None
    return host or "<redacted>"


def _ip_is_unsafe(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _validate_cdn_url(url: str) -> None:
    """SSRF gate — reject anything that isn't https:// to a public IP."""
    if not isinstance(url, str) or not url:
        raise PhotoDownloadError("invalid url: empty or non-string")

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise PhotoDownloadError(f"invalid url: parse_failed {type(exc).__name__}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise PhotoDownloadError(f"scheme not allowed: {scheme or '<empty>'}")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise PhotoDownloadError("invalid url: empty host")

    if host in {"localhost", "ip6-localhost", "metadata", "metadata.google.internal"}:
        raise PhotoDownloadError(f"host not allowed: {host}")

    # IP literal — check directly, no DNS round trip.
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if _ip_is_unsafe(ip):
            raise PhotoDownloadError("host resolves to unsafe ip")
        return

    # Hostname — reject if ANY resolved record is unsafe (mixed-record DNS).
    try:
        addrinfo = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PhotoDownloadError(f"dns: {exc.strerror or 'lookup_failed'}") from exc

    for entry in addrinfo:
        try:
            resolved = ipaddress.ip_address(entry[4][0])
        except (ValueError, IndexError):
            raise PhotoDownloadError("dns: malformed addrinfo entry") from None
        if _ip_is_unsafe(resolved):
            raise PhotoDownloadError("host resolves to unsafe ip")


def download_photo(url: str) -> bytes:
    """Stream-download ``url`` with SSRF validation, size cap and timeout.

    The stream aborts as soon as the cumulative payload crosses
    :data:`MAX_PHOTO_BYTES` — a >10 MiB body never lands in memory.
    Redirects are not followed (a redirect would be an SSRF hop).

    Raises:
      PhotoTooLargeError: cumulative chunks > 10 MiB.
      PhotoDownloadError: SSRF reject, timeout, network error, 4xx/5xx.
    """
    _validate_cdn_url(url)
    host = safe_hostname(url)

    try:
        with httpx.Client(timeout=PHOTO_DOWNLOAD_TIMEOUT_S, follow_redirects=False) as http:
            with http.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    logger.warning("max.photo.cdn_error host=%s status=%d", host, resp.status_code)
                    raise PhotoDownloadError(f"cdn: HTTP {resp.status_code}")
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_PHOTO_BYTES:
                        raise PhotoTooLargeError(f"photo > {MAX_PHOTO_BYTES} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
    except (PhotoTooLargeError, PhotoDownloadError):
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("max.photo.network_failure host=%s exc=%s", host, type(exc).__name__)
        raise PhotoDownloadError(f"network: {type(exc).__name__}") from exc
    except Exception as exc:  # defensive boundary — must not crash the webhook
        logger.warning("max.photo.unexpected_failure host=%s exc=%s", host, type(exc).__name__)
        raise PhotoDownloadError(f"unexpected: {type(exc).__name__}") from exc
