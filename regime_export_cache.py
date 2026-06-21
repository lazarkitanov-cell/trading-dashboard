"""Breadth-Cache von Colab nach trading-dashboard/regime_cache/ kopieren (einmalig / wöchentlich).

In Colab ausführen nach kassandra_regime(9) oder wenn breadth_panel.pkl fehlt auf GitHub.
Danach: colab_upload.py (lädt regime_cache/* mit hoch).
"""
from __future__ import annotations

import shutil
from pathlib import Path

DRIVE = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
    Path(r"h:\Meine Ablage\Colab Notebooks"),
]

FILES = (
    "regime_final.json",
    "breadth_panel.pkl",
    "market_panel.pkl",
    "regime_tuned.json",
    "regime_winner.json",
)


def main() -> None:
    root = next((p for p in DRIVE if (p / "regime_cache").is_dir()), DRIVE[-1])
    src = root / "regime_cache"
    dst = root / "trading-dashboard" / "regime_cache"
    dst.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        s = src / name
        if not s.is_file():
            print(f"  ⚠ übersprungen (fehlt): {name}")
            continue
        shutil.copy2(s, dst / name)
        print(f"  OK {name} ({s.stat().st_size:,} Bytes)")
    print(f"\nZiel: {dst}")
    print("-> colab_upload.py ausfuehren")


if __name__ == "__main__":
    main()
