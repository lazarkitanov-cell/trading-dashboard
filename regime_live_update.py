#!/usr/bin/env python3
"""Tägliches Regime-Signal — läuft in GitHub Actions vor stop_check.py."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_regime_update(quiet: bool = True) -> dict | None:
    if not os.environ.get("EODHD_API_KEY"):
        print("⚠️  EODHD_API_KEY fehlt — Regime-Update übersprungen")
        return None
    script = ROOT / "_kassandra_regime.py"
    if not script.is_file():
        print(f"⚠️  {script.name} fehlt")
        return None
    spec = importlib.util.spec_from_file_location("kassandra_regime_engine", script)
    kr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kr)
    try:
        return kr.live_signal(quiet=quiet, github=True)
    except Exception as e:
        print(f"⚠️  Regime-Update fehlgeschlagen: {e}")
        return None


def main() -> int:
    out = run_regime_update(quiet=False)
    dest = ROOT / "kassandra_regime_live.json"
    if not out:
        if dest.is_file():
            print(
                f"⚠️  Regime-Update fehlgeschlagen — bestehendes {dest.name} bleibt aktiv "
                "(Stop-Check läuft weiter)"
            )
            return 0
        print("❌ Regime-Update fehlgeschlagen und kein kassandra_regime_live.json vorhanden")
        return 1
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ {dest.name} — {out.get('signal')} {int((out.get('invest_pct') or 0) * 100)}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
