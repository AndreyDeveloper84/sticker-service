from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("awaiting_photos", "Awaiting photos"),
                    ("ready_for_checkout", "Ready for checkout"),
                    ("awaiting_payment", "Awaiting payment"),
                    ("paid", "Paid"),
                    ("preview_generating", "Preview generating"),
                    ("internal_preview_review", "Internal preview review"),
                    ("preview_review", "Preview review"),
                    ("revision_requested", "Revision requested"),
                    ("revision_generating", "Revision generating"),
                    ("cancelled", "Cancelled"),
                    ("failed", "Failed"),
                ],
                default="draft",
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="Payment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("provider", models.CharField(max_length=64)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("confirmed", "Confirmed"), ("failed", "Failed"), ("cancelled", "Cancelled"), ("refunded", "Refunded")], default="pending", max_length=32)),
                ("amount_minor", models.PositiveBigIntegerField()),
                ("currency", models.CharField(max_length=8)),
                ("external_payment_id", models.CharField(blank=True, max_length=255, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="core.order")),
            ],
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(fields=("provider", "external_payment_id"), name="uniq_payment_provider_external_id"),
        ),
    ]
