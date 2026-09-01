"""Full-screen Tk overlays for fixed subtitle boxes and one-off snippets."""

from __future__ import annotations

import ctypes
import tkinter as tk
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class VirtualScreen:
    """Physical-pixel bounds of all connected Windows monitors."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


def virtual_screen() -> VirtualScreen:
    """Return virtual desktop bounds, including monitors left of the primary one."""
    try:
        user32 = ctypes.windll.user32
        return VirtualScreen(
            user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
            user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
            user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
            user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
        )
    except Exception:
        return VirtualScreen(0, 0, 1920, 1080)


class _ScreenOverlay:
    """Base class that owns a transparent, topmost virtual-desktop Toplevel."""

    def __init__(self, master: tk.Misc, alpha: float = 0.35) -> None:
        self.master = master
        self.screen = virtual_screen()
        self.window = tk.Toplevel(master)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", alpha)
        self.window.configure(bg="#000000")
        self.window.geometry(f"{self.screen.width}x{self.screen.height}{self.screen.left:+d}{self.screen.top:+d}")
        self.canvas = tk.Canvas(self.window, bg="#000000", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.window.focus_force()

    def screen_to_canvas(self, x: int, y: int) -> tuple[int, int]:
        return x - self.screen.left, y - self.screen.top

    def canvas_to_screen(self, x: float, y: float) -> tuple[int, int]:
        return int(round(x + self.screen.left)), int(round(y + self.screen.top))

    def close(self) -> None:
        """Destroy the overlay safely, even after a parent window has closed."""
        try:
            self.window.destroy()
        except tk.TclError:
            pass


class BoxEditorOverlay(_ScreenOverlay):
    """Move, resize, save, or cancel a persistent fixed OCR bounding box."""

    HANDLE_SIZE = 12
    MIN_SIZE = 20

    def __init__(
        self,
        master: tk.Misc,
        existing_box: list[int] | tuple[int, int, int, int] | None,
        on_confirm: Callable[[list[int]], None],
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__(master, alpha=0.38)
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        self._finished = False
        self._active_handle = ""
        self._drag_origin = (0.0, 0.0)
        self._start_rect = (0.0, 0.0, 0.0, 0.0)
        if existing_box and len(existing_box) == 4:
            left, top = self.screen_to_canvas(int(existing_box[0]), int(existing_box[1]))
            right, bottom = self.screen_to_canvas(int(existing_box[2]), int(existing_box[3]))
            self.rect = self._normalise_and_clamp((left, top, right, bottom))
        else:
            width, height = min(500, self.screen.width - 40), min(180, self.screen.height - 40)
            left = max(20, (self.screen.width - width) // 2)
            top = max(20, (self.screen.height - height) // 2)
            self.rect = (left, top, left + width, top + height)

        self._build_controls()
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.window.bind("<Return>", lambda _event: self.confirm())
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self._draw()

    def _build_controls(self) -> None:
        controls = tk.Frame(self.canvas, bg="#1e293b", padx=10, pady=7)
        tk.Label(
            controls,
            text="Drag inside to move • drag handles/edges to resize • Enter saves • Esc cancels",
            fg="#f8fafc",
            bg="#1e293b",
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 12))
        tk.Button(controls, text="Confirm / Done", command=self.confirm, bg="#16a34a", fg="white", relief="flat", padx=10).pack(side="left", padx=3)
        tk.Button(controls, text="Cancel", command=self.cancel, bg="#475569", fg="white", relief="flat", padx=10).pack(side="left", padx=3)
        self.canvas.create_window(self.screen.width // 2, 28, window=controls)

    def _normalise_and_clamp(self, rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        left, top, right, bottom = rect
        left, right = sorted((max(0, min(self.screen.width, left)), max(0, min(self.screen.width, right))))
        top, bottom = sorted((max(0, min(self.screen.height, top)), max(0, min(self.screen.height, bottom))))
        if right - left < self.MIN_SIZE:
            right = min(self.screen.width, left + self.MIN_SIZE)
            left = max(0, right - self.MIN_SIZE)
        if bottom - top < self.MIN_SIZE:
            bottom = min(self.screen.height, top + self.MIN_SIZE)
            top = max(0, bottom - self.MIN_SIZE)
        return left, top, right, bottom

    def _draw(self) -> None:
        self.canvas.delete("selection")
        left, top, right, bottom = self.rect
        self.canvas.create_rectangle(left, top, right, bottom, outline="#22c55e", width=3, tags="selection")
        self.canvas.create_text(left + 8, max(58, top - 12), anchor="sw", fill="#dcfce7", font=("Segoe UI", 10, "bold"), text=f"{int(right-left)} × {int(bottom-top)} px", tags="selection")
        for x, y in self._handle_positions():
            self.canvas.create_rectangle(
                x - self.HANDLE_SIZE / 2,
                y - self.HANDLE_SIZE / 2,
                x + self.HANDLE_SIZE / 2,
                y + self.HANDLE_SIZE / 2,
                fill="#f8fafc",
                outline="#15803d",
                width=2,
                tags="selection",
            )

    def _handle_positions(self) -> list[tuple[float, float]]:
        left, top, right, bottom = self.rect
        middle_x, middle_y = (left + right) / 2, (top + bottom) / 2
        return [(left, top), (middle_x, top), (right, top), (right, middle_y), (right, bottom), (middle_x, bottom), (left, bottom), (left, middle_y)]

    def _hit_test(self, x: float, y: float) -> str:
        left, top, right, bottom = self.rect
        distance = self.HANDLE_SIZE
        names = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
        for name, (handle_x, handle_y) in zip(names, self._handle_positions()):
            if abs(x - handle_x) <= distance and abs(y - handle_y) <= distance:
                return name
        near = distance / 2
        if left - near <= x <= right + near and top - near <= y <= bottom + near:
            if abs(y - top) <= near:
                return "n"
            if abs(y - bottom) <= near:
                return "s"
            if abs(x - left) <= near:
                return "w"
            if abs(x - right) <= near:
                return "e"
            if left < x < right and top < y < bottom:
                return "move"
        return ""

    def _press(self, event: tk.Event) -> None:
        self._active_handle = self._hit_test(event.x, event.y)
        self._drag_origin = (event.x, event.y)
        self._start_rect = self.rect

    def _drag(self, event: tk.Event) -> None:
        if not self._active_handle:
            return
        origin_x, origin_y = self._drag_origin
        dx, dy = event.x - origin_x, event.y - origin_y
        left, top, right, bottom = self._start_rect
        handle = self._active_handle
        if handle == "move":
            width, height = right - left, bottom - top
            left = max(0, min(self.screen.width - width, left + dx))
            top = max(0, min(self.screen.height - height, top + dy))
            right, bottom = left + width, top + height
        else:
            if "w" in handle:
                left += dx
            if "e" in handle:
                right += dx
            if "n" in handle:
                top += dy
            if "s" in handle:
                bottom += dy
            # Keep the edge opposite the active drag anchored when enforcing
            # the minimum size, rather than unexpectedly moving the box.
            if right - left < self.MIN_SIZE:
                if "w" in handle:
                    left = right - self.MIN_SIZE
                else:
                    right = left + self.MIN_SIZE
            if bottom - top < self.MIN_SIZE:
                if "n" in handle:
                    top = bottom - self.MIN_SIZE
                else:
                    bottom = top + self.MIN_SIZE
            left, top, right, bottom = self._normalise_and_clamp((left, top, right, bottom))
        self.rect = left, top, right, bottom
        self._draw()

    def _release(self, _event: tk.Event) -> None:
        self._active_handle = ""

    def confirm(self) -> None:
        """Save absolute physical-pixel coordinates through the supplied callback."""
        if self._finished:
            return
        self._finished = True
        left, top, right, bottom = self.rect
        screen_left, screen_top = self.canvas_to_screen(left, top)
        screen_right, screen_bottom = self.canvas_to_screen(right, bottom)
        self.close()
        self.master.after_idle(lambda: self._on_confirm([screen_left, screen_top, screen_right, screen_bottom]))

    def cancel(self) -> None:
        """Close without changing the previously saved fixed box."""
        if self._finished:
            return
        self._finished = True
        self.close()
        self.master.after_idle(self._on_cancel)


class QuickSnippetOverlay(_ScreenOverlay):
    """Windows Snipping Tool-style selector that never alters the fixed box."""

    def __init__(self, master: tk.Misc, on_capture: Callable[[list[int]], None], on_cancel: Callable[[], None]) -> None:
        super().__init__(master, alpha=0.36)
        self._on_capture = on_capture
        self._on_cancel = on_cancel
        self._start: tuple[float, float] | None = None
        self._rectangle_id: int | None = None
        self._finished = False
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

    def _press(self, event: tk.Event) -> None:
        self._start = event.x, event.y
        if self._rectangle_id is not None:
            self.canvas.delete(self._rectangle_id)
        self._rectangle_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#ffffff", width=2)

    def _drag(self, event: tk.Event) -> None:
        if self._start is None or self._rectangle_id is None:
            return
        self.canvas.coords(self._rectangle_id, self._start[0], self._start[1], event.x, event.y)

    def _release(self, event: tk.Event) -> None:
        if self._start is None:
            return
        start_x, start_y = self._start
        left, right = sorted((start_x, event.x))
        top, bottom = sorted((start_y, event.y))
        if right - left < 3 or bottom - top < 3:
            self.cancel()
            return
        self._finished = True
        screen_left, screen_top = self.canvas_to_screen(left, top)
        screen_right, screen_bottom = self.canvas_to_screen(right, bottom)
        self.close()
        # Let Windows repaint the underlying game before ImageGrab runs.
        self.master.after(140, lambda: self._on_capture([screen_left, screen_top, screen_right, screen_bottom]))

    def cancel(self) -> None:
        """Dismiss the selector without capturing any screen content."""
        if self._finished:
            return
        self._finished = True
        self.close()
        self.master.after_idle(self._on_cancel)
