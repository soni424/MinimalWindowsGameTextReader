"""Per-monitor Tk overlays for fixed subtitle boxes and one-off snippets."""

from __future__ import annotations

import ctypes
import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Iterable

from appearance import flush_windows_compositor
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
                int(monitor.left),
                int(monitor.top),
                int(monitor.width),
                int(monitor.height),
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
        return max(1, int(pane.canvas.winfo_width())), max(1, int(pane.canvas.winfo_height()))

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

    HANDLE_SIZE = 12
    MIN_SIZE = 20

    def __init__(
        self,
        master: tk.Misc,
        existing_box: list[int] | tuple[int, int, int, int] | None,
        on_confirm: Callable[[list[int]], None],
        on_cancel: Callable[[], None],
        monitor_provider: Callable[[], Iterable[MonitorInfo]] | None = None,
    ) -> None:
        super().__init__(master, alpha=0.38, monitor_provider=monitor_provider)
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        self._finished = False
        self._active_handle = ""
        self._active_pane = self.primary_pane()
        self._drag_origin = (0, 0)
        self._start_rect = (0, 0, 0, 0)
        self._old_rect = (0, 0, 0, 0)
        if existing_box and len(existing_box) == 4:
            self.rect = tuple(int(value) for value in existing_box)
            self._active_pane = self._pane_for_rect(self.rect) or self.primary_pane()
            self.rect = self._normalise_and_clamp(self.rect, self._active_pane.monitor)
        else:
            monitor = self._active_pane.monitor
            width, height = min(500, monitor.width - 40), min(180, monitor.height - 40)
            left = monitor.left + max(20, (monitor.width - width) // 2)
            top = monitor.top + max(20, (monitor.height - height) // 2)
            self.rect = (left, top, left + width, top + height)
        self._build_controls()
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
            pane.window.bind("<Return>", lambda _event: self.confirm())
            pane.window.bind("<Escape>", lambda _event: self.cancel())
        self.primary_pane().window.protocol("WM_DELETE_WINDOW", self.cancel)
        self._draw()

    @staticmethod
    def _intersection_area(rect: tuple[int, int, int, int], monitor: MonitorInfo) -> int:
        left = max(rect[0], monitor.left)
        top = max(rect[1], monitor.top)
        right = min(rect[2], monitor.right)
        bottom = min(rect[3], monitor.bottom)
        return max(0, right - left) * max(0, bottom - top)

    def _pane_for_rect(self, rect: tuple[int, int, int, int]) -> _MonitorPane | None:
        return max(self.panes, key=lambda pane: self._intersection_area(rect, pane.monitor), default=None)

    def _build_controls(self) -> None:
        pane = self.primary_pane()
        controls = tk.Frame(pane.canvas, bg="#1e293b", padx=10, pady=7)
        tk.Label(
            controls,
            text="Drag inside to move • drag handles/edges to resize • Enter saves • Esc cancels",
            fg="#f8fafc",
            bg="#1e293b",
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 12))
        tk.Button(
            controls,
            text="Confirm / Done",
            command=self.confirm,
            bg="#166a34",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=3)
        tk.Button(
            controls,
            text="Cancel",
            command=self.cancel,
            bg="#475569",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=3)
        pane.canvas.create_window(
            max(10, pane.canvas.winfo_width() // 2),
            28,
            window=controls,
            tags="controls",
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
            outline="#22c55e",
            width=3,
            tags="selection",
        )
        pane.canvas.create_text(
            canvas_left + 8,
            max(58, canvas_top - 12),
            anchor="sw",
            fill="#dcfce7",
            font=("Segoe UI", 10, "bold"),
            text=f"{right-left} × {bottom-top} px",
            tags="selection",
        )
        for x, y in self._handle_positions():
            handle_x, handle_y = self.screen_to_canvas(x, y, pane)
            pane.canvas.create_rectangle(
                handle_x - self.HANDLE_SIZE / 2,
                handle_y - self.HANDLE_SIZE / 2,
                handle_x + self.HANDLE_SIZE / 2,
                handle_y + self.HANDLE_SIZE / 2,
                fill="#f8fafc",
                outline="#15803d",
                width=2,
                tags="selection",
            )

    def _handle_positions(self) -> list[tuple[int, int]]:
        left, top, right, bottom = self.rect
        middle_x, middle_y = (left + right) // 2, (top + bottom) // 2
        return [(left, top), (middle_x, top), (right, top), (right, middle_y), (right, bottom), (middle_x, bottom), (left, bottom), (left, middle_y)]

    def _hit_test(self, x: int, y: int) -> str:
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

    def _press(self, event: tk.Event, pane: _MonitorPane | None = None) -> None:
        pane = pane or self._active_pane
        x, y = self._event_position(pane, event)
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
