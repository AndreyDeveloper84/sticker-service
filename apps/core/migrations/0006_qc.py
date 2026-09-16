from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_order_selection"),
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
                    ("pack_generating", "Pack generating"),
                    ("quality_control", "Quality control"),
                    ("ready_for_delivery", "Ready for delivery"),
                    ("cancelled", "Cancelled"),
                    ("failed", "Failed"),
                ],
                default="draft",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="generatedasset",
            name="kind",
            field=models.CharField(
                choices=[("preview", "Preview"), ("final", "Final")],
                default="preview",
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="QcReport",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("attempt", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("in_progress", "In progress"),
                            ("passed", "Passed"),
                            ("failed", "Failed"),
                        ],
                        default="in_progress",
                        max_length=32,
                    ),
                ),
                ("expected_count", models.PositiveIntegerField(default=0)),
                ("asset_ids", models.JSONField(blank=True, default=list)),
                ("automated_checks", models.JSONField(blank=True, default=dict)),
                ("human_checklist", models.JSONField(blank=True, default=dict)),
                ("reason_codes", models.JSONField(blank=True, default=list)),
                ("retry_slots", models.JSONField(blank=True, default=list)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="qc_reports",
                        to="core.order",
                    ),
                ),
            ],
            options={
                "ordering": ["order_id", "attempt"],
            },
        ),
        migrations.AddConstraint(
            model_name="qcreport",
            constraint=models.UniqueConstraint(
                fields=("order", "attempt"), name="uniq_qc_report_attempt"
            ),
        ),
    ]
