"""Transport tests for the Telegram outbound client (DRF-1870).

All HTTP is mocked at the ``_shared_client`` boundary; tests are
environment-independent (Django settings/env for origins are neutralised
where relevant via override_settings / patch.dict).
"""

from unittest import mock

import httpx
from django.test import SimpleTestCase, override_settings

from apps.telegram_bot.client import DEFAULT_API_ORIGIN, TelegramAPIError, TelegramBotClient

TOKEN = "123456:test-token"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fake_http(response=None, side_effect=None):
    """Return (patcher, fake_client) patching the shared httpx client."""
    fake = mock.MagicMock(name="shared_httpx_client")
    for verb in ("post", "get"):
        method_mock = getattr(fake, verb)
        if side_effect is not None:
            method_mock.side_effect = side_effect
        else:
            method_mock.return_value = response or FakeResponse(payload={"ok": True, "result": {"done": True}})
    return mock.patch("apps.telegram_bot.client._shared_client", return_value=fake), fake


class TelegramClientConfigTests(SimpleTestCase):
    def test_default_api_origin(self):
        client = TelegramBotClient(TOKEN)
        self.assertEqual(client.api_origin, "https://api.telegram.org")
        self.assertEqual(DEFAULT_API_ORIGIN, "https://api.telegram.org")

    def test_configurable_api_origin(self):
        client = TelegramBotClient(TOKEN, api_origin="https://relay.example.test/")
        self.assertEqual(client.api_origin, "https://relay.example.test")

    @override_settings(TELEGRAM_API_ORIGIN="https://relay-from-settings.test")
    def test_api_origin_from_settings(self):
        client = TelegramBotClient(TOKEN)
        self.assertEqual(client.api_origin, "https://relay-from-settings.test")

    def test_file_origin_defaults_to_api_origin(self):
        client = TelegramBotClient(TOKEN, api_origin="https://relay.example.test")
        self.assertEqual(client.file_origin, "https://relay.example.test")

    def test_configurable_file_origin(self):
        client = TelegramBotClient(TOKEN, file_origin="https://files.example.test")
        self.assertEqual(client.file_origin, "https://files.example.test")

    def test_proxy_configuration_reaches_shared_client(self):
        with mock.patch("apps.telegram_bot.client.httpx.Client") as client_cls:
            from apps.telegram_bot.client import _SHARED_CLIENTS, _shared_client

            _SHARED_CLIENTS.clear()
            self.addCleanup(_SHARED_CLIENTS.clear)
            _shared_client("http://proxy.example.test:8080", 30.0)
        client_cls.assert_called_once_with(proxy="http://proxy.example.test:8080", timeout=30.0)

    def test_no_proxy_by_default(self):
        with mock.patch("apps.telegram_bot.client.httpx.Client") as client_cls:
            from apps.telegram_bot.client import _SHARED_CLIENTS, _shared_client

            _SHARED_CLIENTS.clear()
            self.addCleanup(_SHARED_CLIENTS.clear)
            _shared_client("", 30.0)
        client_cls.assert_called_once_with(proxy=None, timeout=30.0)


class TelegramClientMethodTests(SimpleTestCase):
    def setUp(self):
        self.client = TelegramBotClient(TOKEN)

    def test_send_message(self):
        patcher, fake = self._fake()
        with patcher:
            self.client.send_message(chat_id=42, text="hi", reply_markup={"inline_keyboard": []})
        args, kwargs = fake.post.call_args
        self.assertEqual(args[0], f"{DEFAULT_API_ORIGIN}/bot{TOKEN}/sendMessage")
        self.assertEqual(
            kwargs["json"],
            {"chat_id": 42, "text": "hi", "reply_markup": {"inline_keyboard": []}},
        )

    def _fake(self, **kwargs):
        return _fake_http(**kwargs)

    def test_get_file(self):
        patcher, fake = self._fake(
            response=FakeResponse(payload={"ok": True, "result": {"file_path": "photos/a.jpg"}})
        )
        with patcher:
            result = self.client.get_file("file-id-1")
        args, kwargs = fake.post.call_args
        self.assertEqual(args[0], f"{DEFAULT_API_ORIGIN}/bot{TOKEN}/getFile")
        self.assertEqual(kwargs["json"], {"file_id": "file-id-1"})
        self.assertEqual(result, {"file_path": "photos/a.jpg"})

    def test_download_file_uses_file_origin(self):
        client = TelegramBotClient(TOKEN, file_origin="https://files.example.test")
        patcher, fake = self._fake(response=FakeResponse(content=b"image-bytes"))
        with patcher:
            content = client.download_file("photos/a.jpg")
        args, _ = fake.get.call_args
        self.assertEqual(args[0], f"https://files.example.test/file/bot{TOKEN}/photos/a.jpg")
        self.assertEqual(content, b"image-bytes")

    def test_download_file_shares_proxy_config(self):
        """getFile and download_file must go through the same client/proxy."""
        client = TelegramBotClient(TOKEN, proxy_url="http://proxy.example.test:8080")
        patcher, fake = self._fake(
            response=FakeResponse(payload={"ok": True, "result": {"file_path": "photos/a.jpg"}}, content=b"x")
        )
        with patcher:
            client.get_file("f")
            client.download_file("photos/a.jpg")
        # both verbs were served by the SAME shared client instance
        self.assertEqual(fake.post.call_count, 1)
        self.assertEqual(fake.get.call_count, 1)

    def test_send_photo_multipart(self):
        patcher, fake = self._fake()
        with patcher:
            self.client.send_photo(
                chat_id=42,
                content=b"png-bytes",
                filename="preview.png",
                mime_type="image/png",
                caption="caption",
            )
        args, kwargs = fake.post.call_args
        self.assertEqual(args[0], f"{DEFAULT_API_ORIGIN}/bot{TOKEN}/sendPhoto")
        self.assertEqual(kwargs["data"], {"chat_id": "42", "caption": "caption"})
        filename, content, mime = kwargs["files"]["photo"]
        self.assertEqual((filename, content, mime), ("preview.png", b"png-bytes", "image/png"))

    def test_send_document_multipart(self):
        patcher, fake = self._fake()
        with patcher:
            self.client.send_document(
                chat_id=42,
                content=b"png-bytes",
                filename="sticker-1-hello.png",
                mime_type="image/png",
                caption="Стикер 1/9",
            )
        args, kwargs = fake.post.call_args
        self.assertEqual(args[0], f"{DEFAULT_API_ORIGIN}/bot{TOKEN}/sendDocument")
        self.assertEqual(kwargs["data"], {"chat_id": "42", "caption": "Стикер 1/9"})
        filename, content, mime = kwargs["files"]["document"]
        self.assertEqual((filename, content, mime), ("sticker-1-hello.png", b"png-bytes", "image/png"))

    def test_send_invoice_stars_xtr_no_provider_token(self):
        patcher, fake = self._fake()
        with patcher:
            self.client.send_invoice(
                chat_id=42,
                title="Sticker Pack",
                description="desc",
                payload="payment:1",
                amount_stars=199,
            )
        body = fake.post.call_args.kwargs["json"]
        self.assertEqual(body["currency"], "XTR")
        self.assertEqual(body["prices"], [{"label": "Sticker Pack", "amount": 199}])
        self.assertNotIn("provider_token", body)

    def test_answer_pre_checkout_query(self):
        patcher, fake = self._fake()
        with patcher:
            self.client.answer_pre_checkout_query(pre_checkout_query_id="pcq-1", ok=True)
        args, kwargs = fake.post.call_args
        self.assertEqual(args[0], f"{DEFAULT_API_ORIGIN}/bot{TOKEN}/answerPreCheckoutQuery")
        self.assertEqual(kwargs["json"], {"pre_checkout_query_id": "pcq-1", "ok": True})

    def test_answer_callback_query(self):
        patcher, fake = self._fake()
        with patcher:
            self.client.answer_callback_query(callback_query_id="cb-1")
        args, kwargs = fake.post.call_args
        self.assertEqual(args[0], f"{DEFAULT_API_ORIGIN}/bot{TOKEN}/answerCallbackQuery")
        self.assertEqual(kwargs["json"], {"callback_query_id": "cb-1"})


class TelegramClientErrorTests(SimpleTestCase):
    def setUp(self):
        self.client = TelegramBotClient(TOKEN)

    def test_network_timeout(self):
        patcher, _ = _fake_http(side_effect=httpx.TimeoutException("slow"))
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                self.client.send_message(chat_id=1, text="x")
        self.assertEqual(ctx.exception.status_code, 0)

    def test_http_error(self):
        patcher, _ = _fake_http(response=FakeResponse(status_code=500, payload=None))
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                self.client.send_message(chat_id=1, text="x")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_telegram_ok_false(self):
        patcher, _ = _fake_http(response=FakeResponse(payload={"ok": False, "description": "Bad Request: chat not found"}))
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                self.client.send_message(chat_id=1, text="x")
        self.assertEqual(ctx.exception.description, "Bad Request: chat not found")

    def test_token_never_leaks_into_errors(self):
        patcher, _ = _fake_http(side_effect=httpx.ConnectError(f"boom {DEFAULT_API_ORIGIN}/bot{TOKEN}/sendMessage"))
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                self.client.send_message(chat_id=1, text="x")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn("bot123456", str(ctx.exception))

    def test_token_not_leaked_on_http_error(self):
        patcher, _ = _fake_http(response=FakeResponse(status_code=401, payload=None))
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                self.client.send_message(chat_id=1, text="x")
        self.assertNotIn(TOKEN, str(ctx.exception))

    def _assert_token_free_traceback(self, ctx):
        import traceback

        exc = ctx.exception
        # chain suppressed: no "During handling of the above" with the
        # httpx error (which embeds the token URL)
        self.assertTrue(exc.__suppress_context__)
        self.assertIsNone(exc.__cause__)
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.assertNotIn(TOKEN, formatted)
        self.assertNotIn(f"bot{TOKEN}", formatted)

    def test_token_absent_from_traceback_chain_post(self):
        patcher, _ = _fake_http(side_effect=httpx.ConnectError(f"boom {DEFAULT_API_ORIGIN}/bot{TOKEN}/sendMessage"))
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                self.client.send_message(chat_id=1, text="x")
        self._assert_token_free_traceback(ctx)

    def test_token_absent_from_traceback_chain_multipart(self):
        patcher, _ = _fake_http(side_effect=httpx.ConnectError(f"boom {DEFAULT_API_ORIGIN}/bot{TOKEN}/sendPhoto"))
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                self.client.send_photo(
                    chat_id=1, content=b"x", filename="a.png", mime_type="image/png"
                )
        self._assert_token_free_traceback(ctx)

    def test_token_absent_from_traceback_chain_download(self):
        patcher, _ = _fake_http(side_effect=httpx.ConnectError(f"boom {DEFAULT_API_ORIGIN}/file/bot{TOKEN}/photos/a.jpg"))
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                self.client.download_file("photos/a.jpg")
        self._assert_token_free_traceback(ctx)


class TelegramClientProxyPoolTests(SimpleTestCase):
    """Pool integration: failover on transport errors, no rotation on
    upstream Telegram answers, legacy TELEGRAM_PROXY_URL override intact."""

    PROXY_A = "http://user-a:secret-a@proxy-a.example:3128"
    PROXY_B = "http://user-b:secret-b@proxy-b.example:3128"

    def _pool(self):
        import json

        from apps.core.outbound_proxy import ProxyPool, parse_proxy_urls

        return ProxyPool(parse_proxy_urls(json.dumps([self.PROXY_A, self.PROXY_B])), cooldown_seconds=60.0)

    def _clients(self, behavior_by_url):
        """Patch _shared_client with per-proxy fake clients."""
        fakes = {}
        for url, behavior in behavior_by_url.items():
            fake = mock.MagicMock(name=f"client:{url}")
            for verb in ("post", "get"):
                method_mock = getattr(fake, verb)
                if isinstance(behavior, Exception):
                    method_mock.side_effect = behavior
                else:
                    method_mock.return_value = behavior
            fakes[url] = fake
        patcher = mock.patch(
            "apps.telegram_bot.client._shared_client",
            side_effect=lambda proxy_url, timeout: fakes[proxy_url],
        )
        return patcher, fakes

    def test_healthy_proxy_selected(self):
        pool = self._pool()
        ok = FakeResponse(payload={"ok": True, "result": {"done": True}})
        patcher, fakes = self._clients({self.PROXY_A: ok, self.PROXY_B: ok})
        client = TelegramBotClient(TOKEN, proxy_pool=pool)
        with patcher:
            client.send_message(chat_id=1, text="hi")
        self.assertEqual(fakes[self.PROXY_A].post.call_count, 1)
        self.assertEqual(fakes[self.PROXY_B].post.call_count, 0)
        from apps.core.outbound_proxy import Service

        self.assertEqual(pool.state(pool.endpoints[0], Service.TELEGRAM), "HEALTHY")

    def test_first_proxy_transport_fail_fails_over_to_second(self):
        pool = self._pool()
        ok = FakeResponse(payload={"ok": True, "result": {"done": True}})
        patcher, fakes = self._clients(
            {self.PROXY_A: httpx.ConnectError("refused"), self.PROXY_B: ok}
        )
        client = TelegramBotClient(TOKEN, proxy_pool=pool)
        with patcher:
            result = client.send_message(chat_id=1, text="hi")
        self.assertEqual(result, {"done": True})
        self.assertEqual(fakes[self.PROXY_A].post.call_count, 1)
        self.assertEqual(fakes[self.PROXY_B].post.call_count, 1)
        from apps.core.outbound_proxy import Service

        self.assertEqual(pool.state(pool.endpoints[0], Service.TELEGRAM), "COOLDOWN")
        self.assertEqual(pool.state(pool.endpoints[1], Service.TELEGRAM), "HEALTHY")

    def test_upstream_400_does_not_rotate(self):
        pool = self._pool()
        bad = FakeResponse(status_code=400, payload=None)
        ok = FakeResponse(payload={"ok": True, "result": {}})
        patcher, fakes = self._clients({self.PROXY_A: bad, self.PROXY_B: ok})
        client = TelegramBotClient(TOKEN, proxy_pool=pool)
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                client.send_message(chat_id=1, text="hi")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(fakes[self.PROXY_A].post.call_count, 1)
        self.assertEqual(fakes[self.PROXY_B].post.call_count, 0)
        from apps.core.outbound_proxy import Service

        self.assertEqual(pool.state(pool.endpoints[0], Service.TELEGRAM), "HEALTHY")

    def test_upstream_401_does_not_rotate(self):
        pool = self._pool()
        unauthorized = FakeResponse(status_code=401, payload=None)
        patcher, fakes = self._clients({self.PROXY_A: unauthorized, self.PROXY_B: unauthorized})
        client = TelegramBotClient(TOKEN, proxy_pool=pool)
        with patcher:
            with self.assertRaises(TelegramAPIError):
                client.get_me()
        self.assertEqual(fakes[self.PROXY_A].post.call_count, 1)
        self.assertEqual(fakes[self.PROXY_B].post.call_count, 0)

    def test_all_proxies_down(self):
        pool = self._pool()
        patcher, _ = self._clients(
            {
                self.PROXY_A: httpx.ConnectError("refused"),
                self.PROXY_B: httpx.ConnectTimeout("timeout"),
            }
        )
        client = TelegramBotClient(TOKEN, proxy_pool=pool)
        with patcher:
            with self.assertRaises(TelegramAPIError) as ctx:
                client.send_message(chat_id=1, text="hi")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn("secret-a", str(ctx.exception))
        self.assertNotIn("secret-b", str(ctx.exception))

    def test_download_file_failover(self):
        pool = self._pool()
        patcher, fakes = self._clients(
            {
                self.PROXY_A: httpx.ConnectError("refused"),
                self.PROXY_B: FakeResponse(content=b"img"),
            }
        )
        client = TelegramBotClient(TOKEN, proxy_pool=pool)
        with patcher:
            content = client.download_file("photos/a.jpg")
        self.assertEqual(content, b"img")
        self.assertEqual(fakes[self.PROXY_B].get.call_count, 1)

    def test_legacy_proxy_url_overrides_pool(self):
        pool = self._pool()
        ok = FakeResponse(payload={"ok": True, "result": {}})
        legacy = "http://legacy:secret-l@legacy-proxy.example:3128"
        patcher, fakes = self._clients({legacy: ok})
        client = TelegramBotClient(TOKEN, proxy_url=legacy, proxy_pool=pool)
        self.assertIsNone(client._pool)
        with patcher:
            client.send_message(chat_id=1, text="hi")
        self.assertEqual(fakes[legacy].post.call_count, 1)

    def test_pool_logs_no_credentials(self):
        pool = self._pool()
        ok = FakeResponse(payload={"ok": True, "result": {}})
        patcher, _ = self._clients(
            {self.PROXY_A: httpx.ConnectError("refused"), self.PROXY_B: ok}
        )
        client = TelegramBotClient(TOKEN, proxy_pool=pool)
        with patcher:
            with self.assertLogs("apps.telegram_bot.client", level="WARNING") as captured:
                client.send_message(chat_id=1, text="hi")
        output = "\n".join(captured.output)
        for leaked in (TOKEN, "secret-a", "secret-b", "user-a", "user-b"):
            self.assertNotIn(leaked, output)
