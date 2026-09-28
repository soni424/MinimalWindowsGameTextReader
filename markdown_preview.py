"""Embedded, read-only WebView2 Markdown preview with no privileged bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import tkinter as tk
from tkinter import ttk
from urllib.parse import urlparse
import webbrowser
from typing import Callable

from markdown_reader import render_markdown, prepare_markdown_for_speech


_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' https:; connect-src 'none'; "
    "frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'"
)


def _assets_path() -> Path:
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    return base / 'assets' / 'markdown_preview'


class MarkdownPreview:
    def __init__(self, parent: ttk.Frame,
                 on_seek: Callable[[int, str], None] | None = None) -> None:
        self.parent = parent
        self.web = None
        self.ready = False
        self.source = ''
        self.theme = 'dark'
        self.images: tuple[str, ...] = ()
        self.approved = False
        self.word_index: int | None = None
        self.on_seek = on_seek
        self.revision = 0
        self._startup_timeout: str | None = None
        self.error = tk.StringVar(value='Preparing Markdown preview…')
        self.message = ttk.Label(parent, textvariable=self.error, style='CardHint.TLabel', wraplength=660)
        self.message.grid(row=0, column=0, sticky='nw', padx=12, pady=12)

    @staticmethod
    def _external(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme in ('http', 'https', 'mailto') and url and not parsed.username and not parsed.password:
            webbrowser.open(url)
        return False

    def _navigation(self, event) -> bool:
        url = event.url
        if (url.startswith('https://tkwry.localhost/')
                or url.startswith('tkwry://localhost/')
                or url == 'about:blank'):
            return True
        return self._external(url)

    def _new_window(self, event):
        from tkwry import NewWindowResponse
        self._external(event.url)
        return NewWindowResponse.Deny

    def ensure_open(self) -> None:
        if self.web is not None:
            return
        try:
            from tkwry import PageLoadEvent, PermissionResponse, WebView
            assets = _assets_path()
            if not (assets / 'index.html').exists():
                raise FileNotFoundError('Markdown preview assets are missing from this build.')
            self.web = WebView(
                self.parent, app=assets,
                data_directory=Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'GameTextReader' / 'WebView2',
                on_download=lambda _download: False,
                devtools=False, clipboard=True, default_context_menus=True,
                csp=_CSP, on_navigation=self._navigation,
                on_new_window=self._new_window,
                on_ipc=self._ipc_message,
                permission_handler=lambda _kind: PermissionResponse.Deny,
                on_page_load=lambda event, url: self._loaded() if (
                    event == PageLoadEvent.Finished and
                    (url.startswith('https://tkwry.localhost/') or url.startswith('tkwry://localhost/'))
                ) else None,
                on_creation_failed=lambda error: self._failed(str(error)),
            )
            self._startup_timeout = self.parent.after(12000, self._timed_out)
        except Exception as exc:
            self._failed(str(exc))

    def _cancel_timeout(self) -> None:
        if self._startup_timeout is not None:
            self.parent.after_cancel(self._startup_timeout)
            self._startup_timeout = None

    def _timed_out(self) -> None:
        self._startup_timeout = None
        if not self.ready:
            self._failed('WebView2 did not initialize within 12 seconds.')

    def _failed(self, reason: str) -> None:
        self._cancel_timeout()
        self.ready = False
        if self.web is not None:
            try:
                self.web.destroy()
            except Exception:
                pass
            self.web = None
        self.error.set('Markdown preview is unavailable. Install Microsoft Edge WebView2 Runtime, '
                       f'or return to Edit. Details: {reason}')
        self.message.grid()

    def _loaded(self) -> None:
        self._cancel_timeout()
        self.ready = True
        self.error.set('')
        self.message.grid_remove()
        self.refresh()
        self.parent.after_idle(self.sync_layout)

    def sync_layout(self) -> None:
        """Resync the native WebView after a Notebook or host layout change."""
        if self.web is not None:
            try:
                self.web.sync_bounds()
            except (RuntimeError, OSError, tk.TclError):
                # The native view may still be initializing or closing.
                pass

    def set_document(self, source: str, theme: str) -> bool:
        if source != self.source:
            self.approved = False
            self.word_index = None
            self.revision += 1
        self.source = source
        self.theme = theme
        self.images = render_markdown(source).images
        self.ensure_open()
        if self.ready:
            self.refresh()
        return bool(self.images)

    def refresh(self) -> None:
        if not self.ready or self.web is None:
            return
        rendered = render_markdown(self.source)
        self.images = rendered.images
        document = prepare_markdown_for_speech(self.source)
        words = [document.spoken_text[w.spoken_start:w.spoken_end] for w in document.words]
        script = ('window.readerRender(' + ','.join(json.dumps(value, ensure_ascii=True) for value in
                  (rendered.html, self.theme, words, self.revision)) + ')')
        try:
            self.web.eval_js(script)
        except Exception as exc:
            self._failed(str(exc))
            return
        if self.approved:
            self.load_images()
        if self.word_index is not None:
            self.highlight(self.word_index)

    def load_images(self) -> None:
        self.approved = True
        if self.ready and self.web is not None and self.images:
            try:
                self.web.eval_js('window.readerLoadImages(' + json.dumps(self.images) + ')')
            except Exception as exc:
                self._failed(str(exc))

    def _ipc_message(self, payload: str) -> None:
        """Only a word index from the current local document may seek speech."""
        try:
            message = json.loads(payload)
            if not isinstance(message, dict) or message.get('kind') != 'seek':
                return
            index, revision = message.get('index'), message.get('revision')
            if type(index) is not int or type(revision) is not int or revision != self.revision:
                return
            document = prepare_markdown_for_speech(self.source)
            if not 0 <= index < len(document.words):
                return
        except (TypeError, ValueError):
            return
        if self.on_seek is not None:
            source = self.source
            self.parent.after(0, lambda: self.on_seek(index, source))

    def highlight(self, index: int) -> None:
        self.word_index = index
        if self.ready and self.web is not None:
            try:
                self.web.eval_js(f'window.readerHighlight({int(index)})')
            except Exception as exc:
                self._failed(str(exc))

    def clear_highlight(self) -> None:
        self.word_index = None
        if self.ready and self.web is not None:
            try:
                self.web.eval_js('window.readerClearHighlight()')
            except Exception as exc:
                self._failed(str(exc))

    def close(self) -> None:
        self._cancel_timeout()
        if self.web is not None:
            self.web.destroy()
            self.web = None


def run_packaged_preview_smoke() -> bool:
    """Internal release check: exercise bundled assets and native WebView2."""
    root = tk.Tk()
    root.geometry('720x480')
    host = ttk.Frame(root)
    host.pack(fill='both', expand=True)
    host.columnconfigure(0, weight=1)
    host.rowconfigure(0, weight=1)
    preview = MarkdownPreview(host)
    preview.set_document('# Packaged Markdown preview\n\n**Ready** and $x^2$.', 'dark')
    result: list[str] = []
    requested = False
    deadline = time.monotonic() + 12
    try:
        while time.monotonic() < deadline:
            root.update()
            if preview.ready and not requested:
                preview.web.eval_js_with_callback(
                    'document.querySelector("h1")?.textContent || ""', result.append)
                requested = True
            if result:
                return 'Packaged Markdown preview' in result[0]
            time.sleep(.04)
        return False
    finally:
        preview.close()
        root.destroy()
