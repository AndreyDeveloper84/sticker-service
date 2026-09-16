"""Centralized application-level outbound proxy pool (Telegram / OpenAI).

Some upstream APIs are unreachable from the staging VPS (Telegram: TCP
timeout to api.telegram.org; OpenAI: 403 unsupported_country_region_territory).
This module provides a small process-local pool of owner-controlled
authenticated proxies with health-aware failover — application-level only:
no host routing, VPN or global HTTPS_PROXY changes.

### Config contract (env / Django settings)

- ``OUTBOUND_PROXY_ENABLED`` — "true"/"1"/"yes" enables the pool.
- ``OUTBOUND_PROXY_URLS_JSON`` — JSON list of proxy URLs, e.g.
  ``["http://user:pass@proxy-a:3128","socks5://user:pass@proxy-b:1080"]``.
  JSON (not comma-separated) so credentials may contain any characters.
- ``OUTBOUND_PROXY_COOLDOWN_SECONDS`` — per proxy/service cooldown after a
  transport failure (default 60).

Credentials live ONLY in env (.env.staging, mode 600). Logs, reprs and
exceptions expose just the credential-free identity ``proxy[i] host:port``.

### Behavior

- Deterministic round-robin across healthy proxies per service; no random
  per-request rotation.
- Health is tracked per (proxy, service): an OpenAI geo block does not
  affect Telegram eligibility of the same proxy and vice versa.
- Failure = transport-level only (connect/timeout/reset, proxy auth,
  SOCKS/CONNECT failure). Upstream API responses (Telegram 400/401,
  OpenAI 400/401/429) are NOT proxy failures and never rotate.
- Cooldown with lazy recovery: no background scheduler; an expired entry
  becomes eligible on the next ``select()``.
- Process-local state: each Gunicorn worker keeps its own pool. This is
  intentional for MVP; shared circuit state (Redis) is out of scope.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 60.0
SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h")
SOCKS_SCHEMES = ("socks5", "socks5h")


class Service(str, Enum):
    TELEGRAM = "telegram"
    OPENAI = "openai"


class ProxyPoolConfigError(ValueError):
    """Invalid proxy pool configuration. Messages never echo credentials."""


@dataclass(frozen=True)
class ProxyEndpoint:
    """One configured proxy. ``url`` carries credentials and is NEVER logged."""

    index: int
    url: str
    scheme: str
    host: str
    port: int | None

    @property
    def identity(self) -> str:
        """Credential-free label for logs: ``proxy[0] host:port``."""
        port = f":{self.port}" if self.port is not None else ""
        return f"proxy[{self.index}] {self.host}{port}"

    def __repr__(self) -> str:
        return f"ProxyEndpoint({self.identity})"

    __str__ = __repr__


def parse_proxy_urls(raw_json: str) -> list[ProxyEndpoint]:
    """Parse OUTBOUND_PROXY_URLS_JSON. Errors never include the raw value."""
    if not raw_json or not raw_json.strip():
        return []
    try:
        data = json.loads(raw_json)
    except ValueError:
        raise ProxyPoolConfigError("OUTBOUND_PROXY_URLS_JSON is not valid JSON") from None
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ProxyPoolConfigError("OUTBOUND_PROXY_URLS_JSON must be a JSON list of URL strings")

    endpoints = []
    for index, url in enumerate(data):
        parts = urlsplit(url)
        if parts.scheme not in SUPPORTED_SCHEMES:
            raise ProxyPoolConfigError(
                f"proxy[{index}]: unsupported scheme {parts.scheme!r} "
                f"(allowed: {', '.join(SUPPORTED_SCHEMES)})"
            )
        if not parts.hostname:
            raise ProxyPoolConfigError(f"proxy[{index}]: missing host")
        if parts.scheme in SOCKS_SCHEMES and importlib.util.find_spec("socksio") is None:
            raise ProxyPoolConfigError(
                f"proxy[{index}]: socks5 proxy requires the socksio package (httpx[socks])"
            )
        try:
            port = parts.port
        except ValueError:
            raise ProxyPoolConfigError(f"proxy[{index}]: invalid port") from None
        endpoints.append(
            ProxyEndpoint(index=index, url=url, scheme=parts.scheme, host=parts.hostname, port=port)
        )
    return endpoints


class ProxyPool:
    """Process-local pool with round-robin selection and per-service cooldown."""

    def __init__(
        self,
        endpoints,
        *,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock=time.monotonic,
    ):
        self._endpoints = list(endpoints)
        self._cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._cooldown_until: dict[tuple[int, Service], float] = {}
        self._rr: dict[Service, int] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._endpoints)

    @property
    def endpoints(self) -> tuple[ProxyEndpoint, ...]:
        return tuple(self._endpoints)

    def state(self, endpoint: ProxyEndpoint, service) -> str:
        """HEALTHY or COOLDOWN for a proxy/service pair (lazy recovery view)."""
        service = Service(service)
        until = self._cooldown_until.get((endpoint.index, service))
        if until is not None and until > self._clock():
            return "COOLDOWN"
        return "HEALTHY"

    def select(self, service) -> ProxyEndpoint | None:
        """Next eligible proxy for the service, or None if all are cooling down."""
        service = Service(service)
        with self._lock:
            now = self._clock()
            eligible = [
                endpoint
                for endpoint in self._endpoints
                if self._cooldown_until.get((endpoint.index, service), 0.0) <= now
            ]
            if not eligible:
                logger.warning("proxy.exhausted service=%s", service.value)
                return None
            counter = self._rr.get(service, 0)
            endpoint = eligible[counter % len(eligible)]
            self._rr[service] = counter + 1
            logger.info("proxy.selected service=%s %s", service.value, endpoint.identity)
            return endpoint

    def report_success(self, endpoint: ProxyEndpoint, service) -> None:
        service = Service(service)
        with self._lock:
            self._cooldown_until.pop((endpoint.index, service), None)
        logger.info("proxy.success service=%s %s", service.value, endpoint.identity)

    def report_failure(self, endpoint: ProxyEndpoint, service, reason: str) -> None:
        service = Service(service)
        with self._lock:
            self._cooldown_until[(endpoint.index, service)] = self._clock() + self._cooldown_seconds
        logger.warning(
            "proxy.cooldown service=%s %s reason=%s cooldown=%ss",
            service.value,
            endpoint.identity,
            reason,
            int(self._cooldown_seconds),
        )


# --------------------------------------------------------------------- config


def _setting_or_env(name: str) -> str:
    try:
        from django.conf import settings

        value = getattr(settings, name, "")
        if value not in ("", None):
            return str(value)
    except Exception:  # settings not configured (plain scripts)
        pass
    return os.getenv(name, "")


def _config_flag(name: str) -> bool:
    value = _setting_or_env(name)
    if value:
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _config_cooldown() -> float:
    raw = _setting_or_env("OUTBOUND_PROXY_COOLDOWN_SECONDS")
    if not raw:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise ProxyPoolConfigError("OUTBOUND_PROXY_COOLDOWN_SECONDS must be a number") from None
    if value <= 0:
        raise ProxyPoolConfigError("OUTBOUND_PROXY_COOLDOWN_SECONDS must be positive")
    return value


_POOL: ProxyPool | None = None
_POOL_INIT = False
_POOL_LOCK = threading.Lock()


def get_proxy_pool() -> ProxyPool | None:
    """Process-local shared pool, or None when disabled/unconfigured.

    Built lazily per process (Gunicorn worker); health state is per-process
    by design (see module docstring).
    """
    global _POOL, _POOL_INIT
    with _POOL_LOCK:
        if _POOL_INIT:
            return _POOL
        _POOL_INIT = True
        if not _config_flag("OUTBOUND_PROXY_ENABLED"):
            _POOL = None
            return None
        endpoints = parse_proxy_urls(_setting_or_env("OUTBOUND_PROXY_URLS_JSON"))
        if not endpoints:
            logger.warning("proxy.config OUTBOUND_PROXY_ENABLED set but OUTBOUND_PROXY_URLS_JSON is empty")
            _POOL = None
            return None
        _POOL = ProxyPool(endpoints, cooldown_seconds=_config_cooldown())
        logger.info("proxy.config pool_size=%d", len(_POOL))
        return _POOL


def reset_proxy_pool() -> None:
    """Drop the cached process pool (tests / config reload)."""
    global _POOL, _POOL_INIT
    with _POOL_LOCK:
        _POOL = None
        _POOL_INIT = False
