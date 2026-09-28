"""Markdown Reader preview, safety, and speech mapping regressions."""
import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tkinter as tk

from config import ConfigStore
from main import GameTextReaderApplication
from markdown_preview import MarkdownPreview, _CSP
from markdown_reader import looks_like_markdown, prepare_markdown_for_speech, render_markdown
from reader_state import ReaderTextState
from settings_ui import SettingsUI


class MarkdownReaderTests(unittest.TestCase):
    def test_emphasized_list_labels_pause_before_explanations(self):
        source = ('- **Empirical Evidence:** Ideas must be backed by data.\n'
                  '- **Falsifiability:** A claim must be testable.\n'
                  '- **Self-Correction:** Science can change.\n'
                  '- **Objectivity:** Experiments reduce bias.')
        document = prepare_markdown_for_speech(source)
        for label, next_word in [('Empirical Evidence:', 'Ideas'),
                                 ('Falsifiability:', 'A'),
                                 ('Self-Correction:', 'Science'),
                                 ('Objectivity:', 'Experiments')]:
            self.assertIn(label, document.spoken_text)
            offset = source.index(next_word, source.index(label))
            word = next(word for word in document.words if word.source_start == offset)
            self.assertIn(250, [pause.milliseconds for pause in document.pauses
                                if pause.spoken_offset == word.spoken_start])
        self.assertEqual(document.source_text, source)

    def test_ordered_steps_are_spoken_and_mapped_to_source_digits(self):
        source = ('### 1. How Science Works\n\n' + ''.join(
            f'{index}. **{name}:** Details.\n' for index, name in enumerate(
                ('Observation', 'Question', 'Hypothesis', 'Experimentation',
                 'Analysis', 'Peer Review'), 1)))
        document = prepare_markdown_for_speech(source)
        self.assertIn('1. How Science Works.', document.spoken_text)
        for index, name in enumerate(('Observation', 'Question', 'Hypothesis',
                                      'Experimentation', 'Analysis', 'Peer Review'), 1):
            word = ('one', 'two', 'three', 'four', 'five', 'six')[index - 1]
            self.assertIn(f'{word}: {name}', document.spoken_text)
            marker = next(span for span in document.words if
                          document.spoken_text[span.spoken_start:span.spoken_end] == word)
            self.assertEqual(source[marker.source_start:marker.source_end], str(index))

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

    def test_nested_science_outline_has_relative_pause_levels(self):
        source = ('### 2. The Main Branches of Science\n\n'
                  'Because the universe is vast, science is divided into several broad categories:\n\n'
                  '- **Natural Sciences** (The physical and biological world):\n'
                  '  - *Physical Sciences:* Physics (matter, energy, motion).\n'
                  '  - *Life Sciences:* Zoology, Botany.\n'
                  '- **Social Sciences** (Human behavior):\n'
                  '  - Psychology and Sociology.\n\n---\n\n'
                  '### 3. Key Principles of Science\n\nEmpirical evidence matters.')
        document = prepare_markdown_for_speech(source)
        by_word = {document.spoken_text[p.spoken_offset:].split()[0]: p.milliseconds
                   for p in document.pauses}
        self.assertEqual(by_word['Physical'], 350)
        self.assertEqual(by_word['Life'], 180)
        self.assertEqual(by_word['Social'], 350)
        self.assertEqual(by_word['3.'], 700)
        self.assertNotIn('**', document.spoken_text)
        self.assertNotIn('---', document.spoken_text)
        self.assertTrue(all(source[word.source_start:word.source_end].strip()
                            for word in document.words))

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

    def test_preview_bridge_accepts_only_current_document_word_index(self):
        preview = MarkdownPreview.__new__(MarkdownPreview)
        calls = []
        preview.parent = SimpleNamespace(after=lambda _delay, callback: callback())
        preview.on_seek = lambda index, source: calls.append((index, source))
        preview.source = '1. **Observation:** Notice details.'
        preview.revision = 4
        preview._ipc_message(json.dumps({'kind': 'seek', 'index': 0, 'revision': 3}))
        preview._ipc_message(json.dumps({'kind': 'seek', 'index': -1, 'revision': 4}))
        preview._ipc_message(json.dumps({'kind': 'seek', 'index': True, 'revision': 4}))
        self.assertEqual(calls, [])
        preview._ipc_message(json.dumps({'kind': 'seek', 'index': 0, 'revision': 4}))
        self.assertEqual(calls, [(0, preview.source)])

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

    def test_preview_seek_uses_current_active_document_only(self):
        source = '1. **Observation:** Notice details.'
        seeks = []
        self.ui.on_seek = lambda *args: seeks.append(args)
        self.ui.captured_text.insert('1.0', source)
        self.ui._markdown_mode = True
        self.ui.begin_speech_progress(7, source)
        self.ui._preview_seek_word(0, source)
        self.assertEqual(seeks, [(7, source, source.index('1'))])
        self.ui._preview_seek_word(0, 'old document')
        self.ui.clear_speech_progress(7)
        self.ui._preview_seek_word(0, source)
        self.assertEqual(len(seeks), 1)

    def test_prepared_markdown_is_reused_until_source_changes(self):
        source = '# Science\n\n1. **Observation:** Notice details.'
        self.ui.captured_text.insert('1.0', source)
        self.ui._markdown_mode = True
        first = self.ui.prepare_reader_document(source)
        self.assertIs(self.ui.prepare_reader_document(source), first)
        self.ui.captured_text.insert('end', '\n2. Question')
        self.ui._captured_text_modified()
        changed = self.ui.captured_text.get('1.0', 'end-1c')
        self.assertIsNot(self.ui.prepare_reader_document(changed), first)

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

    def test_preview_viewport_fills_reader_after_switch(self):
        self.root.deiconify()
        self.root.geometry('1500x900+40+40')
        self.ui.captured_text.insert('1.0', '# Heading\n\nA paragraph to preview.')
        with patch.object(self.ui.markdown_preview, 'set_document', return_value=False):
            self.ui.reader_tabs.select(self.ui.reader_preview_frame)
            self.root.update()
        host = self.ui.markdown_preview.parent
        self.assertGreater(host.winfo_height(), 300, (
            f'root={self.root.winfo_width()}x{self.root.winfo_height()} '
            f'reader={self.ui.reader_tabs.master.master.winfo_height()} '
            f'card={self.ui.reader_tabs.master.winfo_height()} '
            f'notebook={self.ui.reader_tabs.winfo_width()}x{self.ui.reader_tabs.winfo_height()} '
            f'page={self.ui.reader_preview_frame.winfo_width()}x{self.ui.reader_preview_frame.winfo_height()} '
            f'host={host.winfo_width()}x{host.winfo_height()}'
        ))
        self.assertGreaterEqual(host.winfo_height(), self.ui.reader_tabs.winfo_height() * 0.7)
        self.assertGreaterEqual(host.winfo_width(), self.ui.reader_tabs.winfo_width() * 0.8)

    def test_split_reader_sidebar_collapses_and_other_tabs_keep_quick_actions(self):
        self.root.deiconify()
        self.root.geometry('1500x900+40+40')
        self.root.update()
        scale = max(1.0, float(self.root.winfo_fpixels('1i')) / 96.0)
        self.ui._layout_reader_workspace(round(1300 * scale))
        self.root.update_idletasks()
        self.assertFalse(self.ui.quick_actions_card.winfo_manager())
        self.assertTrue(self.ui.reader_sidebar.winfo_ismapped())
        self.assertFalse(self.ui.capture_controls_button.winfo_ismapped())
        self.assertGreater(self.ui.reader_tabs.winfo_width(), self.ui.reader_sidebar.winfo_width())

        self.root.geometry('850x720+40+40')
        self.root.update()
        self.assertFalse(self.ui.reader_sidebar.winfo_ismapped())
        self.assertTrue(self.ui.capture_controls_button.winfo_ismapped())
        self.ui.capture_controls_button.invoke()
        self.root.update()
        self.assertTrue(self.ui.reader_sidebar.winfo_ismapped())
        self.assertEqual(self.ui.capture_controls_button.cget('text'), 'Hide capture controls')

        self.ui.notebook.select(1)
        self.root.update()
        self.assertTrue(self.ui.quick_actions_card.winfo_ismapped())
        self.ui.notebook.select(0)
        self.root.update()
        self.assertFalse(self.ui.quick_actions_card.winfo_manager())

    def test_capture_sidebar_scrolls_and_follows_appearance(self):
        self.root.deiconify()
        self.root.geometry('1500x720+40+40')
        self.root.update()
        scale = max(1.0, float(self.root.winfo_fpixels('1i')) / 96.0)
        self.ui._layout_reader_workspace(round(1300 * scale))
        self.root.update_idletasks()
        canvas = self.ui.reader_sidebar_canvas
        self.assertTrue(self.ui.reader_sidebar.winfo_ismapped())
        self.assertLess(canvas.yview()[1], 1.0)
        canvas.yview_moveto(1.0)
        self.root.update()
        self.assertGreater(canvas.yview()[0], 0.0)
        for appearance in ('Dark', 'Light'):
            self.ui.theme_value.set(appearance)
            self.ui._theme_changed()
            self.assertEqual(canvas.cget('bg'), self.ui._palette.card)

    def test_preview_resyncs_native_bounds_after_layout_changes(self):
        self.root.deiconify()
        self.root.geometry('1500x900+40+40')
        native = SimpleNamespace(sync_bounds=Mock(), destroy=lambda: None)
        self.ui.markdown_preview.web = native
        with patch.object(self.ui.markdown_preview, 'set_document', return_value=False):
            self.ui.reader_tabs.select(self.ui.reader_preview_frame)
            self.root.update()
            self.assertTrue(native.sync_bounds.called)
            native.sync_bounds.reset_mock()
            self.root.geometry('1300x800+40+40')
            self.root.update()
            self.assertTrue(native.sync_bounds.called)

    def test_webview_does_not_move_preview_host_out_of_expanding_row(self):
        class HostWebView:
            def __init__(self, frame, **_kwargs):
                self.frame = frame

            def grid(self, **kwargs):
                self.frame.grid(**kwargs)

            def destroy(self):
                pass

        host = self.ui.markdown_preview.parent
        self.assertEqual(host.grid_info()['row'], 1)
        with patch('tkwry.WebView', HostWebView):
            self.ui.markdown_preview.ensure_open()
        self.assertEqual(host.grid_info()['row'], 1)


if __name__ == '__main__':
    unittest.main()
