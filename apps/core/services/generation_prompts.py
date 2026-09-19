"""Prompt fragments shared by preview and FULL generation (DRF-2080).

FULL generation lost likeness on the first live order because the model
received the emotion as a bare code ("Emotion: hello"), the cartoon preview
as the FIRST reference and a prompt that still said "preview". These
helpers render the expression as label + description, and spell out the
role of every reference so the customer photos stay the identity source.
"""

from __future__ import annotations

from apps.core.models import Order, Product
from apps.core.services.channel_order_flow import (
    order_custom_phrases,
    product_emotion_options,
)

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

# Output requirements of a final sticker (owner spec): part of the FULL default
# so production does not depend on the seed carrying them in product config.
FULL_OUTPUT_REQUIREMENTS = (
    "Use a transparent background, keep the entire head, hairstyle, hands and any "
    "lettering inside the canvas, and add a neat white outline around the sticker."
)

FULL_DEFAULT_PROMPT = (
    "Create one polished personalized final sticker of the person shown in the "
    "reference photos. Preserve the person's recognizable facial identity, key "
    "hairstyle and distinctive features. Use a clean sticker composition with an "
    "isolated subject suitable for messaging apps. "
    + FULL_OUTPUT_REQUIREMENTS
)

# Custom-caption products (kind "custom_pack"): slot_key "custom-N" carries the
# customer's N-th phrase (Order.selection["custom_phrases"]) instead of an emotion.
CUSTOM_SLOT_PREFIX = "custom-"

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


def emotion_label(product: Product, code: str) -> str:
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


def is_custom_slot(slot_key: str) -> bool:
    suffix = slot_key[len(CUSTOM_SLOT_PREFIX):]
    return slot_key.startswith(CUSTOM_SLOT_PREFIX) and suffix.isdigit() and int(suffix) >= 1


def custom_phrase_for_slot(order: Order, slot_key: str) -> str:
    """The customer phrase for slot "custom-N" (1-based), "" if not a custom
    slot or the phrase is missing/blank."""
    if not is_custom_slot(slot_key):
        return ""
    index = int(slot_key[len(CUSTOM_SLOT_PREFIX):]) - 1
    phrases = order_custom_phrases(order)
    if index >= len(phrases):
        return ""
    return phrases[index].strip()


def render_caption(phrase: str) -> str:
    """Caption instruction for a custom-phrase sticker (no "Expression:" line):
    the text must appear verbatim, readable and fully inside the canvas."""
    return (
        f"Add the caption text «{phrase.strip()}» on the sticker, exactly as written, "
        "in bold clean readable lettering, fully inside the canvas, not cropped; "
        "keep the character's expression friendly/neutral."
    )


def render_expression(product: Product, code: str) -> str:
    """'Expression: «Привет» — friendly greeting, …' (label alone if unknown).

    The bare emotion code is deliberately NOT part of the prompt.
    """
    label = emotion_label(product, code)
    description = emotion_description(product, code)
    if description:
        return f"Expression: «{label}» — {description}."
    return f"Expression: «{label}»."


# Revision instructions per Revision.Category (bot channels send the category
# only; the customer text is optional and usually empty). The bare category
# code is deliberately NOT part of the prompt — "Revision category: other"
# gave the model nothing to act on (Order 16, 2026-09-19).
REVISION_INSTRUCTIONS = {
    "face": (
        "make the face match the reference photos much more closely: facial "
        "proportions, eyes, nose, mouth and skin tone."
    ),
    "hair": "match the hairstyle, hair length and hair color of the reference photos exactly.",
    "body": "correct the body shape, proportions and pose so they match the person in the photos.",
    "detail": (
        "reproduce the person's distinctive details from the photos exactly: glasses, "
        "facial hair, accessories and clothing."
    ),
    "colors": "correct the colors: skin, hair, eyes and clothing must match the reference photos.",
    "style_expectation": "follow the chosen art style more faithfully and consistently.",
    "clothes": (
        "replace the clothing with a different, stylish and neutral outfit that "
        "suits the chosen art style; keep the face, hairstyle, likeness and "
        "expression unchanged."
    ),
    "other": (
        "produce a clearly different variation with stronger likeness to the "
        "reference photos and cleaner overall quality."
    ),
}

REVISION_LEAD = "The customer rejected the previous preview and asked for a revision:"

# «Сменить одежду» with the customer's optional "what to wear" text: the
# text names the outfit itself, so it replaces the neutral-outfit default.
CLOTHES_WITH_TEXT = (
    "dress the person in the outfit the customer describes: «{words}»; keep the "
    "face, hairstyle, likeness and expression unchanged."
)


def render_revision_request(category: str, customer_text: str = "") -> str:
    """Natural-language revision instruction; appends the customer's own words
    when present. Unknown categories fall back to the "other" wording."""
    words = (customer_text or "").strip()
    if category == "clothes" and words:
        return f"{REVISION_LEAD} {CLOTHES_WITH_TEXT.format(words=words)}"
    instruction = REVISION_INSTRUCTIONS.get(category) or REVISION_INSTRUCTIONS["other"]
    text = f"{REVISION_LEAD} {instruction}"
    if words:
        text += f" Customer's own words: «{words}»."
    return text


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
