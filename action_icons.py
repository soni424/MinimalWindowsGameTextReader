"""Small accessible text-and-icon actions shared by Reader and settings dialogs."""
from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk


_LABEL_ICONS = {
    'Play': 'play', 'Pause': 'pause', '▶ Play': 'play',
    'Previous sentence': 'previous', 'Next sentence': 'next',
    'Read fixed box': 'play', 'Read now': 'play', 'Select area': 'snippet',
    'Select a snippet': 'snippet', 'Set capture area': 'capture',
    'Start Auto-Read': 'play', 'Stop Auto-Read': 'stop', 'Stop audio': 'stop',
    'Read Again': 'repeat', 'Copy': 'copy', 'Clear': 'delete', 'Delete': 'delete',
    'Add': 'add', 'Add term': 'add', 'New': 'add', 'Rename': 'edit',
    'Edit': 'edit', 'Enable / disable': 'settings', 'Corrections': 'edit',
    'Record': 'record', 'Apply shortcuts': 'check', 'Test selected voice': 'voice',
    'About': 'help', 'Cancel': 'cancel', 'OK': 'check', 'Save rule': 'save',
    'Capture controls': 'capture', 'Load remote images': 'image',
}


def icon_for_button(button: ttk.Button, dark: bool) -> None:
    label = str(button.cget('text'))
    name = _LABEL_ICONS.get(label)
    if name is None:
        return
    theme = 'primary' if str(button.cget('style')) == 'Primary.TButton' else ('dark' if dark else 'light')
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'assets' / 'action_icons'
    scaling = float(button.tk.call('tk', 'scaling'))
    size = 16 if scaling < 1.7 else 24 if scaling < 2.3 else 32
    key = (name, theme, size)
    if getattr(button, '_action_icon_key', None) == key:
        return
    asset = root / theme / str(size) / f'{name}.png'
    if not asset.is_file():
        return
    photo = tk.PhotoImage(master=button, file=str(asset))
    button.configure(image=photo, compound='left')
    button._action_icon_ref = photo
    button._action_icon_key = key


def apply_action_icons(parent: tk.Misc, dark: bool) -> None:
    for child in parent.winfo_children():
        if isinstance(child, ttk.Button):
            icon_for_button(child, dark)
        apply_action_icons(child, dark)
