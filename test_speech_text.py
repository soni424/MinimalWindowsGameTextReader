"""Tests for layout-aware plain-text speech preparation."""

from __future__ import annotations

import unittest

from speech_text import format_for_speech, prepare_for_speech, synthesis_input


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
            "one: First. two: Second. Third.",
        )

    def test_plain_list_label_colon_has_a_mapped_pause(self) -> None:
        source = '- Empirical Evidence: Ideas need measurable data.\n- Falsifiability: Claims must be testable.'
        document = prepare_for_speech(source)
        for label, next_word in [('Empirical Evidence:', 'Ideas'), ('Falsifiability:', 'Claims')]:
            offset = source.index(next_word)
            word = next(word for word in document.words if word.source_start == offset)
            self.assertEqual(source[source.index(label):offset].strip(), label)
            self.assertIn(250, [pause.milliseconds for pause in document.pauses
                                if pause.spoken_offset == word.spoken_start])
        self.assertEqual(document.source_text, source)

    def test_non_label_colons_do_not_gain_extra_pauses(self) -> None:
        for source in ('It is 12:30 now.', 'Visit https://example.com.',
                       'The answer is: yes.'):
            self.assertFalse(prepare_for_speech(source).pauses)

    def test_empty_text_stays_empty(self) -> None:
        self.assertEqual(format_for_speech(" \n\n "), "")

    def test_speech_document_maps_formatted_words_to_displayed_text(self) -> None:
        source = "Sentinel 27's Testimony\n\n1. First item\n• Naytiba's second item"
        document = prepare_for_speech(source)

        self.assertEqual(
            document.spoken_text,
            "Sentinel 27's Testimony. one: First item. Naytiba's second item.",
        )
        self.assertEqual(
            [source[word.source_start : word.source_end] for word in document.words],
            ["Sentinel", "27's", "Testimony", "1", "First", "item", "Naytiba's", "second", "item"],
        )
        self.assertEqual(
            [document.spoken_text[word.spoken_start : word.spoken_end] for word in document.words],
            ["Sentinel", "27's", "Testimony", "one", "First", "item", "Naytiba's", "second", "item"],
        )

    def test_repeated_words_and_compacted_whitespace_keep_forward_mapping(self) -> None:
        source = "One   One\n\n- One"
        document = prepare_for_speech(source)

        self.assertEqual(document.spoken_text, "One One. One.")
        self.assertEqual(
            [source[word.source_start : word.source_end] for word in document.words],
            ["One", "One", "One"],
        )

    def test_sentence_navigation_uses_spoken_pauses_and_source_positions(self) -> None:
        source = "Sentinel's Testimony\n\n1. Dr. Smith saw 3.14 clues.\n• First item!\n• Second item?"
        document = prepare_for_speech(source)
        self.assertEqual(
            [source[part.source_start:part.source_end] for part in document.sentences],
            ["Sentinel's Testimony", "1. Dr. Smith saw 3.14 clues", "First item", "Second item"],
        )
        self.assertEqual(
            [document.spoken_text[part.spoken_start:part.spoken_end] for part in document.sentences],
            ["Sentinel's Testimony", "one: Dr. Smith saw 3.14 clues", "First item", "Second item"],
        )

    def test_wrapped_line_and_unicode_stay_in_one_sentence(self) -> None:
        source = "😀 Go across the\nclassroom. Then return!"
        document = prepare_for_speech(source)
        self.assertEqual(len(document.sentences), 2)
        self.assertEqual(source[document.sentences[0].source_start:document.sentences[0].source_end],
                         "Go across the\nclassroom")

    def test_structural_pauses_distinguish_lists_paragraphs_and_sections(self) -> None:
        source = ("Introduction:\n\n- Parent:\n  - First detail\n  - Second detail\n"
                  "\n---\nNext section\n\nA wrapped\nline.")
        document = prepare_for_speech(source)
        pauses = {source[p.source_offset:document.words[next(i for i, w in enumerate(document.words)
                  if w.spoken_start == p.spoken_offset)].source_end]: p.milliseconds
                  for p in document.pauses}
        self.assertEqual(pauses["Parent"], 350)
        self.assertEqual(pauses["First"], 350)
        self.assertEqual(pauses["Second"], 180)
        self.assertEqual(pauses["Next"], 700)
        self.assertEqual(pauses["A"], 350)
        self.assertNotIn("line", pauses)
        self.assertNotIn("---", document.spoken_text)
        suffix = document.from_source(source.index('Second'))
        self.assertEqual(suffix.spoken_text[:6], 'Second')
        self.assertTrue(any(p.milliseconds == 700 for p in suffix.pauses))

    def test_markup_escapes_source_and_maps_utf16_word_offsets(self) -> None:
        document = prepare_for_speech('😀 A & B < C\n\n- D')
        self.assertTrue(document.pauses)
        for backend, marker in [('winrt', '<break time='), ('sapi', '<silence msec=')]:
            prepared = synthesis_input(document.spoken_text, document.pauses, backend)
            self.assertIn(marker, prepared.text)
            self.assertIn('&amp;', prepared.text)
            self.assertIn('&lt;', prepared.text)
            for word in document.words:
                start = prepared.spoken_to_native[word.spoken_start]
                end = prepared.spoken_to_native[word.spoken_end]
                self.assertEqual(prepared.native_to_spoken[start], word.spoken_start)
                self.assertEqual(prepared.native_to_spoken[end], word.spoken_end)


if __name__ == "__main__":
    unittest.main()
