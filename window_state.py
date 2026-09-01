"""Restore and persist the settings window without recording transient operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from capture_profiles import MonitorInfo, get_monitors
from config import ConfigStore


@dataclass(frozen=True)
class WindowPlacement:
    width: int
    height: int
    x: int | None
    y: int | None
    state: str = "normal"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WindowPlacement":
        return cls(int(value["width"]), int(value["height"]), value.get("x"), value.get("y"), str(value.get("state", "normal")))

    def as_mapping(self) -> dict[str, Any]:
        return asdict(self)


def _intersection(width: int, height: int, x: int, y: int, monitor: MonitorInfo) -> tuple[int, int]:
    visible_width = max(0, min(x + width, monitor.right) - max(x, monitor.left))
    visible_height = max(0, min(y + height, monitor.bottom) - max(y, monitor.top))
    return visible_width, visible_height


def _primary(monitors: list[MonitorInfo]) -> MonitorInfo:
    return next((monitor for monitor in monitors if monitor.primary), monitors[0])


def restore_window_placement(
    saved: WindowPlacement,
    monitors: list[MonitorInfo] | None = None,
    minimum: tuple[int, int] = (720, 660),
) -> WindowPlacement:
    """Return an on-screen placement while preserving valid multi-monitor coordinates."""

    monitors = list(monitors if monitors is not None else get_monitors())
    if not monitors:
        return saved
    width = max(minimum[0], int(saved.width))
    height = max(minimum[1], int(saved.height))
    if saved.x is not None and saved.y is not None:
        target = max(
            monitors,
            key=lambda monitor: _intersection(width, height, int(saved.x), int(saved.y), monitor)[0]
            * _intersection(width, height, int(saved.x), int(saved.y), monitor)[1],
        )
        visible_width, visible_height = _intersection(width, height, int(saved.x), int(saved.y), target)
        if visible_width >= 80 and visible_height >= 60:
            width = min(width, max(minimum[0], target.width))
            height = min(height, max(minimum[1], target.height))
            return WindowPlacement(width, height, int(saved.x), int(saved.y), saved.state)
    target = _primary(monitors)
    width = min(width, max(minimum[0], target.width))
    height = min(height, max(minimum[1], target.height))
    x = target.left + max(0, (target.width - width) // 2)
    y = target.top + max(0, (target.height - height) // 2)
    return WindowPlacement(width, height, x, y, saved.state)


class WindowStateController:
    """Debounced Tk window placement persistence that ignores minimized geometry."""

    def __init__(
        self,
        root: Any,
        config: ConfigStore,
        monitor_provider: Callable[[], list[MonitorInfo]] = get_monitors,
    ) -> None:
        self.root = root
        self.config = config
        self._monitor_provider = monitor_provider
        self._save_after: str | None = None
        self._tracking = False
        self._placement = restore_window_placement(
            WindowPlacement.from_mapping(config.get()["window"]),
            monitor_provider(),
        )
        self._apply()
        self.root.bind("<Configure>", self._on_configure, add="+")
        self.root.after_idle(self._finish_restore)

    @property
    def placement(self) -> WindowPlacement:
        return self._placement

    def _apply(self) -> None:
        placement = self._placement
        position = ""
        if placement.x is not None and placement.y is not None:
            position = f"{placement.x:+d}{placement.y:+d}"
        self.root.geometry(f"{placement.width}x{placement.height}{position}")

    def _finish_restore(self) -> None:
        if self._placement.state == "maximized":
            try:
                self.root.state("zoomed")
            except Exception:
                pass
        self._tracking = True

    def _on_configure(self, event: Any) -> None:
        if not self._tracking or getattr(event, "widget", self.root) is not self.root:
            return
        try:
            state = str(self.root.state()).lower()
        except Exception:
            return
        if state in {"iconic", "withdrawn"}:
            return
        if state == "zoomed":
            self._placement = WindowPlacement(
                self._placement.width,
                self._placement.height,
                self._placement.x,
                self._placement.y,
                "maximized",
            )
        elif state == "normal":
            self._placement = WindowPlacement(
                max(1, int(self.root.winfo_width())),
                max(1, int(self.root.winfo_height())),
                int(self.root.winfo_x()),
                int(self.root.winfo_y()),
                "normal",
            )
        else:
            return
        self._schedule_save()

    def _schedule_save(self) -> None:
        if self._save_after is not None:
            try:
                self.root.after_cancel(self._save_after)
            except Exception:
                pass
        self._save_after = self.root.after(350, self.persist_now)

    def persist_now(self) -> None:
        self._save_after = None
        self.config.update(window=self._placement.as_mapping())

    def close(self) -> None:
        if self._save_after is not None:
            try:
                self.root.after_cancel(self._save_after)
            except Exception:
                pass
            self._save_after = None
        self.persist_now()
