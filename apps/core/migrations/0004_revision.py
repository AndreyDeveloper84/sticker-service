from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0003_generation_domain")]

    operations = [
        migrations.CreateModel(
            name="Revision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.CharField(max_length=32, choices=[("face", "Face"), ("hair", "Hair"), ("body", "Body"), ("detail", "Detail"), ("colors", "Colors"), ("style_expectation", "Style expectation"), ("other", "Other")])),
                ("customer_text", models.TextField(blank=True)),
                ("status", models.CharField(max_length=32, default="requested", choices=[("requested", "Requested"), ("generating", "Generating"), ("completed", "Completed")])),
                ("order", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="revision", to="core.order")),
                ("source_preview", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="revision_requests", to="core.generatedasset")),
            ],
        ),
    ]
