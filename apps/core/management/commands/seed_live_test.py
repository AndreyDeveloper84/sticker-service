from django.core.management.base import BaseCommand

from apps.core.models import Product, Style


# Deterministic pilot emotion set (00-product/product-catalog.md, Standard Pack).
# Codes are the canonical identifiers stored in Order.selection["emotions"];
# labels are user-facing and safe to change without data migration.
PILOT_EMOTIONS = [
    {"code": "hello", "label": "Привет"},
    {"code": "bye", "label": "Пока"},
    {"code": "thanks", "label": "Спасибо"},
    {"code": "great", "label": "Отлично"},
    {"code": "no", "label": "Нет"},
    {"code": "love", "label": "Люблю"},
    {"code": "laugh", "label": "Смеюсь"},
    {"code": "angry", "label": "Злюсь"},
    {"code": "surprised", "label": "Удивление"},
]

GENERATION_PROMPT = (
    "Create one polished personalized sticker preview based on the reference photos. "
    "Preserve the person's recognizable facial identity, key hairstyle, and distinctive features. "
    "Use a clean sticker composition suitable for messaging apps."
)
# FULL production wording (DRF-2080): "final sticker", never "preview".
FULL_GENERATION_PROMPT = (
    "Create one polished personalized final sticker of the person shown in the reference photos. "
    "Preserve the person's recognizable facial identity, key hairstyle, and distinctive features. "
    "Use a clean sticker composition with an isolated subject suitable for messaging apps."
)

# Pricing (DRF-2057): price_minor/currency is the RUB price used by the
# MAX/YooKassa path; price_stars is the Telegram Stars (XTR) price used by the
# Telegram invoice. They are independent and never derived from each other.
# Owner-approved pilot prices: pack 500 RUB / 460 XTR, single 100 RUB / 100 XTR.
PILOT_PRODUCTS = [
    {
        "code": "sticker-pack-9",
        "name": "Стикерпак — 9 стикеров",
        "config": {
            "kind": "pack",
            "quantity": 9,
            "emotion_count": 9,
            "emotions": PILOT_EMOTIONS,
            "price_minor": 50000,
            "price_stars": 460,
            "currency": "RUB",
            "generation_prompt": GENERATION_PROMPT,
            "full_generation_prompt": FULL_GENERATION_PROMPT,
        },
    },
    {
        "code": "single-sticker",
        "name": "Один стикер",
        "config": {
            "kind": "single",
            "quantity": 1,
            "emotion_count": 1,
            "emotions": PILOT_EMOTIONS,
            "price_minor": 10000,
            "price_stars": 100,
            "currency": "RUB",
            "generation_prompt": GENERATION_PROMPT,
            "full_generation_prompt": FULL_GENERATION_PROMPT,
        },
    },
]


class Command(BaseCommand):
    help = "Create or update deterministic pilot catalog data (two products, one style)."

    def handle(self, *args, **options):
        pilot_codes = []
        for spec in PILOT_PRODUCTS:
            product, _ = Product.objects.update_or_create(
                code=spec["code"],
                defaults={
                    "name": spec["name"],
                    "is_active": True,
                    "config": spec["config"],
                },
            )
            pilot_codes.append(product.code)

        # The pilot ships exactly two products; anything else is hidden from
        # the selectors but kept for historical orders (FK is PROTECT).
        Product.objects.exclude(code__in=pilot_codes).update(is_active=False)

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
                f"Pilot catalog ready: products={', '.join(pilot_codes)}, style={style.code}"
            )
        )
