"""Per-monitor Tk overlays for fixed subtitle boxes and one-off snippets."""

from __future__ import annotations

import ctypes
import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Iterable

from appearance import DARK, ThemePalette, flush_windows_compositor
from capture_profiles import MonitorInfo, get_monitors


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
    """Return the virtual desktop bounds, including negative coordinates."""
    try:
        user32 = ctypes.windll.user32
        return VirtualScreen(
            int(user32.GetSystemMetrics(76)),  # SM_XVIRTUALSCREEN
            int(user32.GetSystemMetrics(77)),  # SM_YVIRTUALSCREEN
            int(user32.GetSystemMetrics(78)),  # SM_CXVIRTUALSCREEN
            int(user32.GetSystemMetrics(79)),  # SM_CYVIRTUALSCREEN
        )
    except Exception:
        return VirtualScreen(0, 0, 1920, 1080)


def _fallback_monitor() -> MonitorInfo:
    screen = virtual_screen()
    return MonitorInfo("primary", screen.left, screen.top, screen.right, screen.bottom, 96, 96, True)


def _monitor_snapshot(provider: Callable[[], Iterable[MonitorInfo]] | None) -> list[MonitorInfo]:
    try:
        monitors = list(provider() if provider is not None else get_monitors())
    except Exception:
        monitors = []
    valid = [
        monitor
        for monitor in monitors
        if monitor.right > monitor.left and monitor.bottom > monitor.top
    ]
    return valid or [_fallback_monitor()]


@dataclass
class _MonitorPane:
    monitor: MonitorInfo
    window: tk.Toplevel
    canvas: tk.Canvas


class _ScreenOverlay:
    """Coordinate one topmost overlay pane per connected monitor."""

    _HWND_TOPMOST = -1
    _HWND_TOP = 0
    _SWP_NOMOVE = 0x0002
    _SWP_NOSIZE = 0x0001
    _SWP_NOACTIVATE = 0x0010
    _SWP_SHOWWINDOW = 0x0040

    def __init__(
        self,
        master: tk.Misc,
        alpha: float = 0.35,
        monitor_provider: Callable[[], Iterable[MonitorInfo]] | None = None,
    ) -> None:
        self.master = master
        self.monitors = _monitor_snapshot(monitor_provider)
        self.screen = virtual_screen()
        self._panes: list[_MonitorPane] = []
        self._closed = False
        for monitor in self.monitors:
            window = tk.Toplevel(master)
            window.withdraw()
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.attributes("-alpha", alpha)
            window.configure(bg="#000000")
            # Positive local geometry creates the HWND; native positioning below
            # then applies signed physical desktop coordinates safely.
            window.geometry(f"{monitor.width}x{monitor.height}+0+0")
            canvas = tk.Canvas(
                window,
                bg="#000000",
                highlightthickness=0,
                cursor="crosshair",
            )
            canvas.pack(fill="both", expand=True)
            window.deiconify()
            window.update_idletasks()
            self._place_native_window(window, monitor)
            self._panes.append(_MonitorPane(monitor, window, canvas))
        if not self._panes:
            raise RuntimeError("No display overlay could be created.")
        # Keep the old single-window attributes as compatibility aliases for
        # callers and integrations that only inspect the first pane.
        self.window = self._panes[0].window
        self.canvas = self._panes[0].canvas
        self.window.focus_force()
        self.window.lift()

    @property
    def panes(self) -> tuple[_MonitorPane, ...]:
        return tuple(self._panes)

    @staticmethod
    def _window_handle(window: tk.Toplevel) -> int:
        try:
            user32 = ctypes.windll.user32
            hwnd = int(user32.GetAncestor(int(window.winfo_id()), 2))
            return hwnd or int(window.winfo_id())
        except Exception:
            return 0

    def _place_native_window(self, window: tk.Toplevel, monitor: MonitorInfo) -> bool:
        return self._place_native_rect(
            window, monitor.left, monitor.top, monitor.width, monitor.height
        )

    def _place_native_rect(
        self, window: tk.Toplevel, left: int, top: int, width: int, height: int
    ) -> bool:
        hwnd = self._window_handle(window)
        if not hwnd:
            return False
        try:
            user32 = ctypes.windll.user32
            # Position first with a normal insertion target. Some Tk builds
            # ignore signed coordinates when HWND_TOPMOST is supplied in the
            # same call; promote the already-positioned window in a second
            # call without moving or resizing it.
            result = user32.SetWindowPos(
                hwnd,
                self._HWND_TOP,
                int(left),
                int(top),
                int(width),
                int(height),
                self._SWP_NOACTIVATE | self._SWP_SHOWWINDOW,
            )
            if not result:
                return False
            user32.SetWindowPos(
                hwnd,
                self._HWND_TOPMOST,
                0,
                0,
                0,
                0,
                self._SWP_NOMOVE | self._SWP_NOSIZE | self._SWP_NOACTIVATE,
            )
            return True
        except Exception:
            return False

    def pane_for_screen(self, x: int, y: int) -> _MonitorPane | None:
        for pane in self._panes:
            monitor = pane.monitor
            if monitor.left <= x < monitor.right and monitor.top <= y < monitor.bottom:
                return pane
        return None

    def primary_pane(self) -> _MonitorPane:
        return next((pane for pane in self._panes if pane.monitor.primary), self._panes[0])

    @staticmethod
    def _cursor_position() -> tuple[int, int] | None:
        try:
            class _Point(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            point = _Point()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception:
            pass
        return None

    def _event_position(self, pane: _MonitorPane, event: tk.Event) -> tuple[int, int]:
        physical = self._cursor_position()
        if physical is not None:
            return physical
        width = max(1, int(pane.canvas.winfo_width()))
        height = max(1, int(pane.canvas.winfo_height()))
        x = pane.monitor.left + round(float(event.x) * pane.monitor.width / width)
        y = pane.monitor.top + round(float(event.y) * pane.monitor.height / height)
        return x, y

    @staticmethod
    def _canvas_size(pane: _MonitorPane) -> tuple[int, int]:
        # Tk reports 1x1 until the first Configure event. Drawing against that
        # placeholder collapses the initial selection to a nearly invisible dot.
        width, height = int(pane.canvas.winfo_width()), int(pane.canvas.winfo_height())
        return (
            width if width > 1 else pane.monitor.width,
            height if height > 1 else pane.monitor.height,
        )

    def screen_to_canvas(self, x: int, y: int, pane: _MonitorPane | None = None) -> tuple[float, float]:
        pane = pane or self.pane_for_screen(x, y) or self._panes[0]
        width, height = self._canvas_size(pane)
        return (
            (x - pane.monitor.left) * width / pane.monitor.width,
            (y - pane.monitor.top) * height / pane.monitor.height,
        )

    def canvas_to_screen(self, x: float, y: float, pane: _MonitorPane | None = None) -> tuple[int, int]:
        pane = pane or self._panes[0]
        width, height = self._canvas_size(pane)
        return (
            pane.monitor.left + round(float(x) * pane.monitor.width / width),
            pane.monitor.top + round(float(y) * pane.monitor.height / height),
        )

    @staticmethod
    def _clamp_point(x: int, y: int, monitor: MonitorInfo) -> tuple[int, int]:
        return (
            max(monitor.left, min(monitor.right - 1, x)),
            max(monitor.top, min(monitor.bottom - 1, y)),
        )

    def _release_grabs(self) -> None:
        for pane in self._panes:
            try:
                pane.window.grab_release()
            except tk.TclError:
                pass

    def close(self) -> None:
        """Destroy every monitor pane safely."""
        if self._closed:
            return
        self._closed = True
        self._release_grabs()
        for pane in self._panes:
            try:
                pane.window.destroy()
            except tk.TclError:
                pass
        self._panes.clear()


class BoxEditorOverlay(_ScreenOverlay):
    """Move, resize, save, or cancel a persistent fixed OCR bounding box."""

    MIN_SIZE = 20

    def __init__(
        self,
        master: tk.Misc,
        existing_box: list[int] | tuple[int, int, int, int] | None,
        on_confirm: Callable[[list[int]], None],
        on_cancel: Callable[[], None],
        monitor_provider: Callable[[], Iterable[MonitorInfo]] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        cursor_at_open = self._cursor_position()
        super().__init__(master, alpha=0.38, monitor_provider=monitor_provider)
        self._palette = palette or DARK
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        self._finished = False
        self._active_handle = ""
        self._active_pane = (
            self.pane_for_screen(*cursor_at_open) if cursor_at_open else None
        ) or self.primary_pane()
        self._drag_origin = (0, 0)
        self._start_rect = (0, 0, 0, 0)
        self._old_rect = (0, 0, 0, 0)
        self._old_pane = self._active_pane
        if existing_box and len(existing_box) == 4:
            self.rect = tuple(int(value) for value in existing_box)
            self._active_pane = self._pane_for_rect(self.rect) or self._active_pane
            self.rect = self._normalise_and_clamp(self.rect, self._active_pane.monitor)
        else:
            monitor = self._active_pane.monitor
            width, height = min(500, monitor.width - 40), min(180, monitor.height - 40)
            left = monitor.left + max(20, (monitor.width - width) // 2)
            top = monitor.top + max(20, (monitor.height - height) // 2)
            self.rect = (left, top, left + width, top + height)
        self._build_controls()
        for pane in self.panes:
            pane.canvas.bind("<Configure>", lambda _event: self._draw())
            pane.canvas.bind(
                "<ButtonPress-1>",
                lambda event, current=pane: self._press(event, current),
            )
            pane.canvas.bind(
                "<B1-Motion>",
                lambda event, current=pane: self._drag(event, current),
            )
            pane.canvas.bind(
                "<ButtonRelease-1>",
                lambda event, current=pane: self._release(event, current),
            )
            pane.canvas.bind(
                "<Motion>", lambda event, current=pane: self._update_cursor(event, current)
            )
            pane.window.bind("<KeyPress>", self._on_key)
        self._toolbar.bind("<KeyPress>", self._on_key)
        self.primary_pane().window.protocol("WM_DELETE_WINDOW", self.cancel)
        self._draw()
        self._toolbar.focus_force()

    @staticmethod
    def _intersection_area(rect: tuple[int, int, int, int], monitor: MonitorInfo) -> int:
        left = max(rect[0], monitor.left)
        top = max(rect[1], monitor.top)
        right = min(rect[2], monitor.right)
        bottom = min(rect[3], monitor.bottom)
        return max(0, right - left) * max(0, bottom - top)

    def _pane_for_rect(self, rect: tuple[int, int, int, int]) -> _MonitorPane | None:
        pane = max(self.panes, key=lambda item: self._intersection_area(rect, item.monitor), default=None)
        return pane if pane and self._intersection_area(rect, pane.monitor) else None

    def _build_controls(self) -> None:
        self._toolbar = tk.Toplevel(self.master)
        self._toolbar.withdraw()
        self._toolbar.overrideredirect(True)
        self._toolbar.attributes("-topmost", True)
        self._toolbar.configure(bg=self._palette.border)
        self._toolbar_monitor: MonitorInfo | None = None
        self._toolbar_bounds = (0, 0, 0, 0)
        self._toolbar_content = tk.Frame(self._toolbar, bg=self._palette.border, padx=1, pady=1)
        self._toolbar_content.pack(fill="both", expand=True)
        self._dimension_label: tk.Label | None = None
        self._place_toolbar(self._active_pane)

    def _place_toolbar(self, pane: _MonitorPane) -> None:
        monitor = pane.monitor
        if self._toolbar_monitor != monitor:
            for child in self._toolbar_content.winfo_children():
                child.destroy()
            scale = max(1.0, monitor.dpi_x / 96.0)
            margin = max(16, round(16 * scale))
            padding = max(12, round(14 * scale))
            panel_width = max(1, monitor.width - 2 * margin)
            content = tk.Frame(
                self._toolbar_content, bg=self._palette.card, padx=padding, pady=padding
            )
            content.pack(fill="both", expand=True)
            font_size = max(13, round(14 * scale))
            title = tk.Label(
                content, text="Set capture area", bg=self._palette.card,
                fg=self._palette.text, font=("Segoe UI", -round(font_size * 1.15), "bold"),
                anchor="w",
            )
            title.pack(fill="x")
            self._dimension_label = tk.Label(
                content, bg=self._palette.card, fg=self._palette.accent,
                font=("Segoe UI", -font_size, "bold"), anchor="w",
            )
            self._dimension_label.pack(fill="x", pady=(max(5, round(5 * scale)), 0))
            tk.Label(
                content,
                text="Drag inside to move • drag handles to resize\n"
                     "Drag elsewhere on either screen to draw a new box",
                bg=self._palette.card, fg=self._palette.muted,
                font=("Segoe UI", -font_size), justify="left", anchor="w",
                wraplength=max(80, panel_width - 2 * padding),
            ).pack(fill="x", pady=(max(6, round(7 * scale)), 0))
            tk.Label(
                content, text="Enter saves • Esc cancels • Arrows move • Shift+arrows move 10 px",
                bg=self._palette.card, fg=self._palette.muted,
                font=("Segoe UI", -max(12, round(12 * scale))),
                justify="left", anchor="w", wraplength=max(80, panel_width - 2 * padding),
            ).pack(fill="x", pady=(max(5, round(6 * scale)), 0))
            buttons = tk.Frame(content, bg=self._palette.card)
            buttons.pack(fill="x", pady=(max(9, round(10 * scale)), 0))
            tk.Button(
                buttons, text="Save area", command=self.confirm,
                bg=self._palette.accent, activebackground=self._palette.accent_hover,
                fg="white", activeforeground="white", relief="flat",
                font=("Segoe UI", -font_size, "bold"),
                padx=round(15 * scale), pady=round(5 * scale), cursor="hand2",
            ).pack(side="left")
            tk.Button(
                buttons, text="Cancel", command=self.cancel,
                bg=self._palette.button, activebackground=self._palette.button_hover,
                fg=self._palette.text, activeforeground=self._palette.text,
                relief="flat", font=("Segoe UI", -font_size),
                padx=round(15 * scale), pady=round(5 * scale), cursor="hand2",
            ).pack(side="left", padx=(max(8, round(8 * scale)), 0))
            self._toolbar_monitor = monitor

        self._update_dimensions()
        self._toolbar.update_idletasks()
        requested_width = self._toolbar.winfo_reqwidth()
        requested_height = self._toolbar.winfo_reqheight()
        scale = max(1.0, monitor.dpi_x / 96.0)
        margin = max(16, round(16 * scale))
        width = min(requested_width, max(1, monitor.width - 2 * margin))
        height = min(requested_height, max(1, monitor.height - 2 * margin))
        left = monitor.left + (monitor.width - width) // 2
        top = monitor.top + margin
        self._toolbar_bounds = (left, top, left + width, top + height)
        # Tk interprets negative geometry offsets relative to the right/bottom
        # of the virtual desktop. Create locally, then use signed Win32 pixels.
        self._toolbar.withdraw()
        self._toolbar.geometry(f"{width}x{height}+0+0")
        self._toolbar.deiconify()
        self._toolbar.update_idletasks()
        self._place_native_rect(self._toolbar, left, top, width, height)
        self._toolbar.lift()

    def _update_dimensions(self) -> None:
        if self._dimension_label is None:
            return
        index = self.panes.index(self._active_pane) + 1
        left, top, right, bottom = self.rect
        self._dimension_label.configure(
            text=f"Display {index}  •  {right - left} × {bottom - top} px"
        )

    def _normalise_and_clamp(
        self,
        rect: tuple[float, float, float, float],
        monitor: MonitorInfo | None = None,
    ) -> tuple[int, int, int, int]:
        monitor = monitor or self._active_pane.monitor
        left, top, right, bottom = rect
        left, right = sorted((int(round(left)), int(round(right))))
        top, bottom = sorted((int(round(top)), int(round(bottom))))
        left = max(monitor.left, min(monitor.right, left))
        right = max(monitor.left, min(monitor.right, right))
        top = max(monitor.top, min(monitor.bottom, top))
        bottom = max(monitor.top, min(monitor.bottom, bottom))
        if right - left < self.MIN_SIZE:
            right = min(monitor.right, left + self.MIN_SIZE)
            left = max(monitor.left, right - self.MIN_SIZE)
        if bottom - top < self.MIN_SIZE:
            bottom = min(monitor.bottom, top + self.MIN_SIZE)
            top = max(monitor.top, bottom - self.MIN_SIZE)
        return left, top, right, bottom

    def _draw(self) -> None:
        if self._closed:
            return
        for pane in self.panes:
            pane.canvas.delete("selection")
        pane = self._pane_for_rect(self.rect) or self._active_pane
        left, top, right, bottom = self.rect
        canvas_left, canvas_top = self.screen_to_canvas(left, top, pane)
        canvas_right, canvas_bottom = self.screen_to_canvas(right, bottom, pane)
        pane.canvas.create_rectangle(
            canvas_left,
            canvas_top,
            canvas_right,
            canvas_bottom,
            outline="#052e16",
            width=6,
            tags="selection",
        )
        pane.canvas.create_rectangle(
            canvas_left,
            canvas_top,
            canvas_right,
            canvas_bottom,
            outline="#86efac",
            width=3,
            tags="selection",
        )
        pane.canvas.create_text(
            canvas_left + 8,
            max(22, canvas_top - 12),
            anchor="sw",
            fill="#dcfce7",
            font=("Segoe UI", 10, "bold"),
            text=f"{right-left} × {bottom-top} px",
            tags="selection",
        )
        size = self._handle_size(pane)
        for x, y in self._handle_positions():
            handle_x, handle_y = self.screen_to_canvas(x, y, pane)
            pane.canvas.create_rectangle(
                handle_x - size / 2,
                handle_y - size / 2,
                handle_x + size / 2,
                handle_y + size / 2,
                fill="#f8fafc",
                outline="#14532d",
                width=3,
                tags="selection",
            )
        self._update_dimensions()

    @staticmethod
    def _handle_size(pane: _MonitorPane) -> int:
        return max(18, round(18 * pane.monitor.dpi_x / 96))

    def _handle_positions(self) -> list[tuple[int, int]]:
        left, top, right, bottom = self.rect
        middle_x, middle_y = (left + right) // 2, (top + bottom) // 2
        return [(left, top), (middle_x, top), (right, top), (right, middle_y), (right, bottom), (middle_x, bottom), (left, bottom), (left, middle_y)]

    def _hit_test(self, x: int, y: int) -> str:
        left, top, right, bottom = self.rect
        distance = max(16, round(16 * self._active_pane.monitor.dpi_x / 96))
        names = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
        for name, (handle_x, handle_y) in zip(names, self._handle_positions()):
            if abs(x - handle_x) <= distance and abs(y - handle_y) <= distance:
                return name
        near = distance
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

    def _update_cursor(self, event: tk.Event, pane: _MonitorPane) -> None:
        if self._active_handle:
            return
        x, y = self._event_position(pane, event)
        hit = self._hit_test(x, y) if pane == self._active_pane else ""
        cursor = {
            "move": "fleur", "n": "size_ns", "s": "size_ns",
            "e": "size_we", "w": "size_we",
            "nw": "size_nw_se", "se": "size_nw_se",
            "ne": "size_ne_sw", "sw": "size_ne_sw",
        }.get(hit, "crosshair")
        try:
            pane.canvas.configure(cursor=cursor)
        except tk.TclError:
            pane.canvas.configure(cursor="crosshair")

    def _on_key(self, event: tk.Event) -> str | None:
        key = event.keysym
        if key in {"Return", "KP_Enter"}:
            self.confirm()
            return "break"
        if key == "Escape":
            self.cancel()
            return "break"
        movement = {
            "Left": (-1, 0), "Right": (1, 0),
            "Up": (0, -1), "Down": (0, 1),
        }.get(key)
        if movement is None:
            return None
        step = 10 if event.state & 0x0001 else 1
        dx, dy = movement[0] * step, movement[1] * step
        left, top, right, bottom = self.rect
        monitor = self._active_pane.monitor
        dx = max(monitor.left - left, min(monitor.right - right, dx))
        dy = max(monitor.top - top, min(monitor.bottom - bottom, dy))
        self.rect = (left + dx, top + dy, right + dx, bottom + dy)
        self._draw()
        return "break"

    def _press(self, event: tk.Event, pane: _MonitorPane | None = None) -> None:
        pane = pane or self._active_pane
        x, y = self._event_position(pane, event)
        self._old_pane = self._active_pane
        self._active_pane = pane
        self._drag_origin = (x, y)
        self._start_rect = self.rect
        self._old_rect = self.rect
        self._active_handle = self._hit_test(x, y)
        if not self._active_handle:
            # Dragging outside the current box creates a replacement box on
            # the monitor where the pointer started.
            x, y = self._clamp_point(x, y, pane.monitor)
            self._drag_origin = (x, y)
            self._active_handle = "new"
            self.rect = (x, y, x, y)
            if self._toolbar_monitor != pane.monitor:
                self._place_toolbar(pane)
            self._draw()
        try:
            pane.window.grab_set()
        except tk.TclError:
            pass

    def _drag(self, event: tk.Event, pane: _MonitorPane | None = None) -> None:
        if not self._active_handle:
            return
        pane = self._active_pane
        x, y = self._event_position(pane, event)
        x, y = self._clamp_point(x, y, pane.monitor)
        origin_x, origin_y = self._drag_origin
        left, top, right, bottom = self._start_rect
        handle = self._active_handle
        if handle == "new":
            self.rect = self._normalise_and_clamp((origin_x, origin_y, x, y), pane.monitor)
        else:
            dx, dy = x - origin_x, y - origin_y
            if handle == "move":
                width, height = right - left, bottom - top
                left = max(pane.monitor.left, min(pane.monitor.right - width, left + dx))
                top = max(pane.monitor.top, min(pane.monitor.bottom - height, top + dy))
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
            self.rect = self._normalise_and_clamp((left, top, right, bottom), pane.monitor)
        self._draw()

    def _release(self, _event: tk.Event, _pane: _MonitorPane | None = None) -> None:
        if self._active_handle == "new" and (self.rect[2] - self.rect[0] < self.MIN_SIZE or self.rect[3] - self.rect[1] < self.MIN_SIZE):
            self.rect = self._old_rect
            self._active_pane = self._old_pane
            if self._toolbar_monitor != self._active_pane.monitor:
                self._place_toolbar(self._active_pane)
            self._draw()
        self._active_handle = ""
        self._release_grabs()

    def confirm(self) -> None:
        if self._finished:
            return
        self._finished = True
        box = [int(value) for value in self.rect]
        self.close()
        self.master.after_idle(lambda: self._on_confirm(box))

    def cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.close()
        self.master.after_idle(self._on_cancel)

    def close(self) -> None:
        if hasattr(self, "_toolbar"):
            try:
                self._toolbar.destroy()
            except tk.TclError:
                pass
        super().close()


class QuickSnippetOverlay(_ScreenOverlay):
    """Windows Snipping Tool-style selector across every connected display."""

    MIN_SIZE = 3

    def __init__(
        self,
        master: tk.Misc,
        on_capture: Callable[[list[int]], None],
        on_cancel: Callable[[], None],
        monitor_provider: Callable[[], Iterable[MonitorInfo]] | None = None,
    ) -> None:
        super().__init__(master, alpha=0.36, monitor_provider=monitor_provider)
        self._on_capture = on_capture
        self._on_cancel = on_cancel
        self._start: tuple[int, int] | None = None
        self._active_pane: _MonitorPane | None = None
        self._rectangle_id: int | None = None
        self._finished = False
        for pane in self.panes:
            pane.canvas.bind(
                "<ButtonPress-1>",
                lambda event, current=pane: self._press(event, current),
            )
            pane.canvas.bind(
                "<B1-Motion>",
                lambda event, current=pane: self._drag(event, current),
            )
            pane.canvas.bind(
                "<ButtonRelease-1>",
                lambda event, current=pane: self._release(event, current),
            )
            pane.window.bind("<Escape>", lambda _event: self.cancel())
            pane.window.protocol("WM_DELETE_WINDOW", self.cancel)

    def _draw_rectangle(self, left: int, top: int, right: int, bottom: int) -> None:
        if self._active_pane is None:
            return
        pane = self._active_pane
        canvas_left, canvas_top = self.screen_to_canvas(left, top, pane)
        canvas_right, canvas_bottom = self.screen_to_canvas(right, bottom, pane)
        if self._rectangle_id is not None:
            pane.canvas.delete(self._rectangle_id)
        self._rectangle_id = pane.canvas.create_rectangle(
            canvas_left,
            canvas_top,
            canvas_right,
            canvas_bottom,
            outline="#ffffff",
            width=2,
        )

    def _position(self, event: tk.Event, pane: _MonitorPane) -> tuple[int, int]:
        x, y = self._event_position(pane, event)
        return self._clamp_point(x, y, pane.monitor)

    def _press(self, event: tk.Event, pane: _MonitorPane | None = None) -> None:
        pane = pane or self.panes[0]
        self._active_pane = pane
        self._start = self._position(event, pane)
        self._draw_rectangle(*self._start, *self._start)
        try:
            pane.window.grab_set()
        except tk.TclError:
            pass

    def _drag(self, event: tk.Event, _pane: _MonitorPane | None = None) -> None:
        if self._start is None or self._active_pane is None:
            return
        x, y = self._position(event, self._active_pane)
        start_x, start_y = self._start
        left, right = sorted((start_x, x))
        top, bottom = sorted((start_y, y))
        self._draw_rectangle(left, top, right, bottom)

    def _release(self, event: tk.Event, _pane: _MonitorPane | None = None) -> None:
        if self._start is None or self._active_pane is None:
            return
        x, y = self._position(event, self._active_pane)
        start_x, start_y = self._start
        left, right = sorted((start_x, x))
        top, bottom = sorted((start_y, y))
        self._release_grabs()
        if right - left < self.MIN_SIZE or bottom - top < self.MIN_SIZE:
            self.cancel()
            return
        self._finished = True
        self.close()
        flush_windows_compositor()
        self.master.after_idle(lambda: self._on_capture([left, top, right, bottom]))

    def cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.close()
        self.master.after_idle(self._on_cancel)


__all__ = ["BoxEditorOverlay", "QuickSnippetOverlay", "VirtualScreen", "virtual_screen"]
