from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field, replace
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


def _usage_metadata(usage) -> dict:
    """Token usage from an images response, as plain ints (DRF-2055 cost evidence).

    Accepts the OpenAI SDK usage object (attributes) or a decoded JSON dict
    (OpenAI-compatible providers such as Nodule, DRF-2072).
    """
    if usage is None:
        return {}
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    return result


MODERATION_BLOCKED_CODE = "moderation_blocked"


def _error_payload(exc: Exception) -> dict:
    """The provider's error object from an SDK/httpx failure, {} if none.

    openai.APIStatusError.body is the inner ``error`` dict in the SDK, but a
    full ``{"error": {...}}`` envelope is unwrapped too.
    """
    body = getattr(exc, "body", None)
    if body is None:
        response = getattr(exc, "response", None)
        try:
            body = response.json() if response is not None else None
        except Exception:
            body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    return body if isinstance(body, dict) else {}


def moderation_details(exc: Exception) -> dict | None:
    """Structured facts of a moderation rejection, or None.

    OpenAI answers HTTP 400 ``code: moderation_blocked`` with
    ``moderation_stage`` (input|output) and ``safety_violations`` (categories).
    The request was refused (output stage: generated but withheld) — no
    image, but the call is billable, so the operator must see WHY.
    """
    status = getattr(exc, "status_code", None)
    payload = _error_payload(exc)
    code = getattr(exc, "code", None) or payload.get("code")
    if status != 400 or code != MODERATION_BLOCKED_CODE:
        return None
    categories = payload.get("safety_violations") or payload.get("categories") or []
    if isinstance(categories, str):
        categories = [categories]
    return {
        "moderation_stage": str(payload.get("moderation_stage") or ""),
        "moderation_categories": [str(item) for item in categories],
        "request_id": str(getattr(exc, "request_id", None) or ""),
    }


def classify_provider_failure(provider, exc: Exception) -> str:
    """Best-effort failure classification for retry/cost-safety decisions.

    Providers may expose an optional ``classify_failure`` capability (see
    ``OpenAIImageProvider``); unknown classes and providers without the
    capability map to "unknown", which callers must treat as retryable
    only where a blind retry cannot duplicate billable work.
    """
    classifier = getattr(provider, "classify_failure", None)
    if callable(classifier):
        return classifier(exc)
    return "unknown"


def describe_provider_failure(provider, exc: Exception) -> dict:
    """output_metadata for a failed GenerationJob: failure_class plus the
    moderation facts (stage / categories / request id) when applicable."""
    details = {"failure_class": classify_provider_failure(provider, exc)}
    moderation = moderation_details(exc)
    if moderation:
        details.update(moderation)
    # DRF-2111: keep the provider request id for every definitive answer,
    # not only moderation refusals (billing correlation).
    request_id = getattr(exc, "request_id", None)
    if request_id and not details.get("request_id"):
        details["request_id"] = str(request_id)
    # async C-1: which outbound route carried the failed attempt (evidence;
    # the sanitized proxy identity, never the URL / credentials).
    proxy = getattr(exc, "proxy_identity", None)
    if proxy:
        details["proxy"] = str(proxy)
    return details


# Route label of a direct (no proxy pool) provider call.
PROXY_DIRECT = "direct"


class ProviderConfigurationError(RuntimeError):
    """Provider selection / credentials are missing or unknown: fail closed."""


# Optional images.edit parameters (gpt-image API). Env-gated: an absent /
# empty variable means the parameter is NOT sent and the provider behaves
# exactly as before. Known QC risk (DRF-2052 needs a 512 px side + alpha):
# background=transparent + output_format=png/webp make the raw output
# QC-compatible on alpha; the 512 px side is a separate post-processing
# decision taken after the first real FULL run.
OPENAI_IMAGE_SIZES = frozenset({"1024x1024", "1024x1536", "1536x1024", "auto"})
OPENAI_IMAGE_BACKGROUNDS = frozenset({"transparent", "opaque", "auto"})
OPENAI_IMAGE_OUTPUT_FORMATS = frozenset({"png", "webp", "jpeg"})
# input_fidelity (gpt-image edits): "high" keeps the faces / distinctive
# features of the input images closer to the reference (DRF-2080 likeness
# lever); absent = API default.
OPENAI_IMAGE_INPUT_FIDELITIES = frozenset({"high", "low"})
OPENAI_OUTPUT_MIME_TYPES = {"png": "image/png", "webp": "image/webp", "jpeg": "image/jpeg"}


def _optional_choice(env_name: str, value: str | None, allowed: frozenset) -> str | None:
    """Explicit value wins over env; empty -> None (parameter not sent);
    anything outside ``allowed`` fails closed at provider construction."""
    raw = value if value is not None else os.getenv(env_name, "")
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    if raw not in allowed:
        raise ProviderConfigurationError(
            f"{env_name}={raw!r} is not supported; expected one of {sorted(allowed)}"
        )
    return raw


# Multipart content-type is taken from the explicit tuple, never inferred
# from the filename: MAX photos arrive without an extension, and the SDK
# would otherwise send application/octet-stream -> HTTP 400 "unsupported
# mimetype" (rejected before generation, no charge, but the job fails).
REFERENCE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
REFERENCE_DEFAULT_MIME = "image/jpeg"


def reference_upload(reference: ReferenceImage) -> tuple[str, bytes, str]:
    """SDK file tuple (filename, content, mime_type) for one reference photo.

    mime_type comes from the stored ReferenceImage (falls back to JPEG);
    the filename keeps its own extension, or gets one matching the mime
    type when it has none, so the name and the declared type never disagree.
    """
    mime_type = (reference.mime_type or "").strip().lower() or REFERENCE_DEFAULT_MIME
    filename = (reference.filename or "").strip() or "reference"
    if "." not in filename.rsplit("/", 1)[-1]:
        filename += REFERENCE_EXTENSIONS.get(mime_type, "")
    return filename, reference.content, mime_type


# Explicit OpenAI client timeouts (Order 14, 2026-09-20: two preview jobs were
# killed by gunicorn at 300 s inside images.edit — the SDK default read
# timeout is 600 s, so the hang was never closed by the client itself and the
# jobs went stale → ambiguous with no evidence). The read timeout must be
# shorter than the web worker limit so a hang ends as a clean APITimeoutError
# (classified "ambiguous", fail closed) while the request is still alive.
# Env-tunable so the future background worker can raise the read timeout
# without a code change.
OPENAI_CONNECT_TIMEOUT_S = 10.0
OPENAI_READ_TIMEOUT_S = 240.0
OPENAI_WRITE_TIMEOUT_S = 60.0
OPENAI_POOL_TIMEOUT_S = 10.0

# The SDK retries timeouts / connection errors itself (default max_retries=2).
# A retry after a read timeout re-submits images.edit while the first call
# may still be generating — a blind duplicate billable generation. The
# provider owns the retry decision (classify_failure), so the SDK gets none.
OPENAI_MAX_RETRIES = 0


def _timeout_seconds(env_name: str, default: float) -> float:
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ProviderConfigurationError(f"{env_name}={raw!r} is not a number of seconds") from exc
    if value <= 0:
        raise ProviderConfigurationError(f"{env_name}={raw!r} must be positive")
    return value


def openai_client_timeout():
    """httpx.Timeout for the OpenAI client: connect / read / write / pool."""
    import httpx

    return httpx.Timeout(
        connect=_timeout_seconds("OPENAI_CONNECT_TIMEOUT_S", OPENAI_CONNECT_TIMEOUT_S),
        read=_timeout_seconds("OPENAI_READ_TIMEOUT_S", OPENAI_READ_TIMEOUT_S),
        write=_timeout_seconds("OPENAI_WRITE_TIMEOUT_S", OPENAI_WRITE_TIMEOUT_S),
        pool=_timeout_seconds("OPENAI_POOL_TIMEOUT_S", OPENAI_POOL_TIMEOUT_S),
    )


class OpenAIImageProvider:
    name = "openai"

    def __init__(
        self,
        *,
        client=None,
        model: str | None = None,
        proxy_pool=None,
        client_factory=None,
        size: str | None = None,
        background: str | None = None,
        output_format: str | None = None,
        input_fidelity: str | None = None,
    ):
        self._client = client
        self.model = model or os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
        # Validated once here so a bad env value fails closed before any
        # billable call; None means "do not pass" (current production call).
        self.size = _optional_choice("OPENAI_IMAGE_SIZE", size, OPENAI_IMAGE_SIZES)
        self.background = _optional_choice(
            "OPENAI_IMAGE_BACKGROUND", background, OPENAI_IMAGE_BACKGROUNDS
        )
        self.output_format = _optional_choice(
            "OPENAI_IMAGE_OUTPUT_FORMAT", output_format, OPENAI_IMAGE_OUTPUT_FORMATS
        )
        self.input_fidelity = _optional_choice(
            "OPENAI_IMAGE_INPUT_FIDELITY", input_fidelity, OPENAI_IMAGE_INPUT_FIDELITIES
        )
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

        # timeout is applied per request by the SDK, so it also governs the
        # proxied httpx client; max_retries=0 keeps at-most-once per attempt.
        options = {"timeout": openai_client_timeout(), "max_retries": OPENAI_MAX_RETRIES}
        if proxy_url is None:
            return OpenAI(**options)
        import httpx

        return OpenAI(http_client=httpx.Client(proxy=proxy_url), **options)

    # ----------------------------------------------------------- failover

    @staticmethod
    def classify_failure(exc: Exception) -> str:
        """Classify an SDK failure for failover decisions.

        - "transport": safe pre-request/connect failure — request provably NOT
          accepted by the provider; failover allowed.
        - "geo": definitive 403 unsupported_country_region_territory — the
          provider rejected BEFORE any billable work; failover allowed.
        - "ambiguous": possible post-submit failure (e.g. read timeout) — the
          provider may already be generating; fail closed, NO retry, to avoid
          duplicate billable generations.
        - "moderation": definitive 400 moderation_blocked — the safety system
          refused the input or withheld the output; not a proxy problem, no
          rotation, but the slot stays retryable (another photo / retry).
        - "api": definitive upstream answer (400/401/429/...) — not a proxy
          problem; no rotation.
        """
        import httpx
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        if isinstance(exc, APIStatusError):
            if exc.status_code == 403 and GEO_BLOCK_MARKER in str(exc):
                return "geo"
            if moderation_details(exc) is not None:
                return "moderation"
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

        images = [reference_upload(reference) for reference in request.reference_images]

        if self._pool is None:
            try:
                result = self._generate(self.client, images, request.prompt)
            except Exception as exc:
                exc.proxy_identity = PROXY_DIRECT
                raise
            return self._with_proxy(result, PROXY_DIRECT)

        from apps.core.outbound_proxy import Service

        last_error: Exception | None = None
        for _ in range(len(self._pool)):
            endpoint = self._pool.select(Service.OPENAI)
            if endpoint is None:
                break
            try:
                result = self._generate(self._client_factory(endpoint.url), images, request.prompt)
            except Exception as exc:
                exc.proxy_identity = endpoint.identity
                kind = self.classify_failure(exc)
                if kind in ("transport", "geo"):
                    logger.warning(
                        "openai.failover %s class=%s", endpoint.identity, kind
                    )
                    self._pool.report_failure(endpoint, Service.OPENAI, kind)
                    last_error = exc
                    continue
                raise  # api/ambiguous: no rotation, no blind duplicate
            self._pool.report_success(endpoint, Service.OPENAI)
            return self._with_proxy(result, endpoint.identity)
        raise RuntimeError(
            "OpenAI generation failed: all configured outbound proxies unavailable"
        ) from last_error

    @staticmethod
    def _with_proxy(result: ImageGenerationResult, identity: str) -> ImageGenerationResult:
        """Record the route that carried a successful call (async C-1 evidence)."""
        return replace(result, metadata={**(result.metadata or {}), "proxy": str(identity)})

    def edit_parameters(self) -> dict:
        """Optional images.edit kwargs; only the configured ones are sent."""
        params = {}
        if self.size is not None:
            params["size"] = self.size
        if self.background is not None:
            params["background"] = self.background
        if self.output_format is not None:
            params["output_format"] = self.output_format
        if self.input_fidelity is not None:
            params["input_fidelity"] = self.input_fidelity
        return params

    def _generate(self, client, images, prompt: str) -> ImageGenerationResult:
        response = client.images.edit(
            model=self.model,
            image=images,
            prompt=prompt,
            **self.edit_parameters(),
        )
        data = getattr(response, "data", None) or []
        if not data or not getattr(data[0], "b64_json", None):
            raise RuntimeError("OpenAI image provider returned no image")

        content = base64.b64decode(data[0].b64_json)
        metadata = {"model": self.model, **self.edit_parameters()}
        usage = _usage_metadata(getattr(response, "usage", None))
        if usage:
            metadata["usage"] = usage
        # DRF-2111: provider request id for billing correlation (SDK exposes
        # the x-request-id header as _request_id); absent → not invented.
        request_id = getattr(response, "_request_id", None)
        if request_id:
            metadata["request_id"] = str(request_id)
        return ImageGenerationResult(
            content=content,
            # Without output_format the API returns PNG (current behaviour).
            mime_type=OPENAI_OUTPUT_MIME_TYPES.get(self.output_format or "png", "image/png"),
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Nodule — EXPERIMENTAL TEXT-TO-IMAGE provider (DRF-2072). NOT reference-
# equivalent: Nodule exposes only /v1/images/generations (prompt -> image);
# /v1/images/edits does not exist, so identity-preserving preview / revision /
# FULL generation from customer photos is impossible through it.
# ---------------------------------------------------------------------------

NODULE_DEFAULT_MODEL = "gpt-image-2"
NODULE_GENERATIONS_PATH = "/v1/images/generations"


class ProviderCapabilityError(RuntimeError):
    """The selected provider cannot perform the requested operation: fail closed.

    Raised BEFORE any HTTP call, so nothing is charged.
    """


class NoduleImageProvider:
    """Text-to-image over Nodule's OpenAI-compatible generations endpoint.

    Contract (Agent B discovery, DRF-2044): ``POST {base_url}/v1/images/generations``
    with ``Authorization: Bearer <NODULE_IMAGE_API_KEY>`` and JSON body
    ``{"model", "prompt", "size", "n": 1}``; the answer carries
    ``data[0].b64_json`` (a ``url`` item is also accepted and fetched).

    Credentials come from NODULE_IMAGE_API_KEY / NODULE_IMAGE_BASE_URL only —
    never from OPENAI_API_KEY. Any request that carries reference images is
    refused with ProviderCapabilityError before a request is made; there is
    no fallback to another provider and no retry (one POST per call, so an
    ambiguous failure can never turn into a double charge).
    """

    name = "nodule"
    supports_text_to_image = True
    supports_reference_generation = False

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        size: str = "1024x1024",
        timeout: float = 120.0,
        transport=None,
    ):
        self.api_key = api_key if api_key is not None else os.getenv("NODULE_IMAGE_API_KEY", "")
        self.base_url = (
            base_url if base_url is not None else os.getenv("NODULE_IMAGE_BASE_URL", "")
        ).rstrip("/")
        self.model = model or os.getenv("NODULE_IMAGE_MODEL", NODULE_DEFAULT_MODEL)
        self.size = size
        self.timeout = timeout
        # httpx transport injectable for tests (httpx.MockTransport); the real
        # client is built lazily so constructing the provider never connects.
        self._transport = transport
        if not self.api_key:
            raise ProviderConfigurationError(
                "NODULE_IMAGE_API_KEY is not set (the Nodule key is never read from OPENAI_API_KEY)"
            )
        if not self.base_url:
            raise ProviderConfigurationError("NODULE_IMAGE_BASE_URL is not set")

    # ------------------------------------------------------------ http

    @property
    def generations_url(self) -> str:
        return f"{self.base_url}{NODULE_GENERATIONS_PATH}"

    def _client(self):
        import httpx

        return httpx.Client(timeout=self.timeout, transport=self._transport)

    @staticmethod
    def classify_failure(exc: Exception) -> str:
        """Same vocabulary as OpenAIImageProvider.classify_failure.

        "transport": connect-level failure, request provably not accepted;
        "ambiguous": failure after submit (read/write/pool timeout, dropped
        connection) — the image may have been generated and charged;
        "api": definitive HTTP answer from Nodule (4xx/5xx).
        """
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            return "api"
        if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
            return "transport"
        if isinstance(exc, httpx.TransportError):
            return "ambiguous"
        return "ambiguous"

    # -------------------------------------------------------- contract

    def generate_preview(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        """ImageProvider protocol entry used by GenerationService /
        FullProductionService. Those flows are personalised (reference
        photos), which Nodule cannot honour: fail closed, no HTTP call."""
        if request.reference_images:
            raise ProviderCapabilityError(
                "Nodule provider is text-to-image only and cannot use reference "
                "images; personalised generation is refused (no fallback)"
            )
        return self.generate_text_to_image(prompt=request.prompt, metadata=request.metadata)

    def generate_text_to_image(
        self, *, prompt: str, metadata: dict | None = None
    ) -> ImageGenerationResult:
        """One POST to /v1/images/generations; never retried."""
        if not prompt or not prompt.strip():
            raise ValueError("Prompt is required for text-to-image generation")
        payload = {"model": self.model, "prompt": prompt, "size": self.size, "n": 1}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with self._client() as client:
            response = client.post(self.generations_url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            if not data or not isinstance(data[0], dict):
                raise RuntimeError("Nodule image provider returned no image")
            item = data[0]
            if item.get("b64_json"):
                content = base64.b64decode(item["b64_json"])
                mime_type = "image/png"
            elif item.get("url"):
                # The asset URL is a foreign location: fetch it WITHOUT the
                # bearer token so the key is never sent to a third party.
                fetched = client.get(item["url"])
                fetched.raise_for_status()
                content = fetched.content
                mime_type = fetched.headers.get("content-type", "image/png").split(";")[0]
            else:
                raise RuntimeError("Nodule image provider returned no image")
        if not content:
            raise RuntimeError("Nodule image provider returned empty content")
        result_metadata = {
            "provider": self.name,
            "model": self.model,
            "size": self.size,
            "endpoint": NODULE_GENERATIONS_PATH,
            "experimental": True,
            "reference_equivalent": False,
        }
        usage = _usage_metadata(body.get("usage") if isinstance(body.get("usage"), dict) else None)
        if usage:
            result_metadata["usage"] = usage
        return ImageGenerationResult(content=content, mime_type=mime_type, metadata=result_metadata)


# ---------------------------------------------------------------------------
# Provider selection — deterministic, no fallback.
# ---------------------------------------------------------------------------

IMAGE_PROVIDER_ENV = "IMAGE_PROVIDER"
IMAGE_PROVIDER_DEFAULT = "openai"


def get_image_provider(name: str | None = None) -> ImageProvider:
    """Build the configured image provider.

    IMAGE_PROVIDER=openai (default) -> OpenAIImageProvider (reference-based,
    the paid production path); IMAGE_PROVIDER=nodule -> NoduleImageProvider
    (experimental text-to-image; personalised flows fail closed). Anything
    else raises ProviderConfigurationError — there is deliberately no
    "unknown -> openai" fallback so a misconfiguration cannot silently pick
    a provider.
    """
    selected = (name if name is not None else os.getenv(IMAGE_PROVIDER_ENV, IMAGE_PROVIDER_DEFAULT))
    selected = (selected or "").strip().lower()
    if selected == "openai":
        return OpenAIImageProvider()
    if selected == "nodule":
        return NoduleImageProvider()
    raise ProviderConfigurationError(
        f"Unknown IMAGE_PROVIDER {selected!r}; expected 'openai' or 'nodule'"
    )
