"""System-tray integration for keeping the reader ready during gameplay."""

from __future__ import annotations

from typing import Callable


class TrayApp:
    """A small pystray wrapper that routes tray actions back to Tk safely."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_read_fixed: Callable[[], None],
        on_quick_snippet: Callable[[], None],
        on_stop_speech: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._callbacks = {
            "show": on_show,
            "fixed": on_read_fixed,
            "snippet": on_quick_snippet,
            "stop": on_stop_speech,
            "quit": on_quit,
        }
        self._icon = None

    @staticmethod
    def _make_icon_image():
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), "#0f172a")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((9, 8, 55, 56), radius=10, fill="#2563eb")
        draw.rectangle((18, 18, 46, 22), fill="#e0f2fe")
        draw.rectangle((18, 29, 42, 33), fill="#e0f2fe")
        draw.rectangle((18, 40, 36, 44), fill="#e0f2fe")
        return image

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
                    pystray.MenuItem("Stop audio", lambda *_: self._invoke("stop")),
                    pystray.Menu.SEPARATOR,
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
