"""Tests for the centralized outbound proxy pool (apps.core.outbound_proxy).

Environment-independent: selection/cooldown use an injectable fake clock,
and all secrets used here are fake placeholders that must never leak into
reprs, logs or exception messages.
"""

import importlib.util
import json
import logging

from django.test import SimpleTestCase, override_settings

from apps.core.outbound_proxy import (
    ProxyPool,
    ProxyPoolConfigError,
    Service,
    get_proxy_pool,
    parse_proxy_urls,
    reset_proxy_pool,
)

URL_A = "http://user-a:secret-a@proxy-a.example:3128"
URL_B = "socks5://user-b:secret-b@proxy-b.example:1080"
URL_C = "http://user-c:secret-c@proxy-c.example:3128"

SECRETS = ("secret-a", "secret-b", "secret-c", "user-a", "user-b", "user-c")


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _pool(*urls, cooldown=60.0, clock=None):
    endpoints = parse_proxy_urls(json.dumps(list(urls)))
    return ProxyPool(endpoints, cooldown_seconds=cooldown, clock=clock or FakeClock())


class ParseProxyUrlsTests(SimpleTestCase):
    def test_empty_config(self):
        self.assertEqual(parse_proxy_urls(""), [])
        self.assertEqual(parse_proxy_urls("   "), [])

    def test_single_proxy(self):
        endpoints = parse_proxy_urls(json.dumps([URL_A]))
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0].host, "proxy-a.example")
        self.assertEqual(endpoints[0].port, 3128)
        self.assertEqual(endpoints[0].scheme, "http")

    def test_multiple_proxies(self):
        endpoints = parse_proxy_urls(json.dumps([URL_A, URL_C]))
        self.assertEqual([e.index for e in endpoints], [0, 1])

    def test_special_chars_in_credentials(self):
        url = "http://us%40er:p%2Fss%3Aword@proxy-x.example:3128"
        endpoints = parse_proxy_urls(json.dumps([url]))
        self.assertEqual(endpoints[0].url, url)

    def test_invalid_json(self):
        with self.assertRaises(ProxyPoolConfigError) as ctx:
            parse_proxy_urls("{not json, secret-a}")
        self.assertNotIn("secret-a", str(ctx.exception))

    def test_not_a_list(self):
        with self.assertRaises(ProxyPoolConfigError):
            parse_proxy_urls(json.dumps({"url": URL_A}))

    def test_unsupported_scheme_no_credential_echo(self):
        with self.assertRaises(ProxyPoolConfigError) as ctx:
            parse_proxy_urls(json.dumps(["gopher://user-a:secret-a@proxy-a.example:70"]))
        message = str(ctx.exception)
        self.assertIn("gopher", message)
        for secret in ("secret-a", "user-a"):
            self.assertNotIn(secret, message)

    def test_missing_host(self):
        with self.assertRaises(ProxyPoolConfigError):
            parse_proxy_urls(json.dumps(["http://"]))

    def test_invalid_port(self):
        with self.assertRaises(ProxyPoolConfigError):
            parse_proxy_urls(json.dumps(["http://proxy-a.example:not-a-port"]))

    def test_socks5_requires_socksio(self):
        if importlib.util.find_spec("socksio") is not None:
            self.skipTest("socksio installed")
        with self.assertRaises(ProxyPoolConfigError) as ctx:
            parse_proxy_urls(json.dumps([URL_B]))
        self.assertIn("socksio", str(ctx.exception))


class ProxyEndpointIdentityTests(SimpleTestCase):
    def test_identity_has_no_credentials(self):
        (endpoint,) = parse_proxy_urls(json.dumps([URL_A]))
        self.assertEqual(endpoint.identity, "proxy[0] proxy-a.example:3128")
        for secret in SECRETS:
            self.assertNotIn(secret, endpoint.identity)
            self.assertNotIn(secret, repr(endpoint))
            self.assertNotIn(secret, str(endpoint))


class ProxyPoolSelectionTests(SimpleTestCase):
    def test_round_robin(self):
        pool = _pool(URL_A, URL_C)
        first = pool.select(Service.TELEGRAM)
        second = pool.select(Service.TELEGRAM)
        third = pool.select(Service.TELEGRAM)
        self.assertEqual(first.host, "proxy-a.example")
        self.assertEqual(second.host, "proxy-c.example")
        self.assertEqual(third.host, "proxy-a.example")

    def test_round_robin_per_service(self):
        pool = _pool(URL_A, URL_C)
        self.assertEqual(pool.select(Service.TELEGRAM).host, "proxy-a.example")
        # openai has its own counter — starts from A again
        self.assertEqual(pool.select(Service.OPENAI).host, "proxy-a.example")
        self.assertEqual(pool.select(Service.TELEGRAM).host, "proxy-c.example")

    def test_failure_puts_proxy_in_cooldown(self):
        clock = FakeClock()
        pool = _pool(URL_A, URL_C, clock=clock)
        failed = pool.select(Service.TELEGRAM)
        pool.report_failure(failed, Service.TELEGRAM, "ConnectError")
        self.assertEqual(pool.state(failed, Service.TELEGRAM), "COOLDOWN")
        # next selection skips the cooled-down proxy
        self.assertEqual(pool.select(Service.TELEGRAM).host, "proxy-c.example")

    def test_recovery_after_cooldown(self):
        clock = FakeClock()
        pool = _pool(URL_A, URL_C, cooldown=60.0, clock=clock)
        failed = pool.select(Service.TELEGRAM)
        pool.report_failure(failed, Service.TELEGRAM, "ConnectError")
        clock.now += 61
        self.assertEqual(pool.state(failed, Service.TELEGRAM), "HEALTHY")
        # A is eligible again — round-robin counter continues, so both
        # proxies are served across the next two selections
        selected = {pool.select(Service.TELEGRAM).host, pool.select(Service.TELEGRAM).host}
        self.assertEqual(selected, {"proxy-a.example", "proxy-c.example"})

    def test_all_unavailable_returns_none(self):
        pool = _pool(URL_A, URL_C)
        for endpoint in pool.endpoints:
            pool.report_failure(endpoint, Service.TELEGRAM, "ConnectError")
        self.assertIsNone(pool.select(Service.TELEGRAM))

    def test_service_specific_health(self):
        pool = _pool(URL_A)
        endpoint = pool.endpoints[0]
        pool.report_failure(endpoint, Service.OPENAI, "geo")
        # geo-blocked for openai, still healthy for telegram
        self.assertEqual(pool.state(endpoint, Service.OPENAI), "COOLDOWN")
        self.assertEqual(pool.state(endpoint, Service.TELEGRAM), "HEALTHY")
        self.assertIsNone(pool.select(Service.OPENAI))
        self.assertEqual(pool.select(Service.TELEGRAM), endpoint)

    def test_success_clears_cooldown(self):
        pool = _pool(URL_A)
        endpoint = pool.endpoints[0]
        pool.report_failure(endpoint, Service.TELEGRAM, "ConnectError")
        pool.report_success(endpoint, Service.TELEGRAM)
        self.assertEqual(pool.state(endpoint, Service.TELEGRAM), "HEALTHY")

    def test_failure_logs_no_credentials(self):
        pool = _pool(URL_A)
        endpoint = pool.endpoints[0]
        logger = "apps.core.outbound_proxy"
        with self.assertLogs(logger, level="INFO") as captured:
            pool.select(Service.TELEGRAM)
            pool.report_success(endpoint, Service.TELEGRAM)
            pool.report_failure(endpoint, Service.TELEGRAM, "ConnectError")
            pool.select(Service.TELEGRAM)
        output = "\n".join(captured.output)
        self.assertIn("proxy[0] proxy-a.example:3128", output)
        for secret in SECRETS:
            self.assertNotIn(secret, output)


class ProxyPoolConfigTests(SimpleTestCase):
    def setUp(self):
        reset_proxy_pool()
        self.addCleanup(reset_proxy_pool)

    def test_disabled_by_default(self):
        self.assertIsNone(get_proxy_pool())

    @override_settings(OUTBOUND_PROXY_ENABLED=True, OUTBOUND_PROXY_URLS_JSON="")
    def test_enabled_without_urls(self):
        self.assertIsNone(get_proxy_pool())

    @override_settings(
        OUTBOUND_PROXY_ENABLED=True,
        OUTBOUND_PROXY_URLS_JSON='["http://user-a:secret-a@proxy-a.example:3128"]',
        OUTBOUND_PROXY_COOLDOWN_SECONDS="30",
    )
    def test_enabled_pool_from_settings(self):
        pool = get_proxy_pool()
        self.assertIsNotNone(pool)
        self.assertEqual(len(pool), 1)
        # cached per process
        self.assertIs(get_proxy_pool(), pool)

    @override_settings(OUTBOUND_PROXY_ENABLED=True, OUTBOUND_PROXY_URLS_JSON="{broken")
    def test_invalid_config_raises_without_credentials(self):
        with self.assertRaises(ProxyPoolConfigError):
            get_proxy_pool()
