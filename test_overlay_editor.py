"""Capture-area editor placement and interaction regressions."""

from __future__ import annotations

import tkinter as tk
import unittest
import ctypes
import os
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import patch

from capture_profiles import MonitorInfo
from appearance import LIGHT
from overlay import BoxEditorOverlay


@unittest.skipUnless(os.name == "nt", "Windows display APIs are required")
class CaptureEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlays: list[BoxEditorOverlay] = []

    def tearDown(self) -> None:
        for overlay in self.overlays:
            overlay.close()
        self.root.update()
        self.root.destroy()

    def make_overlay(
        self,
        monitors: list[MonitorInfo],
        existing_box: list[int] | None = None,
    ) -> BoxEditorOverlay:
        overlay = BoxEditorOverlay(
            self.root,
            existing_box,
            lambda _box: None,
            lambda: None,
            monitor_provider=lambda: monitors,
        )
        self.overlays.append(overlay)
        return overlay

    def assert_toolbar_centered(self, overlay: BoxEditorOverlay, monitor: MonitorInfo) -> None:
        old_controls = overlay.primary_pane().canvas.find_withtag("controls")
        if old_controls:
            canvas_x = overlay.primary_pane().canvas.coords(old_controls[0])[0]
            self.assertAlmostEqual(canvas_x, monitor.width / 2, delta=1)
            return
        left, top, right, bottom = overlay._toolbar_bounds
        self.assertEqual(overlay._toolbar_monitor, monitor)
        self.assertAlmostEqual((left + right) / 2, (monitor.left + monitor.right) / 2, delta=1)
        self.assertGreaterEqual(left, monitor.left)
        self.assertLessEqual(right, monitor.right)
        self.assertGreaterEqual(top, monitor.top)
        self.assertLessEqual(bottom, monitor.bottom)
        self.assertGreater(right - left, 200)
        self.assertGreater(bottom - top, 80)
        self.assertEqual(float(overlay._toolbar.attributes("-alpha")), 1.0)
        native = wintypes.RECT()
        hwnd = overlay._window_handle(overlay._toolbar)
        self.assertTrue(ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(native)))
        self.assertEqual((native.left, native.top), (left, top))
        self.assertEqual((native.right, native.bottom), (right, bottom))

    def test_saved_box_places_controls_and_selection_on_its_monitor_before_update(self) -> None:
        primary = MonitorInfo("primary", 0, 0, 800, 600, 96, 96, True)
        other = MonitorInfo("other", -960, 0, 0, 720, 144, 144, False)
        overlay = self.make_overlay([primary, other], [-800, 200, -400, 340])

        self.assert_toolbar_centered(overlay, other)
        self.assertEqual(overlay.rect, (-800, 200, -400, 340))
        selection = overlay.panes[1].canvas.find_withtag("selection")
        self.assertTrue(selection)
        bounds = overlay.panes[1].canvas.coords(selection[0])
        self.assertGreater(bounds[2] - bounds[0], 300)
        self.root.update()
        self.assert_toolbar_centered(overlay, other)
        self.assertEqual(overlay.panes[1].canvas.winfo_width(), other.width)

    def test_new_box_starts_under_cursor_and_controls_follow_new_monitor(self) -> None:
        primary = MonitorInfo("primary", 0, 0, 800, 600, 96, 96, True)
        other = MonitorInfo("other", 800, 0, 1600, 600, 144, 144, False)
        with patch.object(BoxEditorOverlay, "_cursor_position", return_value=(1100, 250)):
            overlay = self.make_overlay([primary, other])
        self.assert_toolbar_centered(overlay, other)

        with patch.object(overlay, "_event_position", side_effect=[(100, 100), (220, 180)]):
            overlay._press(SimpleNamespace(x=100, y=100), overlay.panes[0])
            overlay._drag(SimpleNamespace(x=220, y=180), overlay.panes[0])
            overlay._release(SimpleNamespace(x=220, y=180), overlay.panes[0])
        self.assert_toolbar_centered(overlay, primary)
        self.assertEqual(overlay.rect, (100, 100, 220, 180))

    def test_keyboard_moves_box_within_monitor_and_cancel_keeps_original(self) -> None:
        monitor = MonitorInfo("primary", 0, 0, 800, 600, 96, 96, True)
        cancelled: list[bool] = []
        overlay = BoxEditorOverlay(
            self.root, [100, 100, 300, 220], lambda _box: None,
            lambda: cancelled.append(True), monitor_provider=lambda: [monitor],
        )
        self.overlays.append(overlay)
        self.assertEqual(overlay._on_key(SimpleNamespace(keysym="Right", state=0)), "break")
        self.assertEqual(overlay.rect, (101, 100, 301, 220))
        self.assertEqual(overlay._on_key(SimpleNamespace(keysym="Down", state=1)), "break")
        self.assertEqual(overlay.rect, (101, 110, 301, 230))
        overlay._on_key(SimpleNamespace(keysym="Escape", state=0))
        self.root.update()
        self.assertEqual(cancelled, [True])

    def test_pointer_cursors_and_light_palette(self) -> None:
        monitor = MonitorInfo("primary", 0, 0, 800, 600, 96, 96, True)
        overlay = BoxEditorOverlay(
            self.root, [100, 100, 300, 220], lambda _box: None,
            lambda: None, monitor_provider=lambda: [monitor], palette=LIGHT,
        )
        self.overlays.append(overlay)
        self.assertEqual(overlay._toolbar_content.winfo_children()[0].cget("bg"), LIGHT.card)
        pane = overlay.panes[0]
        with patch.object(overlay, "_event_position", return_value=(150, 150)):
            overlay._update_cursor(SimpleNamespace(x=150, y=150), pane)
        self.assertEqual(pane.canvas.cget("cursor"), "fleur")
        with patch.object(overlay, "_event_position", return_value=(100, 100)):
            overlay._update_cursor(SimpleNamespace(x=100, y=100), pane)
        self.assertEqual(pane.canvas.cget("cursor"), "size_nw_se")

    def test_aborted_box_on_other_monitor_restores_previous_monitor(self) -> None:
        primary = MonitorInfo("primary", 0, 0, 800, 600, 96, 96, True)
        other = MonitorInfo("other", 800, 0, 1600, 600, 96, 96, False)
        overlay = self.make_overlay([primary, other], [100, 100, 300, 220])
        with patch.object(overlay, "_event_position", return_value=(900, 150)):
            overlay._press(SimpleNamespace(x=100, y=150), overlay.panes[1])
            overlay._release(SimpleNamespace(x=100, y=150), overlay.panes[1])
        self.assertEqual(overlay.rect, (100, 100, 300, 220))
        self.assert_toolbar_centered(overlay, primary)


if __name__ == "__main__":
    unittest.main()
