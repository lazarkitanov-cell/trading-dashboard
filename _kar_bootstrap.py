"""Bootstrap: kassandra_ampel_research.py v2+ auf Drive."""
from __future__ import annotations

import re
import shutil
import urllib.request
from pathlib import Path

MODULE = "kassandra_ampel_research.py"
MARKER = "def run_trailing_compare"
MIN_VERSION = 2
GITHUB = (
    "https://raw.githubusercontent.com/lazarkitanov-cell/trading-dashboard/main/"
    + MODULE
)


def _version(path: Path) -> int:
    try:
        m = re.search(r"^RESEARCH_VERSION\s*=\s*(\d+)", path.read_text(encoding="utf-8"), re.M)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def _ok(path: Path) -> bool:
    try:
        t = path.read_text(encoding="utf-8")
        return path.is_file() and MARKER in t and _version(path) >= MIN_VERSION
    except Exception:
        return False


def ensure_kar_module(nb_dir: Path) -> Path:
    dest = nb_dir / MODULE
    if _ok(dest):
        return dest

    for src in (nb_dir / "trading-dashboard" / MODULE,):
        if _ok(src):
            shutil.copy2(src, dest)
            print(f"  📂 kopiert: {src}")
            return dest

    drive = Path("/content/drive/MyDrive")
    if drive.is_dir():
        best, best_v = None, 0
        for hit in drive.rglob(MODULE):
            if MARKER in hit.read_text(encoding="utf-8"):
                v = _version(hit)
                if v > best_v:
                    best, best_v = hit, v
        if best and best_v >= MIN_VERSION:
            shutil.copy2(best, dest)
            print(f"  📂 gefunden: {best}")
            return dest

    data = urllib.request.urlopen(GITHUB, timeout=60).read()
    if MARKER.encode() not in data:
        raise FileNotFoundError(f"{MODULE} v{MIN_VERSION}+ nicht verfügbar")
    dest.write_bytes(data)
    print("  ⬇️  von GitHub")
    return dest
