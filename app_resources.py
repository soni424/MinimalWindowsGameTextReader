"""Shared paths and loaders for packaged application resources."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def _resource_root() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        return Path(bundled_root)
    return Path(__file__).resolve().parent


ASSET_ROOT = _resource_root() / "assets"
APP_ICON_MASTER_PATH = ASSET_ROOT / "app_icon.png"
APP_ICON_WINDOW_PATH = ASSET_ROOT / "icons" / "app_icon_256.png"
APP_ICON_ICO_PATH = ASSET_ROOT / "app_icon.ico"


def load_app_icon(size: int | None = None) -> Image.Image:
    """Return an independent RGBA icon image for tray and UI consumers."""
    exported = ASSET_ROOT / "icons" / f"app_icon_{size}.png" if size else APP_ICON_MASTER_PATH
    source_path = exported if exported.is_file() else APP_ICON_MASTER_PATH
    with Image.open(source_path) as source:
        image = source.convert("RGBA")
    if size is not None and image.size != (size, size):
        image = image.resize((size, size), Image.Resampling.LANCZOS)
    return image


def apply_window_icon(root: object) -> bool:
    """Apply the branded icon to the Tk window and future child windows."""
    applied = False
    try:
        root.iconbitmap(default=str(APP_ICON_ICO_PATH))
        applied = True
    except Exception:
        pass
    try:
        import tkinter as tk

        photo = tk.PhotoImage(master=root, file=str(APP_ICON_WINDOW_PATH))
        root.iconphoto(True, photo)
        setattr(root, "_game_text_reader_icon", photo)
        applied = True
    except Exception:
        pass
    return applied
