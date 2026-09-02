"""Tests for preserving Windows OCR line and block layout."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

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
