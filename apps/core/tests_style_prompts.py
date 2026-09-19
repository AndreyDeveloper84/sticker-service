"""Style prompts (owner GO 2026-09-20, Order 16 / asset 20): the one-phrase
"3D" prompt produced an almost photorealistic retouch. Every pilot style must
now name the medium, say it is not a photograph and keep likeness — without
contradicting the shared SFW / framing / output clauses.
"""

from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from apps.core.models import Style
from apps.core.services.generation_prompts import (
    FRAMING_CLAUSE,
    FULL_OUTPUT_REQUIREMENTS,
    NOT_A_PHOTOGRAPH,
    SAFE_FOR_WORK_CLAUSE,
    STYLE_PROMPTS,
)

PILOT_STYLES = ["3d", "drawn", "embroidery", "help-choose", "meme"]

# Words that would pull the render back towards a photo or fight the shared clauses.
CONTRADICTIONS = (
    "photorealistic",
    "realistic",
    "photo-real",
    "background",  # background is owned by FULL_OUTPUT_REQUIREMENTS
    "outline around",  # white outline is owned by FULL_OUTPUT_REQUIREMENTS
    "full body",  # framing is owned by FRAMING_CLAUSE
    "below the chest",
)


class StylePromptWordingTests(SimpleTestCase):
    def test_every_pilot_style_has_a_prompt(self):
        self.assertEqual(sorted(STYLE_PROMPTS), PILOT_STYLES)

    def test_each_style_states_medium_not_photo_and_likeness(self):
        for code, prompt in STYLE_PROMPTS.items():
            with self.subTest(code=code):
                self.assertTrue(prompt.endswith(NOT_A_PHOTOGRAPH))
                self.assertIn("not a retouched photograph", prompt)
                lowered = prompt.lower()
                self.assertTrue("likeness" in lowered and "recogniz" in lowered, prompt)
                self.assertIn("simplified", lowered)

    def test_3d_is_a_stylized_character_render(self):
        prompt = STYLE_PROMPTS["3d"].lower()
        for cue in ("stylized 3d animated character", "smooth skin", "expressive eyes", "rim light", "simplified clothing"):
            self.assertIn(cue, prompt)

    def test_no_contradictions_with_shared_clauses(self):
        for code, prompt in STYLE_PROMPTS.items():
            lowered = prompt.lower()
            for word in CONTRADICTIONS:
                with self.subTest(code=code, word=word):
                    self.assertNotIn(word, lowered)
            # The shared clauses are appended by the services, never duplicated here.
            self.assertNotIn(SAFE_FOR_WORK_CLAUSE, prompt)
            self.assertNotIn(FRAMING_CLAUSE, prompt)
            self.assertNotIn(FULL_OUTPUT_REQUIREMENTS, prompt)


class StylePromptSeedTests(TestCase):
    def test_seed_pins_style_prompts_and_is_idempotent(self):
        Style.objects.create(code="3d", name="3D", is_active=True, config={"prompt": "old one-phrase prompt"})
        call_command("seed_live_test", stdout=StringIO())
        call_command("seed_live_test", stdout=StringIO())
        for code in PILOT_STYLES:
            with self.subTest(code=code):
                style = Style.objects.get(code=code)
                self.assertTrue(style.is_active)
                self.assertEqual(style.config["prompt"], STYLE_PROMPTS[code])
        self.assertEqual(Style.objects.filter(code="3d").count(), 1)
