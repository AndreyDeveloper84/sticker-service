"""«Навигация бота без потерь» (owner GO 2026-09-20), both channels.

Owner finding (Telegram, Order 17, single/meme, one photo): the bot waited
for an emotion button in an old message, stayed silent on any other text
(OrderStepper.handle_text → False) and every old keyboard kept working.

1. re-prompt of the current step on any unexpected input — never silence;
2. «▶️ Продолжить заказ #N (шаг: …)» in the main menu, «🎨 Заказать
   стикеры» with an in-progress order → continue / choose another product;
3. stale buttons: the pressed message loses its keyboard (best-effort);
   a button of a step already behind → «Этот шаг уже пройден» + the
   current screen, the order untouched;
4. /status (Telegram) and «📍 Где я?» (both channels);
5. a photo sent «as a file» is a photo; the «Фото загружены» button is
   always on the screen after a photo.
"""

from django.test import TestCase, override_settings

from apps.core.bot_menu import (
    CONTACT_PROMPT,
    MAIN_MENU,
    NO_ORDER_STATUS,
    PHOTO_SAVED,
    PHOTOS_DONE_LABEL,
    PRODUCTS_TITLE,
    STALE_STEP,
    STEP_TITLES,
)
from apps.core.customer_hints import NEED_EMOTIONS_HINT, PHOTO_GUIDANCE
from apps.core.models import Order
from apps.core.tests_bot_menu_flow import CONTACT, MaxBot, TelegramBot


class NavigationScenarios:
    def step_prefix(self, step):
        return f"📍 Вы на шаге: {STEP_TITLES[step]}."

    def start_single(self, style="meme"):
        self.start()
        self.tap("menu:order")
        self.tap("product:single-sticker")
        self.tap(f"style:single-sticker:{style}")
        self.assertIn("Выберите эмоцию", self.last_text())

    def assert_photos_done_button(self):
        self.assertEqual(self.last_payloads()[0], "photos_done")
        buttons = self.last().get("buttons") or (self.last().get("reply_markup") or {}).get("inline_keyboard") or []
        self.assertEqual(buttons[0][0]["text"], PHOTOS_DONE_LABEL)

    # -- 1. re-prompt -----------------------------------------------------------

    def test_text_at_the_emotion_step_reprompts_the_emotions(self):
        """The Order 17 situation: an emotion is awaited, the customer writes."""
        self.start_single()
        sends = len(self.texts())
        self.say("а где кнопка?")
        self.assertEqual(len(self.texts()), sends + 1, "exactly one answer, never silence")
        self.assertTrue(self.last_text().startswith(self.step_prefix("emotions")), self.last_text())
        self.assertIn("Выберите эмоцию для стикера.", self.last_text())
        self.assertEqual(self.last_payloads()[:3], ["emotion:hello", "emotion:bye", "emotion:thanks"])
        self.assertEqual(self.order().selection.get("emotions"), [])

    def test_text_at_the_photo_step_reprompts_photos_then_photos_done(self):
        self.start_single()
        self.tap("emotion:bye")
        self.say("что дальше?")
        self.assertEqual(self.last_text(), f"{self.step_prefix('photos')}\n\n{PHOTO_GUIDANCE}")
        self.photo("a")
        self.say("ещё?")
        self.assertEqual(self.last_text(), f"{self.step_prefix('photos')}\n\n{PHOTO_SAVED}")
        self.assert_photos_done_button()

    def test_text_at_the_card_step_reprompts_the_card(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        self.tap("photos_done")
        self.say(CONTACT)
        self.assertIn("🧾 Ваш заказ", self.last_text())
        self.say("ок")
        self.assertTrue(self.last_text().startswith(self.step_prefix("card")))
        self.assertIn("🧾 Ваш заказ", self.last_text())
        self.assertEqual(self.last_payloads()[0], "order:confirm")
        self.assertEqual(self.order().selection.get("contact"), CONTACT, "the contact is not overwritten")

    def test_sticker_document_and_unknown_command_reprompt(self):
        self.start_single()
        self.tap("emotion:bye")
        for send in (self.sticker, lambda: self.say("/help")):
            send()
            self.assertEqual(self.last_text(), f"{self.step_prefix('photos')}\n\n{PHOTO_GUIDANCE}")
        self.assertEqual(self.order().photos.count(), 0)

    def test_text_without_an_order_shows_the_main_menu(self):
        self.start()
        self.say("привет")
        self.assertEqual(self.last_text(), MAIN_MENU)
        self.assertEqual(self.last_payloads()[0], "menu:order")
        self.assertIsNone(self.order())

    # -- 2. continue / restart ----------------------------------------------------

    def test_main_menu_offers_to_continue_the_order_at_its_step(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        order = self.order()
        self.tap("menu:main")
        self.assertEqual(self.last_text(), MAIN_MENU)
        self.assertEqual(self.last_payloads()[0], "order:continue")
        buttons = self.last().get("buttons") or (self.last().get("reply_markup") or {}).get("inline_keyboard")
        self.assertEqual(buttons[0][0]["text"], f"▶️ Продолжить заказ #{order.pk} (шаг: фото)")
        self.tap("order:continue")
        self.assertEqual(self.last_text(), PHOTO_SAVED)
        self.assert_photos_done_button()
        self.tap("photos_done")
        self.assertEqual(self.last_text(), CONTACT_PROMPT)
        self.tap("menu:main")  # the menu stops waiting for the contact → the photo step («Фото загружены») again
        self.assertIsNone(self.order().selection.get("awaiting_input"))
        buttons = self.last().get("buttons") or (self.last().get("reply_markup") or {}).get("inline_keyboard")
        self.assertEqual(buttons[0][0]["text"], f"▶️ Продолжить заказ #{order.pk} (шаг: фото)")
        self.tap("order:continue")
        self.assertEqual(self.last_text(), PHOTO_SAVED)
        self.assertEqual(Order.objects.count(), 1)

    def test_reprompt_at_the_contact_step_keeps_waiting_for_the_contact(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        self.tap("photos_done")
        self.assertEqual(self.order().selection.get("awaiting_input"), "contact")
        self.sticker()  # not an answer: the contact prompt again, still armed
        self.assertEqual(self.last_text(), f"{self.step_prefix('contact')}\n\n{CONTACT_PROMPT}")
        self.assertEqual(self.order().selection.get("awaiting_input"), "contact")
        self.say(CONTACT)
        self.assertIn("🧾 Ваш заказ", self.last_text())

    def test_order_button_with_an_order_in_progress_asks_continue_or_restart(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        order = self.order()
        self.tap("menu:order")
        self.assertIn(f"незавершённый заказ #{order.pk} (шаг: фото)", self.last_text())
        self.assertEqual(self.last_payloads(), ["order:continue", "order:restart", "menu:main"])
        self.tap("order:continue")
        self.assertEqual(self.last_text(), PHOTO_SAVED)
        self.tap("menu:order")
        self.tap("order:restart")
        self.assertEqual(self.last_text(), PRODUCTS_TITLE)
        self.tap("product:sticker-pack-9")
        self.tap("style:sticker-pack-9:3d")
        order.refresh_from_db()
        self.assertEqual(order.product.code, "sticker-pack-9", "the same order, another product")
        self.assertEqual(order.photos.count(), 1, "photos survive the restart")
        self.assertEqual(Order.objects.count(), 1)

    def test_order_button_without_an_order_lists_products(self):
        self.start()
        self.tap("menu:order")
        self.assertEqual(self.last_text(), PRODUCTS_TITLE)

    # -- 3. stale buttons ----------------------------------------------------------

    def test_pressed_message_loses_its_keyboard(self):
        self.start()
        self.assertEqual(self.keyboard_drops(), [])
        self.tap("menu:order")
        drops = self.keyboard_drops()
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["message_id"], self.pressed_message_id())
        if self.telegram:
            self.assertEqual(drops[0]["reply_markup"], {"inline_keyboard": []})
        else:
            self.assertEqual(drops[0]["attachments"], [])
        self.tap("order:confirm")  # channel payload: no drop by the stepper
        self.assertEqual(len(self.keyboard_drops()), 1)

    def test_keyboard_drop_failure_does_not_break_the_step(self):
        self.fail_keyboard_drop()
        self.start_single()
        self.tap("emotion:bye")
        self.assertEqual(self.last_text(), PHOTO_GUIDANCE)
        self.assertEqual(self.order().selection.get("emotions"), ["bye"])

    def test_old_emotion_button_after_the_step_is_stale(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        before = self.order().selection
        self.tap("emotion:hello")  # the keyboard of the first message, pressed again
        self.assertEqual(self.last_text(), f"{STALE_STEP}\n\n{PHOTO_SAVED}")
        self.assert_photos_done_button()
        self.assertEqual(self.order().selection, before, "the order is not changed")
        self.tap("emotions:confirm")
        self.assertTrue(self.last_text().startswith(STALE_STEP))
        self.assertEqual(self.order().selection, before)

    def test_continue_button_of_an_old_menu_after_payment_shows_the_menu(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        self.tap("photos_done")
        self.say(CONTACT)
        self.tap("order:confirm")
        self.tap("consent:accept")
        if self.telegram:
            self.tap("pay")
        self.assertEqual(self.order().status, Order.Status.AWAITING_PAYMENT)
        self.tap("order:continue")
        self.assertEqual(self.last_text(), MAIN_MENU)
        self.assertEqual(self.last_payloads()[0], "menu:order", "nothing to continue: no «Продолжить» row")
        self.assertEqual(self.order().status, Order.Status.AWAITING_PAYMENT)

    # -- 4. where am I -------------------------------------------------------------

    def test_where_am_i_describes_the_order_and_its_step(self):
        self.start()
        self.tap("menu:where")
        self.assertEqual(self.last_text(), NO_ORDER_STATUS)
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        self.photo("b")
        order = self.order()
        self.tap("menu:where")
        text = self.last_text()
        self.assertEqual(
            text,
            f"📍 Заказ #{order.pk}\nВариант: 1 стикер\nСтиль: Мемные\nФото: 2\nЭмоции: Пока\nШаг: фото",
        )
        self.assertEqual(self.last_payloads(), ["order:continue", "menu:main"])
        self.tap("photos_done")
        self.say(CONTACT)
        self.tap("order:confirm")
        self.tap("consent:accept")
        if self.telegram:
            self.tap("pay")
        self.tap("menu:where")
        text = self.last_text()
        self.assertIn(f"📍 Заказ #{order.pk}", text)
        self.assertIn(f"Контакт: {CONTACT}", text)
        self.assertIn("Статус: Ждём оплату", text)
        self.assertIn("Сейчас: ждём оплату", text)
        self.assertEqual(self.last_payloads(), ["menu:main"])

    # -- 5. photos ------------------------------------------------------------------

    def test_photo_sent_as_a_file_is_saved_and_the_done_button_is_shown(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo_as_file("scan")
        self.assertEqual(self.last_text(), PHOTO_SAVED)
        self.assert_photos_done_button()
        photo = self.order().photos.get()
        self.assertEqual(photo.mime_type, "image/png")

    def test_every_photo_answers_with_the_done_button(self):
        self.start_single()
        self.tap("emotion:bye")
        for name in ("a", "b", "c"):
            self.photo(name)
            self.assertEqual(self.last_text(), PHOTO_SAVED)
            self.assert_photos_done_button()
        self.assertEqual(self.order().photos.count(), 3)

    def test_photo_before_the_emotion_order_17(self):
        """Hotfix, Order 17 (Telegram, single/meme): the photo came while the
        emotion was still awaited. It was saved; «Фото загружены» answered
        «Emotion selection is not complete» (the 57-byte webhook body); the
        emotion picked from the old keyboard was followed by the bare photo
        guidance — no «Фото загружены» button anywhere, and any text was
        met with silence. Now: the button is on every photo screen, the
        emotion is asked again right there, and the step is re-prompted."""
        self.start_single()
        self.photo("early")
        self.assertEqual(self.last_text(), PHOTO_SAVED)
        self.assert_photos_done_button()
        self.tap("photos_done")
        self.assertTrue(self.last_text().startswith(NEED_EMOTIONS_HINT), self.last_text())
        self.assertEqual(self.last_payloads()[:3], ["emotion:hello", "emotion:bye", "emotion:thanks"])
        self.say("?")
        self.assertTrue(self.last_text().startswith(self.step_prefix("emotions")))
        self.tap("emotion:bye")  # from any of the emotion keyboards
        self.assertEqual(self.last_text(), PHOTO_SAVED, "the photo is already in: «Фото загружены» right away")
        self.assert_photos_done_button()
        self.assertEqual(self.order().photos.count(), 1)
        self.tap("photos_done")
        self.assertEqual(self.last_text(), CONTACT_PROMPT)

    def test_back_to_photos_with_a_photo_shows_the_done_button(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        self.tap("photos_done")
        self.tap("back:photos")
        self.assertEqual(self.last_text(), PHOTO_SAVED)
        self.assert_photos_done_button()
        self.tap("back:emotions")
        self.tap("emotion:hello")  # stale: the emotion is chosen; back:emotions shows the choice again though
        self.assertTrue(self.last_text().startswith(STALE_STEP))


class MaxNavigationTests(NavigationScenarios, MaxBot, TestCase):
    pass


@override_settings(TELEGRAM_WEBHOOK_SECRET="", TELEGRAM_BOT_TOKEN="test-token")
class TelegramNavigationTests(NavigationScenarios, TelegramBot, TestCase):
    def test_status_command(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo("a")
        self.say("/status")
        self.assertIn(f"📍 Заказ #{self.order().pk}", self.last_text())
        self.assertIn("Шаг: фото", self.last_text())
        self.assertEqual(self.last_payloads()[0], "order:continue")

    def test_uncompressed_photo_uses_the_document_mime_type_and_file(self):
        self.start_single()
        self.tap("emotion:bye")
        self.photo_as_file("scan")
        self.bot.get_file.assert_called_with("scan")
        self.assertEqual(self.order().photos.get().mime_type, "image/png")

    def test_non_image_document_reprompts_without_download(self):
        self.start_single()
        self.tap("emotion:bye")
        self.attachment({"document": {"file_id": "doc", "file_name": "a.pdf", "mime_type": "application/pdf"}})
        self.assertTrue(self.last_text().startswith(self.step_prefix("photos")))
        self.bot.get_file.assert_not_called()
