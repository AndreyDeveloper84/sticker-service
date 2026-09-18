"""Prompt fragments shared by preview and FULL generation (DRF-2080).

FULL generation lost likeness on the first live order because the model
received the emotion as a bare code ("Emotion: hello"), the cartoon preview
as the FIRST reference and a prompt that still said "preview". These
helpers render the expression as label + description, and spell out the
role of every reference so the customer photos stay the identity source.
"""

from __future__ import annotations

from apps.core.models import Product
from apps.core.services.channel_order_flow import product_emotion_options

# Safe-for-work guard appended to every preview / revision / FULL prompt
# (DRF-2089): the output-stage moderation rejected 3/3 renders of an
# ordinary customer photo; the prompt now states the intended register.
SAFE_FOR_WORK_CLAUSE = (
    "Fully clothed character, neutral non-suggestive pose, family-friendly "
    "messenger sticker."
)

# Framing guard (DRF-2089, second moderation_blocked on a customer photo):
# the output filter fires on the body the model invents below the photo's
# crop; asking for a head-and-shoulders portrait removes that invention.
FRAMING_CLAUSE = "Head-and-shoulders portrait, nothing below the chest."

FULL_DEFAULT_PROMPT = (
    "Create one polished personalized final sticker of the person shown in the "
    "reference photos. Preserve the person's recognizable facial identity, key "
    "hairstyle and distinctive features. Use a clean sticker composition with an "
    "isolated subject suitable for messaging apps."
)

# Short expression descriptions for the Pilot emotion catalog (seed_live_test).
# A product may override or extend these with a "description" key on its
# emotion option; unknown codes fall back to the label alone.
EMOTION_EXPRESSIONS = {
    "hello": "friendly greeting, warm open smile, one hand raised in a wave",
    "bye": "cheerful farewell, soft smile, hand waving goodbye",
    "thanks": "grateful expression, gentle smile, hands together or hand on heart",
    "great": "enthusiastic approval, big confident smile, thumbs up",
    "no": "firm refusal, brows slightly furrowed, head shake or crossed arms",
    "love": "affectionate look, tender smile, heart gesture with the hands",
    "laugh": "hearty laughter, eyes squeezed shut, wide open smile",
    "angry": "annoyed frown, furrowed brows, tight lips, clenched fists",
    "surprised": "wide-open eyes, raised eyebrows, mouth open in astonishment",
}


def emotion_label(product: Product, code: str, *, label_override: str = "") -> str:
    if label_override.strip():
        return label_override.strip()
    for option in product_emotion_options(product):
        if option["code"] == code:
            return option["label"]
    return code


def emotion_description(product: Product, code: str) -> str:
    """Expression description: product option override, then the Pilot map, else ''."""
    for item in (product.config or {}).get("emotions") or []:
        if isinstance(item, dict) and str(item.get("code")) == code and item.get("description"):
            return str(item["description"]).strip()
    return EMOTION_EXPRESSIONS.get(code, "")


def render_expression(product: Product, code: str, *, label_override: str = "") -> str:
    """'Expression: «Привет» — friendly greeting, …' (label alone if unknown).

    The bare emotion code is deliberately NOT part of the prompt.
    """
    label = emotion_label(product, code, label_override=label_override)
    description = emotion_description(product, code)
    if description:
        return f"Expression: «{label}» — {description}."
    return f"Expression: «{label}»."


def render_reference_roles(*, photo_count: int, has_preview: bool) -> str:
    """Tell the model which reference is the identity source and which is style only."""
    if photo_count == 1:
        photos = "Reference 1 is a photo of the person"
    else:
        photos = f"References 1..{photo_count} are photos of the person"
    text = (
        f"{photos} — preserve their exact facial identity, hairstyle and "
        "distinctive features."
    )
    if has_preview:
        text += (
            " The last reference is the customer-approved preview: reuse its art "
            "style, palette and character design only; do not copy its pose, "
            "expression or facial proportions over the photos."
        )
    return text
