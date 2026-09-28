"""Regression coverage for reader navigation and contextual correction."""
import unittest
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk

from appearance import DARK, LIGHT
from ocr_correction import OcrCorrector, CorrectionOptions
from settings_ui import SettingsUI, _ShortcutRecorderDialog
from speech_text import prepare_for_speech


class ReaderUpgradeTests(unittest.TestCase):
    def test_numbered_marker_and_content_have_distinct_source_positions(self):
        numbered = prepare_for_speech('1. 1 survivor remains.')
        self.assertEqual(numbered.spoken_text, 'one: 1 survivor remains.')
        self.assertEqual([word.source_start for word in numbered.words[:2]], [0, 3])
        alphabetic = prepare_for_speech('A. A hero waits.')
        self.assertEqual(alphabetic.words[0].source_start, 3)

    def test_released_modifier_is_not_recorded(self):
        recorder = _ShortcutRecorderDialog.__new__(_ShortcutRecorderDialog)
        recorder._pressed_modifiers = set()
        recorder.prompt = SimpleNamespace(set=lambda value: None)
        recorder.window = SimpleNamespace(destroy=lambda: None)
        recorder.result = None
        recorder._key_pressed(SimpleNamespace(keysym='Control_L', state=0))
        recorder._key_released(SimpleNamespace(keysym='Control_L', state=0))
        recorder._key_pressed(SimpleNamespace(keysym='q', state=0x20000))
        self.assertEqual(recorder.result, 'Alt+Q')

    def test_all_scrollbar_states_follow_palette(self):
        root = tk.Tk()
        root.withdraw()
        try:
            ui = SettingsUI.__new__(SettingsUI)
            ui.root = root
            ui.style = ttk.Style(root)
            ui.style.theme_use('clam')
            for palette in (DARK, LIGHT):
                ui._configure_styles(palette)
                for state in ((), ('disabled',), ('active',), ('pressed',)):
                    value = ui.style.lookup('Vertical.TScrollbar', 'background', state)
                    self.assertIn(value, (palette.button, palette.button_hover, palette.accent_pressed))
        finally:
            root.destroy()

    def test_names_are_not_dictionary_normalized(self):
        for strength in ('conservative', 'balanced', 'strong'):
            text = 'Travel safely to Xion. Yohan, please stay. Thank you, Mory.'
            self.assertEqual(OcrCorrector().correct(text, CorrectionOptions(strength=strength)).corrected_text, text)


if __name__ == '__main__':
    unittest.main()
