
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  REGIME MOMENTUM — Live (wöchentlich Fr · Colab → JSON → GitHub → Dashboard) ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

MEINE_POSITIONEN = ""   # US-Ticker kommagetrennt, z.B. NVDA, MSFT, AAPL
KAPITAL_EUR = 100_000
POSITIONEN_FILE_NAME = "regime_momentum_positionen.txt"

import importlib.util
import os
import sys
from pathlib import Path

DRIVE_CANDIDATES = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
]

try:
    from google.colab import drive, userdata

    if not Path("/content/drive/MyDrive").exists():
        drive.mount("/content/drive")
    NOTEBOOK_DIR = next((p for p in DRIVE_CANDIDATES if p.exists()), Path("."))
    try:
        GITHUB_TOKEN = userdata.get("GITHUB_TOKEN")
    except Exception:
        GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
except ImportError:
    NOTEBOOK_DIR = Path(r"h:\Meine Ablage\Colab Notebooks")
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def _load_bt():
    for rel in ("regime_momentum_bt.py", Path("trading-dashboard") / "regime_momentum_bt.py"):
        p = NOTEBOOK_DIR / rel
        if p.is_file() and "run_live_signal" in p.read_text(encoding="utf-8"):
            spec = importlib.util.spec_from_file_location("regime_momentum_bt", p)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["regime_momentum_bt"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError(
        "regime_momentum_bt.py v10+ fehlt — Regime_Momentum_Backtest.ipynb Zelle 1 oder Drive syncen."
    )


def run_live(meine_positionen=None, kapital_eur=None, upload_github=True):
    global MEINE_POSITIONEN, KAPITAL_EUR
    if meine_positionen is not None:
        MEINE_POSITIONEN = (
            meine_positionen if isinstance(meine_positionen, str)
            else ", ".join(str(t).strip() for t in meine_positionen if str(t).strip())
        )
    if kapital_eur is not None:
        KAPITAL_EUR = float(kapital_eur)

    bt = _load_bt()
    pos_file = NOTEBOOK_DIR / POSITIONEN_FILE_NAME
    if meine_positionen is not None:
        pos = (
            meine_positionen if isinstance(meine_positionen, str)
            else ", ".join(str(t).strip() for t in meine_positionen if str(t).strip())
        )
    elif MEINE_POSITIONEN.strip():
        pos = MEINE_POSITIONEN.strip()
    elif pos_file.is_file() and pos_file.read_text(encoding="utf-8").strip():
        pos = None  # Modul liest regime_momentum_positionen.txt
        print(f"📂 Depot aus {POSITIONEN_FILE_NAME}")
    else:
        pos = None
        print("⚠️ Kein Depot — alle Ziel-Titel werden als KAUFEN gezählt.")
    return bt.run_live_signal(
        meine_positionen=pos,
        kapital_eur=KAPITAL_EUR,
        upload_github=bool(upload_github),
        github_token=GITHUB_TOKEN,
    )


if __name__ == "__main__":
    run_live()
