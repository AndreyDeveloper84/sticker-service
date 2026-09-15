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
