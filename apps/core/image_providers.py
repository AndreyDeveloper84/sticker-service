from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from io import BytesIO
from typing import Protocol

logger = logging.getLogger(__name__)

# OpenAI rejection meaning the egress IP country is blocked; the proxy used is
# unfit for OpenAI (but may stay healthy for Telegram — health is per service).
GEO_BLOCK_MARKER = "unsupported_country_region_territory"


@dataclass(frozen=True)
class ReferenceImage:
    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class ImageGenerationRequest:
    prompt: str
    reference_images: list[ReferenceImage]
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ImageGenerationResult:
    content: bytes
    mime_type: str = "image/png"
    metadata: dict = field(default_factory=dict)


class ImageProvider(Protocol):
    name: str

    def generate_preview(self, request: ImageGenerationRequest) -> ImageGenerationResult: ...


class OpenAIImageProvider:
    name = "openai"

    def __init__(self, *, client=None, model: str | None = None, proxy_pool=None, client_factory=None):
        self._client = client
        self.model = model or os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
        # Outbound proxy pool: None = direct (current production behaviour).
        # When a pool is configured, each generation attempt goes through a
        # selected healthy proxy with failover (see generate_preview).
        if proxy_pool is not None:
            self._pool = proxy_pool
        else:
            from apps.core.outbound_proxy import get_proxy_pool

            self._pool = get_proxy_pool()
        # client_factory(proxy_url) -> OpenAI-compatible client; injectable for
        # tests. Default builds the real SDK client with an httpx proxy client.
        self._client_factory = client_factory or self._default_client_factory

    @property
    def client(self):
        if self._client is None:
            self._client = self._client_factory(None)
        return self._client

    @staticmethod
    def _default_client_factory(proxy_url: str | None):
        from openai import OpenAI

        if proxy_url is None:
            return OpenAI()
        import httpx

        return OpenAI(http_client=httpx.Client(proxy=proxy_url))

    # ----------------------------------------------------------- failover

    @staticmethod
    def _classify_failure(exc: Exception) -> str:
        """Classify an SDK failure for failover decisions.

        - "transport": safe pre-request/connect failure — request provably NOT
          accepted by the provider; failover allowed.
        - "geo": definitive 403 unsupported_country_region_territory — the
          provider rejected BEFORE any billable work; failover allowed.
        - "ambiguous": possible post-submit failure (e.g. read timeout) — the
          provider may already be generating; fail closed, NO retry, to avoid
          duplicate billable generations.
        - "api": definitive upstream answer (400/401/429/...) — not a proxy
          problem; no rotation.
        """
        import httpx
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        if isinstance(exc, APIStatusError):
            if exc.status_code == 403 and GEO_BLOCK_MARKER in str(exc):
                return "geo"
            return "api"
        if isinstance(exc, APITimeoutError):
            # Connect timeout = never reached the provider; read/write/pool
            # timeout after submit is ambiguous.
            return "transport" if isinstance(exc.__cause__, httpx.ConnectTimeout) else "ambiguous"
        if isinstance(exc, APIConnectionError):
            return "transport" if isinstance(exc.__cause__, httpx.ConnectError) else "ambiguous"
        return "ambiguous"

    def generate_preview(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        if not request.reference_images:
            raise ValueError("At least one reference image is required")

        images = []
        for reference in request.reference_images:
            buffer = BytesIO(reference.content)
            buffer.name = reference.filename or "reference.png"
            images.append(buffer)

        if self._pool is None:
            return self._generate(self.client, images, request.prompt)

        from apps.core.outbound_proxy import Service

        last_error: Exception | None = None
        for _ in range(len(self._pool)):
            endpoint = self._pool.select(Service.OPENAI)
            if endpoint is None:
                break
            try:
                result = self._generate(self._client_factory(endpoint.url), images, request.prompt)
            except Exception as exc:
                kind = self._classify_failure(exc)
                if kind in ("transport", "geo"):
                    logger.warning(
                        "openai.failover %s class=%s", endpoint.identity, kind
                    )
                    self._pool.report_failure(endpoint, Service.OPENAI, kind)
                    last_error = exc
                    continue
                raise  # api/ambiguous: no rotation, no blind duplicate
            self._pool.report_success(endpoint, Service.OPENAI)
            return result
        raise RuntimeError(
            "OpenAI generation failed: all configured outbound proxies unavailable"
        ) from last_error

    def _generate(self, client, images, prompt: str) -> ImageGenerationResult:
        response = client.images.edit(
            model=self.model,
            image=images,
            prompt=prompt,
        )
        data = getattr(response, "data", None) or []
        if not data or not getattr(data[0], "b64_json", None):
            raise RuntimeError("OpenAI image provider returned no image")

        content = base64.b64decode(data[0].b64_json)
        return ImageGenerationResult(
            content=content,
            mime_type="image/png",
            metadata={"model": self.model},
        )
