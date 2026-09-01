"""System-tray integration for keeping the reader ready during gameplay."""

from __future__ import annotations

from typing import Callable

from app_resources import load_app_icon


class TrayApp:
    """A small pystray wrapper that routes tray actions back to Tk safely."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_read_fixed: Callable[[], None],
        on_quick_snippet: Callable[[], None],
        on_stop_speech: Callable[[], None],
        on_quit: Callable[[], None],
        *,
        on_hide: Callable[[], None] | None = None,
        on_read_again: Callable[[], None] | None = None,
    ) -> None:
        self._callbacks = {
            "show": on_show,
            "fixed": on_read_fixed,
            "snippet": on_quick_snippet,
            "stop": on_stop_speech,
            "quit": on_quit,
        }
        if on_hide is not None:
            self._callbacks["hide"] = on_hide
        if on_read_again is not None:
            self._callbacks["again"] = on_read_again
        self._icon = None

    @staticmethod
    def _make_icon_image():
        return load_app_icon(64)

    def _invoke(self, name: str) -> None:
        callback = self._callbacks[name]
        callback()

    def start(self) -> None:
        """Start pystray on its own loop without blocking Tk's event loop."""
        try:
            import pystray

            self._icon = pystray.Icon(
                "game_text_reader",
                self._make_icon_image(),
                "Game Text Reader",
                menu=pystray.Menu(
                    pystray.MenuItem("Show settings", lambda *_: self._invoke("show"), default=True),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Read fixed box now", lambda *_: self._invoke("fixed")),
                    pystray.MenuItem("Quick snippet", lambda *_: self._invoke("snippet")),
                    *(
                        [pystray.MenuItem("Read last text again", lambda *_: self._invoke("again"))]
                        if "again" in self._callbacks
                        else []
                    ),
                    pystray.MenuItem("Stop audio", lambda *_: self._invoke("stop")),
                    pystray.Menu.SEPARATOR,
                    *(
                        [pystray.MenuItem("Hide settings to tray", lambda *_: self._invoke("hide"))]
                        if "hide" in self._callbacks
                        else []
                    ),
                    pystray.MenuItem("Quit", lambda *_: self._invoke("quit")),
                ),
            )
            self._icon.run_detached()
        except Exception:
            # The main app remains usable if a desktop policy blocks tray icons.
            self._icon = None

    def stop(self) -> None:
        """Remove the icon before the process exits."""
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
