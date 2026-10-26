"""Bot webhooks reject updates when their shared secret is not configured.

Before: an empty TELEGRAM_/MAX_WEBHOOK_SECRET skipped the check, so a deploy
that forgot the env var accepted forged updates from anyone.
"""
import json
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.webhook_auth import webhook_secret_ok

BOT_STARTED = {"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 2}}
TELEGRAM_START = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 1}, "from": {"id": 2}, "text": "/start"}}


class WebhookSecretOkTests(SimpleTestCase):
    @override_settings(WEBHOOK_ALLOW_UNSIGNED=False)
    def test_empty_secret_rejects_by_default(self):
        self.assertFalse(webhook_secret_ok("", None, channel="t"))
        self.assertFalse(webhook_secret_ok("", "anything", channel="t"))

    @override_settings(WEBHOOK_ALLOW_UNSIGNED=True)
    def test_empty_secret_accepts_only_when_opted_in(self):
        self.assertTrue(webhook_secret_ok("", None, channel="t"))

    @override_settings(WEBHOOK_ALLOW_UNSIGNED=True)
    def test_configured_secret_is_checked_even_when_opted_in(self):
        self.assertTrue(webhook_secret_ok("s3cret", "s3cret", channel="t"))
        self.assertFalse(webhook_secret_ok("s3cret", "wrong", channel="t"))
        self.assertFalse(webhook_secret_ok("s3cret", None, channel="t"))

    def test_production_settings_do_not_opt_in(self):
        from config.settings import base, production, staging

        self.assertFalse(base.WEBHOOK_ALLOW_UNSIGNED)
        self.assertFalse(production.WEBHOOK_ALLOW_UNSIGNED)
        self.assertFalse(staging.WEBHOOK_ALLOW_UNSIGNED)


@override_settings(WEBHOOK_ALLOW_UNSIGNED=False)
class WebhookViewsFailClosedTests(TestCase):
    def _post(self, url, payload, **headers):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json", **headers)

    def test_max_webhook_without_configured_secret_is_403(self):
        with mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": ""}), mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            response = self._post("/max/webhook/", BOT_STARTED)
        self.assertEqual(response.status_code, 403)
        client_cls.assert_not_called()

    @override_settings(TELEGRAM_WEBHOOK_SECRET="")
    def test_telegram_webhook_without_configured_secret_is_403(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            response = self._post("/telegram/webhook/", TELEGRAM_START)
        self.assertEqual(response.status_code, 403)
        client_cls.assert_not_called()

    @override_settings(TELEGRAM_WEBHOOK_SECRET="s3cret")
    def test_telegram_webhook_wrong_secret_is_403(self):
        response = self._post("/telegram/webhook/", TELEGRAM_START, HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="wrong")
        self.assertEqual(response.status_code, 403)

    @override_settings(TELEGRAM_WEBHOOK_SECRET="s3cret")
    def test_telegram_webhook_right_secret_passes_the_gate(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient"), mock.patch("apps.telegram_bot.views._handle_update"):
            response = self._post("/telegram/webhook/", TELEGRAM_START, HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="s3cret")
        self.assertEqual(response.status_code, 200)
