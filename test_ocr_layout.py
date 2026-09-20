"""Tests for preserving Windows OCR line and block layout."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from PIL import Image, ImageDraw

from ocr_engine import OcrEngine


def _mapped_line(text: str, y: float, height: float = 20.0) -> dict[str, object]:
    return {
        "text": text,
        "words": [
            {
                "text": text,
                "bounding_rect": {"x": 14.0, "y": y, "width": 300.0, "height": height},
            }
        ],
    }


class OcrLayoutTests(unittest.TestCase):
    def test_enhanced_mode_keeps_a_clear_baseline_without_another_ocr_pass(self) -> None:
        class Session:
            calls = 0

            def recognise(self, _image: Image.Image) -> dict[str, str]:
                self.calls += 1
                return {"text": "I saw it. An Angel was fighting!"}

            def close(self) -> None:
                pass

        session = Session()
        image = Image.new("RGB", (600, 120), "black")
        ImageDraw.Draw(image).rectangle((12, 18, 580, 105), fill="white")
        engine = OcrEngine(session_factory=lambda _language: session)

        outcome = engine.recognise_with_details(image, mode="enhanced")

        self.assertEqual(outcome.text, "I saw it. An Angel was fighting!")
        self.assertEqual(outcome.variant, "original")
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(session.calls, 1)

    def test_enhanced_mode_can_choose_a_clearer_reading_of_small_faint_text(self) -> None:
        class Session:
            calls = 0

            def recognise(self, image: Image.Image) -> dict[str, str]:
                self.calls += 1
                return {"text": "An Angel was fighting!" if image.width > 220 else "A n g e l was f|ghting!"}

            def close(self) -> None:
                pass

        session = Session()
        image = Image.new("RGB", (220, 48), (55, 55, 70))
        ImageDraw.Draw(image).text((8, 12), "An Angel was fighting!", fill=(105, 105, 118))
        engine = OcrEngine(session_factory=lambda _language: session)

        outcome = engine.recognise_with_details(image, mode="enhanced")

        self.assertEqual(outcome.text, "An Angel was fighting!")
        self.assertNotEqual(outcome.variant, "original")
        self.assertGreater(outcome.attempts, 1)
        self.assertLessEqual(outcome.attempts, 3)

    def test_enhanced_mode_preserves_plausible_names_and_words_on_ties(self) -> None:
        class Session:
            calls = 0

            def recognise(self, image: Image.Image) -> dict[str, str]:
                self.calls += 1
                return {"text": "Xion saw the machine ports." if image.width == 220
                        else "Lion saw the machine parts."}

            def close(self) -> None:
                pass

        session = Session()
        image = Image.new("RGB", (220, 48), (70, 70, 70))
        engine = OcrEngine(session_factory=lambda _language: session)

        outcome = engine.recognise_with_details(image, mode="enhanced")

        self.assertEqual(outcome.text, "Xion saw the machine ports.")
        self.assertEqual(outcome.variant, "original")

    def test_enhanced_mode_limits_prepared_image_sizes_and_keeps_standard_unchanged(self) -> None:
        class Session:
            max_image_dimension = 120
            sizes: list[tuple[int, int]] = []

            def recognise(self, image: Image.Image) -> dict[str, str]:
                self.sizes.append(image.size)
                return {"text": ""}

            def close(self) -> None:
                pass

        session = Session()
        image = Image.new("RGB", (200, 80), (30, 30, 30))
        ImageDraw.Draw(image).rectangle((10, 10, 170, 50), fill=(180, 180, 180))
        engine = OcrEngine(session_factory=lambda _language: session)

        standard = engine.recognise_with_details(image)
        enhanced = engine.recognise_with_details(image, mode="enhanced")

        self.assertEqual(standard.attempts, 1)
        self.assertEqual(enhanced.attempts, 3)
        self.assertEqual(session.sizes[0], (200, 80))
        self.assertTrue(all(max(size) <= 120 for size in session.sizes[2:]))

    def test_blank_image_does_not_trigger_slow_extra_passes(self) -> None:
        class Session:
            calls = 0

            def recognise(self, _image: Image.Image) -> dict[str, str]:
                self.calls += 1
                return {"text": ""}

            def close(self) -> None:
                pass

        session = Session()
        engine = OcrEngine(session_factory=lambda _language: session)

        outcome = engine.recognise_with_details(Image.new("RGB", (300, 100), "black"), mode="enhanced")

        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(session.calls, 1)

    def test_enhanced_mode_does_not_speak_short_or_corrupted_guess_from_empty_baseline(self) -> None:
        class Session:
            calls = 0

            def recognise(self, _image: Image.Image) -> dict[str, str]:
                self.calls += 1
                return {"text": "" if self.calls == 1 else "kvel was�"}

            def close(self) -> None:
                pass

        image = Image.new("RGB", (200, 50), "black")
        ImageDraw.Draw(image).line((5, 20, 180, 20), fill="gray", width=2)
        engine = OcrEngine(session_factory=lambda _language: Session())

        outcome = engine.recognise_with_details(image, mode="enhanced")

        self.assertEqual(outcome.text, "")
        self.assertEqual(outcome.variant, "original")

    def test_colored_text_is_not_mistaken_for_a_blank_grayscale_image(self) -> None:
        class Session:
            calls = 0

            def recognise(self, _image: Image.Image) -> dict[str, str]:
                self.calls += 1
                return {"text": "" if self.calls == 1 else "Mother Sphere sent an Angel."}

            def close(self) -> None:
                pass

        image = Image.new("RGB", (250, 50), (50, 100, 50))
        ImageDraw.Draw(image).rectangle((5, 10, 220, 35), fill=(100, 70, 50))
        engine = OcrEngine(session_factory=lambda _language: Session())

        outcome = engine.recognise_with_details(image, mode="enhanced")

        self.assertEqual(outcome.text, "Mother Sphere sent an Angel.")

    def test_optional_ocr_pass_failure_keeps_the_original_result(self) -> None:
        class Session:
            calls = 0

            def recognise(self, _image: Image.Image) -> dict[str, str]:
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("optional image rejected")
                return {"text": "Naytibas"}

            def close(self) -> None:
                pass

        session = Session()
        engine = OcrEngine(session_factory=lambda _language: session)

        outcome = engine.recognise_with_details(Image.new("RGB", (80, 30), (50, 50, 50)), mode="enhanced")

        self.assertEqual(outcome.text, "Naytibas")
        self.assertEqual(outcome.variant, "original")

    def test_structured_lines_and_visual_blocks_override_flat_text(self) -> None:
        result = {
            "text": "flattened text that must not win",
            "lines": [
                _mapped_line("Sentinel 27's Testimony", 26),
                _mapped_line("I saw it. I definitely saw it!", 87),
                _mapped_line("An Angel was fighting!", 121),
                _mapped_line("There was a group surrounding her.", 154),
                _mapped_line("It was amazing!", 188),
                _mapped_line("Mother Sphere sent an Angel for us.", 289),
                _mapped_line("Mother Sphere would never abandon us!", 322),
            ],
        }

        self.assertEqual(
            OcrEngine.clean_text(OcrEngine._result_text(result)),
            "Sentinel 27's Testimony\n\n"
            "I saw it. I definitely saw it!\n"
            "An Angel was fighting!\n"
            "There was a group surrounding her.\n"
            "It was amazing!\n\n"
            "Mother Sphere sent an Angel for us.\n"
            "Mother Sphere would never abandon us!",
        )

    def test_native_object_shape_preserves_bullets(self) -> None:
        rect = SimpleNamespace(x=10.0, y=10.0, width=150.0, height=18.0)
        result = SimpleNamespace(
            text="It documents: • First item",
            lines=(
                SimpleNamespace(
                    text="It documents:",
                    words=(SimpleNamespace(text="It documents:", bounding_rect=rect),),
                ),
                SimpleNamespace(
                    text="• First item",
                    words=(
                        SimpleNamespace(
                            text="• First item",
                            bounding_rect=SimpleNamespace(
                                x=10.0, y=40.0, width=150.0, height=18.0
                            ),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(
            OcrEngine.clean_text(OcrEngine._result_text(result)),
            "It documents:\n• First item",
        )

    def test_lines_without_geometry_still_keep_line_boundaries(self) -> None:
        result = {
            "text": "First Second",
            "lines": [{"text": "First"}, {"text": "Second"}],
        }
        self.assertEqual(OcrEngine._result_text(result), "First\nSecond")

    def test_clean_text_collapses_but_preserves_structural_blank_lines(self) -> None:
        self.assertEqual(
            OcrEngine.clean_text("  First  line  \n\n\n  Second\tline  \n"),
            "First line\n\nSecond line",
        )

    def test_flat_text_remains_the_fallback(self) -> None:
        self.assertEqual(OcrEngine._result_text({"text": "Fallback"}), "Fallback")


if __name__ == "__main__":
    unittest.main()
