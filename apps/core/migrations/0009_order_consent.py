# MAX Pilot consent gate: persist which consent text the customer accepted
# and when, per order. Depends on 0008_final_delivery (DRF-2053), the current
# head of the core migration graph on dev.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_final_delivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="consent_version",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="order",
            name="consent_accepted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
