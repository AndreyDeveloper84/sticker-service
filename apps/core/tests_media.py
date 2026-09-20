from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import ChannelIdentity, Order, OrderPhoto, Product, Style, User
from apps.core.services.media import MediaService
from apps.core.tests_photo_gate import good_photo_bytes


class MediaServiceTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-media-1",
        )
        product = Product.objects.create(code="media-stickers", name="Sticker Pack")
        style = Style.objects.create(code="media-style", name="Style")
        self.order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=product,
            style=style,
        )
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _file(self, name="photo.jpg", content=None, content_type="image/jpeg"):
        return SimpleUploadedFile(name, good_photo_bytes() if content is None else content, content_type=content_type)

    @override_settings(ORDER_PHOTO_MAX_BYTES=5 * 1024 * 1024)
    def test_saves_original_and_creates_order_photo(self):
        with override_settings(MEDIA_ROOT=self.tmp.name):
            photo = MediaService().save_order_photo(order=self.order, file=self._file())

        self.assertEqual(photo.order, self.order)
        self.assertEqual(photo.mime_type, "image/jpeg")
        self.assertEqual(photo.size_bytes, len(good_photo_bytes()))
        self.assertTrue(photo.storage_key.startswith(f"orders/{self.order.pk}/source/"))

    @override_settings(ORDER_PHOTO_MAX_BYTES=5 * 1024 * 1024)
    def test_repeated_upload_creates_distinct_photo(self):
        with override_settings(MEDIA_ROOT=self.tmp.name):
            first = MediaService().save_order_photo(order=self.order, file=self._file())
            second = MediaService().save_order_photo(order=self.order, file=self._file())

        self.assertNotEqual(first.storage_key, second.storage_key)
        self.assertEqual(OrderPhoto.objects.filter(order=self.order).count(), 2)

    @override_settings(ORDER_PHOTO_MAX_BYTES=5 * 1024 * 1024)
    def test_rejects_unsupported_mime_type(self):
        with override_settings(MEDIA_ROOT=self.tmp.name):
            with self.assertRaises(ValidationError):
                MediaService().save_order_photo(
                    order=self.order,
                    file=self._file(name="note.txt", content_type="text/plain"),
                )

        self.assertFalse(OrderPhoto.objects.exists())

    @override_settings(ORDER_PHOTO_MAX_BYTES=4)
    def test_rejects_oversized_file(self):
        with override_settings(MEDIA_ROOT=self.tmp.name):
            with self.assertRaises(ValidationError):
                MediaService().save_order_photo(order=self.order, file=self._file(content=b"12345"))

        self.assertFalse(OrderPhoto.objects.exists())

    @override_settings(ORDER_PHOTO_MAX_BYTES=5 * 1024 * 1024)
    def test_staff_can_retrieve_saved_file_through_admin(self):
        with override_settings(MEDIA_ROOT=self.tmp.name):
            photo = MediaService().save_order_photo(order=self.order, file=self._file())
            staff = get_user_model().objects.create_user(
                username="operator",
                password="test-password",
                is_staff=True,
            )
            self.client.force_login(staff)
            response = self.client.get(reverse("admin:core_orderphoto_file", args=[photo.pk]))
            body = b"".join(response.streaming_content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, good_photo_bytes())
