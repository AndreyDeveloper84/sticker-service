from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_payment_m2_order_states"),
    ]

    operations = [
        migrations.CreateModel(
            name="GenerationJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("task_type", models.CharField(choices=[("preview", "Preview"), ("revision", "Revision")], max_length=32)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("running", "Running"), ("succeeded", "Succeeded"), ("failed", "Failed")], default="pending", max_length=32)),
                ("attempt", models.PositiveIntegerField()),
                ("provider", models.CharField(max_length=64)),
                ("input_metadata", models.JSONField(blank=True, default=dict)),
                ("output_metadata", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="generation_jobs", to="core.order")),
            ],
            options={
                "ordering": ["order_id", "task_type", "attempt"],
            },
        ),
        migrations.CreateModel(
            name="GeneratedAsset",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("kind", models.CharField(choices=[("preview", "Preview")], default="preview", max_length=32)),
                ("storage_key", models.CharField(max_length=512, unique=True)),
                ("mime_type", models.CharField(default="image/png", max_length=127)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assets", to="core.generationjob")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="generated_assets", to="core.order")),
            ],
        ),
        migrations.AddConstraint(
            model_name="generationjob",
            constraint=models.UniqueConstraint(fields=("order", "task_type", "attempt"), name="uniq_generation_attempt"),
        ),
    ]
