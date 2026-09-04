"""Tests for layout-aware plain-text speech preparation."""

from __future__ import annotations

import unittest

from speech_text import format_for_speech, prepare_for_speech


class SpeechTextTests(unittest.TestCase):
    def test_heading_block_receives_a_pause(self) -> None:
        self.assertEqual(
            format_for_speech("Sentinel 27's Testimony\n\nI saw it."),
            "Sentinel 27's Testimony. I saw it.",
        )

    def test_bullets_are_removed_and_separated(self) -> None:
        text = "It documents:\n• First item\n• Second item"
        self.assertEqual(
            format_for_speech(text),
            "It documents: First item. Second item.",
        )

    def test_wrapped_dialogue_is_joined_without_an_extra_pause(self) -> None:
        self.assertEqual(
            format_for_speech("This is an ordinary wrapped\nline of dialogue."),
            "This is an ordinary wrapped line of dialogue.",
        )

    def test_existing_terminal_punctuation_is_not_duplicated(self) -> None:
        self.assertEqual(
            format_for_speech("Question?\n\nAnswer!\n\nIntroduction:"),
            "Question? Answer! Introduction:",
        )

    def test_common_numbered_and_dash_lists_are_supported(self) -> None:
        self.assertEqual(
            format_for_speech("1. First\n2) Second\n- Third"),
            "First. Second. Third.",
        )

    def test_empty_text_stays_empty(self) -> None:
        self.assertEqual(format_for_speech(" \n\n "), "")

    def test_speech_document_maps_formatted_words_to_displayed_text(self) -> None:
        source = "Sentinel 27's Testimony\n\n1. First item\n• Naytiba's second item"
        document = prepare_for_speech(source)

        self.assertEqual(
            document.spoken_text,
            "Sentinel 27's Testimony. First item. Naytiba's second item.",
        )
        self.assertEqual(
            [source[word.source_start : word.source_end] for word in document.words],
            ["Sentinel", "27's", "Testimony", "First", "item", "Naytiba's", "second", "item"],
        )
        self.assertEqual(
            [document.spoken_text[word.spoken_start : word.spoken_end] for word in document.words],
            ["Sentinel", "27's", "Testimony", "First", "item", "Naytiba's", "second", "item"],
        )

    def test_repeated_words_and_compacted_whitespace_keep_forward_mapping(self) -> None:
        source = "One   One\n\n- One"
        document = prepare_for_speech(source)

        self.assertEqual(document.spoken_text, "One One. One.")
        self.assertEqual(
            [source[word.source_start : word.source_end] for word in document.words],
            ["One", "One", "One"],
        )


if __name__ == "__main__":
    unittest.main()
