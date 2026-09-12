from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                ("config", models.JSONField(blank=True, default=dict)),
            ],
        ),
        migrations.CreateModel(
            name="Style",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                ("config", models.JSONField(blank=True, default=dict)),
            ],
        ),
        migrations.CreateModel(
            name="User",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
        ),
        migrations.CreateModel(
            name="ChannelIdentity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("channel", models.CharField(choices=[("telegram", "Telegram"), ("max", "MAX")], max_length=20)),
                ("external_user_id", models.CharField(max_length=255)),
                ("username", models.CharField(blank=True, max_length=255)),
                ("display_name", models.CharField(blank=True, max_length=255)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="channel_identities", to="core.user")),
            ],
        ),
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("awaiting_photos", "Awaiting photos"), ("ready_for_checkout", "Ready for checkout"), ("cancelled", "Cancelled"), ("failed", "Failed")], default="draft", max_length=32)),
                ("customer_notes", models.TextField(blank=True)),
                ("operator_notes", models.TextField(blank=True)),
                ("channel_identity", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="orders", to="core.channelidentity")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="orders", to="core.product")),
                ("style", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="orders", to="core.style")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="orders", to="core.user")),
            ],
        ),
        migrations.CreateModel(
            name="OrderPhoto",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("storage_key", models.CharField(max_length=512)),
                ("original_filename", models.CharField(blank=True, max_length=255)),
                ("mime_type", models.CharField(blank=True, max_length=127)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("status", models.CharField(choices=[("accepted", "Accepted"), ("rejected", "Rejected"), ("primary_reference", "Primary reference"), ("secondary_reference", "Secondary reference")], default="accepted", max_length=32)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="photos", to="core.order")),
            ],
        ),
        migrations.AddConstraint(
            model_name="channelidentity",
            constraint=models.UniqueConstraint(fields=("channel", "external_user_id"), name="uniq_channel_external_user"),
        ),
        migrations.AddConstraint(
            model_name="channelidentity",
            constraint=models.UniqueConstraint(fields=("user", "channel"), name="uniq_user_channel"),
        ),
        migrations.AddConstraint(
            model_name="orderphoto",
            constraint=models.UniqueConstraint(fields=("order", "storage_key"), name="uniq_order_photo_storage_key"),
        ),
    ]
