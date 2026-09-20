"""Photo Suitability Gate (DRF-2164): reject an obviously unusable source
photo BEFORE payment and before any billable generation.

Order 14 accepted a 269×576 image; media.py checked only the MIME type and
the byte size. The gate decodes the image (Pillow only — no OpenCV / numpy)
and measures:

- decode: Pillow ``verify()`` + a real ``load()`` (truncated / not-an-image
  / decompression bomb → rejected); the real container format is recorded
  and wins over the declared MIME type (MAX guesses it from the file name);
- min side (PHOTO_MIN_SIDE, default 512 px): gpt-image edits keep the
  identity from the reference (DRF-2080 input fidelity); a face inside a
  <512 px frame is ~150 px of usable detail, which is what produced the
  weak likeness on Order 14. Telegram/MAX deliver phone photos at
  ≥ 1000 px, so 512 only stops thumbnails / avatars / screenshots;
- aspect (PHOTO_MAX_ASPECT, default 2.5): stories and 21:9 crops (≤ 2.33)
  pass, panoramas and strips do not — a head-and-shoulders portrait is
  not recoverable from a strip (critical crop);
- blur (PHOTO_BLUR_MIN_VARIANCE, default 30): variance of the Laplacian on
  the grayscale image normalised to a 512 px long side, measured on the
  whole frame and on the central 60 % (where the face usually is); the
  larger of the two must reach the threshold. Calibrated on synthetic
  portraits: sharp ≈ 400–1000, JPEG q70 unchanged, Gaussian r=2 ≈ 120,
  r=3 ≈ 25, r≥4 < 10, flat colour = 0. The threshold was calibrated on
  synthetic portraits only (no real photos offline), so it is NOT enforced
  by default: PHOTO_BLUR_MODE=observe (default) accepts the photo, stores
  the metric in OrderPhoto.metadata and logs ``photo_gate.blur_observed``
  when it falls below the threshold; PHOTO_BLUR_MODE=enforce rejects.
  Decision rule: after >= 20 live photos, pick the threshold from
  metadata.gate.blur_variance* and switch to enforce. Decode, min side,
  aspect and the bomb guard are always enforced.

Face count is NOT implemented: there is no light-weight face detector in
Pillow, and OpenCV / mediapipe are heavy dependencies the pilot does not
carry. Follow-up: DRF-2164-b (a Haar cascade via opencv-python-headless,
~50 MB, only if live evidence shows multi-face / no-face photos slipping
through).
"""

from __future__ import annotations

import logging
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageFilter, ImageStat

from apps.core.services.flow_errors import ChannelFlowError

logger = logging.getLogger(__name__)

GATE_VERSION = 1

# Container formats Pillow reports for the accepted MIME types. MPO is the
# multi-picture JPEG produced by iPhones — a JPEG for every consumer.
FORMAT_MIME = {"JPEG": "image/jpeg", "MPO": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

# Laplacian kernel; offset 128 keeps the negative responses (8-bit output
# would otherwise clip them to 0 and halve the signal).
_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)
BLUR_NORMALISED_SIDE = 512
BLUR_CENTER_FRACTION = 0.6


class PhotoRejected(ChannelFlowError):
    """A source photo the gate refused. ``reason`` is a stable code (evidence
    / tests), ``hint`` the Russian sentence shown to the customer,
    ``metrics`` what was measured (logged; never stored — the photo is not)."""

    def __init__(self, reason: str, hint: str, metrics: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.metrics = metrics or {}


def _setting(name: str, default):
    return getattr(settings, name, default)


def gate_enabled() -> bool:
    return bool(_setting("PHOTO_GATE_ENABLED", True))


BLUR_MODE_OBSERVE = "observe"
BLUR_MODE_ENFORCE = "enforce"


def blur_mode() -> str:
    mode = str(_setting("PHOTO_BLUR_MODE", BLUR_MODE_OBSERVE)).strip().lower()
    return BLUR_MODE_ENFORCE if mode == BLUR_MODE_ENFORCE else BLUR_MODE_OBSERVE


def thresholds() -> dict:
    return {
        "min_side": int(_setting("PHOTO_MIN_SIDE", 512)),
        "max_aspect": float(_setting("PHOTO_MAX_ASPECT", 2.5)),
        "blur_min_variance": float(_setting("PHOTO_BLUR_MIN_VARIANCE", 30.0)),
        "blur_mode": blur_mode(),
    }


def _laplacian_variance(gray: Image.Image) -> float:
    edges = gray.filter(_LAPLACIAN)
    width, height = edges.size
    if width < 3 or height < 3:
        return 0.0
    # the 1-px border is left unfiltered by Pillow: exclude it
    return float(ImageStat.Stat(edges.crop((1, 1, width - 1, height - 1))).var[0])


def blur_metrics(image: Image.Image) -> tuple[float, float]:
    """(whole-frame, central-crop) Laplacian variance on the grayscale image
    normalised to BLUR_NORMALISED_SIDE — resolution independent."""
    gray = image.convert("L")
    width, height = gray.size
    scale = BLUR_NORMALISED_SIDE / max(width, height)
    if scale < 1:
        gray = gray.resize((max(3, round(width * scale)), max(3, round(height * scale))), Image.LANCZOS)
    width, height = gray.size
    crop_w, crop_h = max(3, int(width * BLUR_CENTER_FRACTION)), max(3, int(height * BLUR_CENTER_FRACTION))
    left, top = (width - crop_w) // 2, (height - crop_h) // 2
    center = gray.crop((left, top, left + crop_w, top + crop_h))
    return round(_laplacian_variance(gray), 1), round(_laplacian_variance(center), 1)


def hint_too_small(min_side: int, width: int, height: int) -> str:
    return (
        f"Фото слишком маленькое ({width}×{height}). Нужна сторона не меньше {min_side} px — "
        "отправьте оригинал с телефона, а не сжатую копию, скриншот или аватар."
    )


HINT_UNREADABLE = "Не удалось прочитать это изображение. Отправьте фото ещё раз в формате JPEG, PNG или WebP."
HINT_TOO_LARGE_DIMENSIONS = "Изображение слишком большое по размеру в пикселях. Отправьте обычное фото с телефона."
HINT_BAD_ASPECT = (
    "Фото слишком вытянутое — лицо и плечи на нём не поместятся. "
    "Отправьте обычный вертикальный или квадратный кадр без панорам и узких полос."
)
HINT_BLURRY = (
    "Фото размытое или не в фокусе — по нему не получится сохранить сходство. "
    "Отправьте чёткий кадр при хорошем свете."
)


def inspect_photo(content: bytes) -> dict:
    """Decode + measure. Raises PhotoRejected; returns the metrics dict that
    goes into OrderPhoto.metadata["gate"] for an accepted photo."""
    limits = thresholds()
    try:
        probe = Image.open(BytesIO(content))
        probe.verify()
        image = Image.open(BytesIO(content))
        image.load()
    except Image.DecompressionBombError:
        raise PhotoRejected("Photo dimensions are too large", HINT_TOO_LARGE_DIMENSIONS)
    except Exception as exc:  # UnidentifiedImageError, OSError (truncated), SyntaxError (verify)
        raise PhotoRejected("Photo cannot be decoded", HINT_UNREADABLE, {"decode_error": exc.__class__.__name__})

    fmt = (image.format or "").upper()
    if fmt not in FORMAT_MIME:
        raise PhotoRejected("Photo format is not supported", HINT_UNREADABLE, {"format": fmt})

    width, height = image.size
    min_side, max_side = min(width, height), max(width, height)
    aspect = round(max_side / min_side, 2) if min_side else 0.0
    metrics = {
        "version": GATE_VERSION,
        "format": fmt,
        "width": width,
        "height": height,
        "min_side": min_side,
        "aspect": aspect,
        "thresholds": limits,
    }
    if min_side < limits["min_side"]:
        raise PhotoRejected("Photo is too small", hint_too_small(limits["min_side"], width, height), metrics)
    if aspect > limits["max_aspect"]:
        raise PhotoRejected("Photo aspect ratio is too extreme", HINT_BAD_ASPECT, metrics)

    whole, center = blur_metrics(image)
    metrics.update({"blur_variance": whole, "blur_variance_center": center})
    if max(whole, center) < limits["blur_min_variance"]:
        if limits["blur_mode"] == BLUR_MODE_ENFORCE:
            raise PhotoRejected("Photo is too blurry", HINT_BLURRY, metrics)
        # observe: accept, keep the evidence, make the miss visible
        metrics["blur_below_threshold"] = True
        logger.warning(
            "photo_gate.blur_observed variance=%s center=%s threshold=%s size=%sx%s",
            whole, center, limits["blur_min_variance"], width, height,
        )

    metrics["checked_at"] = timezone.now().isoformat()
    return metrics
