"""DRF-2163 Final Sticker UX.

Telegram: after a complete FinalDelivery the bot creates / extends the
customer's sticker set (createNewStickerSet first, addStickerToSet later),
PNG ≤ 512 KB (re-encoded copy for the set only), emoji per slot, and sends
the t.me/addstickers link — at-most-once per slot and per link; Bot API
errors are stored and the console offers «Создать набор повторно».
MAX: the delivery summary carries the «Стикеры в MAX» instruction.
"""

import io
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.core.final_delivery_console import FinalDeliveryOrderAdmin
from apps.core.models import ChannelIdentity, FinalDelivery, Order
from apps.core.services.final_delivery import SUMMARY_TEXT, FinalDeliveryService
from apps.core.tests_final_delivery import FakeAdapter, FinalDeliveryTestCase
from apps.max_bot.final_delivery import MAX_STICKERS_INSTRUCTION, MAX_SUMMARY_TEXT, MaxFinalDeliveryAdapter
from apps.telegram_bot.client import DEFAULT_API_ORIGIN, TelegramAPIError, TelegramBotClient
from apps.telegram_bot.sticker_set import (
    LINK_TEXT,
    STICKER_MAX_BYTES,
    StickerSetError,
    TelegramStickerSetService,
    set_name,
    sticker_emoji,
    sticker_png,
    sticker_set_record,
)
from apps.telegram_bot.tests_client import _fake_http


def fake_client(*, set_exists=False):
    """Bot API double: getMe, getStickerSet (400 when absent), uploads, set
    creation / extension, the link message."""
    client = mock.Mock(spec=TelegramBotClient)
    client.get_me.return_value = {"username": "sticker_pilot_bot"}
    if set_exists:
        client.get_sticker_set.return_value = {"name": "x", "stickers": []}
    else:
        client.get_sticker_set.side_effect = TelegramAPIError("getStickerSet", status_code=400, description="Bad Request: STICKERSET_INVALID")
    client.upload_sticker_file.side_effect = lambda **kw: {"file_id": f"file-{kw['filename']}"}
    client.create_new_sticker_set.return_value = True
    client.add_sticker_to_set.return_value = True
    client.send_message.return_value = {"message_id": 501}
    return client


def png_bytes(size=(512, 512), noisy=False):
    image = Image.new("RGBA", size, (255, 0, 0, 0))
    if noisy:
        import random

        random.seed(1)
        pixels = image.load()
        for x in range(size[0]):
            for y in range(size[1]):
                pixels[x, y] = (random.randrange(256), random.randrange(256), random.randrange(256), 255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class StickerHelpersTests(SimpleTestCase):
    def test_set_name_is_prefix_user_by_bot(self):
        identity = ChannelIdentity(channel="telegram", external_user_id="1001")
        self.assertEqual(set_name(identity, "sticker_pilot_bot"), "sticks_1001_by_sticker_pilot_bot")
        with override_settings(TELEGRAM_STICKER_SET_PREFIX="My-Pack"):
            self.assertEqual(set_name(identity, "b"), "mypack_1001_by_b")

    def test_emoji_from_slot_and_neutral_for_custom(self):
        from apps.core.models import Product

        order = Order(product=Product(code="p", name="p", config={"emotion_count": 1}))
        self.assertEqual(sticker_emoji(order, "hello"), "👋")
        self.assertEqual(sticker_emoji(order, "unknown-code"), "🙂")
        custom = Order(product=Product(code="c", name="c", config={"requires_custom_phrases": True}))
        self.assertEqual(sticker_emoji(custom, "custom-1"), "🙂")

    def test_sticker_png_keeps_small_files_and_shrinks_big_ones(self):
        small = png_bytes()
        self.assertIs(sticker_png(small), small)
        big = png_bytes(noisy=True)  # random RGBA noise: ~1 MB as PNG
        self.assertGreater(len(big), STICKER_MAX_BYTES)
        shrunk = sticker_png(big)
        self.assertLessEqual(len(shrunk), STICKER_MAX_BYTES)
        self.assertEqual(Image.open(io.BytesIO(shrunk)).size, (512, 512))
        with self.assertRaises(StickerSetError):
            sticker_png(png_bytes(size=(2048, 2048), noisy=True))  # too much even after quantizing

    def test_client_methods_hit_the_bot_api(self):
        token = "1:abc"
        patcher, fake = _fake_http()
        with patcher:
            client = TelegramBotClient(token)
            client.get_sticker_set(name="s_by_b")
            client.create_new_sticker_set(user_id="1", name="s_by_b", title="T", stickers=[{"sticker": "f", "format": "static", "emoji_list": ["👋"]}])
            client.add_sticker_to_set(user_id="1", name="s_by_b", sticker={"sticker": "g", "format": "static", "emoji_list": ["🙂"]})
            client.upload_sticker_file(user_id="1", content=b"png", filename="sticker-hello.png")
        calls = fake.post.call_args_list
        self.assertEqual(calls[0].args[0], f"{DEFAULT_API_ORIGIN}/bot{token}/getStickerSet")
        self.assertEqual(calls[1].kwargs["json"]["stickers"][0]["emoji_list"], ["👋"])
        self.assertEqual(calls[2].kwargs["json"], {"user_id": "1", "name": "s_by_b", "sticker": {"sticker": "g", "format": "static", "emoji_list": ["🙂"]}})
        self.assertEqual(calls[3].args[0], f"{DEFAULT_API_ORIGIN}/bot{token}/uploadStickerFile")
        self.assertEqual(calls[3].kwargs["data"], {"user_id": "1", "sticker_format": "static"})
        self.assertEqual(calls[3].kwargs["files"]["sticker"], ("sticker-hello.png", b"png", "image/png"))


class DeliveryFixture(TestCase):
    """The FinalDeliveryTestCase helpers without inheriting its tests."""

    setUp = FinalDeliveryTestCase.setUp
    make_order = FinalDeliveryTestCase.make_order
    add_final = FinalDeliveryTestCase.add_final
    pass_qc = staticmethod(FinalDeliveryTestCase.pass_qc)
    service = FinalDeliveryTestCase.service

    def ready_order(self, quantity=1):
        """A QC-passed order whose final assets are real 512×512 PNGs."""
        order = self.make_order(quantity=quantity)
        assets = {}
        for attempt, slot in enumerate(order.selection["emotions"], start=1):
            _job, assets[slot] = self.add_final(order, slot, attempt=attempt, content=png_bytes())
        self.pass_qc(order)
        return order, assets


class StickerSetServiceTests(DeliveryFixture):
    def delivered(self, quantity=3):
        order, _assets = self.ready_order(quantity=quantity)
        plan = self.service(FakeAdapter()).deliver(order=order)
        self.assertTrue(plan.complete)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)
        return order

    def test_first_order_creates_the_set_and_sends_the_link(self):
        order = self.delivered()
        client = fake_client()
        record = TelegramStickerSetService(client=client, storage=self.storage).ensure(order, actor_ref="op")
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["set_name"], "sticks_1_by_sticker_pilot_bot")  # "tg-user-1" → digits only → 1
        self.assertEqual(record["link"], "https://t.me/addstickers/sticks_1_by_sticker_pilot_bot")
        self.assertEqual(record["stickers"], {"hello": "file-sticker-hello.png", "bye": "file-sticker-bye.png", "thanks": "file-sticker-thanks.png"})
        self.assertEqual(record["link_message_id"], "501")
        client.create_new_sticker_set.assert_called_once()
        created = client.create_new_sticker_set.call_args.kwargs
        self.assertEqual(created["title"], "Product")
        self.assertEqual(created["stickers"], [{"sticker": "file-sticker-hello.png", "format": "static", "emoji_list": ["👋"]}])
        self.assertEqual(client.add_sticker_to_set.call_count, 2)
        self.assertEqual([c.kwargs["sticker"]["emoji_list"] for c in client.add_sticker_to_set.call_args_list], [["🙋"], ["🙏"]])
        client.send_message.assert_called_once_with(chat_id="tg-user-1", text=LINK_TEXT.format(name="sticks_1_by_sticker_pilot_bot"))
        run = order.final_deliveries.order_by("-attempt").first()
        self.assertEqual(run.summary["status"], "sent", "the delivery summary record is kept")
        self.assertEqual(run.summary["sticker_set"]["actor_ref"], "op")

        # at-most-once: a second ensure adds nothing and sends no second link
        client.reset_mock()
        again = TelegramStickerSetService(client=client, storage=self.storage).ensure(order)
        self.assertEqual(again["stickers"], record["stickers"])
        client.upload_sticker_file.assert_not_called()
        client.add_sticker_to_set.assert_not_called()
        client.send_message.assert_not_called()

    def test_second_order_of_the_customer_extends_the_existing_set(self):
        order = self.delivered(quantity=1)
        client = fake_client(set_exists=True)
        record = TelegramStickerSetService(client=client, storage=self.storage).ensure(order)
        self.assertEqual(record["status"], "done")
        client.create_new_sticker_set.assert_not_called()
        client.add_sticker_to_set.assert_called_once()

    def test_api_error_is_stored_and_a_retry_adds_only_the_missing_slots(self):
        order = self.delivered()
        client = fake_client()
        client.add_sticker_to_set.side_effect = [True, TelegramAPIError("addStickerToSet", status_code=400, description="Bad Request: STICKERS_TOO_MUCH")]
        record = TelegramStickerSetService(client=client, storage=self.storage).ensure(order)
        self.assertEqual(record["status"], "failed")
        self.assertIn("STICKERS_TOO_MUCH", record["error"])
        self.assertEqual(sorted(record["stickers"]), ["bye", "hello"])  # hello created the set, bye added, thanks failed
        client.send_message.assert_not_called()  # no link until the set is complete
        self.assertEqual(sticker_set_record(order)["status"], "failed")

        client.add_sticker_to_set.side_effect = None
        client.add_sticker_to_set.return_value = True
        client.reset_mock()
        record = TelegramStickerSetService(client=client, storage=self.storage).ensure(order)
        self.assertEqual(record["status"], "done")
        client.create_new_sticker_set.assert_not_called()
        client.get_sticker_set.assert_not_called()  # the record already knows the set exists
        self.assertEqual(client.upload_sticker_file.call_count, 1)
        self.assertEqual(client.add_sticker_to_set.call_args.kwargs["sticker"]["sticker"], "file-sticker-thanks.png")
        client.send_message.assert_called_once()

    def test_bot_username_from_settings_skips_get_me(self):
        order = self.delivered(quantity=1)
        client = fake_client()
        with override_settings(TELEGRAM_BOT_USERNAME="@pilot_bot"):
            record = TelegramStickerSetService(client=client, storage=self.storage).ensure(order)
        self.assertEqual(record["set_name"], "sticks_1_by_pilot_bot")
        client.get_me.assert_not_called()

    def test_only_delivered_telegram_orders(self):
        order, _assets = self.ready_order(quantity=1)
        with self.assertRaisesMessage(StickerSetError, "after the delivery is complete"):
            TelegramStickerSetService(client=fake_client(), storage=self.storage).ensure(order)
        max_identity = ChannelIdentity.objects.create(user=self.user, channel=ChannelIdentity.Channel.MAX, external_user_id="9")
        max_order, _assets = self.ready_order(quantity=1)
        max_order.channel_identity = max_identity
        max_order.status = Order.Status.DELIVERED
        max_order.save(update_fields=["channel_identity", "status"])
        with self.assertRaisesMessage(StickerSetError, "Telegram orders only"):
            TelegramStickerSetService(client=fake_client(), storage=self.storage).ensure(max_order)


@override_settings(TELEGRAM_BOT_TOKEN="1:console")
class StickerSetConsoleTests(DeliveryFixture):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.client.force_login(User.objects.create_superuser(username="op", email="op@example.com", password="pass"))
        self.order, _assets = self.ready_order(quantity=1)
        self.bot = fake_client()
        self.delivery = FinalDeliveryService(adapter=FakeAdapter(), storage=self.storage)

    def _patched(self):
        return (
            mock.patch.object(FinalDeliveryOrderAdmin, "get_final_delivery_service", return_value=self.delivery),
            mock.patch.object(FinalDeliveryOrderAdmin, "get_sticker_set_service",
                              return_value=TelegramStickerSetService(client=self.bot, storage=self.storage)),
        )

    def test_completed_delivery_creates_the_set_and_the_card_shows_the_link(self):
        p1, p2 = self._patched()
        with p1, p2:
            response = self.client.post(reverse("admin:core_order_deliver_final", args=[self.order.pk]), follow=True)
        self.assertContains(response, "Набор стикеров Telegram готов: https://t.me/addstickers/sticks_1_by_sticker_pilot_bot")
        self.assertContains(response, 'href="https://t.me/addstickers/sticks_1_by_sticker_pilot_bot"')
        self.assertContains(response, "1 стикеров · ссылка клиенту: отправлена")
        self.assertNotContains(response, "Создать набор повторно")
        self.bot.create_new_sticker_set.assert_called_once()

    def test_api_failure_shows_the_error_and_the_retry_button_which_finishes_the_set(self):
        self.bot.create_new_sticker_set.side_effect = TelegramAPIError("createNewStickerSet", status_code=400, description="Bad Request: PEER_ID_INVALID")
        p1, p2 = self._patched()
        with p1, p2:
            response = self.client.post(reverse("admin:core_order_deliver_final", args=[self.order.pk]), follow=True)
        self.assertContains(response, "Набор стикеров Telegram не создан: Bad Request: PEER_ID_INVALID")
        self.assertContains(response, "Создать набор повторно")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED, "the delivery itself is untouched")
        self.assertEqual(self.delivery.delivery_plan(self.order).summary_status, "sent")

        retry_url = reverse("admin:core_order_create_sticker_set", args=[self.order.pk])
        self.bot.create_new_sticker_set.side_effect = None
        with p1, p2:
            page = self.client.get(retry_url)
            self.assertContains(page, "Создать набор повторно")
            response = self.client.post(retry_url, follow=True)
        self.assertContains(response, "Набор стикеров Telegram готов")
        self.assertContains(response, "ссылка клиенту: отправлена")
        self.assertEqual(self.bot.upload_sticker_file.call_count, 2)  # the failed upload is redone, nothing was added before
        self.bot.send_message.assert_called_once()

    def test_retry_is_refused_before_delivery_and_for_max(self):
        p1, p2 = self._patched()
        with p1, p2:
            response = self.client.post(reverse("admin:core_order_create_sticker_set", args=[self.order.pk]), follow=True)
        self.assertContains(response, "Набор создаётся после полной доставки заказа в Telegram.")
        self.bot.upload_sticker_file.assert_not_called()

    def test_missing_bot_token_is_a_warning_not_a_crash(self):
        p1, _p2 = self._patched()
        with p1, override_settings(TELEGRAM_BOT_TOKEN=""), mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": ""}):
            response = self.client.post(reverse("admin:core_order_deliver_final", args=[self.order.pk]), follow=True)
        self.assertContains(response, "Набор стикеров Telegram не создан")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)


class MaxSummaryTextTests(DeliveryFixture):
    def test_max_summary_carries_the_stickers_instruction(self):
        self.assertTrue(MAX_SUMMARY_TEXT.startswith(SUMMARY_TEXT))
        for phrase in ("Файл прозрачный", "белый фон только в превью", "Сохраните файл", "«Стикеры в MAX»",
                       "«Создать набор»", "загрузите PNG", "нужен Цифровой ID"):
            self.assertIn(phrase, MAX_STICKERS_INSTRUCTION)
        self.assertEqual(MaxFinalDeliveryAdapter(client=mock.Mock()).summary_text, MAX_SUMMARY_TEXT)

    def test_max_delivery_sends_the_instruction_and_telegram_the_plain_summary(self):
        max_identity = ChannelIdentity.objects.create(user=self.user, channel=ChannelIdentity.Channel.MAX, external_user_id="9")
        order, _assets = self.ready_order(quantity=1)
        order.channel_identity = max_identity
        order.save(update_fields=["channel_identity"])

        class MaxFake(FakeAdapter):
            channel = ChannelIdentity.Channel.MAX
            summary_text = MAX_SUMMARY_TEXT

        adapter = MaxFake()
        self.service(adapter).deliver(order=order)
        self.assertEqual(adapter.summaries[0]["text"], MAX_SUMMARY_TEXT)

        telegram_order, _assets = self.ready_order(quantity=1)
        plain = FakeAdapter()
        self.service(plain).deliver(order=telegram_order)
        self.assertEqual(plain.summaries[0]["text"], SUMMARY_TEXT)
