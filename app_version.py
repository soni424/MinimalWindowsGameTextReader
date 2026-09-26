"""One version source and immutable metadata for each packaged build."""
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys

APP_VERSION = '1.2.5-dev'
FILE_VERSION = (1, 2, 5, 0)


def source_build_info() -> dict[str, str]:
    revision = 'source'
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        revision = subprocess.check_output(
            ['git', 'describe', '--always', '--dirty'], cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL, creationflags=flags, timeout=2,
        ).decode().strip()
    except (OSError, subprocess.SubprocessError):
        pass
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return {'version': APP_VERSION, 'build': f'{stamp}-{revision}'}


@lru_cache(maxsize=1)
def build_info() -> dict[str, str]:
    if getattr(sys, 'frozen', False):
        try:
            return json.loads((Path(__file__).parent / 'build_info.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'version': APP_VERSION, 'build': 'unavailable'}
    return source_build_info()


def version_label() -> str:
    info = build_info()
    return f"Version {info['version']} • Build {info['build']}"
