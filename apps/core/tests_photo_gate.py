"""DRF-2164 Photo Suitability Gate: decode, min side, aspect, blur — every
rejection, the boundaries, and good photos passing with metrics recorded.
"""

import os
import random
from functools import lru_cache
from io import BytesIO
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image, ImageDraw, ImageFilter

from apps.core.customer_hints import customer_hint
from apps.core.models import ChannelIdentity, Order, OrderPhoto, Product, Style, User
from apps.core.services.channel_order_flow import ChannelFlowError
from apps.core.services.media import MediaService
from apps.core.services.photo_gate import (
    HINT_BAD_ASPECT,
    HINT_BLURRY,
    HINT_UNREADABLE,
    PhotoRejected,
    blur_metrics,
    hint_too_small,
    inspect_photo,
)


def portrait_image(size=(1200, 1600), seed=1):
    """A synthetic, sharp, portrait-like picture: gradient background, a
    face-shaped ellipse with eyes, mouth and hair strokes (texture)."""
    random.seed(seed)
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=(200 - y // 12 % 120, 210 - y // 15 % 120, 230 - y // 10 % 120))
    draw.ellipse([width * 0.3, height * 0.2, width * 0.7, height * 0.6], fill=(224, 180, 150), outline=(120, 80, 60), width=6)
    draw.ellipse([width * 0.4, height * 0.33, width * 0.46, height * 0.36], fill=(40, 30, 30))
    draw.ellipse([width * 0.54, height * 0.33, width * 0.6, height * 0.36], fill=(40, 30, 30))
    draw.arc([width * 0.42, height * 0.45, width * 0.58, height * 0.52], 10, 170, fill=(150, 60, 60), width=5)
    for _ in range(400):
        x, y = random.uniform(width * 0.3, width * 0.7), random.uniform(height * 0.18, height * 0.26)
        draw.line([(x, y), (x + random.uniform(-15, 15), y + random.uniform(5, 25))], fill=(60, 40, 20), width=2)
    return image


def encode(image, fmt="JPEG", **kwargs):
    buffer = BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


@lru_cache(maxsize=8)
def good_photo_bytes(fmt="JPEG", size=(1200, 1600)):
    """A photo the gate accepts — for every test that uploads a source photo."""
    return encode(portrait_image(size), fmt, **({"quality": 85} if fmt == "JPEG" else {}))


class InspectPhotoTests(SimpleTestCase):
    def test_good_jpeg_passes_with_metrics(self):
        metrics = inspect_photo(good_photo_bytes())
        self.assertEqual((metrics["format"], metrics["width"], metrics["height"]), ("JPEG", 1200, 1600))
        self.assertEqual(metrics["min_side"], 1200)
        self.assertEqual(metrics["aspect"], 1.33)
        self.assertGreater(metrics["blur_variance_center"], 100)
        self.assertEqual(metrics["thresholds"], {"min_side": 512, "max_aspect": 2.5, "blur_min_variance": 30.0})
        self.assertIn("checked_at", metrics)

    def test_png_and_webp_pass(self):
        for fmt in ("PNG", "WEBP"):
            with self.subTest(fmt=fmt):
                self.assertEqual(inspect_photo(good_photo_bytes(fmt))["format"], fmt)

    def test_not_an_image_and_truncated_are_rejected(self):
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(b"image-bytes")
        self.assertEqual(ctx.exception.reason, "Photo cannot be decoded")
        self.assertEqual(ctx.exception.hint, HINT_UNREADABLE)
        truncated = good_photo_bytes()[: 40 * 1024]
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(truncated)
        self.assertEqual(ctx.exception.reason, "Photo cannot be decoded")

    def test_unsupported_container_is_rejected(self):
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(encode(portrait_image((800, 800)), "BMP"))
        self.assertEqual(ctx.exception.reason, "Photo format is not supported")

    def test_order_14_shape_is_too_small(self):
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(encode(portrait_image((269, 576))))
        self.assertEqual(ctx.exception.reason, "Photo is too small")
        self.assertEqual(ctx.exception.hint, hint_too_small(512, 269, 576))
        self.assertEqual(ctx.exception.metrics["min_side"], 269)

    def test_min_side_boundary(self):
        self.assertEqual(inspect_photo(encode(portrait_image((512, 900))))["min_side"], 512)
        with self.assertRaises(PhotoRejected):
            inspect_photo(encode(portrait_image((511, 900))))

    @override_settings(PHOTO_MIN_SIDE=1000)
    def test_min_side_from_settings(self):
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(encode(portrait_image((900, 1200))))
        self.assertIn("не меньше 1000 px", ctx.exception.hint)

    def test_aspect_boundary(self):
        self.assertEqual(inspect_photo(encode(portrait_image((600, 1500))))["aspect"], 2.5)  # 2.5 passes
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(encode(portrait_image((600, 1560))))  # 2.6
        self.assertEqual(ctx.exception.reason, "Photo aspect ratio is too extreme")
        self.assertEqual(ctx.exception.hint, HINT_BAD_ASPECT)
        with self.assertRaises(PhotoRejected):
            inspect_photo(encode(portrait_image((2000, 600))))  # landscape strip 3.33

    def test_blurred_is_rejected_light_blur_and_jpeg_compression_pass(self):
        sharp = portrait_image()
        self.assertGreater(inspect_photo(encode(sharp, quality=60))["blur_variance_center"], 100)
        self.assertGreater(inspect_photo(encode(sharp.filter(ImageFilter.GaussianBlur(2))))["blur_variance_center"], 30)
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(encode(sharp.filter(ImageFilter.GaussianBlur(4))))
        self.assertEqual(ctx.exception.reason, "Photo is too blurry")
        self.assertEqual(ctx.exception.hint, HINT_BLURRY)
        self.assertLess(ctx.exception.metrics["blur_variance"], 30)

    def test_flat_colour_is_rejected_as_blurry(self):
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(encode(Image.new("RGB", (1000, 1000), (180, 180, 180))))
        self.assertEqual(ctx.exception.reason, "Photo is too blurry")
        self.assertEqual(ctx.exception.metrics["blur_variance"], 0.0)

    @override_settings(PHOTO_BLUR_MIN_VARIANCE=0)
    def test_blur_threshold_zero_disables_the_blur_check(self):
        metrics = inspect_photo(encode(Image.new("RGB", (1000, 1000), (180, 180, 180))))
        self.assertEqual(metrics["blur_variance"], 0.0)

    def test_blur_metric_is_resolution_independent(self):
        small = blur_metrics(portrait_image((600, 800)))
        large = blur_metrics(portrait_image((2400, 3200)))
        self.assertGreater(small[1], 100)
        self.assertGreater(large[1], 100)

    def test_decompression_bomb_is_rejected(self):
        huge = Image.new("1", (30000, 30000))  # 900 MP, 1-bit: tiny PNG, huge canvas
        with self.assertRaises(PhotoRejected) as ctx:
            inspect_photo(encode(huge, "PNG"))
        self.assertEqual(ctx.exception.reason, "Photo dimensions are too large")

    def test_customer_hint_uses_the_photo_sentence(self):
        exc = PhotoRejected("Photo is too small", hint_too_small(512, 269, 576))
        self.assertIsInstance(exc, ChannelFlowError)
        self.assertEqual(customer_hint(exc), hint_too_small(512, 269, 576))


class MediaServiceGateTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="gate-1")
        product = Product.objects.create(code="gate-stickers", name="Sticker Pack")
        style = Style.objects.create(code="gate-style", name="Style")
        self.order = Order.objects.create(user=user, channel_identity=identity, product=product, style=style)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.tmp.name)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def _upload(self, content, name="photo.jpg", content_type="image/jpeg"):
        return MediaService().save_order_photo(
            order=self.order, file=SimpleUploadedFile(name, content, content_type=content_type)
        )

    def test_good_photo_saved_with_gate_metrics_and_real_mime(self):
        photo = self._upload(good_photo_bytes("PNG"), name="photo", content_type="image/jpeg")  # MAX guessed jpeg
        self.assertEqual(photo.mime_type, "image/png")  # decoded container wins
        gate = photo.metadata["gate"]
        self.assertEqual((gate["format"], gate["width"], gate["height"]), ("PNG", 1200, 1600))
        self.assertGreater(gate["blur_variance_center"], 30)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, photo.storage_key)))

    def test_rejected_photo_leaves_no_row_and_no_file(self):
        with self.assertRaises(PhotoRejected) as ctx:
            self._upload(encode(portrait_image((269, 576))))
        self.assertEqual(ctx.exception.reason, "Photo is too small")
        self.assertFalse(OrderPhoto.objects.filter(order=self.order).exists())
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_garbage_bytes_are_rejected_not_saved(self):
        with self.assertRaises(PhotoRejected):
            self._upload(b"image-bytes")
        self.assertFalse(OrderPhoto.objects.exists())

    def test_mime_and_size_checks_still_come_first(self):
        with self.assertRaises(ValidationError):
            self._upload(good_photo_bytes(), name="n.txt", content_type="text/plain")
        with override_settings(ORDER_PHOTO_MAX_BYTES=10):
            with self.assertRaises(ValidationError):
                self._upload(good_photo_bytes())

    @override_settings(PHOTO_GATE_ENABLED=False)
    def test_gate_can_be_switched_off(self):
        photo = self._upload(b"image-bytes")
        self.assertEqual(photo.metadata, {})
        self.assertEqual(photo.mime_type, "image/jpeg")
