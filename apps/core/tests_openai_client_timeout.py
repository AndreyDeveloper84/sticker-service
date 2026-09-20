"""Explicit OpenAI client timeouts (Order 14, 2026-09-20).

Two preview jobs were killed by gunicorn at 300 s inside images.edit because
the SDK default read timeout is 600 s. The client must now close a hang itself
(connect 10 s / read 240 s), raise a real APITimeoutError that
classify_failure maps to "ambiguous" (fail closed, no duplicate), and never
let the SDK retry the request on its own (max_retries=0).
"""

import os
from unittest import mock

import httpx
from django.test import SimpleTestCase
from openai import APITimeoutError

from apps.core.image_providers import (
    OPENAI_MAX_RETRIES,
    OpenAIImageProvider,
    ProviderConfigurationError,
    openai_client_timeout,
)


class _HangingTransport(httpx.BaseTransport):
    """Counts requests and raises the given httpx timeout for each one."""

    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.requests = []

    def handle_request(self, request):
        self.requests.append(request)
        raise self.exc_type("simulated", request=request)


def _client_with_transport(transport):
    """The real SDK client built exactly like _default_client_factory, with the
    network replaced by ``transport``."""
    from openai import OpenAI

    return OpenAI(
        api_key="test-key",
        http_client=httpx.Client(transport=transport),
        timeout=openai_client_timeout(),
        max_retries=OPENAI_MAX_RETRIES,
    )


class OpenAIClientTimeoutTests(SimpleTestCase):
    def test_defaults_connect_10_read_240(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("OPENAI_CONNECT_TIMEOUT_S", "OPENAI_READ_TIMEOUT_S", "OPENAI_WRITE_TIMEOUT_S", "OPENAI_POOL_TIMEOUT_S"):
                os.environ.pop(name, None)
            timeout = openai_client_timeout()
        self.assertEqual(timeout.connect, 10.0)
        self.assertEqual(timeout.read, 240.0)
        self.assertEqual(timeout.write, 60.0)
        self.assertEqual(timeout.pool, 10.0)
        self.assertEqual(OPENAI_MAX_RETRIES, 0)

    def test_env_override_for_the_future_worker(self):
        with mock.patch.dict(os.environ, {"OPENAI_READ_TIMEOUT_S": "540"}):
            self.assertEqual(openai_client_timeout().read, 540.0)

    def test_bad_env_fails_closed(self):
        with mock.patch.dict(os.environ, {"OPENAI_READ_TIMEOUT_S": "soon"}):
            with self.assertRaises(ProviderConfigurationError):
                openai_client_timeout()
        with mock.patch.dict(os.environ, {"OPENAI_CONNECT_TIMEOUT_S": "0"}):
            with self.assertRaises(ProviderConfigurationError):
                openai_client_timeout()

    def test_read_timeout_is_ambiguous_and_sent_exactly_once(self):
        transport = _HangingTransport(httpx.ReadTimeout)
        client = _client_with_transport(transport)

        with self.assertRaises(APITimeoutError) as ctx:
            client.images.edit(model="m", image=[("p.jpg", b"x", "image/jpeg")], prompt="p")

        self.assertIsInstance(ctx.exception.__cause__, httpx.ReadTimeout)
        self.assertEqual(OpenAIImageProvider.classify_failure(ctx.exception), "ambiguous")
        # no SDK-level retry: a second images.edit would be a blind billable duplicate
        self.assertEqual(len(transport.requests), 1)

    def test_connect_timeout_is_transport_and_sent_once(self):
        transport = _HangingTransport(httpx.ConnectTimeout)
        client = _client_with_transport(transport)

        with self.assertRaises(APITimeoutError) as ctx:
            client.images.edit(model="m", image=[("p.jpg", b"x", "image/jpeg")], prompt="p")

        self.assertEqual(OpenAIImageProvider.classify_failure(ctx.exception), "transport")
        self.assertEqual(len(transport.requests), 1)

    def test_sdk_default_would_have_retried(self):
        """Documents why max_retries=0 matters: the SDK's own default retries a
        read timeout, i.e. re-submits the billable request."""
        from openai import OpenAI

        transport = _HangingTransport(httpx.ReadTimeout)
        client = OpenAI(api_key="test-key", http_client=httpx.Client(transport=transport), timeout=openai_client_timeout())
        with mock.patch("time.sleep"):
            with self.assertRaises(APITimeoutError):
                client.images.edit(model="m", image=[("p.jpg", b"x", "image/jpeg")], prompt="p")
        self.assertGreater(len(transport.requests), 1)
