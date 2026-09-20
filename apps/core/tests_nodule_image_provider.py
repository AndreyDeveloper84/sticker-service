"""DRF-2072: Nodule experimental text-to-image provider — fake HTTP only.

No live calls, no real keys. Nodule exposes only /v1/images/generations
(prompt -> image); it is NOT reference-equivalent, so every personalised
flow (reference images) must fail closed before any request is made, and
provider selection must never fall back to OpenAI.
"""

import base64
import json
from unittest import mock

import httpx
from django.test import SimpleTestCase

from apps.core.image_providers import (
    NODULE_GENERATIONS_PATH,
    ImageGenerationRequest,
    NoduleImageProvider,
    OpenAIImageProvider,
    ProviderCapabilityError,
    ProviderConfigurationError,
    ReferenceImage,
    classify_provider_failure,
    get_image_provider,
)

BASE_URL = "https://nodule.example"
KEY = "test-nodule-key"
PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"


class Recorder:
    """httpx.MockTransport handler that records requests and scripts replies."""

    def __init__(self, handler):
        self.requests = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def b64_reply(request):
    return httpx.Response(
        200,
        json={
            "created": 1,
            "data": [{"b64_json": base64.b64encode(PNG).decode()}],
            "usage": {"input_tokens": 10, "output_tokens": 32, "total_tokens": 42, "cost": "n/a"},
        },
    )


def provider(handler=b64_reply, **kwargs):
    recorder = Recorder(handler)
    return (
        NoduleImageProvider(
            api_key=KEY, base_url=BASE_URL, transport=httpx.MockTransport(recorder), **kwargs
        ),
        recorder,
    )


class NoduleContractTests(SimpleTestCase):
    def test_capability_flags(self):
        nodule, _ = provider()
        self.assertEqual(nodule.name, "nodule")
        self.assertTrue(nodule.supports_text_to_image)
        self.assertFalse(nodule.supports_reference_generation)
        self.assertEqual(nodule.model, "gpt-image-2")
        self.assertFalse(getattr(OpenAIImageProvider, "supports_text_to_image", False))

    def test_text_to_image_posts_generations_schema_and_decodes_b64(self):
        nodule, recorder = provider()
        result = nodule.generate_text_to_image(prompt="a sticker of a fictional man")

        self.assertEqual(len(recorder.requests), 1)
        request = recorder.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), f"{BASE_URL}{NODULE_GENERATIONS_PATH}")
        self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(
            json.loads(request.content),
            {
                "model": "gpt-image-2",
                "prompt": "a sticker of a fictional man",
                "size": "1024x1024",
                "n": 1,
            },
        )
        self.assertEqual(result.content, PNG)
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.metadata["provider"], "nodule")
        self.assertEqual(result.metadata["endpoint"], NODULE_GENERATIONS_PATH)
        self.assertIs(result.metadata["experimental"], True)
        self.assertIs(result.metadata["reference_equivalent"], False)
        # Same shape as OpenAI usage (DRF-2055 _usage_metadata): ints only.
        self.assertEqual(
            result.metadata["usage"],
            {"input_tokens": 10, "output_tokens": 32, "total_tokens": 42},
        )

    def test_missing_usage_leaves_metadata_without_usage(self):
        nodule, _ = provider(lambda r: httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}))
        result = nodule.generate_text_to_image(prompt="x")
        self.assertNotIn("usage", result.metadata)

    def test_never_calls_edits(self):
        nodule, recorder = provider()
        nodule.generate_text_to_image(prompt="x")
        self.assertTrue(all("/edits" not in str(r.url) for r in recorder.requests))
        self.assertFalse(hasattr(nodule, "edit"))

    def test_url_response_is_fetched_without_bearer_token(self):
        def handler(request):
            if request.url.path == NODULE_GENERATIONS_PATH:
                return httpx.Response(200, json={"data": [{"url": "https://cdn.example/img.webp"}]})
            return httpx.Response(200, content=PNG, headers={"content-type": "image/webp"})

        nodule, recorder = provider(handler)
        result = nodule.generate_text_to_image(prompt="x")
        self.assertEqual(result.content, PNG)
        self.assertEqual(result.mime_type, "image/webp")
        self.assertEqual([r.method for r in recorder.requests], ["POST", "GET"])
        self.assertEqual(str(recorder.requests[1].url), "https://cdn.example/img.webp")
        self.assertNotIn("authorization", recorder.requests[1].headers)

    def test_provider_error_status_raises_after_single_post(self):
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status):
                nodule, recorder = provider(
                    lambda r, status=status: httpx.Response(status, json={"error": "nope"})
                )
                with self.assertRaises(httpx.HTTPStatusError) as ctx:
                    nodule.generate_text_to_image(prompt="x")
                self.assertEqual(len(recorder.requests), 1)  # no retry
                self.assertEqual(nodule.classify_failure(ctx.exception), "api")

    def test_empty_or_malformed_response_raises(self):
        for body in ({}, {"data": []}, {"data": [{}]}, {"data": [{"b64_json": ""}]}):
            with self.subTest(body=body):
                nodule, _ = provider(lambda r, body=body: httpx.Response(200, json=body))
                with self.assertRaises(RuntimeError):
                    nodule.generate_text_to_image(prompt="x")

    def test_blank_prompt_rejected_before_request(self):
        nodule, recorder = provider()
        with self.assertRaises(ValueError):
            nodule.generate_text_to_image(prompt="   ")
        self.assertEqual(recorder.requests, [])


class NoduleFailClosedTests(SimpleTestCase):
    def test_reference_images_fail_closed_without_http(self):
        nodule, recorder = provider()
        request = ImageGenerationRequest(
            prompt="personalised preview",
            reference_images=[ReferenceImage("photo.jpg", "image/jpeg", b"jpg")],
            metadata={"task_type": "preview"},
        )
        with self.assertRaises(ProviderCapabilityError):
            nodule.generate_preview(request)
        self.assertEqual(recorder.requests, [])
        self.assertEqual(classify_provider_failure(nodule, ProviderCapabilityError("x")), "ambiguous")

    def test_generate_preview_without_references_is_plain_text_to_image(self):
        nodule, recorder = provider()
        result = nodule.generate_preview(ImageGenerationRequest(prompt="bg", reference_images=[]))
        self.assertEqual(result.content, PNG)
        self.assertEqual(len(recorder.requests), 1)

    def test_domain_services_fail_closed_with_nodule(self):
        """GenerationService always passes reference photos: the provider
        refuses before any request, no fallback provider is consulted."""
        from apps.core.services.generation import GenerationService

        nodule, recorder = provider()
        service = GenerationService(provider=nodule, storage=mock.Mock())
        job = mock.Mock(task_type="preview", pk=1)
        request = ImageGenerationRequest(
            prompt="p", reference_images=[ReferenceImage("a.jpg", "image/jpeg", b"a")]
        )
        with mock.patch.object(service, "_build_request", return_value=request), mock.patch.object(
            service, "_fail_job"
        ) as fail_job:
            # worker side (async C-1): the failure is recorded on the job, not raised
            self.assertIsNone(service.execute_claimed(job))
        fail_job.assert_called_once()
        self.assertIn("text-to-image only", str(fail_job.call_args.kwargs["exc"]))
        self.assertEqual(recorder.requests, [])


class NoduleCostSafetyTests(SimpleTestCase):
    def test_ambiguous_timeout_after_submit_is_not_retried(self):
        def handler(request):
            raise httpx.ReadTimeout("read timed out", request=request)

        nodule, recorder = provider(handler)
        with self.assertRaises(httpx.ReadTimeout) as ctx:
            nodule.generate_text_to_image(prompt="x")
        self.assertEqual(len(recorder.requests), 1)  # exactly one POST, no double charge
        self.assertEqual(nodule.classify_failure(ctx.exception), "ambiguous")

    def test_connect_error_is_transport_and_single_attempt(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        nodule, recorder = provider(handler)
        with self.assertRaises(httpx.ConnectError) as ctx:
            nodule.generate_text_to_image(prompt="x")
        self.assertEqual(len(recorder.requests), 1)
        self.assertEqual(nodule.classify_failure(ctx.exception), "transport")
        self.assertEqual(classify_provider_failure(nodule, ctx.exception), "transport")


class NoduleCredentialsTests(SimpleTestCase):
    def test_key_never_taken_from_openai_api_key(self):
        env = {"OPENAI_API_KEY": "sk-openai", "NODULE_IMAGE_BASE_URL": BASE_URL}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(ProviderConfigurationError) as ctx:
                NoduleImageProvider()
        self.assertIn("NODULE_IMAGE_API_KEY", str(ctx.exception))

    def test_env_credentials_and_model(self):
        env = {
            "OPENAI_API_KEY": "sk-openai",
            "NODULE_IMAGE_API_KEY": "nodule-env-key",
            "NODULE_IMAGE_BASE_URL": BASE_URL + "/",
            "NODULE_IMAGE_MODEL": "gpt-image-2-custom",
        }
        recorder = Recorder(b64_reply)
        with mock.patch.dict("os.environ", env, clear=True):
            nodule = NoduleImageProvider(transport=httpx.MockTransport(recorder))
        nodule.generate_text_to_image(prompt="x")
        self.assertEqual(nodule.generations_url, f"{BASE_URL}{NODULE_GENERATIONS_PATH}")
        self.assertEqual(recorder.requests[0].headers["authorization"], "Bearer nodule-env-key")
        self.assertNotIn("sk-openai", recorder.requests[0].headers["authorization"])
        self.assertEqual(json.loads(recorder.requests[0].content)["model"], "gpt-image-2-custom")

    def test_missing_base_url_fails_closed(self):
        with mock.patch.dict("os.environ", {"NODULE_IMAGE_API_KEY": KEY}, clear=True):
            with self.assertRaises(ProviderConfigurationError):
                NoduleImageProvider()


class ProviderSelectionTests(SimpleTestCase):
    def test_default_is_openai(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(get_image_provider(), OpenAIImageProvider)
        with mock.patch.dict("os.environ", {"IMAGE_PROVIDER": "OpenAI"}, clear=True):
            self.assertIsInstance(get_image_provider(), OpenAIImageProvider)

    def test_nodule_selected_explicitly(self):
        env = {
            "IMAGE_PROVIDER": "nodule",
            "NODULE_IMAGE_API_KEY": KEY,
            "NODULE_IMAGE_BASE_URL": BASE_URL,
        }
        with mock.patch.dict("os.environ", env, clear=True):
            selected = get_image_provider()
        self.assertIsInstance(selected, NoduleImageProvider)

    def test_unknown_provider_fails_closed_no_fallback(self):
        for value in ("", "   ", "gemini", "openai,nodule"):
            with self.subTest(value=value):
                with mock.patch.dict("os.environ", {"IMAGE_PROVIDER": value}, clear=True):
                    with self.assertRaises(ProviderConfigurationError):
                        get_image_provider()

    def test_nodule_misconfigured_does_not_fall_back_to_openai(self):
        env = {"IMAGE_PROVIDER": "nodule", "OPENAI_API_KEY": "sk-openai"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(ProviderConfigurationError):
                get_image_provider()

    def test_production_console_uses_selection(self):
        from apps.core.production_console import ProductionOrderAdmin
        from apps.core.models import Order

        admin_instance = ProductionOrderAdmin(Order, None)
        env = {
            "IMAGE_PROVIDER": "nodule",
            "NODULE_IMAGE_API_KEY": KEY,
            "NODULE_IMAGE_BASE_URL": BASE_URL,
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertIsInstance(admin_instance.get_generation_service().provider, NoduleImageProvider)
            self.assertIsInstance(
                admin_instance.get_full_production_service().provider, NoduleImageProvider
            )
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(admin_instance.get_generation_service().provider, OpenAIImageProvider)
