"""Render the app icon source and export Windows-friendly image sizes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PROJECT_ROOT / "assets"
SOURCE_SVG = ASSET_ROOT / "app_icon.svg"
MASTER_PNG = ASSET_ROOT / "app_icon.png"
WINDOWS_ICO = ASSET_ROOT / "app_icon.ico"
PNG_ROOT = ASSET_ROOT / "icons"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def _find_edge() -> Path:
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Microsoft Edge is required to render assets/app_icon.svg.")


def _render_master() -> None:
    edge = _find_edge()
    subprocess.run(
        [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            f"--screenshot={MASTER_PNG}",
            "--window-size=1024,1024",
            SOURCE_SVG.as_uri(),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def build() -> None:
    """Create the high-resolution PNG, common PNG sizes, and multi-size ICO."""
    PNG_ROOT.mkdir(parents=True, exist_ok=True)
    _render_master()
    with Image.open(MASTER_PNG) as source:
        master = source.convert("RGBA")
        if master.size != (1024, 1024):
            raise RuntimeError(f"Unexpected master icon size: {master.size}")
        for size in ICON_SIZES:
            resized = master.resize((size, size), Image.Resampling.LANCZOS)
            resized.save(PNG_ROOT / f"app_icon_{size}.png", optimize=True)
        master.save(
            WINDOWS_ICO,
            format="ICO",
            sizes=[(size, size) for size in ICON_SIZES],
            append_images=[],
        )


if __name__ == "__main__":
    build()
