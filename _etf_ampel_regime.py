"""ETF Aktien Ampel — Kassandra Regime Overlay (Backtest-Vergleich + Live-Hook)."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from _laender_kass_regime import _invest_from_regime_series, _load_regime_invest_series

_DRIVE_ROOTS = (
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
)


def _resolve_cache_dir(cache_dir=None, g=None):
    if cache_dir:
        return Path(cache_dir)
    if g:
        for key in ("CACHE_ORDNER", "cache_dir", "folder"):
            if g.get(key):
                return Path(g[key])
    for root in _DRIVE_ROOTS:
        for sub in ("regime_cache", "KassandraStrategy", ""):
            p = root / sub if sub else root
            if (p / "regime_final.json").is_file() or (p / "breadth_panel.pkl").is_file():
                return p
    return _DRIVE_ROOTS[-1]


def resolve_ampel_quote(
    overlay: str,
    tag,
    p_slice,
    regime_invest: pd.Series | None,
    historische_ampel,
) -> tuple[str, float, int | None, float | None]:
    """(signal, quote, score, vix) — quote = Investitionsanteil 0..1."""
    if overlay == "off":
        return "GRUEN", 1.0, 100, None

    leg_sig, leg_q, score, vix = historische_ampel(p_slice)
    if overlay in ("legacy", None, "") or regime_invest is None:
        return leg_sig, float(leg_q), score, vix

    rp = _invest_from_regime_series(regime_invest, tag)
    if overlay == "regime":
        q = rp
    elif overlay == "regime_and_own":
        q = min(float(leg_q), rp)
    else:
        return leg_sig, float(leg_q), score, vix

    sig = "GRUEN" if q >= 0.999 else ("GELB" if q >= 0.749 else "ROT")
    return sig, q, int(round(q * 100)), vix


def _parse_metric(val, as_pct=False) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    s = str(val).strip().replace(",", ".")
    m = re.search(r"[-+]?\d*\.?\d+", s)
    if not m:
        return 0.0
    v = float(m.group())
    if as_pct and "%" in s and abs(v) > 1.5:
        v /= 100.0
    return v


def metrics_from_bt(k_port: dict | None, raw_stats: dict | None = None) -> dict:
    if raw_stats and isinstance(raw_stats, dict):
        cagr = raw_stats.get("cagr")
        if cagr is not None:
            cagr_f = float(cagr)
            dd_raw = float(raw_stats.get("max_dd", raw_stats.get("dd", 0)))
            dd_pct = dd_raw * 100.0 if abs(dd_raw) <= 1.5 else dd_raw
            mar = raw_stats.get("mar")
            if mar is not None:
                mar_f = float(mar)
            elif dd_raw:
                mar_f = cagr_f / abs(dd_raw)
            else:
                mar_f = 0.0
            return {
                "cagr": cagr_f,
                "dd": dd_pct,
                "sharpe": float(raw_stats.get("sharpe", 0)),
                "mar": mar_f,
            }
    if not k_port:
        return {}
    cagr = _parse_metric(k_port.get("CAGR"), as_pct=True)
    dd = _parse_metric(k_port.get("Max Drawdown", k_port.get("MaxDD")))
    sharpe = _parse_metric(k_port.get("Sharpe"))
    mar = _parse_metric(k_port.get("MAR", k_port.get("Calmar")))
    if not mar and dd:
        mar = cagr / abs(dd / 100.0) if abs(dd) > 1.5 else cagr / abs(dd)
    if abs(dd) <= 1.5 and dd != 0:
        dd = dd * 100.0
    return {"cagr": cagr, "dd": dd, "sharpe": sharpe, "mar": mar}


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
    """
    ETF Aktien Ampel: Ohne Ampel | Eigene Ampel | Regime | min(beide).
    Erwartet Notebook-Globals: run_backtest, _historische_ampel, BT_JAHRE, …
    """
    g = nb_globals or {}
    run_backtest = g.get("run_backtest")
    if not callable(run_backtest):
        raise RuntimeError("run_backtest() muss im Notebook geladen sein.")

    cache = _resolve_cache_dir(cache_dir, g)
    bt_jahre = int(g.get("BT_JAHRE", 5))
    end = pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    start = (pd.Timestamp.today() - pd.DateOffset(years=bt_jahre)).strftime("%Y-%m-%d")

    regime_invest = _load_regime_invest_series(start, end, cache)

    bt_kw = dict(
        rebal_freq=g.get("BT_REBAL", "ME"),
        trailing_stop=g.get("TRAILING_STOP_PCT", 0.10),
        max_vola=g.get("MAX_VOLA_PA"),
        sektor_cap=g.get("SEKTOR_CAP", 2),
        rsl_periode=g.get("RSL_PERIODE"),
        rsl_filter=g.get("RSL_FILTER"),
        top_n_etfs=g.get("TOP_N_ETFS", 4),
        top_n_aktien=g.get("TOP_N_AKTIEN", 3),
        ampel_freq=g.get("BT_AMPEL_FREQ", "rebal"),
        trailing_freq="daily",
        verbose=False,
    )

    variants = (
        ("Ohne Ampel (100%)", "off", None),
        ("Eigene Ampel (Score)", "legacy", None),
        ("Kassandra Regime", "regime", regime_invest),
        ("Eigene + Regime (min)", "regime_and_own", regime_invest),
    )

    print("═" * 78)
    print("  ETF AKTIEN AMPEL — REGIME vs. EIGENE AMPEL")
    print("═" * 78)
    print(
        f"  ETFs={bt_kw['top_n_etfs']} · Aktien/ETF={bt_kw['top_n_aktien']} · "
        f"Rebal={bt_kw['rebal_freq']} · TS {bt_kw['trailing_stop']:.0%}"
    )
    print("  Eigene Ampel: Score 75/50 → Quote 100/75/50%")
    print("  Regime: Breadth+EMA200 · 100/50/0 · 3d Glättung · SPY −3% Overlay")
    print("  'Beide' = min(eigene Quote, Regime-Quote)")
    print("═" * 78)

    rows = []
    for label, mode, regime_series in variants:
        print(f"  ▶ {label} …", flush=True)
        try:
            _df, _log, k_port, raw = run_backtest(
                ampel_overlay=mode,
                regime_invest=regime_series,
                **bt_kw,
            )
        except Exception as ex:
            print(f"    ⚠ {ex}")
            continue
        m = metrics_from_bt(k_port, raw)
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

    out_dir = Path(g.get("CACHE_ORDNER", cache))
    out_csv = out_dir / "etf_ampel_regime_overlay_compare.csv"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_csv, index=False)
        print(f"  💾 CSV: {out_csv}")
    except Exception as ex:
        print(f"  ⚠ CSV: {ex}")
    return df.to_dict("records")
