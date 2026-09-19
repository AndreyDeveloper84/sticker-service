from django.core.management.base import BaseCommand

from apps.core.models import Product, Style
from apps.core.services.generation_prompts import FULL_DEFAULT_PROMPT, STYLE_PROMPTS


# Deterministic pilot emotion set (00-product/product-catalog.md, Standard Pack).
# Codes are the canonical identifiers stored in Order.selection["emotions"];
# labels are user-facing and safe to change without data migration.
LEGACY_COMIC_PROMPT = (
    "Use a friendly modern comic illustration style with clear contours, expressive but natural features, "
    "and strong likeness to the reference person."
)

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
# Single source: generation_prompts.FULL_DEFAULT_PROMPT (incl. output
# requirements) — the seed only pins it into product config.
FULL_GENERATION_PROMPT = FULL_DEFAULT_PROMPT

# Pricing (DRF-2057): price_minor/currency is the RUB price used by the
# MAX/YooKassa path; price_stars is the Telegram Stars (XTR) price used by the
# Telegram invoice. They are independent and never derived from each other.
# Owner-approved pilot prices: pack 500 RUB / 460 XTR, single 100 RUB / 100 XTR.
PILOT_PRODUCTS = [
    {
        "code": "sticker-pack-9-custom",
        "name": "9 стикеров с надписями",
        "config": {
            "kind": "custom_pack",
            "quantity": 9,
            "emotion_count": 9,
            "emotions": [
                {"code": f"custom-{number}", "label": f"Фраза {number}"}
                for number in range(1, 10)
            ],
            "requires_custom_phrases": True,
            "requires_customer_contact": True,
            "price_minor": 80000,
            "price_stars": 736,
            "currency": "RUB",
            "generation_prompt": GENERATION_PROMPT,
            "full_generation_prompt": FULL_GENERATION_PROMPT,
        },
    },
    {
        "code": "sticker-pack-9",
        "name": "9 стикеров без надписей",
        "config": {
            "kind": "pack",
            "quantity": 9,
            "emotion_count": 9,
            "emotions": PILOT_EMOTIONS,
            "price_minor": 50000,
            "price_stars": 460,
            "currency": "RUB",
            "requires_customer_contact": True,
            "generation_prompt": GENERATION_PROMPT,
            "full_generation_prompt": FULL_GENERATION_PROMPT,
        },
    },
    {
        "code": "single-sticker",
        "name": "1 стикер",
        "config": {
            "kind": "single",
            "quantity": 1,
            "emotion_count": 1,
            "emotions": PILOT_EMOTIONS,
            "price_minor": 10000,
            "price_stars": 100,
            "currency": "RUB",
            "requires_customer_contact": True,
            "generation_prompt": GENERATION_PROMPT,
            "full_generation_prompt": FULL_GENERATION_PROMPT,
        },
    },
]


class Command(BaseCommand):
    help = "Create or update the pilot catalog data."

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

        # The pilot ships exactly three products; anything else is hidden from
        # the selectors but kept for historical orders (FK is PROTECT).
        Product.objects.exclude(code__in=pilot_codes).update(is_active=False)

        # Prompt wording lives in generation_prompts.STYLE_PROMPTS (single
        # source, covered by tests); the seed only pins name + prompt per code.
        styles = [
            ("3d", "3D", STYLE_PROMPTS["3d"]),
            ("drawn", "Рисованные", STYLE_PROMPTS["drawn"]),
            ("meme", "Мемные", STYLE_PROMPTS["meme"]),
            ("embroidery", "Вышивка", STYLE_PROMPTS["embroidery"]),
            ("help-choose", "Помогите выбрать", STYLE_PROMPTS["help-choose"]),
        ]
        style_codes = []
        for code, name, prompt in styles:
            style, _ = Style.objects.update_or_create(
                code=code,
                defaults={"name": name, "is_active": True, "config": {"prompt": prompt}},
            )
            style_codes.append(style.code)
        # Legacy pilot style: Orders 11–13 reference it (FK PROTECT), so it is
        # kept with its original name and only hidden from the selectors —
        # never renamed into "Помогите выбрать".
        Style.objects.get_or_create(
            code="comic",
            defaults={"name": "Комикс", "is_active": False, "config": {"prompt": LEGACY_COMIC_PROMPT}},
        )
        Style.objects.exclude(code__in=style_codes).update(is_active=False)
        self.stdout.write(
            self.style.SUCCESS(
                f"Pilot catalog ready: products={', '.join(pilot_codes)}, styles={', '.join(style_codes)}"
            )
        )
