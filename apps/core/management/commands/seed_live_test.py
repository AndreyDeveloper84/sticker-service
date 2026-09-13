from django.core.management.base import BaseCommand

from apps.core.models import Product, Style


class Command(BaseCommand):
    help = "Create or update deterministic catalog data for the first live bot test."

    def handle(self, *args, **options):
        product, _ = Product.objects.update_or_create(
            code="personal-sticker-pack",
            defaults={
                "name": "Персональный стикерпак",
                "is_active": True,
                "config": {
                    "price_stars": 1,
                    "price_minor": 100,
                    "currency": "RUB",
                    "generation_prompt": (
                        "Create one polished personalized sticker preview based on the reference photos. "
                        "Preserve the person's recognizable facial identity, key hairstyle, and distinctive features. "
                        "Use a clean sticker composition suitable for messaging apps."
                    ),
                },
            },
        )
        style, _ = Style.objects.update_or_create(
            code="comic",
            defaults={
                "name": "Комикс",
                "is_active": True,
                "config": {
                    "prompt": (
                        "Use a friendly modern comic illustration style with clear contours, expressive but natural features, "
                        "and strong likeness to the reference person."
                    )
                },
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Live-test catalog ready: product={product.code}, style={style.code}"
            )
        )
