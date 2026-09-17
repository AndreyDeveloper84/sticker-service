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
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
        PAID = "paid", "Paid"
        PREVIEW_GENERATING = "preview_generating", "Preview generating"
        INTERNAL_PREVIEW_REVIEW = "internal_preview_review", "Internal preview review"
        PREVIEW_REVIEW = "preview_review", "Preview review"
        REVISION_REQUESTED = "revision_requested", "Revision requested"
        REVISION_GENERATING = "revision_generating", "Revision generating"
        PACK_GENERATING = "pack_generating", "Pack generating"
        QUALITY_CONTROL = "quality_control", "Quality control"
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
    # Channel-agnostic product selection captured during the bot flow.
    # Canonical shape: {"emotions": [<emotion code>, ...]} — codes come from
    # Product.config["emotions"]; the required count is Product.config["emotion_count"].
    selection = models.JSONField(default=dict, blank=True)
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


class Payment(TimestampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=64)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=8)
    external_payment_id = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_payment_id"],
                name="uniq_payment_provider_external_id",
            )
        ]

    def __str__(self) -> str:
        return f"Payment #{self.pk} for order #{self.order_id}"


class GenerationJob(TimestampedModel):
    class TaskType(models.TextChoices):
        PREVIEW = "preview", "Preview"
        REVISION = "revision", "Revision"
        FULL = "full", "Full production"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="generation_jobs")
    task_type = models.CharField(max_length=32, choices=TaskType.choices)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    attempt = models.PositiveIntegerField()
    # Stable production slot (emotion code) for FULL jobs; empty for
    # preview/revision jobs.
    slot_key = models.CharField(max_length=64, blank=True)
    provider = models.CharField(max_length=64)
    input_metadata = models.JSONField(default=dict, blank=True)
    output_metadata = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "task_type", "attempt"],
                name="uniq_generation_attempt",
            ),
            models.UniqueConstraint(
                fields=["order", "slot_key", "attempt"],
                condition=models.Q(task_type="full"),
                name="uniq_full_slot_attempt",
            ),
        ]
        ordering = ["order_id", "task_type", "attempt"]

    def __str__(self) -> str:
        return f"GenerationJob #{self.pk} {self.task_type} attempt {self.attempt}"


class GeneratedAsset(TimestampedModel):
    class Kind(models.TextChoices):
        PREVIEW = "preview", "Preview"
        FINAL = "final", "Final"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="generated_assets")
    job = models.ForeignKey(GenerationJob, on_delete=models.PROTECT, related_name="assets")
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.PREVIEW)
    # Production slot (emotion code) for FINAL assets; empty for previews.
    slot_key = models.CharField(max_length=64, blank=True)
    storage_key = models.CharField(max_length=512, unique=True)
    mime_type = models.CharField(max_length=127, default="image/png")
    size_bytes = models.PositiveBigIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"GeneratedAsset #{self.pk} for order #{self.order_id}"


class Revision(TimestampedModel):
    class Category(models.TextChoices):
        FACE = "face", "Face"
        HAIR = "hair", "Hair"
        BODY = "body", "Body"
        DETAIL = "detail", "Detail"
        COLORS = "colors", "Colors"
        STYLE_EXPECTATION = "style_expectation", "Style expectation"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        GENERATING = "generating", "Generating"
        COMPLETED = "completed", "Completed"

    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="revision")
    source_preview = models.ForeignKey(
        GeneratedAsset,
        on_delete=models.PROTECT,
        related_name="revision_requests",
    )
    category = models.CharField(max_length=32, choices=Category.choices)
    customer_text = models.TextField(blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.REQUESTED)

    def __str__(self) -> str:
        return f"Revision #{self.pk} for order #{self.order_id}"
