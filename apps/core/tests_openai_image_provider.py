import base64
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.core.image_providers import ImageGenerationRequest, OpenAIImageProvider, ReferenceImage


class OpenAIImageProviderTests(SimpleTestCase):
    def test_multiple_reference_images_are_sent_and_result_is_decoded(self):
        encoded = base64.b64encode(b"openai-result").decode("ascii")

        class Images:
            def __init__(self):
                self.kwargs = None

            def edit(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

        images = Images()
        provider = OpenAIImageProvider(client=SimpleNamespace(images=images), model="test-model")
        request = ImageGenerationRequest(
            prompt="Preserve likeness",
            reference_images=[
                ReferenceImage("one.jpg", "image/jpeg", b"one"),
                ReferenceImage("two.jpg", "image/jpeg", b"two"),
            ],
        )

        result = provider.generate_preview(request)

        self.assertEqual(result.content, b"openai-result")
        self.assertEqual(result.metadata["model"], "test-model")
        self.assertEqual(images.kwargs["model"], "test-model")
        self.assertEqual(images.kwargs["prompt"], "Preserve likeness")
        self.assertEqual(len(images.kwargs["image"]), 2)
