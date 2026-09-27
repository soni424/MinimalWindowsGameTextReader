"""Markdown Reader preview, safety, and speech mapping regressions."""
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tkinter as tk

from config import ConfigStore
from main import GameTextReaderApplication
from markdown_preview import MarkdownPreview, _CSP
from markdown_reader import looks_like_markdown, prepare_markdown_for_speech, render_markdown
from reader_state import ReaderTextState
from settings_ui import SettingsUI


class MarkdownReaderTests(unittest.TestCase):
    def test_detection_does_not_switch_plain_paste(self):
        self.assertFalse(looks_like_markdown('I saw it. Another line of dialogue.'))
        self.assertTrue(looks_like_markdown('# A title\n\n**Bold** words'))
        self.assertTrue(looks_like_markdown('- [x] A completed task'))

    def test_renders_rich_sample_features_without_remote_image_request(self):
        text = ('# Science\n\n**Bold** and *italic* and ~~removed~~.\n\n'
                '- [x] Read evidence\n\n| Name | Value |\n| --- | --- |\n| A | 2 |\n\n'
                '$x^2$ and $$\\frac{1}{2}$$\n\n```python\nprint(1)\n```\n\n'
                '<details><summary>More</summary>Hidden prose</details>\n\n'
                'A footnote[^1].\n\n[^1]: Footnote prose.\n\n'
                '![Diagram](https://example.com/image.png)')
        rendered = render_markdown(text)
        self.assertIn('<h1>Science</h1>', rendered.html)
        self.assertIn('<strong>Bold</strong>', rendered.html)
        self.assertIn('<table>', rendered.html)
        self.assertIn('math-inline', rendered.html)
        self.assertIn('<details>', rendered.html)
        self.assertIn('Footnote prose', rendered.html)
        self.assertNotIn('<img', rendered.html)
        self.assertNotIn('src="https://example.com/image.png"', rendered.html)
        self.assertEqual(rendered.images, ('https://example.com/image.png',))

    def test_unsafe_html_and_protocols_are_removed(self):
        source = ('<script>alert(1)</script><img src="https://evil.example/a">'
                  '<a href="javascript:alert(2)" onclick="alert(3)">click</a>'
                  '![Local](file:///C:/secret.png)')
        rendered = render_markdown(source)
        self.assertNotIn('<script', rendered.html)
        self.assertNotIn('<img', rendered.html)
        self.assertNotIn('javascript:', rendered.html)
        self.assertNotIn('onclick', rendered.html)
        self.assertNotIn('alert(1)', rendered.html)
        self.assertEqual(rendered.images, ())
        self.assertNotIn('alert', prepare_markdown_for_speech(source).spoken_text)

    def test_speech_omits_markup_code_urls_and_math_but_maps_repeated_words(self):
        source = ('# Heading\n\nMary saw Mary. [Open site](https://example.com).\n\n'
                  '- One item\n- Two items\n\n| Name | Value |\n| --- | --- |\n'
                  '| Alpha | Beta |\n\n![Chart](https://example.com/x.png)\n\n'
                  '`code` $x^2$\n\n```python\nprint(1)\n```')
        document = prepare_markdown_for_speech(source)
        self.assertIn('Heading. Mary saw Mary.', document.spoken_text)
        self.assertIn('Open site', document.spoken_text)
        self.assertIn('One item. Two items.', document.spoken_text)
        self.assertIn('Chart', document.spoken_text)
        for omitted in ('https://', 'print', 'code', 'x^2', '---'):
            self.assertNotIn(omitted, document.spoken_text)
        mary_positions = [w.source_start for w in document.words
                          if document.spoken_text[w.spoken_start:w.spoken_end] == 'Mary']
        self.assertEqual(mary_positions, [source.index('Mary'), source.index('Mary', source.index('Mary') + 1)])
        self.assertGreater(len(document.sentences), 3)

    def test_footnote_prose_is_spoken_but_reference_urls_are_not(self):
        source = 'Claim[^1].\n\n[^1]: Evidence for the claim.\n\n[site]: https://example.com'
        spoken = prepare_markdown_for_speech(source).spoken_text
        self.assertIn('Evidence for the claim.', spoken)
        self.assertNotIn('https:', spoken)

    def test_external_navigation_is_restricted_and_browser_bridge_is_not_exposed(self):
        preview = MarkdownPreview.__new__(MarkdownPreview)
        with patch('markdown_preview.webbrowser.open') as browser:
            self.assertTrue(preview._navigation(SimpleNamespace(url='https://tkwry.localhost/index.html')))
            self.assertFalse(preview._navigation(SimpleNamespace(url='javascript:alert(1)')))
            self.assertFalse(preview._navigation(SimpleNamespace(url='file:///C:/secret.txt')))
            browser.assert_not_called()
            self.assertFalse(preview._navigation(SimpleNamespace(url='https://example.com/')))
            browser.assert_called_once_with('https://example.com/')
        self.assertIn("connect-src 'none'", _CSP)
        self.assertIn("frame-src 'none'", _CSP)

    def test_remote_images_are_sent_to_browser_only_after_explicit_approval(self):
        preview = MarkdownPreview.__new__(MarkdownPreview)
        calls = []
        preview.web = SimpleNamespace(eval_js=calls.append)
        preview.ready = True
        preview.images = ('https://example.com/a.png',)
        preview.approved = False
        self.assertEqual(calls, [])
        preview.load_images()
        self.assertTrue(preview.approved)
        self.assertEqual(len(calls), 1)
        self.assertIn('https://example.com/a.png', calls[0])

    def test_read_again_and_play_use_the_same_markdown_document(self):
        source = '# Heading\n\n**Visible** words and `code`.'
        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.text_state = ReaderTextState()
        app.text_state.accept_manual_text(source)
        documents = []
        app.ui = SimpleNamespace(prepare_reader_document=prepare_markdown_for_speech,
                                 set_status=lambda *_args, **_kwargs: None)
        app.config = SimpleNamespace(get=lambda: {'voice':'x', 'rate':0, 'volume':50,
                                                   'speech':{'reader_playback_rate':1.0}})
        app.tts = SimpleNamespace(
            stop=lambda: None,
            replace_reader=lambda document, *_args: documents.append(document) or SimpleNamespace(request_id=11),
            play_reader=lambda document, *_args: documents.append(document) or SimpleNamespace(request_id=12),
        )
        app.read_again()
        app.reader_play_pause(None, source, False)
        self.assertEqual([document.spoken_text for document in documents],
                         ['Heading. Visible words and.', 'Heading. Visible words and.'])


class MarkdownReaderUiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        store = ConfigStore(Path(self.folder.name) / 'config.json')
        store.load()
        tts = SimpleNamespace(list_voices=lambda: [], stop=lambda: None)
        self.ui = SettingsUI(self.root, store, tts, lambda: None, lambda: None,
                             lambda: None, lambda *_args: None)

    def tearDown(self):
        self.ui.close()
        self.root.destroy()
        self.folder.cleanup()

    def test_paste_switches_to_preview_and_new_ocr_returns_to_edit(self):
        source = '# Science\n\n**Evidence** matters.'
        with patch.object(self.ui.markdown_preview, 'set_document', return_value=False):
            self.ui._reader_paste(None)
            self.ui.captured_text.insert('1.0', source)
            self.root.update()
            self.assertEqual(self.ui.reader_tabs.select(), str(self.ui.reader_preview_frame))
            self.assertEqual(self.ui.prepare_reader_document(source).spoken_text,
                             'Science. Evidence matters.')
            self.ui.copy_text()
            self.assertEqual(self.root.clipboard_get(), source)
            self.ui.set_last_text('Plain OCR text.')
            self.assertEqual(self.ui.reader_tabs.select(), str(self.ui.captured_text_frame))
            self.assertEqual(self.ui.prepare_reader_document('Plain OCR text.').spoken_text,
                             'Plain OCR text.')

    def test_preview_follows_theme_and_document_words_remain_source_mapped(self):
        source = '# Heading\n\nWord and Word.'
        with patch.object(self.ui.markdown_preview, 'set_document', return_value=False) as update:
            self.ui.captured_text.insert('1.0', source)
            self.ui.reader_tabs.select(self.ui.reader_preview_frame)
            self.root.update()
            self.assertTrue(update.called)
            self.ui.theme_value.set('Dark')
            self.ui._theme_changed()
            self.assertEqual(update.call_args.args[1], 'dark')
            document = self.ui.prepare_reader_document(source)
            self.assertEqual([source[w.source_start:w.source_end] for w in document.words],
                             ['Heading', 'Word', 'and', 'Word'])

    def test_unavailable_webview_leaves_edit_accessible(self):
        with patch('markdown_preview._assets_path', return_value=Path(self.folder.name) / 'missing'):
            self.ui.captured_text.insert('1.0', '# Safe text')
            self.ui.reader_tabs.select(self.ui.reader_preview_frame)
            self.root.update()
            self.assertIn('preview is unavailable', self.ui.markdown_preview.error.get())
            self.ui.reader_tabs.select(self.ui.captured_text_frame)
            self.assertEqual(self.ui.captured_text.get('1.0', 'end-1c'), '# Safe text')


if __name__ == '__main__':
    unittest.main()
