from django.db import models


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class User(TimestampedModel):
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"User #{self.pk}"


class ChannelIdentity(TimestampedModel):
    class Channel(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="channel_identities")
    channel = models.CharField(max_length=20, choices=Channel.choices)
    external_user_id = models.CharField(max_length=255)
    username = models.CharField(max_length=255, blank=True)
    display_name = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "external_user_id"],
                name="uniq_channel_external_user",
            ),
            models.UniqueConstraint(
                fields=["user", "channel"],
                name="uniq_user_channel",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.channel}:{self.external_user_id}"


class Product(TimestampedModel):
    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return self.name


class Style(TimestampedModel):
    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return self.name


class Order(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        AWAITING_PHOTOS = "awaiting_photos", "Awaiting photos"
        READY_FOR_CHECKOUT = "ready_for_checkout", "Ready for checkout"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="orders")
    channel_identity = models.ForeignKey(
        ChannelIdentity,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="orders")
    style = models.ForeignKey(Style, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    customer_notes = models.TextField(blank=True)
    operator_notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Order #{self.pk}"


class OrderPhoto(TimestampedModel):
    class Status(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        PRIMARY_REFERENCE = "primary_reference", "Primary reference"
        SECONDARY_REFERENCE = "secondary_reference", "Secondary reference"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="photos")
    storage_key = models.CharField(max_length=512)
    original_filename = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=127, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.ACCEPTED)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "storage_key"],
                name="uniq_order_photo_storage_key",
            )
        ]

    def __str__(self) -> str:
        return f"OrderPhoto #{self.pk} for order #{self.order_id}"
