"""Wrapper → regime_momentum_live.py (Notebook-Root)."""
from __future__ import annotations

import runpy
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MAIN = _ROOT / "regime_momentum_live.py"
if not _MAIN.is_file():
    raise FileNotFoundError(f"regime_momentum_live.py fehlt: {_MAIN}")

_g = runpy.run_path(str(_MAIN))
run_live = _g["run_live"]
print_live_report = _g.get("print_live_report")
MEINE_POSITIONEN = _g.get("MEINE_POSITIONEN", "")
KAPITAL_EUR = _g.get("KAPITAL_EUR", 100_000)

if __name__ == "__main__":
    run_live()
