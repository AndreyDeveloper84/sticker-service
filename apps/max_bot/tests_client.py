from unittest import mock

import httpx
from django.test import SimpleTestCase, override_settings

from apps.max_bot.client import (
    DEFAULT_API_BASE,
    MaxAPIError,
    MaxBotClient,
    created_message_id,
    uploaded_image_token,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class MaxBotClientTests(SimpleTestCase):
    def setUp(self):
        self.client = MaxBotClient("raw-max-token")

    def _fake_http(self, response=None, side_effect=None):
        """Return (patcher, fake_client) patching the shared httpx client."""
        fake = mock.MagicMock(name="shared_httpx_client")
        if side_effect is not None:
            fake.request.side_effect = side_effect
        else:
            fake.request.return_value = response or FakeResponse(payload={"ok": True})
        return mock.patch("apps.max_bot.client._shared_client", return_value=fake), fake

    def test_default_api_base_is_botapi(self):
        self.assertEqual(self.client.base_url, DEFAULT_API_BASE)
        self.assertEqual(DEFAULT_API_BASE, "https://botapi.max.ru")

    @override_settings(MAX_API_BASE="https://max-staging.test")
    def test_api_base_override_via_settings(self):
        client = MaxBotClient("raw-max-token")
        self.assertEqual(client.base_url, "https://max-staging.test")

    def test_send_message_with_user_id(self):
        patcher, fake = self._fake_http()
        with patcher:
            self.client.send_message(user_id="123", text="hello")
        args, kwargs = fake.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], f"{DEFAULT_API_BASE}/messages")
        self.assertEqual(kwargs["params"], {"user_id": "123"})
        self.assertEqual(kwargs["json"], {"text": "hello"})

    def test_send_message_with_chat_id(self):
        patcher, fake = self._fake_http()
        with patcher:
            self.client.send_message(chat_id="456", text="hello")
        _, kwargs = fake.request.call_args
        self.assertEqual(kwargs["params"], {"chat_id": "456"})

    def test_send_message_rejects_both_addresses(self):
        with self.assertRaises(ValueError):
            self.client.send_message(chat_id="1", user_id="2", text="x")

    def test_send_message_rejects_missing_address(self):
        with self.assertRaises(ValueError):
            self.client.send_message(text="x")

    def test_authorization_is_raw_token_not_bearer(self):
        patcher, fake = self._fake_http()
        with patcher:
            self.client.send_message(user_id="123", text="hello")
        headers = fake.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "raw-max-token")
        self.assertNotIn("Bearer", headers["Authorization"])

    def test_buttons_become_inline_keyboard_attachment(self):
        patcher, fake = self._fake_http()
        with patcher:
            self.client.send_message(
                user_id="1",
                text="pick",
                buttons=[[{"text": "A", "payload": "a"}], [{"text": "B", "url": "https://x.test"}]],
            )
        attachments = fake.request.call_args.kwargs["json"]["attachments"]
        self.assertEqual(attachments[0]["type"], "inline_keyboard")
        rows = attachments[0]["payload"]["buttons"]
        self.assertEqual(rows[0], [{"type": "callback", "text": "A", "payload": "a"}])
        self.assertEqual(rows[1], [{"type": "link", "text": "B", "url": "https://x.test"}])

    def test_non_2xx_raises_max_api_error(self):
        patcher, _ = self._fake_http(response=FakeResponse(status_code=403, text="forbidden"))
        with patcher:
            with self.assertRaises(MaxAPIError) as ctx:
                self.client.send_message(user_id="1", text="x")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_network_failure_raises_max_api_error(self):
        patcher, _ = self._fake_http(side_effect=httpx.ConnectError("boom"))
        with patcher:
            with self.assertRaises(MaxAPIError) as ctx:
                self.client.send_message(user_id="1", text="x")
        self.assertEqual(ctx.exception.status_code, 0)

    def test_timeout_raises_max_api_error(self):
        patcher, _ = self._fake_http(side_effect=httpx.TimeoutException("slow"))
        with patcher:
            with self.assertRaises(MaxAPIError) as ctx:
                self.client.send_message(user_id="1", text="x")
        self.assertEqual(ctx.exception.status_code, 0)

    def test_non_json_2xx_returns_empty_dict(self):
        patcher, _ = self._fake_http(response=FakeResponse(status_code=200, payload=None, text="OK"))
        with patcher:
            result = self.client.send_message(user_id="1", text="x")
        self.assertEqual(result, {})

    def test_answer_callback_posts_to_answers_with_callback_id(self):
        patcher, fake = self._fake_http()
        with patcher:
            self.client.answer_callback(callback_id="cb-1")
        args, kwargs = fake.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], f"{DEFAULT_API_BASE}/answers")
        self.assertEqual(kwargs["params"], {"callback_id": "cb-1"})
        # current MAX contract rejects an empty body — notification is required
        self.assertEqual(kwargs["json"], {"notification": ""})


class CreatedMessageIdTests(SimpleTestCase):
    """``POST /messages`` answers with a Message envelope; the id is
    ``message.body.mid`` (staging Order 10 evidence: the legacy probes
    body.mid / message.mid / mid all missed it and recorded "")."""

    def test_real_envelope_message_body_mid(self):
        envelope = {
            "message": {
                "sender": {"user_id": 1, "name": "bot"},
                "recipient": {"chat_id": 2, "chat_type": "dialog"},
                "timestamp": 1758000000000,
                "body": {"mid": "mid.abc123", "seq": 4, "text": "Оплата получена."},
            }
        }
        self.assertEqual(created_message_id(envelope), "mid.abc123")

    def test_seq_is_the_fallback_inside_the_real_envelope(self):
        self.assertEqual(created_message_id({"message": {"body": {"seq": 42}}}), "42")

    def test_legacy_shapes_stay_accepted(self):
        self.assertEqual(created_message_id({"body": {"mid": "a"}}), "a")
        self.assertEqual(created_message_id({"message": {"mid": "b"}}), "b")
        self.assertEqual(created_message_id({"mid": "c"}), "c")

    def test_empty_or_foreign_payloads_give_empty_string(self):
        for payload in ({}, None, [], "x", {"message": None}, {"message": {"body": {}}}, {"body": None}):
            self.assertEqual(created_message_id(payload), "", payload)


class ImageUploadTokenTests(SimpleTestCase):
    """Live blocker (Order 10, step 8): the upload URL answers
    ``{"photos": {"<opaque key>": {"token": "<str>"}}}``; the previous
    extractor only looked at top-level / retval / init / query tokens and
    raised "MAX image upload returned no token" before send_message."""

    UPLOAD_URL = "https://upload.max.test/api/upload?sig=abc"
    ENVELOPE = {"message": {"body": {"mid": "mid.sent-1", "seq": 3}}}

    def _fake_http(self, *, uploaded, init=None):
        fake = mock.MagicMock(name="shared_httpx_client")
        # /uploads (init) and /messages (send) go through .request; the
        # multipart upload to the returned URL goes through .post.
        fake.request.side_effect = [
            FakeResponse(payload=init or {"url": self.UPLOAD_URL}),
            FakeResponse(payload=self.ENVELOPE),
        ]
        fake.post.return_value = FakeResponse(payload=uploaded)
        return mock.patch("apps.max_bot.client._shared_client", return_value=fake), fake

    def test_uploaded_image_token_reads_first_photos_entry(self):
        self.assertEqual(uploaded_image_token({"photos": {"k1": {"token": "tok-1"}}}), "tok-1")
        self.assertEqual(uploaded_image_token({"photos": {"a": {}, "b": {"token": "tok-b"}}}), "tok-b")
        for payload in ({}, None, {"photos": None}, {"photos": {}}, {"photos": {"k": {"token": ""}}}, {"token": "x"}):
            self.assertEqual(uploaded_image_token(payload), "", payload)

    def test_send_image_uses_photos_token_and_documented_attachment_form(self):
        patcher, fake = self._fake_http(uploaded={"photos": {"3fa9c1": {"token": "tok.real"}}})
        with patcher:
            result = MaxBotClient("raw-max-token").send_image(
                user_id="200", content=b"png", filename="preview.png", mime_type="image/png", caption="Превью"
            )

        self.assertEqual(created_message_id(result), "mid.sent-1")
        # multipart went to the upload URL from /uploads
        upload_call = fake.post.call_args
        self.assertEqual(upload_call.args[0], self.UPLOAD_URL)
        self.assertIn("multipart/form-data", upload_call.kwargs["headers"]["Content-Type"])
        self.assertIn(b'name="data"; filename="preview.png"', upload_call.kwargs["content"])
        # send: recipient in the query, attachment payload carries the token
        init_call, send_call = fake.request.call_args_list
        self.assertEqual(init_call.args[:2], ("POST", f"{DEFAULT_API_BASE}/uploads"))
        self.assertEqual(init_call.kwargs["params"], {"type": "image"})
        self.assertEqual(send_call.args[:2], ("POST", f"{DEFAULT_API_BASE}/messages"))
        self.assertEqual(send_call.kwargs["params"], {"user_id": "200"})
        body = send_call.kwargs["json"]
        self.assertEqual(body["text"], "Превью")
        self.assertEqual(body["attachments"], [{"type": "image", "payload": {"token": "tok.real"}}])

    def test_legacy_token_shapes_still_work(self):
        for uploaded in ({"token": "legacy-top"}, {"retval": {"token": "legacy-retval"}}):
            patcher, fake = self._fake_http(uploaded=uploaded)
            with patcher:
                MaxBotClient("raw-max-token").send_image(
                    user_id="200", content=b"png", filename="p.png", mime_type="image/png"
                )
            token = fake.request.call_args_list[1].kwargs["json"]["attachments"][0]["payload"]["token"]
            self.assertEqual(token, next(iter(uploaded.values())) if "token" in uploaded else "legacy-retval")

    def test_upload_without_any_token_still_fails_closed_before_send(self):
        patcher, fake = self._fake_http(uploaded={"photos": {"k": {"size": 10}}})
        with patcher:
            with self.assertRaisesMessage(MaxAPIError, "returned no token"):
                MaxBotClient("raw-max-token").send_image(
                    user_id="200", content=b"png", filename="p.png", mime_type="image/png"
                )
        self.assertEqual(len(fake.request.call_args_list), 1)  # only /uploads, no /messages

