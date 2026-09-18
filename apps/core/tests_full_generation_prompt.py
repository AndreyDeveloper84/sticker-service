"""DRF-2080: FULL generation inputs — photos first, preview last, expression
rendered as label + description (never the bare code), "final sticker"
wording, rendered prompt persisted as live evidence.
"""

from io import BytesIO

from django.test import SimpleTestCase

from apps.core.models import GenerationJob, Order, OrderPhoto, Product
from apps.core.services.generation import GenerationService
from apps.core.services.generation_prompts import (
    EMOTION_EXPRESSIONS,
    FULL_DEFAULT_PROMPT,
    emotion_label,
    render_expression,
    render_reference_roles,
)
from apps.core.tests_full_generation import (
    PACK3_CONFIG,
    PHOTO_BYTES,
    PREVIEW_BYTES,
    FakeProvider,
    FullProductionTestCase,
)

PILOT9 = ["hello", "bye", "thanks", "great", "no", "love", "laugh", "angry", "surprised"]


class ExpressionRenderingTests(SimpleTestCase):
    def _product(self, emotions):
        return Product(code="p", name="P", config={"emotions": emotions})

    def test_pilot_catalog_has_a_description_for_every_code(self):
        self.assertEqual(sorted(EMOTION_EXPRESSIONS), sorted(PILOT9))
        self.assertTrue(all(EMOTION_EXPRESSIONS[c].strip() for c in PILOT9))

    def test_label_and_description_without_bare_code(self):
        product = self._product([{"code": "hello", "label": "Привет"}])
        text = render_expression(product, "hello")
        self.assertEqual(
            text,
            "Expression: «Привет» — friendly greeting, warm open smile, one hand raised in a wave.",
        )
        self.assertNotIn("hello", text)
        self.assertNotIn("Emotion:", text)

    def test_product_description_overrides_map(self):
        product = self._product(
            [{"code": "hello", "label": "Привет", "description": "big grin, both hands up"}]
        )
        self.assertEqual(render_expression(product, "hello"), "Expression: «Привет» — big grin, both hands up.")

    def test_unknown_code_falls_back_to_label_or_code(self):
        product = self._product([{"code": "wink", "label": "Подмигиваю"}])
        self.assertEqual(render_expression(product, "wink"), "Expression: «Подмигиваю».")
        self.assertEqual(emotion_label(product, "missing"), "missing")
        self.assertEqual(render_expression(product, "missing"), "Expression: «missing».")

    def test_reference_roles_text(self):
        one = render_reference_roles(photo_count=1, has_preview=True)
        self.assertIn("Reference 1 is a photo of the person", one)
        self.assertIn("preserve their exact facial identity", one)
        self.assertIn("The last reference is the customer-approved preview", one)
        self.assertIn("art style, palette and character design only", one)
        many = render_reference_roles(photo_count=3, has_preview=False)
        self.assertIn("References 1..3 are photos of the person", many)
        self.assertNotIn("approved preview", many)


class FullGenerationPromptTests(FullProductionTestCase):
    def _add_photo(self, order, name, content):
        key = f"orders/{order.pk}/{name}"
        self.storage.save(key, BytesIO(content))
        return OrderPhoto.objects.create(
            order=order,
            storage_key=key,
            original_filename=name,
            mime_type="image/jpeg",
            size_bytes=len(content),
            status=OrderPhoto.Status.ACCEPTED,
        )

    def test_reference_order_photos_first_preview_last(self):
        order, preview = self._make_order(emotions=("hello",), config={**PACK3_CONFIG, "quantity": 1, "emotion_count": 1})
        second = self._add_photo(order, "second.jpg", b"second-photo")
        provider = FakeProvider()
        self._service(provider).start(order=order, max_slots=None)

        request = provider.requests[0]
        self.assertEqual(
            [r.content for r in request.reference_images],
            [PHOTO_BYTES, b"second-photo", PREVIEW_BYTES],
        )
        self.assertEqual(request.reference_images[-1].filename, f"approved-preview-{preview.pk}.png")
        self.assertIn("References 1..2 are photos of the person", request.prompt)
        self.assertIn("The last reference is the customer-approved preview", request.prompt)

        job = self._full_jobs(order).get()
        first_photo = order.photos.order_by("created_at", "pk").first()
        self.assertEqual(
            job.input_metadata["reference_order"],
            [f"photo:{first_photo.pk}", f"photo:{second.pk}", f"preview:{preview.pk}"],
        )

    def test_prompt_uses_expression_label_description_and_final_sticker_wording(self):
        order, _preview = self._make_order()
        provider = FakeProvider()
        self._service(provider).start(order=order, max_slots=None)

        by_slot = {r.metadata["slot_key"]: r for r in provider.requests}
        self.assertEqual(set(by_slot), {"hello", "bye", "thanks"})
        for slot, request in by_slot.items():
            with self.subTest(slot=slot):
                self.assertNotIn(f"Emotion: {slot}", request.prompt)
                self.assertNotIn(f"«{slot}»", request.prompt)
                self.assertIn(EMOTION_EXPRESSIONS[slot], request.prompt)
                self.assertEqual(request.metadata["emotion"], slot)
        self.assertIn("Expression: «Привет» — friendly greeting", by_slot["hello"].prompt)
        self.assertEqual(by_slot["hello"].metadata["emotion_label"], "Привет")
        # FULL wording: default FULL prompt, not the product's preview prompt.
        self.assertTrue(by_slot["hello"].prompt.startswith(FULL_DEFAULT_PROMPT))
        self.assertIn("final sticker", by_slot["hello"].prompt)
        self.assertNotIn("Make a personalized sticker pack", by_slot["hello"].prompt)
        self.assertNotIn("preview", by_slot["hello"].prompt.split("\n")[0])
        self.assertIn("Preserve likeness", by_slot["hello"].prompt)  # style prompt kept

    def test_product_full_generation_prompt_overrides_default(self):
        config = {**PACK3_CONFIG, "full_generation_prompt": "Draw the final sticker of this exact person."}
        order, _preview = self._make_order(config=config)
        provider = FakeProvider()
        self._service(provider).start(order=order, max_slots=1)
        prompt = provider.requests[0].prompt
        self.assertTrue(prompt.startswith("Draw the final sticker of this exact person."))
        self.assertNotIn(FULL_DEFAULT_PROMPT, prompt)

    def test_notes_are_appended_after_reference_roles(self):
        order, _preview = self._make_order()
        order.customer_notes = "glasses always on"
        order.operator_notes = "keep the beard"
        order.save(update_fields=["customer_notes", "operator_notes"])
        provider = FakeProvider()
        self._service(provider).start(order=order, max_slots=1)
        lines = provider.requests[0].prompt.split("\n")
        self.assertEqual(lines[-2:], ["Customer notes: glasses always on", "Operator notes: keep the beard"])
        self.assertTrue(lines[-3].startswith("Reference 1 is a photo of the person"))

    def test_rendered_prompt_is_persisted_in_job_input_metadata(self):
        order, _preview = self._make_order()
        provider = FakeProvider()
        self._service(provider).start(order=order, max_slots=None)
        for request in provider.requests:
            job = GenerationJob.objects.get(pk=request.metadata["job_id"])
            self.assertEqual(job.input_metadata["prompt"], request.prompt)
            self.assertEqual(job.input_metadata["emotion_label"], request.metadata["emotion_label"])
            self.assertEqual(job.input_metadata["emotion"], job.slot_key)  # kept for audit
            self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)

    def test_failed_slot_also_keeps_the_prompt_it_used(self):
        order, _preview = self._make_order()
        provider = FakeProvider(fail_slots={"bye"})
        self._service(provider).start(order=order, max_slots=None)
        failed = self._full_jobs(order).get(slot_key="bye")
        self.assertEqual(failed.status, GenerationJob.Status.FAILED)
        self.assertIn(EMOTION_EXPRESSIONS["bye"], failed.input_metadata["prompt"])


class PreviewPromptPersistenceTests(FullProductionTestCase):
    def test_preview_job_persists_rendered_prompt(self):
        order, _preview = self._make_order(status=Order.Status.PAID)
        provider = FakeProvider()
        asset = GenerationService(provider=provider, storage=self.storage).generate_preview(order=order)
        self.assertEqual(asset.job.input_metadata["prompt"], provider.requests[-1].prompt)
        self.assertIn("Make a personalized sticker pack", asset.job.input_metadata["prompt"])
