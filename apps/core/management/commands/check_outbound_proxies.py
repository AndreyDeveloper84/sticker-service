"""Safe connectivity probe for every configured outbound proxy.

Checks each proxy from OUTBOUND_PROXY_URLS_JSON against both services:

- telegram: GET https://api.telegram.org/ through the proxy — any HTTP
  response (404 expected) proves transport reachability.
- openai: ``models.list()`` through the proxy — a non-billable call that
  proves the route, the API key and the absence of a geo block.

Output contains NO credentials and NO API keys — only ``proxy[i] host:port``
and PASS/FAIL/GEO_BLOCKED status per service.
"""

import httpx
from django.core.management.base import BaseCommand, CommandError

from apps.core.image_providers import GEO_BLOCK_MARKER
from apps.core.outbound_proxy import ProxyPoolConfigError, _setting_or_env, parse_proxy_urls

TELEGRAM_PROBE_URL = "https://api.telegram.org/"
PROBE_TIMEOUT = 20.0


class Command(BaseCommand):
    help = "Probe every configured outbound proxy for Telegram and OpenAI reachability."

    def handle(self, *args, **options):
        try:
            endpoints = parse_proxy_urls(_setting_or_env("OUTBOUND_PROXY_URLS_JSON"))
        except ProxyPoolConfigError as exc:
            raise CommandError(f"proxy config error: {exc}") from None
        if not endpoints:
            raise CommandError(
                "no outbound proxies configured (OUTBOUND_PROXY_URLS_JSON is empty)"
            )

        telegram_ok = False
        openai_ok = False
        for endpoint in endpoints:
            self.stdout.write(endpoint.identity)
            telegram_status = self._probe_telegram(endpoint)
            openai_status = self._probe_openai(endpoint)
            telegram_ok = telegram_ok or telegram_status == "PASS"
            openai_ok = openai_ok or openai_status == "PASS"
            self.stdout.write(f"  telegram: {telegram_status}")
            self.stdout.write(f"  openai: {openai_status}")

        if not telegram_ok or not openai_ok:
            raise CommandError(
                "insufficient proxy health: need at least one proxy with "
                "telegram PASS and one with openai PASS"
            )

    def _probe_telegram(self, endpoint) -> str:
        try:
            with httpx.Client(proxy=endpoint.url, timeout=PROBE_TIMEOUT) as client:
                # 404 from api.telegram.org still proves CONNECT tunnel works.
                client.get(TELEGRAM_PROBE_URL)
            return "PASS"
        except httpx.HTTPError as exc:
            # type name only — httpx messages can embed URLs
            self.stderr.write(f"  telegram probe error: {type(exc).__name__}")
            return "FAIL"

    def _probe_openai(self, endpoint) -> str:
        from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, OpenAIError

        try:
            client = OpenAI(http_client=httpx.Client(proxy=endpoint.url, timeout=PROBE_TIMEOUT))
            client.models.list()
            return "PASS"
        except APIStatusError as exc:
            if exc.status_code == 403 and GEO_BLOCK_MARKER in str(exc):
                return "GEO_BLOCKED"
            # Any other definitive API answer (e.g. 401) still proves the
            # route works — the proxy is fine, the key/quota is not.
            return "FAIL"
        except (APIConnectionError, APITimeoutError) as exc:
            self.stderr.write(f"  openai probe error: {type(exc).__name__}")
            return "FAIL"
        except OpenAIError as exc:  # e.g. OPENAI_API_KEY not set
            self.stderr.write(f"  openai probe error: {type(exc).__name__}")
            return "FAIL"
