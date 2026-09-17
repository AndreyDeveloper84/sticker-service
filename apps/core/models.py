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
        READY_FOR_DELIVERY = "ready_for_delivery", "Ready for delivery"
        # Final delivery (DRF-2053): entered from READY_FOR_DELIVERY (QC PASS,
        # DRF-2052); DELIVERED is terminal.
        DELIVERY_IN_PROGRESS = "delivery_in_progress", "Delivery in progress"
        DELIVERED = "delivered", "Delivered"
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


class QcReport(TimestampedModel):
    """QC result for the current set of final assets (DRF-2052).

    PASS requires the full expected asset set, all automated format checks
    and the complete human checklist. FAIL persists reason codes; the
    operator then selects concrete slots for selective retry.

    Canonical slot identity is GeneratedAsset.slot_key (owned by DRF-2051);
    asset ids are stored for audit/reference only.
    """

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="qc_reports")
    attempt = models.PositiveIntegerField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.IN_PROGRESS)
    expected_count = models.PositiveIntegerField(default=0)
    # Canonical identity of the evaluated set (slot_key list). The delivery
    # gate compares it against the current set to catch post-PASS changes.
    slot_keys = models.JSONField(default=list, blank=True)
    # Concrete assets evaluated by this report (parallel to slot_keys).
    # Not domain identity, but the delivery gate uses them to detect a slot
    # regenerated after PASS (same slot_key, new current asset).
    asset_ids = models.JSONField(default=list, blank=True)
    # {slot_key: {check_name: bool}} plus set-level "expected_count".
    automated_checks = models.JSONField(default=dict, blank=True)
    # {criterion: {"passed": bool, "note": str}} — human QC only.
    human_checklist = models.JSONField(default=dict, blank=True)
    reason_codes = models.JSONField(default=list, blank=True)
    # [{"slot_key": str, "asset_id": int (audit), "reason_codes": [...]}]
    retry_slots = models.JSONField(default=list, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "attempt"],
                name="uniq_qc_report_attempt",
            )
        ]
        ordering = ["order_id", "attempt"]

    def __str__(self) -> str:
        return f"QcReport #{self.pk} for order #{self.order_id} attempt {self.attempt}"


class FinalDelivery(TimestampedModel):
    """One delivery run of the final sticker set to the order channel (DRF-2053).

    A run sends the CURRENT final asset of every expected slot that has
    not been sent yet. Per-slot outcomes are recorded in ``results``;
    idempotency across runs is by slot_key: a slot with a "sent" result
    (message_id) in ANY run of the order is never sent again, so the
    customer cannot receive the same sticker twice.
    """

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="final_deliveries")
    channel = models.CharField(max_length=20, choices=ChannelIdentity.Channel.choices)
    attempt = models.PositiveIntegerField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.IN_PROGRESS)
    # Per-slot outcomes of THIS run, in send order:
    # [{"slot_key", "asset_id", "message_id", "status": "sent"|"failed",
    #   "error", "failure_class": "retryable"|"permanent"|"", "metadata",
    #   "created_at"}]
    results = models.JSONField(default=list, blank=True)
    # Final "set is ready" message: {"status", "message_id", "error", ...}.
    summary = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "attempt"],
                name="uniq_final_delivery_attempt",
            )
        ]
        ordering = ["order_id", "attempt"]

    def __str__(self) -> str:
        return f"FinalDelivery #{self.pk} for order #{self.order_id} attempt {self.attempt}"


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


class OrderEvent(models.Model):
    """Append-only pilot metrics log (DRF-2055).

    Records only facts that cannot be derived from other tables: order state
    transitions (drop-off point, stage timing), the customer's preview
    approval (which does not change order status) and explicitly logged
    operator minutes. Payments, generation jobs and revisions are derived from
    their own tables and are deliberately not duplicated here.
    """

    class Type(models.TextChoices):
        STATUS_CHANGED = "order.status_changed", "Order status changed"
        PREVIEW_CUSTOMER_APPROVED = "preview.customer_approved", "Preview approved by customer"
        MANUAL_WORK_LOGGED = "manual.work_logged", "Manual work logged"

    class Actor(models.TextChoices):
        SYSTEM = "system", "System"
        CUSTOMER = "customer", "Customer"
        OPERATOR = "operator", "Operator"

    class Activity(models.TextChoices):
        PHOTO_REVIEW = "photo_review", "Photo review"
        PREVIEW_REVIEW = "preview_review", "Preview review"
        REVISION_HANDLING = "revision_handling", "Revision handling"
        QC = "qc", "Quality control"
        DELIVERY = "delivery", "Delivery"
        SUPPORT = "support", "Customer support"
        OTHER = "other", "Other"

    # CASCADE: the log has no life of its own; orders with payments/jobs are
    # already protected from deletion by those FKs.
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=64, choices=Type.choices)
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32, blank=True)
    actor_kind = models.CharField(max_length=16, choices=Actor.choices, default=Actor.SYSTEM)
    actor_ref = models.CharField(max_length=150, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["order", "created_at"], name="core_orderevent_order_idx"),
            models.Index(fields=["event_type", "created_at"], name="core_orderevent_type_idx"),
        ]

    def __str__(self) -> str:
        return f"OrderEvent #{self.pk} {self.event_type} for order #{self.order_id}"


class ManualWorkLogManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(event_type=OrderEvent.Type.MANUAL_WORK_LOGGED)


class ManualWorkLog(OrderEvent):
    """Operator-entered minutes per order; the only hand-written OrderEvent."""

    objects = ManualWorkLogManager()

    class Meta:
        proxy = True
        verbose_name = "Manual work log"
        verbose_name_plural = "Manual work logs"
