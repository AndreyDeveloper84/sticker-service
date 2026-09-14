from django.test import SimpleTestCase

from apps.max_bot.parser import MaxParseError, parse_max_event


class MaxParserTests(SimpleTestCase):
    def test_bot_started_becomes_start(self):
        event = parse_max_event(
            {
                "update_type": "bot_started",
                "timestamp": 1731320000000,
                "chat_id": 67890,
                "user": {"user_id": 12345, "first_name": "Ivan"},
            }
        )
        self.assertEqual(event.update_type, "bot_started")
        self.assertEqual(event.user_id, "12345")
        self.assertEqual(event.chat_id, "67890")
        self.assertEqual(event.text, "/start")
        self.assertEqual(event.user["first_name"], "Ivan")

    def test_bot_started_keeps_deeplink_payload(self):
        event = parse_max_event(
            {
                "update_type": "bot_started",
                "chat_id": 1,
                "user": {"user_id": 2},
                "payload": "ref_99",
            }
        )
        self.assertEqual(event.text, "/start ref_99")

    def test_message_created(self):
        event = parse_max_event(
            {
                "update_type": "message_created",
                "message": {
                    "sender": {"user_id": 111},
                    "recipient": {"chat_id": 222},
                    "body": {
                        "mid": "mid-1",
                        "text": "hi",
                        "attachments": [{"type": "image", "payload": {"url": "https://cdn.test/a.jpg"}}],
                    },
                },
            }
        )
        self.assertEqual(event.update_type, "message_created")
        self.assertEqual(event.user_id, "111")
        self.assertEqual(event.chat_id, "222")
        self.assertEqual(event.message_id, "mid-1")
        self.assertEqual(event.text, "hi")
        self.assertEqual(len(event.attachments), 1)

    def test_message_created_tolerates_missing_body_parts(self):
        event = parse_max_event(
            {
                "update_type": "message_created",
                "message": {
                    "sender": {"user_id": 1},
                    "recipient": {"chat_id": 2},
                },
            }
        )
        self.assertEqual(event.text, "")
        self.assertEqual(event.attachments, [])
        self.assertEqual(event.message_id, "")

    def test_message_callback(self):
        event = parse_max_event(
            {
                "update_type": "message_callback",
                "callback": {
                    "callback_id": "cb-42",
                    "payload": "product:stickers",
                    "user": {"user_id": 777},
                },
                "message": {
                    "recipient": {"chat_id": 888},
                    "body": {"mid": "mid-9"},
                },
            }
        )
        self.assertEqual(event.update_type, "message_callback")
        self.assertEqual(event.user_id, "777")
        self.assertEqual(event.chat_id, "888")
        self.assertEqual(event.message_id, "mid-9")
        self.assertEqual(event.callback_id, "cb-42")
        self.assertEqual(event.callback_payload, "product:stickers")
        self.assertEqual(event.text, "product:stickers")

    def test_unsupported_update_type_raises(self):
        with self.assertRaises(MaxParseError):
            parse_max_event({"update_type": "bot_added"})

    def test_missing_update_type_raises(self):
        with self.assertRaises(MaxParseError):
            parse_max_event({})

    def test_non_dict_payload_raises(self):
        with self.assertRaises(MaxParseError):
            parse_max_event(["not", "a", "dict"])

    def test_message_created_without_sender_raises(self):
        with self.assertRaises(MaxParseError):
            parse_max_event(
                {
                    "update_type": "message_created",
                    "message": {"recipient": {"chat_id": 1}},
                }
            )

    def test_callback_without_callback_id_raises(self):
        with self.assertRaises(MaxParseError):
            parse_max_event(
                {
                    "update_type": "message_callback",
                    "callback": {"user": {"user_id": 1}, "payload": "x"},
                    "message": {"recipient": {"chat_id": 2}},
                }
            )
