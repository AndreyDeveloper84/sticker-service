from unittest.mock import Mock, patch

from django.test import TestCase


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
