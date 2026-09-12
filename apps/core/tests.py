from unittest.mock import Mock, patch

from django.test import TestCase

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.core.services import InvalidOrderTransition, OrderStateService


class HealthEndpointTests(TestCase):
    @patch("apps.core.views.redis.Redis.from_url")
    def test_health_returns_ok_when_dependencies_are_available(self, from_url):
        client = Mock()
        client.ping.return_value = True
        from_url.return_value = client

        response = self.client.get("/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["database"])
        self.assertTrue(response.json()["redis"])


class OrderStateServiceTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-1",
        )
        product = Product.objects.create(code="stickers", name="Sticker Pack")
        style = Style.objects.create(code="style-1", name="Style 1")
        self.order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=product,
            style=style,
        )

    def test_draft_to_awaiting_photos(self):
        OrderStateService.transition(
            order=self.order,
            to_status=Order.Status.AWAITING_PHOTOS,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PHOTOS)

    def test_awaiting_photos_to_ready_for_checkout(self):
        self.order.status = Order.Status.AWAITING_PHOTOS
        self.order.save(update_fields=["status"])

        OrderStateService.transition(
            order=self.order,
            to_status=Order.Status.READY_FOR_CHECKOUT,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.READY_FOR_CHECKOUT)

    def test_ready_for_checkout_to_draft_is_rejected(self):
        self.order.status = Order.Status.READY_FOR_CHECKOUT
        self.order.save(update_fields=["status"])

        with self.assertRaises(InvalidOrderTransition):
            OrderStateService.transition(
                order=self.order,
                to_status=Order.Status.DRAFT,
            )

    def test_cancelled_is_terminal(self):
        self.order.status = Order.Status.CANCELLED
        self.order.save(update_fields=["status"])

        with self.assertRaises(InvalidOrderTransition):
            OrderStateService.transition(
                order=self.order,
                to_status=Order.Status.AWAITING_PHOTOS,
            )

    def test_failed_is_terminal(self):
        self.order.status = Order.Status.FAILED
        self.order.save(update_fields=["status"])

        with self.assertRaises(InvalidOrderTransition):
            OrderStateService.transition(
                order=self.order,
                to_status=Order.Status.READY_FOR_CHECKOUT,
            )

    def test_unknown_target_status_is_rejected(self):
        with self.assertRaises(InvalidOrderTransition):
            OrderStateService.transition(
                order=self.order,
                to_status="not-a-real-status",
            )
