"""Small Cap EU — Investitionsquote aus Kassandra Regime (Live + Backtest-Hook)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

_DRIVE_ROOTS = (
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
)


def _load_kr_module():
    import importlib.util

    here = Path(__file__).resolve().parent
    for root in (*_DRIVE_ROOTS, here):
        p = root / "_kassandra_regime.py"
        if p.is_file():
            spec = importlib.util.spec_from_file_location("kr_sc_live", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(
        "_kassandra_regime.py nicht gefunden — Kassandra_Regime.ipynb Zelle 1 ausführen."
    )


def _from_json(data: dict) -> dict:
    sig = str(data.get("signal", "GRÜN"))
    if " + CRASH" in sig:
        sig = sig.replace(" + CRASH", "").strip()
    return {
        "score": int(data.get("score", 100)),
        "signal": sig,
        "invest_pct": float(data.get("invest_pct", 1.0)),
        "ampel_source": "kassandra_regime",
        "quoten": data.get("quotes", "100/50/0"),
        "overlay": data.get("overlay", "SPY −3%"),
        "components": data.get("components", {}),
        "datum": data.get("datum"),
    }


def _read_live_json() -> dict | None:
    candidates = []
    for root in _DRIVE_ROOTS:
        candidates.extend([
            root / "trading-dashboard" / "kassandra_regime_live.json",
            root / "regime_cache" / "kassandra_regime_live.json",
        ])
    candidates.append(Path(__file__).resolve().parent / "kassandra_regime_live.json")
    today = pd.Timestamp.today().normalize()
    for p in candidates:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            d = pd.Timestamp(str(data.get("datum", ""))[:10])
            if (today - d).days <= 7:
                return _from_json(data)
        except Exception:
            continue
    return None


def get_regime_quota(date=None, quiet: bool = True) -> dict:
    """
    Ersetzt compute_kassandra_score() für die Live-Investitionsquote.
    Gleiche Logik wie Dashboard-Banner (Kassandra Regime v5).
    """
    dt = pd.Timestamp(date or pd.Timestamp.today()).normalize()

    cached = _read_live_json()
    if cached is not None and pd.Timestamp(str(cached.get("datum", ""))[:10]) == dt:
        return cached

    kr = _load_kr_module()
    if dt.normalize() >= pd.Timestamp.today().normalize() - pd.Timedelta(days=1):
        try:
            out = kr.live_signal(quiet=quiet)
            return _from_json(out)
        except Exception:
            if cached:
                return cached
            raise

    series = kr.build_regime_invest_series(
        start=(dt - pd.Timedelta(days=30)).strftime("%Y-%m-%d"),
        end=dt.strftime("%Y-%m-%d"),
    )
    pct = float(series.asof(dt))
    if pd.isna(pct):
        pct = 1.0
    score = int(round(pct * 100))
    if pct >= 0.999:
        signal = "GRÜN"
    elif pct >= 0.499:
        signal = "GELB"
    else:
        signal = "ROT"
    return {
        "score": score,
        "signal": signal,
        "invest_pct": pct,
        "ampel_source": "kassandra_regime",
        "quoten": "100/50/0",
        "overlay": "SPY −3%",
        "components": {},
        "datum": dt.strftime("%Y-%m-%d"),
    }
