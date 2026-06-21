"""Länder-ETF Kassandra — Kassandra Regime Overlay (Backtest + Live-Hook)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

_DRIVE_ROOTS = (
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
)


def _resolve_cache_dir(cache_dir=None, g=None):
    if cache_dir:
        return Path(cache_dir)
    if g:
        for key in ("CACHE_DIR", "cache_dir", "folder"):
            if g.get(key):
                return Path(g[key])
    for root in _DRIVE_ROOTS:
        for sub in ("regime_cache", "KassandraStrategy", ""):
            p = root / sub if sub else root
            if (p / "regime_final.json").is_file() or (p / "breadth_panel.pkl").is_file():
                return p
    return _DRIVE_ROOTS[-1]


def _invest_from_regime_series(regime_series: pd.Series | None, dt) -> float:
    if regime_series is None or regime_series.empty:
        return 1.0
    try:
        val = regime_series.asof(pd.Timestamp(dt))
        if pd.isna(val):
            return 1.0
        return float(np.clip(val, 0, 1))
    except Exception:
        return 1.0


def regime_pct_to_slots(pct: float, max_slots: int = 2) -> tuple[str, int]:
    pct = float(np.clip(pct, 0, 1))
    if pct >= 0.999:
        return "bullish", int(max_slots)
    if pct >= 0.499:
        return "neutral", max(int(max_slots) - 1, 1)
    if pct >= 0.249:
        return "defensiv", 1
    return "cash", 0


def apply_ampel_overlay(
    overlay: str,
    regime_invest: pd.Series | None,
    idx,
    legacy_regime: str,
    legacy_tpb: int,
    max_slots: int = 2,
) -> tuple[str, int]:
    if overlay == "off":
        return "bullish", int(max_slots)
    if overlay in ("legacy", None, "") or regime_invest is None:
        return legacy_regime, int(legacy_tpb)
    pct = _invest_from_regime_series(regime_invest, idx)
    r_reg, r_tpb = regime_pct_to_slots(pct, max_slots)
    if overlay == "regime":
        return r_reg, r_tpb
    if overlay == "regime_and_own":
        tpb = min(int(legacy_tpb), int(r_tpb))
        if tpb <= 0:
            return "cash", 0
        if tpb >= max_slots:
            return "bullish", max_slots
        if tpb >= max(max_slots - 1, 1):
            return "neutral", tpb
        return "defensiv", 1
    return legacy_regime, int(legacy_tpb)


def _load_kr_module():
    """Lädt _kassandra_regime.py — Drive, trading-dashboard/, GitHub, .b64."""
    import base64
    import gzip
    import importlib.util
    import urllib.request

    def _ok(p: Path) -> bool:
        if not p.is_file():
            return False
        try:
            return "build_regime_invest_series" in p.read_text(encoding="utf-8")
        except Exception:
            return False

    candidates = []
    for root in _DRIVE_ROOTS:
        candidates.extend([
            root / "_kassandra_regime.py",
            root / "trading-dashboard" / "_kassandra_regime.py",
        ])
    candidates.append(Path(__file__).resolve().parent / "_kassandra_regime.py")
    candidates.append(Path(__file__).resolve().parent / "trading-dashboard" / "_kassandra_regime.py")

    for p in candidates:
        if _ok(p):
            spec = importlib.util.spec_from_file_location("kr_laender", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "build_regime_invest_series"):
                return mod

    for root in _DRIVE_ROOTS:
        boot = root / "_kr_bootstrap.py"
        if boot.is_file():
            try:
                spec = importlib.util.spec_from_file_location("kr_boot", boot)
                bmod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(bmod)
                dest = bmod._ensure_regime_script(root)
                if _ok(dest):
                    spec = importlib.util.spec_from_file_location("kr_laender", dest)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "build_regime_invest_series"):
                        return mod
            except Exception:
                pass

    github = "https://raw.githubusercontent.com/lazarkitanov-cell/trading-dashboard/main/_kassandra_regime.py"
    for root in _DRIVE_ROOTS:
        dest = root / "_kassandra_regime.py"
        try:
            data = urllib.request.urlopen(github, timeout=45).read()
            if data and b"build_regime_invest_series" in data:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                spec = importlib.util.spec_from_file_location("kr_laender", dest)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
        except Exception:
            pass

    for root in _DRIVE_ROOTS:
        for b64 in (root / "_kassandra_regime.b64", root / "trading-dashboard" / "_kassandra_regime.b64"):
            if not b64.is_file():
                continue
            try:
                raw = gzip.decompress(base64.b64decode(b64.read_text().strip()))
                dest = root / "_kassandra_regime.py"
                dest.write_bytes(raw)
                spec = importlib.util.spec_from_file_location("kr_laender", dest)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "build_regime_invest_series"):
                    return mod
            except Exception:
                continue

    raise FileNotFoundError(
        "_kassandra_regime.py nicht gefunden.\n"
        "  Option A: Kassandra_Regime.ipynb → Zelle 1 ausführen\n"
        "  Option B: Datei nach Colab Notebooks/ kopieren (auch trading-dashboard/)\n"
        "  Option C: _kassandra_regime.b64 auf Drive ablegen"
    )


def _load_regime_invest_series(start: str, end: str, cache: Path) -> pd.Series:
    import pickle

    cache = Path(cache)
    series_path = cache / "regime_invest_series.pkl"
    if series_path.is_file():
        try:
            data = pickle.loads(series_path.read_bytes())
            if data.get("start") == start and data.get("end") == end:
                print(f"  📂 Regime-Serie aus Cache: {series_path.name}")
                return data["series"]
        except Exception:
            pass

    kr = _load_kr_module()
    print("  ⏳ Regime-Investitionsquote berechnen …")
    series = kr.build_regime_invest_series(start=start, end=end)
    try:
        cache.mkdir(parents=True, exist_ok=True)
        series_path.write_bytes(
            pickle.dumps({"start": start, "end": end, "series": series}, protocol=4)
        )
    except Exception:
        pass
    return series


def metrics_from_equity(res: pd.DataFrame, equity: pd.DataFrame) -> dict:
    if res is None or equity is None or res.empty or equity.empty:
        return {}
    yrs = max((equity.index[-1] - equity.index[0]).days / 365.25, 1)
    s = res["Strategie"]
    eq = equity["Strategie"]
    cagr = float(eq.iloc[-1] ** (1 / yrs) - 1)
    dd = float(((eq / eq.cummax() - 1) * 100).min())
    vola = float(s.std() * np.sqrt(252) * 100)
    sharpe = float((s.mean() * 252) / (s.std() * np.sqrt(252) + 1e-10))
    mar = cagr * 100 / abs(dd) if dd else 0.0
    return {
        "cagr": cagr,
        "total": float((eq.iloc[-1] - 1) * 100),
        "vola": vola,
        "dd": dd,
        "sharpe": sharpe,
        "mar": mar,
    }


def _print_results_table(df: pd.DataFrame, title: str):
    print(f"\n{'═' * 78}")
    print(f"  {title}")
    print(f"{'═' * 78}")
    print(
        f"  {'Rang':<4} {'Variante':<28} {'MAR':>6} {'CAGR':>8} "
        f"{'MaxDD':>8} {'Sharpe':>7} {'ΔMAR':>7}"
    )
    print(f"  {'─' * 78}")
    for i, row in df.iterrows():
        dm = row.get("delta_mar_vs_off")
        dm_s = f"{dm:+.2f}" if dm is not None and not pd.isna(dm) else "—"
        print(
            f"  {i + 1:<4} {row['label']:<28} {row['mar']:>6.2f} "
            f"{row['cagr'] * 100:>+7.1f}% {row['dd']:>7.1f}% "
            f"{row['sharpe']:>7.2f} {dm_s:>7}"
        )
    print(f"{'═' * 78}\n")


def run_regime_overlay_compare(nb_globals: dict | None = None, cache_dir: str | Path | None = None):
    g = nb_globals or {}
    cache = _resolve_cache_dir(cache_dir, g)
    data = g.get("data")
    backtest = g.get("backtest")
    if data is None or backtest is None:
        raise RuntimeError("data und backtest müssen im Notebook geladen sein.")

    meine_etfs = g.get("meine_etfs")
    if meine_etfs is None and callable(g.get("frage_meine_ticker")) and g.get("folder"):
        meine_etfs = g["frage_meine_ticker"](g["folder"])
    if meine_etfs is None:
        meine_etfs = []

    start = data.index[0].strftime("%Y-%m-%d")
    end = data.index[-1].strftime("%Y-%m-%d")
    max_slots = int(g.get("MAX_SLOTS_PER_BEREICH", 2))
    regime_invest = _load_regime_invest_series(start, end, cache)

    bt_kw = dict(
        rebal_freq=g.get("REBAL_FREQ", "biweekly"),
        use_cash_etf=True,
        use_dynamic_cash=g.get("USE_DYNAMIC_CASH", False),
        tagesgeldsatz=g.get("TAGESGELDSATZ_DEFAULT", 0.0),
        verbose=False,
    )

    variants = (
        ("Ohne Ampel (100%)", "off", None),
        ("Eigene Ampel (Slots)", "legacy", None),
        ("Kassandra Regime", "regime", regime_invest),
        ("Eigene + Regime (min)", "regime_and_own", regime_invest),
    )

    print("═" * 78)
    print("  LÄNDER-ETF KASSANDRA — REGIME vs. EIGENE AMPel")
    print("═" * 78)
    print(f"  Slots/Bereich: {max_slots} · Rebal: {bt_kw['rebal_freq']} · TS {g.get('TRAILING_STOP', 0.2):.0%}")
    print("  Eigene Ampel: Score 75/50/25 → Slots")
    print("  Regime: 100/50/0 · Breadth+EMA200")
    print("═" * 78)

    rows = []
    for label, mode, regime_series in variants:
        print(f"  ▶ {label} …", flush=True)
        res, equity, _, _, rd = backtest(
            data, meine_etfs,
            ampel_overlay=mode,
            regime_invest=regime_series,
            **bt_kw,
        )
        m = metrics_from_equity(res, equity)
        if m:
            rows.append({"label": label, "ampel_mode": mode, **m})

    if not rows:
        print("  ⚠ Keine Ergebnisse.")
        return {}

    df = pd.DataFrame(rows).sort_values(["mar", "cagr"], ascending=False).reset_index(drop=True)
    base = df.loc[df["ampel_mode"] == "off", "mar"]
    if not base.empty:
        df["delta_mar_vs_off"] = df["mar"] - float(base.iloc[0])
    _print_results_table(df, "REGIME-OVERLAY — sortiert nach MAR")

    out_csv = cache / "laender_kass_regime_overlay_compare.csv"
    try:
        df.to_csv(out_csv, index=False)
        print(f"  💾 CSV: {out_csv}")
    except Exception as ex:
        print(f"  ⚠ CSV: {ex}")
    return df.to_dict("records")
