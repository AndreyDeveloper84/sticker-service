"""Revision prompt (Order 16, 2026-09-19): the bot channels send only the
category, so ``customer_text`` is usually empty. The prompt must then carry a
meaningful natural-language instruction — never the bare category code
("Revision category: other").
"""

import tempfile
from io import BytesIO
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import ChannelIdentity, GenerationJob, Order, OrderPhoto, Product, Revision, Style, User
from apps.core.services.generation import GenerationService
from apps.core.services.generation_prompts import (
    REVISION_INSTRUCTIONS,
    REVISION_LEAD,
    render_revision_request,
)
from apps.core.services.order_state import OrderStateService
from apps.core.services.preview_feedback import PreviewFeedbackService
from apps.core.storage import LocalMediaStorage


class RenderRevisionRequestTests(SimpleTestCase):
    def test_every_category_has_an_instruction(self):
        codes = {value for value, _ in Revision.Category.choices}
        self.assertEqual(set(REVISION_INSTRUCTIONS), codes)
        self.assertTrue(all(REVISION_INSTRUCTIONS[c].strip() for c in codes))

    def test_other_without_text_is_a_full_instruction_not_a_bare_code(self):
        text = render_revision_request(Revision.Category.OTHER, "")
        self.assertTrue(text.startswith(REVISION_LEAD))
        self.assertIn("stronger likeness to the reference photos", text)
        self.assertNotIn("Revision category", text)
        self.assertNotIn("other", text.replace("another", "").lower())
        self.assertNotIn("Customer's own words", text)

    def test_category_specific_wording_without_bare_code(self):
        text = render_revision_request(Revision.Category.FACE)
        self.assertIn("face match the reference photos", text)
        self.assertNotIn(": face", text)
        self.assertNotIn("Revision category", text)

    def test_customer_text_is_appended_verbatim(self):
        text = render_revision_request(Revision.Category.HAIR, "  сделайте волосы темнее ")
        self.assertIn(REVISION_INSTRUCTIONS["hair"], text)
        self.assertTrue(text.endswith("Customer's own words: «сделайте волосы темнее»."))

    def test_unknown_category_falls_back_to_other(self):
        self.assertEqual(
            render_revision_request("something-new"),
            render_revision_request(Revision.Category.OTHER),
        )


class _Provider:
    name = "fake"

    def __init__(self):
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request)
        return ImageGenerationResult(content=b"img", metadata={})


class RevisionJobPromptTests(TestCase):
    """End-to-end: MAX/Telegram button → Revision(category, text="") → job prompt."""

    def setUp(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel="max", external_user_id="rev-user")
        product = Product.objects.create(code="sticker-single", name="Single", config={"generation_prompt": "Make preview"})
        style = Style.objects.create(code="3d", name="3D", config={"prompt": "Use a polished 3D style."})
        self.order = Order.objects.create(user=user, channel_identity=identity, product=product, style=style, status=Order.Status.PAID)

    def _request_revision(self, storage, category, customer_text=""):
        key = f"orders/{self.order.pk}/reference.jpg"
        storage.save(key, BytesIO(b"person-photo"))
        OrderPhoto.objects.create(order=self.order, storage_key=key, original_filename="reference.jpg", mime_type="image/jpeg", size_bytes=12)
        service = GenerationService(provider=_Provider(), storage=storage)
        source = service.generate_preview(order=self.order)
        source.metadata = {**(source.metadata or {}), "internal_approved": True, "deliveries": [{"status": "sent", "channel": "max", "message_id": "1"}]}
        source.save(update_fields=["metadata", "updated_at"])
        self.order.refresh_from_db()
        OrderStateService.transition(order=self.order, to_status=Order.Status.PREVIEW_REVIEW)
        # Bot channels call request_revision with the category only (no text field).
        PreviewFeedbackService.request_revision(order=self.order, category=category, customer_text=customer_text)
        self.order.refresh_from_db()
        return service

    def test_other_with_empty_text_yields_meaningful_prompt(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = self._request_revision(LocalMediaStorage(), Revision.Category.OTHER)
            service.generate_revision(order=self.order)
            job = GenerationJob.objects.get(order=self.order, task_type=GenerationJob.TaskType.REVISION)
            prompt = job.input_metadata["prompt"]
            self.assertEqual(service.provider.requests[-1].prompt, prompt)
            self.assertNotIn("Revision category", prompt)
            self.assertNotIn("Customer revision request", prompt)
            self.assertIn(REVISION_LEAD, prompt)
            self.assertIn(REVISION_INSTRUCTIONS["other"], prompt)
            # Base preview prompt and style stay ahead of the revision clause.
            self.assertLess(prompt.index("Make preview"), prompt.index(REVISION_LEAD))
            self.assertLess(prompt.index("Use a polished 3D style."), prompt.index(REVISION_LEAD))

    def test_category_with_text_keeps_customer_words(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = self._request_revision(LocalMediaStorage(), Revision.Category.FACE, "глаза больше")
            service.generate_revision(order=self.order)
            prompt = service.provider.requests[-1].prompt
            self.assertIn(REVISION_INSTRUCTIONS["face"], prompt)
            self.assertIn("Customer's own words: «глаза больше».", prompt)
            self.assertNotIn("Revision category", prompt)
