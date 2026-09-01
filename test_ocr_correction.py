"""Behavior tests for conservative game-dialogue OCR correction."""

from __future__ import annotations

import unittest

from ocr_correction import CorrectionOptions, OcrCorrector, ReplacementRule


class OcrCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corrector = OcrCorrector()
        self.options = CorrectionOptions(strength="conservative")

    def test_corrects_known_stylized_font_errors_using_context(self) -> None:
        examples = {
            "I sow it.": "I saw it.",
            "I definitely sow it!": "I definitely saw it!",
            "There was o group.": "There was a group.",
            "In o second.": "In a second.",
            "She dealt with oll of them.": "She dealt with all of them.",
            "Mother Sphere sent on Angel.": "Mother Sphere sent an Angel.",
        }

        for raw, expected in examples.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.corrector.correct(raw, self.options).corrected_text, expected)

    def test_preserves_valid_words_fictional_terms_numbers_and_mixed_case(self) -> None:
        examples = (
            "Farmers sow seeds.",
            "Go on ahead.",
            "Naytiba met Mother Sphere.",
            "Equip the B8-G6 rifle in Sector 5.",
            "The eVe protocol is called NIKKE-2.",
            "O Angel, hear us.",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(self.corrector.correct(text, self.options).corrected_text, text)

    def test_result_retains_raw_text_and_describes_each_change(self) -> None:
        raw = "In o second, she dealt with oll of them!"

        result = self.corrector.correct(raw, self.options)

        self.assertEqual(result.raw_text, raw)
        self.assertEqual(result.corrected_text, "In a second, she dealt with all of them!")
        self.assertEqual([(change.original, change.replacement) for change in result.corrections], [("o", "a"), ("oll", "all")])

    def test_disabled_correction_returns_raw_text_unchanged(self) -> None:
        raw = "In o second."

        result = self.corrector.correct(raw, CorrectionOptions(enabled=False))

        self.assertEqual(result.corrected_text, raw)
        self.assertEqual(result.corrections, ())

    def test_custom_replacement_overrides_automatic_correction_and_is_traced(self) -> None:
        options = CorrectionOptions(
            replacements=(
                ReplacementRule("Noytibos", "Naytibas", case_sensitive=False, whole_word=True),
            )
        )

        result = self.corrector.correct("Noytibos met NOYTIBOS.", options)

        self.assertEqual(result.corrected_text, "Naytibas met Naytibas.")
        self.assertEqual([change.source for change in result.corrections], ["custom", "custom"])

    def test_balanced_correction_repairs_spelling_and_word_boundaries(self) -> None:
        options = CorrectionOptions(strength="balanced")

        result = self.corrector.correct("The warrlor waited overthere.", options)

        self.assertEqual(result.corrected_text, "The warrior waited over there.")

    def test_balanced_correction_preserves_game_terms_and_ambiguous_words(self) -> None:
        options = CorrectionOptions(strength="balanced", protected_words=("overthere",))

        result = self.corrector.correct(
            "Naytiba met NIKKE-2 overthere. Farmers sow seeds. Sent on Monday.",
            options,
        )

        self.assertEqual(
            result.corrected_text,
            "Naytiba met NIKKE-2 overthere. Farmers sow seeds. Sent on Monday.",
        )

    def test_options_can_be_built_from_persisted_settings(self) -> None:
        options = CorrectionOptions.from_mapping(
            {
                "enabled": True,
                "strength": "balanced",
                "protected_words": ["Naytiba"],
                "replacements": [
                    {
                        "original": "Noytibos",
                        "replacement": "Naytibas",
                        "enabled": True,
                        "case_sensitive": False,
                        "whole_word": True,
                    }
                ],
            }
        )

        self.assertEqual(options.strength, "balanced")
        self.assertEqual(options.protected_words, ("Naytiba",))
        self.assertEqual(options.replacements[0].replacement, "Naytibas")

    def test_contextual_rules_handle_pronoun_and_common_glyph_confusions(self) -> None:
        result = self.corrector.correct("l am ready. 1 saw her. 5he was with tbe Angel.")

        self.assertEqual(result.corrected_text, "I am ready. I saw her. She was with the Angel.")

    def test_balanced_mode_repairs_safe_spacing_and_alphanumeric_words(self) -> None:
        result = self.corrector.correct(
            "The R0bot,waited!Then it moved  again.",
            CorrectionOptions(strength="balanced"),
        )

        self.assertEqual(result.corrected_text, "The Robot, waited! Then it moved again.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
