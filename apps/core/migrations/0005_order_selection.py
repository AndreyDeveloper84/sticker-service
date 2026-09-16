from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_revision"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="selection",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
