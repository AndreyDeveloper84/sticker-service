from __future__ import annotations

import json
import os

import httpx
from django.utils import timezone

from apps.core.models import Payment
from apps.max_bot.payments import CheckoutSession, MaxPaymentError, PaymentConfirmation


class YooKassaPaymentProvider:
    """YooKassa payments provider (https://yookassa.ru/developers/api)."""

    name = "yookassa"

    def __init__(
        self,
        *,
        shop_id: str,
        secret_key: str,
        return_url: str,
        api_base: str = "https://api.yookassa.ru/v3",
        http_client=None,
    ):
        self.shop_id = shop_id.strip()
        self.secret_key = secret_key.strip()
        self.return_url = return_url.strip()
        self.api_base = api_base.strip().rstrip("/")
        self.http_client = http_client or httpx.Client(timeout=10)

    @classmethod
    def from_env(cls) -> "YooKassaPaymentProvider":
        shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip()
        secret_key = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
        return_url = os.getenv("YOOKASSA_RETURN_URL", "").strip()
        missing = [
            name
            for name, value in (
                ("YOOKASSA_SHOP_ID", shop_id),
                ("YOOKASSA_SECRET_KEY", secret_key),
                ("YOOKASSA_RETURN_URL", return_url),
            )
            if not value
        ]
        if missing:
            raise MaxPaymentError(f"YooKassa is not configured: missing {', '.join(missing)}")
        return cls(
            shop_id=shop_id,
            secret_key=secret_key,
            return_url=return_url,
            api_base=os.getenv("YOOKASSA_API_BASE", "").strip() or "https://api.yookassa.ru/v3",
        )

    def create_checkout(self, *, payment: Payment) -> CheckoutSession:
        payload = {
            "amount": {
                "value": f"{payment.amount_minor / 100:.2f}",
                "currency": payment.currency,
            },
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self.return_url},
            "description": f"Sticker order #{payment.order_id}",
            "metadata": {
                "payment_id": str(payment.pk),
                "order_id": str(payment.order_id),
            },
        }
        try:
            response = self.http_client.post(
                f"{self.api_base}/payments",
                json=payload,
                auth=(self.shop_id, self.secret_key),
                headers={"Idempotence-Key": f"sticker-payment-{payment.pk}"},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise MaxPaymentError(f"YooKassa payment creation failed: {exc.__class__.__name__}") from exc
        except ValueError as exc:
            raise MaxPaymentError("YooKassa returned invalid JSON") from exc

        confirmation_url = str((data.get("confirmation") or {}).get("confirmation_url") or "")
        if not confirmation_url.startswith("https://"):
            raise MaxPaymentError("YooKassa returned invalid confirmation URL")
        return CheckoutSession(
            checkout_url=confirmation_url,
            provider_reference=str(data.get("id") or ""),
        )

    def parse_webhook(self, *, body: bytes, signature: str = "") -> PaymentConfirmation:
        # YooKassa does not sign webhooks; verify via fetch-from-provider and
        # trust only the object returned by api.yookassa.ru with our credentials.
        try:
            event = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise MaxPaymentError("Invalid YooKassa webhook payload") from exc

        object_id = str((event.get("object") or {}).get("id") or "").strip()
        if not object_id:
            raise MaxPaymentError("YooKassa webhook has no payment id")

        try:
            response = self.http_client.get(
                f"{self.api_base}/payments/{object_id}",
                auth=(self.shop_id, self.secret_key),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise MaxPaymentError(f"YooKassa payment fetch failed: {exc.__class__.__name__}") from exc
        except ValueError as exc:
            raise MaxPaymentError("YooKassa returned invalid JSON") from exc

        metadata = data.get("metadata") or {}
        try:
            payment_id = int(metadata.get("payment_id"))
        except (TypeError, ValueError) as exc:
            raise MaxPaymentError("YooKassa payment has no valid payment_id metadata") from exc

        amount = data.get("amount") or {}
        try:
            amount_minor = round(float(amount["value"]) * 100)
            currency = str(amount["currency"]).strip().upper()
        except (KeyError, TypeError, ValueError) as exc:
            raise MaxPaymentError("YooKassa payment has no valid amount") from exc

        confirmation_metadata = {"event": str(event.get("event") or ""), "payment": metadata}
        fee = fee_evidence(data, amount_minor=amount_minor, currency=currency)
        if fee is not None:
            confirmation_metadata["fee"] = fee
        return PaymentConfirmation(
            payment_id=payment_id,
            external_payment_id=str(data.get("id") or "").strip(),
            status=str(data.get("status") or "").strip().lower(),
            metadata=confirmation_metadata,
            amount_minor=amount_minor,
            currency=currency,
        )


def fee_evidence(data: dict, *, amount_minor: int, currency: str) -> dict | None:
    """DRF-2111: provider-confirmed fee from YooKassa's ``income_amount`` (the
    amount credited to the shop after the commission). Only the object
    returned by the provider is trusted; absent or inconsistent → None (the
    fee stays UNKNOWN — never a default percentage)."""
    income = data.get("income_amount") or {}
    try:
        income_minor = round(float(income["value"]) * 100)
        income_currency = str(income["currency"]).strip().upper()
    except (KeyError, TypeError, ValueError):
        return None
    if income_currency != currency or income_minor < 0 or income_minor > amount_minor:
        return None
    return {
        "amount_minor": amount_minor - income_minor,
        "income_amount_minor": income_minor,
        "currency": currency,
        "source": "PROVIDER_CONFIRMED",
        "captured_at": timezone.now().isoformat(),
    }
