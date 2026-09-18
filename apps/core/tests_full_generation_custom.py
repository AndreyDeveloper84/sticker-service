"""Custom-caption stickers (owner task 2026-09-18): slot "custom-N" renders the
customer's N-th phrase as a caption instruction — never as an emotion
"Expression:" line — and the FULL default prompt carries the output
requirements (transparent background, everything inside the canvas, white
outline) without depending on the seed.
"""

from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase

from apps.core.management.commands.seed_live_test import FULL_GENERATION_PROMPT
from apps.core.models import GenerationJob, Order, Style
from apps.core.services.generation_prompts import (
    FULL_DEFAULT_PROMPT,
    FULL_OUTPUT_REQUIREMENTS,
    custom_phrase_for_slot,
    is_custom_slot,
    render_caption,
    render_expression,
)
from apps.core.tests_full_generation import (
    PACK3_CONFIG,
    FakeProvider,
    FullProductionTestCase,
)

CUSTOM3_CONFIG = {
    "kind": "custom_pack",
    "quantity": 3,
    "emotion_count": 3,
    "emotions": [{"code": f"custom-{n}", "label": f"Фраза {n}"} for n in (1, 2, 3)],
    "requires_custom_phrases": True,
    "requires_customer_contact": True,
    "price_minor": 80000,
    "currency": "RUB",
}
PHRASES = ["Доброе утро!", "Я на месте", "Спасибо, друг"]


class CaptionHelpersTests(SimpleTestCase):
    def test_render_caption_is_verbatim_and_has_no_expression_line(self):
        text = render_caption("  Доброе утро!  ")
        self.assertEqual(
            text,
            "Add the caption text «Доброе утро!» on the sticker, exactly as written, in bold "
            "clean readable lettering, fully inside the canvas, not cropped; keep the "
            "character's expression friendly/neutral.",
        )
        self.assertNotIn("Expression:", text)

    def test_is_custom_slot(self):
        for key in ("custom-1", "custom-9", "custom-12"):
            self.assertTrue(is_custom_slot(key), key)
        for key in ("hello", "custom-", "custom-0", "custom-x", "custom", "mycustom-1"):
            self.assertFalse(is_custom_slot(key), key)

    def test_custom_phrase_for_slot(self):
        order = Order(selection={"emotions": ["custom-1", "custom-2"], "custom_phrases": [" Привет ", "Пока"]})
        self.assertEqual(custom_phrase_for_slot(order, "custom-1"), "Привет")
        self.assertEqual(custom_phrase_for_slot(order, "custom-2"), "Пока")
        self.assertEqual(custom_phrase_for_slot(order, "custom-3"), "")  # missing phrase
        self.assertEqual(custom_phrase_for_slot(order, "hello"), "")  # not a custom slot
        self.assertEqual(custom_phrase_for_slot(Order(selection=None), "custom-1"), "")

    def test_full_default_prompt_carries_output_requirements_and_seed_reuses_it(self):
        self.assertIn(FULL_OUTPUT_REQUIREMENTS, FULL_DEFAULT_PROMPT)
        self.assertIn("transparent background", FULL_DEFAULT_PROMPT)
        self.assertIn("head, hairstyle, hands and any lettering inside the canvas", FULL_DEFAULT_PROMPT)
        self.assertIn("neat white outline", FULL_DEFAULT_PROMPT)
        self.assertIn("final sticker", FULL_DEFAULT_PROMPT)
        self.assertEqual(FULL_GENERATION_PROMPT, FULL_DEFAULT_PROMPT)


class CustomProductFullGenerationTests(FullProductionTestCase):
    def _custom_order(self, phrases=PHRASES):
        order, preview = self._make_order(
            config=CUSTOM3_CONFIG, emotions=("custom-1", "custom-2", "custom-3")
        )
        order.selection = {
            "emotions": ["custom-1", "custom-2", "custom-3"],
            "custom_phrases": list(phrases),
            "contact": "Анна, @anna",
        }
        order.save(update_fields=["selection", "updated_at"])
        return order, preview

    def test_custom_slots_get_caption_not_expression_and_phrase_as_label(self):
        order, _preview = self._custom_order()
        provider = FakeProvider()
        self._service(provider).start(order=order, max_slots=None)

        by_slot = {r.metadata["slot_key"]: r for r in provider.requests}
        self.assertEqual(list(by_slot), ["custom-1", "custom-2", "custom-3"])
        for number, phrase in enumerate(PHRASES, start=1):
            request = by_slot[f"custom-{number}"]
            with self.subTest(slot=number):
                self.assertIn(f"Add the caption text «{phrase}» on the sticker, exactly as written", request.prompt)
                self.assertNotIn("Expression:", request.prompt)
                self.assertNotIn(f"Фраза {number}", request.prompt)  # placeholder label never leaks
                self.assertEqual(request.metadata["emotion_label"], phrase)
                self.assertEqual(request.metadata["emotion"], f"custom-{number}")
                job = GenerationJob.objects.get(pk=request.metadata["job_id"])
                self.assertEqual(job.input_metadata["emotion_label"], phrase)
                self.assertEqual(job.input_metadata["prompt"], request.prompt)
                self.assertIn(f"«{phrase}»", job.input_metadata["prompt"])
                self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)
        # Output requirements come from the code default (no full_generation_prompt in config).
        self.assertTrue(by_slot["custom-1"].prompt.startswith(FULL_DEFAULT_PROMPT))
        self.assertIn("neat white outline", by_slot["custom-1"].prompt)
        # Everything else in the FULL contract is untouched.
        self.assertIn("Reference 1 is a photo of the person", by_slot["custom-1"].prompt)
        self.assertIn("The last reference is the customer-approved preview", by_slot["custom-1"].prompt)
        lines = by_slot["custom-1"].prompt.split("\n")
        caption_index = next(i for i, line in enumerate(lines) if line.startswith("Add the caption text"))
        self.assertTrue(lines[caption_index + 1].startswith("Reference 1 is a photo"))

    def test_regular_emotion_slots_are_unchanged(self):
        order, _preview = self._make_order(config=PACK3_CONFIG)
        provider = FakeProvider()
        self._service(provider).start(order=order, max_slots=None)
        for request in provider.requests:
            slot = request.metadata["slot_key"]
            with self.subTest(slot=slot):
                self.assertIn("Expression: «", request.prompt)
                self.assertNotIn("Add the caption text", request.prompt)
                self.assertEqual(
                    request.metadata["emotion_label"],
                    render_expression(order.product, slot).split("«")[1].split("»")[0],
                )

    def test_custom_slot_without_phrase_fails_closed_without_provider_call(self):
        order, _preview = self._custom_order(phrases=PHRASES[:2])  # third phrase missing
        provider = FakeProvider()
        plan = self._service(provider).start(order=order, max_slots=None)
        self.assertEqual([r.metadata["slot_key"] for r in provider.requests], ["custom-1", "custom-2"])
        third = next(slot for slot in plan if slot.slot_key == "custom-3")
        self.assertEqual(third.status, "failed")
        self.assertTrue(third.retryable)
        job = self._full_jobs(order).get(slot_key="custom-3")
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertIn("has no customer phrase", job.error)
        self.assertNotIn("prompt", job.input_metadata)  # nothing was rendered/sent

    def test_seed_pins_full_prompt_and_deactivates_comic_without_renaming(self):
        Style.objects.filter(code="comic").delete()
        Style.objects.create(code="comic", name="Комикс", is_active=True)
        call_command("seed_live_test", stdout=StringIO())
        comic = Style.objects.get(code="comic")
        self.assertEqual(comic.name, "Комикс")
        self.assertFalse(comic.is_active)
        helper = Style.objects.get(code="help-choose")
        self.assertEqual(helper.name, "Помогите выбрать")
        self.assertTrue(helper.is_active)
        self.assertEqual(
            sorted(Style.objects.filter(is_active=True).values_list("code", flat=True)),
            ["3d", "drawn", "embroidery", "help-choose", "meme"],
        )
