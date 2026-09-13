from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from io import BytesIO
from typing import Protocol


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

    def __init__(self, *, client=None, model: str | None = None):
        self._client = client
        self.model = model or os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI()
        return self._client

    def generate_preview(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        if not request.reference_images:
            raise ValueError("At least one reference image is required")

        images = []
        for reference in request.reference_images:
            buffer = BytesIO(reference.content)
            buffer.name = reference.filename or "reference.png"
            images.append(buffer)

        response = self.client.images.edit(
            model=self.model,
            image=images,
            prompt=request.prompt,
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
