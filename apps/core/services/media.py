from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.core.models import OrderPhoto
from apps.core.storage import LocalMediaStorage


ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


class MediaService:
    def __init__(self, storage=None):
        self.storage = storage

    def _storage(self):
        return self.storage or LocalMediaStorage()

    def save_order_photo(self, *, order, file, original_filename=None, mime_type=None):
        mime_type = (mime_type or getattr(file, "content_type", "") or "").lower()
        if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
            raise ValidationError("Unsupported image MIME type")

        size_bytes = getattr(file, "size", None)
        if size_bytes is None:
            current_position = file.tell()
            file.seek(0, 2)
            size_bytes = file.tell()
            file.seek(current_position)

        max_size = getattr(settings, "ORDER_PHOTO_MAX_BYTES", 20 * 1024 * 1024)
        if size_bytes <= 0 or size_bytes > max_size:
            raise ValidationError("Image size is outside allowed limits")

        original_filename = original_filename or getattr(file, "name", "") or "photo"
        suffix = Path(original_filename).suffix.lower()
        storage_key = f"orders/{order.pk}/source/{uuid4().hex}{suffix}"

        if hasattr(file, "seek"):
            file.seek(0)
        self._storage().save(storage_key, file)

        return OrderPhoto.objects.create(
            order=order,
            storage_key=storage_key,
            original_filename=original_filename[:255],
            mime_type=mime_type,
            size_bytes=size_bytes,
        )
