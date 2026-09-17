import base64
from types import SimpleNamespace
from unittest import mock

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


class OpenAIProxyPoolFailoverTests(SimpleTestCase):
    """Pool integration for OpenAI generation.

    Failover allowed only for safe pre-request transport failures and
    definitive geo blocks; upstream answers (401/429) and ambiguous
    post-submit failures never rotate and never duplicate a request.
    """

    PROXY_A = "http://user-a:secret-a@proxy-a.example:3128"
    PROXY_B = "http://user-b:secret-b@proxy-b.example:3128"

    def _pool(self):
        import json

        from apps.core.outbound_proxy import ProxyPool, parse_proxy_urls

        return ProxyPool(parse_proxy_urls(json.dumps([self.PROXY_A, self.PROXY_B])), cooldown_seconds=60.0)

    def _request(self):
        return ImageGenerationRequest(
            prompt="Preserve likeness",
            reference_images=[ReferenceImage("one.jpg", "image/jpeg", b"one")],
        )

    def _ok_client(self):
        encoded = base64.b64encode(b"img").decode("ascii")
        return SimpleNamespace(
            images=SimpleNamespace(
                edit=lambda **kwargs: SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])
            )
        )

    def _raise_with_cause(self, exc, cause):
        try:
            raise exc from cause
        except Exception as raised:
            return raised

    def _api_request(self):
        import httpx

        return httpx.Request("POST", "https://api.openai.com/v1/images/edits")

    def _status_error(self, status, message="error"):
        import httpx
        from openai import APIStatusError

        response = httpx.Response(status, request=self._api_request())
        return APIStatusError(message, response=response, body=None)

    def _factory(self, behavior_by_url):
        calls = []

        def factory(proxy_url):
            calls.append(proxy_url)
            behavior = behavior_by_url[proxy_url]
            if isinstance(behavior, Exception):
                return SimpleNamespace(
                    images=SimpleNamespace(edit=mock.Mock(side_effect=behavior))
                )
            return behavior

        return factory, calls

    def test_proxy_client_passed_to_sdk(self):
        from unittest import mock as _mock

        with _mock.patch("httpx.Client") as httpx_cls, _mock.patch("openai.OpenAI") as openai_cls:
            OpenAIImageProvider._default_client_factory(self.PROXY_A)
        httpx_cls.assert_called_once_with(proxy=self.PROXY_A)
        openai_cls.assert_called_once_with(http_client=httpx_cls.return_value)

    def test_direct_factory_no_proxy(self):
        from unittest import mock as _mock

        with _mock.patch("httpx.Client") as httpx_cls, _mock.patch("openai.OpenAI") as openai_cls:
            OpenAIImageProvider._default_client_factory(None)
        httpx_cls.assert_not_called()
        openai_cls.assert_called_once_with()

    def test_geo_block_fails_over_to_second_proxy(self):
        pool = self._pool()
        geo = self._status_error(403, "Request not allowed: unsupported_country_region_territory")
        factory, calls = self._factory({self.PROXY_A: geo, self.PROXY_B: self._ok_client()})
        provider = OpenAIImageProvider(model="m", proxy_pool=pool, client_factory=factory)

        result = provider.generate_preview(self._request())

        self.assertEqual(result.content, b"img")
        self.assertEqual(calls, [self.PROXY_A, self.PROXY_B])
        from apps.core.outbound_proxy import Service

        # geo block affects only the openai service health of proxy A
        self.assertEqual(pool.state(pool.endpoints[0], Service.OPENAI), "COOLDOWN")
        self.assertEqual(pool.state(pool.endpoints[0], Service.TELEGRAM), "HEALTHY")
        self.assertEqual(pool.state(pool.endpoints[1], Service.OPENAI), "HEALTHY")

    def test_connect_failure_fails_over(self):
        import httpx
        from openai import APIConnectionError

        pool = self._pool()
        exc = self._raise_with_cause(
            APIConnectionError(message="conn", request=self._api_request()),
            httpx.ConnectError("refused"),
        )
        factory, calls = self._factory({self.PROXY_A: exc, self.PROXY_B: self._ok_client()})
        provider = OpenAIImageProvider(model="m", proxy_pool=pool, client_factory=factory)

        result = provider.generate_preview(self._request())

        self.assertEqual(result.content, b"img")
        self.assertEqual(calls, [self.PROXY_A, self.PROXY_B])

    def test_connect_timeout_fails_over(self):
        import httpx
        from openai import APITimeoutError

        pool = self._pool()
        exc = self._raise_with_cause(
            APITimeoutError(request=self._api_request()),
            httpx.ConnectTimeout("slow connect"),
        )
        factory, calls = self._factory({self.PROXY_A: exc, self.PROXY_B: self._ok_client()})
        provider = OpenAIImageProvider(model="m", proxy_pool=pool, client_factory=factory)

        provider.generate_preview(self._request())
        self.assertEqual(calls, [self.PROXY_A, self.PROXY_B])

    def test_401_does_not_rotate(self):
        pool = self._pool()
        exc = self._status_error(401, "Incorrect API key")
        factory, calls = self._factory({self.PROXY_A: exc, self.PROXY_B: self._ok_client()})
        provider = OpenAIImageProvider(model="m", proxy_pool=pool, client_factory=factory)

        with self.assertRaises(Exception):
            provider.generate_preview(self._request())
        self.assertEqual(calls, [self.PROXY_A])
        from apps.core.outbound_proxy import Service

        self.assertEqual(pool.state(pool.endpoints[0], Service.OPENAI), "HEALTHY")

    def test_429_does_not_rotate(self):
        pool = self._pool()
        exc = self._status_error(429, "Rate limit reached")
        factory, calls = self._factory({self.PROXY_A: exc, self.PROXY_B: self._ok_client()})
        provider = OpenAIImageProvider(model="m", proxy_pool=pool, client_factory=factory)

        with self.assertRaises(Exception):
            provider.generate_preview(self._request())
        self.assertEqual(calls, [self.PROXY_A])

    def test_ambiguous_read_timeout_fails_closed_no_duplicate(self):
        import httpx
        from openai import APITimeoutError

        pool = self._pool()
        exc = self._raise_with_cause(
            APITimeoutError(request=self._api_request()),
            httpx.ReadTimeout("response never arrived"),
        )
        factory, calls = self._factory({self.PROXY_A: exc, self.PROXY_B: self._ok_client()})
        provider = OpenAIImageProvider(model="m", proxy_pool=pool, client_factory=factory)

        with self.assertRaises(Exception):
            provider.generate_preview(self._request())
        # NO blind retry: provider may already be generating (billable)
        self.assertEqual(calls, [self.PROXY_A])

    def test_all_proxies_exhausted(self):
        import httpx
        from openai import APIConnectionError

        pool = self._pool()
        exc = self._raise_with_cause(
            APIConnectionError(message="conn", request=self._api_request()),
            httpx.ConnectError("refused"),
        )
        factory, calls = self._factory({self.PROXY_A: exc, self.PROXY_B: exc})
        provider = OpenAIImageProvider(model="m", proxy_pool=pool, client_factory=factory)

        with self.assertRaises(RuntimeError) as ctx:
            provider.generate_preview(self._request())
        self.assertEqual(calls, [self.PROXY_A, self.PROXY_B])
        message = str(ctx.exception)
        for leaked in ("secret-a", "secret-b", "user-a", "user-b"):
            self.assertNotIn(leaked, message)

    def test_no_pool_direct_behaviour_unchanged(self):
        provider = OpenAIImageProvider(client=self._ok_client(), model="m", proxy_pool=None)
        # proxy_pool=None resolves the (disabled-by-default) process pool
        result = provider.generate_preview(self._request())
        self.assertEqual(result.content, b"img")


class OpenAIReferenceUploadTests(SimpleTestCase):
    """Hotfix: reference photos must carry their stored mime type explicitly.

    MAX photos have no filename extension; the SDK infers the multipart
    content-type from the name and sent application/octet-stream -> 400
    "unsupported mimetype" (Order 8, GenerationJob 3). The tuple form
    (filename, content, mime_type) makes the declared type authoritative.
    """

    def _edit_call(self, *references):
        class Images:
            def __init__(self):
                self.kwargs = None

            def edit(self, **kwargs):
                self.kwargs = kwargs
                encoded = base64.b64encode(b"img").decode("ascii")
                return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

        images = Images()
        provider = OpenAIImageProvider(
            client=SimpleNamespace(images=images), model="test-model", proxy_pool=None
        )
        provider.generate_preview(ImageGenerationRequest(prompt="p", reference_images=list(references)))
        return images.kwargs["image"]

    def test_filename_without_extension_gets_jpeg_extension_and_mime(self):
        parts = self._edit_call(ReferenceImage("photo-7", "image/jpeg", b"jpg-bytes"))
        self.assertEqual(parts, [("photo-7.jpg", b"jpg-bytes", "image/jpeg")])

    def test_filename_with_extension_is_preserved(self):
        parts = self._edit_call(
            ReferenceImage("selfie.JPG", "image/jpeg", b"a"),
            ReferenceImage("mask.png", "image/png", b"b"),
            ReferenceImage("dir.name/no-ext", "image/webp", b"c"),
        )
        self.assertEqual(
            parts,
            [
                ("selfie.JPG", b"a", "image/jpeg"),
                ("mask.png", b"b", "image/png"),
                ("dir.name/no-ext.webp", b"c", "image/webp"),
            ],
        )

    def test_missing_mime_and_name_fall_back_to_jpeg(self):
        parts = self._edit_call(ReferenceImage("", "", b"x"))
        self.assertEqual(parts, [("reference.jpg", b"x", "image/jpeg")])

    def test_mime_is_normalised_and_unknown_mime_keeps_bare_name(self):
        parts = self._edit_call(ReferenceImage("photo", " IMAGE/PNG ", b"x"))
        self.assertEqual(parts, [("photo.png", b"x", "image/png")])
        parts = self._edit_call(ReferenceImage("scan", "image/heic", b"y"))
        self.assertEqual(parts, [("scan", b"y", "image/heic")])

    def test_tuple_is_accepted_by_sdk_multipart_encoder(self):
        from openai._files import to_httpx_files

        from apps.core.image_providers import reference_upload

        part = reference_upload(ReferenceImage("photo-7", "image/jpeg", b"jpg-bytes"))
        self.assertEqual(to_httpx_files({"image": part})["image"], part)
