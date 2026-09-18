"""Env-gated images.edit parameters for OpenAIImageProvider (DRF-2052 QC follow-up).

Default (no env) must call images.edit with exactly the historical argument
set; configured values are passed through; anything outside the gpt-image
API vocabulary fails closed at provider construction, before any call.
"""

import base64
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.core.image_providers import (
    ImageGenerationRequest,
    OpenAIImageProvider,
    ProviderConfigurationError,
    ReferenceImage,
)

ENV_NAMES = (
    "OPENAI_IMAGE_SIZE",
    "OPENAI_IMAGE_BACKGROUND",
    "OPENAI_IMAGE_OUTPUT_FORMAT",
    "OPENAI_IMAGE_INPUT_FIDELITY",
)


class Images:
    def __init__(self):
        self.calls = []

    def edit(self, **kwargs):
        self.calls.append(kwargs)
        encoded = base64.b64encode(b"openai-result").decode("ascii")
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])


def request():
    return ImageGenerationRequest(
        prompt="Preserve likeness",
        reference_images=[ReferenceImage("one.jpg", "image/jpeg", b"one")],
    )


def provider(images, **kwargs):
    return OpenAIImageProvider(
        client=SimpleNamespace(images=images), model="test-model", proxy_pool=None, **kwargs
    )


class OpenAIImageParamsDefaultTests(SimpleTestCase):
    def test_without_env_edit_receives_historical_arguments_only(self):
        images = Images()
        with mock.patch.dict("os.environ", {}, clear=True):
            result = provider(images).generate_preview(request())
        self.assertEqual(len(images.calls), 1)
        self.assertEqual(sorted(images.calls[0]), ["image", "model", "prompt"])
        self.assertEqual(images.calls[0]["model"], "test-model")
        self.assertEqual(images.calls[0]["prompt"], "Preserve likeness")
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.metadata, {"model": "test-model"})

    def test_empty_env_values_are_treated_as_unset(self):
        images = Images()
        env = {name: "   " for name in ENV_NAMES}
        with mock.patch.dict("os.environ", env, clear=True):
            p = provider(images)
            p.generate_preview(request())
        self.assertEqual(p.edit_parameters(), {})
        self.assertEqual(sorted(images.calls[0]), ["image", "model", "prompt"])


class OpenAIImageParamsConfiguredTests(SimpleTestCase):
    def test_env_values_are_passed_through_to_edit(self):
        images = Images()
        env = {
            "OPENAI_IMAGE_SIZE": "1024x1024",
            "OPENAI_IMAGE_BACKGROUND": "transparent",
            "OPENAI_IMAGE_OUTPUT_FORMAT": "png",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            result = provider(images).generate_preview(request())
        call = images.calls[0]
        self.assertEqual(
            {k: call[k] for k in ("model", "prompt", "size", "background", "output_format")},
            {
                "model": "test-model",
                "prompt": "Preserve likeness",
                "size": "1024x1024",
                "background": "transparent",
                "output_format": "png",
            },
        )
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(
            result.metadata,
            {"model": "test-model", "size": "1024x1024", "background": "transparent", "output_format": "png"},
        )

    def test_partial_configuration_sends_only_set_parameters(self):
        images = Images()
        with mock.patch.dict("os.environ", {"OPENAI_IMAGE_BACKGROUND": "Transparent"}, clear=True):
            provider(images).generate_preview(request())
        self.assertEqual(sorted(images.calls[0]), ["background", "image", "model", "prompt"])
        self.assertEqual(images.calls[0]["background"], "transparent")  # normalised

    def test_output_format_drives_result_mime_type(self):
        for fmt, mime in (("png", "image/png"), ("webp", "image/webp"), ("jpeg", "image/jpeg")):
            with self.subTest(fmt=fmt):
                images = Images()
                with mock.patch.dict("os.environ", {"OPENAI_IMAGE_OUTPUT_FORMAT": fmt}, clear=True):
                    result = provider(images).generate_preview(request())
                self.assertEqual(images.calls[0]["output_format"], fmt)
                self.assertEqual(result.mime_type, mime)

    def test_explicit_constructor_values_override_env(self):
        images = Images()
        with mock.patch.dict("os.environ", {"OPENAI_IMAGE_SIZE": "auto"}, clear=True):
            provider(images, size="1536x1024", background="opaque").generate_preview(request())
        self.assertEqual(images.calls[0]["size"], "1536x1024")
        self.assertEqual(images.calls[0]["background"], "opaque")

    def test_every_allowed_value_is_accepted(self):
        for name, values in (
            ("OPENAI_IMAGE_SIZE", ("1024x1024", "1024x1536", "1536x1024", "auto")),
            ("OPENAI_IMAGE_BACKGROUND", ("transparent", "opaque", "auto")),
            ("OPENAI_IMAGE_OUTPUT_FORMAT", ("png", "webp", "jpeg")),
            ("OPENAI_IMAGE_INPUT_FIDELITY", ("high", "low")),
        ):
            for value in values:
                with self.subTest(name=name, value=value):
                    with mock.patch.dict("os.environ", {name: value}, clear=True):
                        provider(Images())  # must not raise


class OpenAIImageInputFidelityTests(SimpleTestCase):
    """DRF-2080 likeness lever: input_fidelity is env-gated like the rest."""

    def test_without_env_input_fidelity_is_not_sent(self):
        images = Images()
        with mock.patch.dict("os.environ", {}, clear=True):
            p = provider(images)
            result = p.generate_preview(request())
        self.assertIsNone(p.input_fidelity)
        self.assertEqual(sorted(images.calls[0]), ["image", "model", "prompt"])
        self.assertNotIn("input_fidelity", result.metadata)

    def test_env_high_is_passed_through_and_recorded(self):
        images = Images()
        with mock.patch.dict("os.environ", {"OPENAI_IMAGE_INPUT_FIDELITY": "High"}, clear=True):
            result = provider(images).generate_preview(request())
        self.assertEqual(sorted(images.calls[0]), ["image", "input_fidelity", "model", "prompt"])
        self.assertEqual(images.calls[0]["input_fidelity"], "high")  # normalised
        self.assertEqual(result.metadata, {"model": "test-model", "input_fidelity": "high"})
        self.assertEqual(result.mime_type, "image/png")

    def test_combines_with_other_parameters(self):
        images = Images()
        env = {"OPENAI_IMAGE_INPUT_FIDELITY": "low", "OPENAI_IMAGE_BACKGROUND": "transparent"}
        with mock.patch.dict("os.environ", env, clear=True):
            provider(images).generate_preview(request())
        self.assertEqual(
            sorted(images.calls[0]), ["background", "image", "input_fidelity", "model", "prompt"]
        )
        self.assertEqual(images.calls[0]["input_fidelity"], "low")

    def test_explicit_constructor_value_overrides_env(self):
        images = Images()
        with mock.patch.dict("os.environ", {"OPENAI_IMAGE_INPUT_FIDELITY": "low"}, clear=True):
            provider(images, input_fidelity="high").generate_preview(request())
        self.assertEqual(images.calls[0]["input_fidelity"], "high")

    def test_invalid_value_fails_closed_before_any_call(self):
        images = Images()
        with mock.patch.dict("os.environ", {"OPENAI_IMAGE_INPUT_FIDELITY": "max"}, clear=True):
            with self.assertRaises(ProviderConfigurationError) as ctx:
                provider(images)
        self.assertIn("OPENAI_IMAGE_INPUT_FIDELITY", str(ctx.exception))
        self.assertEqual(images.calls, [])


class OpenAIImageParamsFailClosedTests(SimpleTestCase):
    def test_invalid_values_fail_closed_at_construction(self):
        cases = (
            ("OPENAI_IMAGE_SIZE", "512x512"),
            ("OPENAI_IMAGE_SIZE", "1024"),
            ("OPENAI_IMAGE_BACKGROUND", "alpha"),
            ("OPENAI_IMAGE_OUTPUT_FORMAT", "gif"),
            ("OPENAI_IMAGE_OUTPUT_FORMAT", "jpg"),
            ("OPENAI_IMAGE_INPUT_FIDELITY", "medium"),
            ("OPENAI_IMAGE_INPUT_FIDELITY", "true"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                images = Images()
                with mock.patch.dict("os.environ", {name: value}, clear=True):
                    with self.assertRaises(ProviderConfigurationError) as ctx:
                        provider(images)
                self.assertIn(name, str(ctx.exception))
                self.assertEqual(images.calls, [])  # nothing reached the API

    def test_invalid_explicit_value_fails_closed(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ProviderConfigurationError):
                provider(Images(), output_format="bmp")
