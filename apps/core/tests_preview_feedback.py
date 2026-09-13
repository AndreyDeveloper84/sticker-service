from django.test import TestCase

from apps.core.models import ChannelIdentity, GeneratedAsset, GenerationJob, Order, Product, Revision, Style, User
from apps.core.services.preview_feedback import PreviewFeedbackService


class PreviewFeedbackServiceTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="100",
        )
        product = Product.objects.create(code="stickers", name="Stickers")
        style = Style.objects.create(code="comic", name="Comic")
        self.order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=product,
            style=style,
            status=Order.Status.PREVIEW_REVIEW,
        )
        job = GenerationJob.objects.create(
            order=self.order,
            task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED,
            attempt=1,
            provider="fake",
        )
        self.asset = GeneratedAsset.objects.create(
            order=self.order,
            job=job,
            storage_key="generated/preview.png",
            metadata={
                "internal_approved": True,
                "deliveries": [{"status": "sent", "channel": "telegram", "message_id": "77"}],
            },
        )

    def test_approve_is_idempotent(self):
        first = PreviewFeedbackService.approve(order=self.order)
        second = PreviewFeedbackService.approve(order=self.order)
        first.refresh_from_db()
        self.assertEqual(first.pk, second.pk)
        self.assertTrue(first.metadata["customer_approved"])

    def test_only_one_revision_is_created(self):
        first = PreviewFeedbackService.request_revision(
            order=self.order,
            category=Revision.Category.FACE,
            customer_text="Лицо должно быть ближе к оригиналу",
        )
        self.order.refresh_from_db()
        second = PreviewFeedbackService.request_revision(
            order=self.order,
            category=Revision.Category.HAIR,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Revision.objects.filter(order=self.order).count(), 1)
        self.assertEqual(self.order.status, Order.Status.REVISION_REQUESTED)
        self.assertEqual(first.source_preview_id, self.asset.pk)
        self.assertEqual(first.category, Revision.Category.FACE)
