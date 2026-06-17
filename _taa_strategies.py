
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  TAA-STRATEGIEN — Keller (HAA/BAA/DAA/VAA) + Faber GTAA + Dual Momentum       ║
║  Gleiche Daten · gleiche Kosten · fair vergleichbar                           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

START_DATE = "2008-01-01"
END_DATE = None
INITIAL_CAPITAL = 100_000
TRANSACTION_COST = 0.001   # 0,1 % wie Keller/TuringTrader
WARMUP_MONTHS = 13

import os
import pickle
import time
import warnings
from datetime import date as dt_date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

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
        EODHD_TOKEN = userdata.get("EODHD_API_KEY") or userdata.get("EODHD_TOKEN")
    except Exception:
        EODHD_TOKEN = os.environ.get("EODHD_API_KEY", "")
except ImportError:
    NOTEBOOK_DIR = Path(r"h:\Meine Ablage\Colab Notebooks")
    EODHD_TOKEN = os.environ.get("EODHD_API_KEY", "")

if END_DATE is None:
    END_DATE = dt_date.today().strftime("%Y-%m-%d")

CACHE_FILE = NOTEBOOK_DIR / "taa_strategies_cache.pkl"
EODHD_BASE = "https://eodhd.com/api"
EODHD_DELAY = 0.10

# ── Universen (TuringTrader Keller-Implementierungen) ─────────────────────────

HAA_O = ["SPY", "IWM", "VWO", "VEA", "VNQ", "DBC", "IEF", "TLT"]
HAA_D = ["IEF", "SHY"]
HAA_P = ["TIP"]
HAA_POOL = sorted(set(HAA_O + HAA_D + HAA_P))
HAA_ETF_NAMES = {
    "SPY": "S&P 500", "IWM": "Russell 2000", "VWO": "EM Stocks", "VEA": "Dev. ex-US",
    "VNQ": "US REITs", "DBC": "Commodities", "IEF": "Treasury 7-10Y", "TLT": "Treasury 20+Y",
    "SHY": "Treasury 1-3Y", "TIP": "TIPS (Canary)",
}
MAX_STALE_DAYS = 35   # Warnung + Re-Download wenn Kurs älter als Referenz − N Tage

BAA_O = ["SPY", "QQQ", "IWM", "VGK", "EWJ", "VWO", "VNQ", "DBC", "GLD", "TLT", "HYG", "LQD"]
BAA_D = ["TIP", "DBC", "SHY", "IEF", "TLT", "LQD", "BND"]
BAA_P = ["SPY", "VWO", "VEA", "BND"]

DAA_R = ["SPY", "IWM", "QQQ", "VGK", "EWJ", "VWO", "VNQ", "GSG", "GLD", "TLT", "HYG", "LQD"]
DAA_C = ["SHY", "IEF", "LQD"]
DAA_P = ["VWO", "BND"]

VAA_R = DAA_R
FABER_G = ["SPY", "EFA", "IEF", "VNQ", "DBC"]

ALL_TICKERS = sorted(set(
    HAA_O + HAA_D + HAA_P
    + BAA_O + BAA_D + BAA_P
    + DAA_R + DAA_C + DAA_P
    + FABER_G
    + ["SPY", "EFA", "ACWX", "TLT", "BWX", "HYG", "AGG", "SHY"]
))


def to_eodhd(ticker):
    return f"{ticker}.US" if "." not in ticker else ticker


def fetch_daily(ticker, start=START_DATE, end=END_DATE):
    url = f"{EODHD_BASE}/eod/{to_eodhd(ticker)}"
    try:
        r = requests.get(
            url,
            params={"api_token": EODHD_TOKEN, "fmt": "json", "from": start, "to": end, "period": "d"},
            timeout=30,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        df = pd.DataFrame(data)
        col = "adjusted_close" if "adjusted_close" in df.columns else "close"
        s = df.set_index(pd.to_datetime(df["date"]))[col].astype(float).dropna()
        return s if len(s) > 200 else None
    except Exception:
        return None


def _load_daily_dict(tickers, refresh_stale=True, max_stale_days=MAX_STALE_DAYS):
    """Tagesdaten laden; veraltete Titel optional neu von EODHD holen."""
    tickers = list(dict.fromkeys(tickers))
    daily = {}
    if CACHE_FILE.exists():
        try:
            cached = pickle.loads(CACHE_FILE.read_bytes())
            if cached.get("start") == START_DATE:
                df = cached["daily"]
                daily = {c: df[c] for c in df.columns if c in tickers}
                if daily:
                    print(f"💾 TAA-Cache: {len(daily)} Titel", flush=True)
        except Exception:
            pass

    missing = [t for t in tickers if t not in daily or daily[t].dropna().empty]
    if missing:
        print(f"📡 TAA-Download: {len(missing)} Titel …", flush=True)
        for i, t in enumerate(missing, 1):
            s = fetch_daily(t)
            if s is not None:
                daily[t] = s
            if i % 10 == 0 or i == len(missing):
                print(f"   [{i}/{len(missing)}] · {len(daily)} OK", flush=True)
            time.sleep(EODHD_DELAY)

    if refresh_stale and daily:
        ref = max(s.dropna().index[-1] for s in daily.values() if s is not None and not s.dropna().empty)
        stale = []
        for t in tickers:
            s = daily.get(t)
            if s is None or s.dropna().empty:
                stale.append(t)
                continue
            if (ref - s.dropna().index[-1]).days > max_stale_days:
                stale.append(t)
        if stale:
            print(f"🔄 Veraltete Kurse (> {max_stale_days} Tage): {', '.join(stale)}", flush=True)
            for t in stale:
                s = fetch_daily(t)
                if s is not None:
                    daily[t] = s
                time.sleep(EODHD_DELAY)

    if daily:
        try:
            full = {}
            if CACHE_FILE.exists():
                try:
                    cached = pickle.loads(CACHE_FILE.read_bytes())
                    if cached.get("start") == START_DATE:
                        full = {c: cached["daily"][c] for c in cached["daily"].columns}
                except Exception:
                    pass
            full.update(daily)
            pickle.dump(
                {"start": START_DATE, "end": END_DATE, "daily": pd.DataFrame(full).sort_index()},
                CACHE_FILE.open("wb"),
                protocol=4,
            )
        except Exception:
            pass

    return daily


def ticker_freshness_table(daily, tickers=None, max_stale_days=MAX_STALE_DAYS):
    """Pro ETF: letzter Kurs, Datum, Abstand zur Referenz."""
    if isinstance(daily, pd.DataFrame):
        daily = {c: daily[c] for c in daily.columns}
    tickers = tickers or sorted(daily.keys())
    valid = [daily[t].dropna() for t in tickers if t in daily and not daily[t].dropna().empty]
    if not valid:
        return pd.DataFrame(), None
    ref = max(s.index[-1] for s in valid)
    rows = []
    for t in tickers:
        role = []
        if t in HAA_O:
            role.append("Offensiv")
        if t in HAA_D:
            role.append("Defensiv")
        if t in HAA_P:
            role.append("Canary")
        s = daily.get(t)
        if s is None or s.dropna().empty:
            rows.append({
                "Ticker": t, "Name": HAA_ETF_NAMES.get(t, t), "Rolle": "/".join(role) or "—",
                "Kurs_USD": np.nan, "Letztes_Datum": None, "Tage_hinter_Ref": np.nan, "Status": "FEHLT",
            })
            continue
        s = s.dropna()
        last_dt = s.index[-1]
        lag = (ref - last_dt).days
        rows.append({
            "Ticker": t,
            "Name": HAA_ETF_NAMES.get(t, t),
            "Rolle": "/".join(role) or "—",
            "Kurs_USD": round(float(s.iloc[-1]), 2),
            "Letztes_Datum": last_dt.strftime("%Y-%m-%d"),
            "Tage_hinter_Ref": lag,
            "Status": "WARNUNG" if lag > max_stale_days else "OK",
        })
    return pd.DataFrame(rows), ref


def print_haa_pool_info():
    print("\n" + "═" * 72)
    print("  HAA-BALANCED — ETF-POOL")
    print("═" * 72)
    print(f"  Offensiv (Ranking) : {len(HAA_O)} ETFs  →  {', '.join(HAA_O)}")
    print(f"  Defensiv (Cash)    : {len(HAA_D)} ETFs  →  {', '.join(HAA_D)}")
    print(f"  Canary (Regime)    : {len(HAA_P)} ETF   →  {', '.join(HAA_P)}")
    print("  ─────────────────────────────────────────────────")
    print(f"  Eindeutig im Pool  : {len(HAA_POOL)} ETFs  →  {', '.join(HAA_POOL)}")
    print("  (IEF zählt 1× — in Offensiv + Defensiv)")
    print("═" * 72)


def print_ticker_freshness(daily, tickers=None, max_stale_days=MAX_STALE_DAYS):
    df, ref = ticker_freshness_table(daily, tickers, max_stale_days)
    print("\n" + "═" * 88)
    print("  DATEN-FRISCHE — letzter EOD-Kurs je ETF")
    print("═" * 88)
    if ref is not None:
        print(f"  Referenz (neuester Kurs im Pool): {ref.strftime('%Y-%m-%d')}")
        print(f"  Warnschwelle: > {max_stale_days} Tage hinter Referenz → Re-Download\n")
    hdr = f"  {'Ticker':<6} {'Name':<18} {'Rolle':<14} {'Kurs':>10} {'Datum':>12} {'Lag':>5} {'Status':>8}"
    print(hdr)
    print("  " + "─" * 84)
    for _, r in df.iterrows():
        kurs = f"{r['Kurs_USD']:>10.2f}" if pd.notna(r["Kurs_USD"]) else f"{'—':>10}"
        datum = r["Letztes_Datum"] or "—"
        lag = f"{int(r['Tage_hinter_Ref']):>5}" if pd.notna(r["Tage_hinter_Ref"]) else f"{'—':>5}"
        print(f"  {r['Ticker']:<6} {str(r['Name'])[:18]:<18} {str(r['Rolle'])[:14]:<14} {kurs} {datum:>12} {lag} {r['Status']:>8}")
    n_warn = int((df["Status"] == "WARNUNG").sum()) if not df.empty else 0
    n_miss = int((df["Status"] == "FEHLT").sum()) if not df.empty else 0
    if n_warn or n_miss:
        print(f"\n  ⚠️  {n_warn} veraltet · {n_miss} fehlend — Backtest kann verzerrt sein!")
    else:
        print("\n  ✅ Alle Pool-ETFs haben aktuelle Daten.")
    print("═" * 88)
    return df


def load_monthly(tickers=None, refresh_stale=True, return_daily=False):
    tickers = tickers or ALL_TICKERS
    daily = _load_daily_dict(tickers, refresh_stale=refresh_stale)
    monthly = pd.DataFrame(daily).sort_index().resample("ME").last()
    if return_daily:
        return monthly, pd.DataFrame(daily).sort_index()
    return monthly


def mom13612u(series, i):
    """Keller 13612U — gleichgewichtet 1/3/6/12 Monate."""
    if i < 12:
        return np.nan
    s = series.iloc[: i + 1]
    moms = []
    for m in (1, 3, 6, 12):
        if s.iloc[i - m] <= 0:
            return np.nan
        moms.append(s.iloc[i] / s.iloc[i - m] - 1)
    return float(np.mean(moms))


def mom13612w(series, i):
    """Keller 13612W — gewichtet 12/4/2/1 (Canary / VAA)."""
    if i < 12:
        return np.nan
    s = series.iloc[: i + 1]
    w = [(12, 1), (4, 3), (2, 6), (1, 12)]
    total = 0.0
    for wt, m in w:
        if s.iloc[i - m] <= 0:
            return np.nan
        total += wt * (s.iloc[i] / s.iloc[i - m] - 1)
    return total / 22.0


def mom_sma13(series, i):
    """BAA Ranking: Kurs vs. SMA(12) = 13 Monate."""
    if i < 12:
        return np.nan
    s = series.iloc[: i + 1]
    sma = s.iloc[i - 12 : i + 1].mean()
    if sma <= 0:
        return np.nan
    return s.iloc[i] / sma - 1


def mom12m(series, i):
    if i < 12:
        return np.nan
    s = series.iloc[: i + 1]
    if s.iloc[i - 12] <= 0:
        return np.nan
    return s.iloc[i] / s.iloc[i - 12] - 1


def mom_at(monthly, ticker, i, fn):
    if ticker not in monthly.columns:
        return np.nan
    s = monthly[ticker].iloc[: i + 1].dropna()
    if s.empty:
        return np.nan
    return fn(s, len(s) - 1)


def mom_table(monthly, tickers, i, fn):
    return {t: mom_at(monthly, t, i, fn) for t in tickers if t in monthly.columns}


def _w(weights):
    w = {k: v for k, v in weights.items() if v and v > 0}
    s = sum(w.values())
    return {k: v / s for k, v in w.items()} if s > 0 else {}


def haa_signal_detail(monthly, i=None):
    """Live-Signal inkl. Canary-Status und Rankings."""
    if i is None:
        i = len(monthly) - 1
    o_m = mom_table(monthly, HAA_O, i, mom13612u)
    d_m = mom_table(monthly, HAA_D, i, mom13612u)
    p_m = mom_table(monthly, HAA_P, i, mom13612u)
    if not o_m or not d_m:
        return {"weights": {}, "crash": False, "cash": None, "picks": [], "top_n": 0, "o_m": o_m, "d_m": d_m, "p_m": p_m}
    cash = max(d_m.items(), key=lambda x: x[1] if x[1] is not None else -999)[0]
    crash = any(v is not None and v < 0 for v in p_m.values())
    if crash:
        return {
            "weights": _w({cash: 1.0}),
            "crash": True,
            "cash": cash,
            "picks": [],
            "top_n": 0,
            "o_m": o_m,
            "d_m": d_m,
            "p_m": p_m,
            "regime": "defensive",
        }
    top_n = max(1, len(HAA_O) // 2)
    ranked = sorted(o_m.items(), key=lambda x: x[1] if x[1] is not None else -999, reverse=True)
    picks = [t for t, _ in ranked[:top_n]]
    slots = {}
    for t in picks:
        slots[t] = slots.get(t, 0) + 1.0 / top_n
        if o_m.get(t) is not None and o_m[t] < 0:
            slots[cash] = slots.get(cash, 0) + 1.0 / top_n
            slots[t] -= 1.0 / top_n
    return {
        "weights": _w(slots),
        "crash": False,
        "cash": cash,
        "picks": picks,
        "top_n": top_n,
        "o_m": o_m,
        "d_m": d_m,
        "p_m": p_m,
        "regime": "offensive",
    }


def haa_selection_rows(sig, weights=None):
    """Vergleichstabelle: warum welche ETFs gewählt wurden."""
    weights = weights or sig.get("weights") or {}
    o_m = sig.get("o_m") or {}
    d_m = sig.get("d_m") or {}
    p_m = sig.get("p_m") or {}
    cash = sig.get("cash")
    crash = bool(sig.get("crash"))
    picks = sig.get("picks") or []
    top_n = sig.get("top_n") or max(1, len(HAA_O) // 2)

    ranked = sorted(o_m.items(), key=lambda x: x[1] if x[1] is not None else -999, reverse=True)
    rank_map = {t: r for r, (t, _) in enumerate(ranked, 1)}

    off_rows = []
    for t, m in ranked:
        rank = rank_map[t]
        w = float(weights.get(t, 0.0))
        mom = None if m is None or (isinstance(m, float) and np.isnan(m)) else float(m)
        if crash:
            status, grund = "—", "TIP-Canary negativ → nur Defensiv aktiv"
        elif t in picks and w > 0.001:
            status, grund = "GEWÄHLT", f"Top-{top_n} Momentum (Rang {rank}/{len(HAA_O)})"
        elif t in picks:
            status, grund = "→ Cash", f"Top-{top_n} (Rang {rank}), Momentum negativ → Slot an {cash}"
        else:
            status, grund = "—", f"Rang {rank}/{len(HAA_O)} — unter Top-{top_n}, nicht im Ziel"
        off_rows.append({
            "ticker": t,
            "name": HAA_ETF_NAMES.get(t, t),
            "rolle": "Offensiv",
            "rang": rank,
            "momentum_13612u": mom,
            "momentum_pct": None if mom is None else round(mom * 100, 2),
            "ziel_gewicht": round(w, 4),
            "status": status,
            "begruendung": grund,
        })

    def_ranked = sorted(d_m.items(), key=lambda x: x[1] if x[1] is not None else -999, reverse=True)
    def_rows = []
    for r, (t, m) in enumerate(def_ranked, 1):
        mom = None if m is None or (isinstance(m, float) and np.isnan(m)) else float(m)
        w = float(weights.get(t, 0.0))
        if crash and t == cash:
            status, grund = "100%", f"Bester Defensiv-Momentum (Rang {r}) + TIP-Crash"
        elif w > 0.001:
            status, grund = f"{w:.0%}", f"Im Ziel-Portfolio ({w:.0%})"
        elif t == cash:
            status, grund = "Cash-FB", f"Bester Defensiv-Momentum (Rang {r}) — Fallback bei negativem Slot"
        else:
            status, grund = "—", f"Rang {r}/{len(HAA_D)} Defensiv — nicht im Ziel"
        def_rows.append({
            "ticker": t,
            "name": HAA_ETF_NAMES.get(t, t),
            "rolle": "Defensiv",
            "rang": r,
            "momentum_13612u": mom,
            "momentum_pct": None if mom is None else round(mom * 100, 2),
            "ziel_gewicht": round(w, 4),
            "status": status,
            "begruendung": grund,
        })

    tip_m = p_m.get("TIP")
    tip_val = None if tip_m is None or (isinstance(tip_m, float) and np.isnan(tip_m)) else float(tip_m)
    canary = {
        "ticker": "TIP",
        "name": HAA_ETF_NAMES.get("TIP", "TIP"),
        "momentum_13612u": tip_val,
        "momentum_pct": None if tip_val is None else round(tip_val * 100, 2),
        "schwelle": 0.0,
        "status": "CRASH → 100% Defensiv" if crash else "OK → Offensiv-Ranking",
        "begruendung": (
            "TIP-Momentum negativ — Keller-Regel: komplett in besten Defensiv-ETF"
            if crash
            else "TIP positiv — Top-4 Offensive nach 13612U-Momentum"
        ),
    }

    regel = (
        "Regel: TIP (Canary) negativ → 100 % "
        f"{HAA_ETF_NAMES.get(cash, cash)}. "
        f"Sonst Top-{top_n} aus {len(HAA_O)} Offensive; pro Slot mit negativem Momentum → {cash}."
    )
    return {
        "regel_text": regel,
        "momentum_methode": "13612U = Ø(1M, 3M, 6M, 12M Rendite) — Keller/TuringTrader",
        "offensiv": off_rows,
        "defensiv": def_rows,
        "canary": canary,
    }


def print_haa_selection_explanation(sig, weights=None):
    """Konsolen-Ausgabe: Vergleich aller Pool-ETFs vs. Auswahl."""
    data = haa_selection_rows(sig, weights)
    print("\n" + "═" * 96)
    print("  WARUM DIESE ETFs? — Vergleich aller Kandidaten")
    print("═" * 96)
    print(f"  {data['momentum_methode']}")
    print(f"  {data['regel_text']}\n")

    c = data["canary"]
    tip_s = f"{c['momentum_13612u']:+.1%}" if c.get("momentum_13612u") is not None else "—"
    print(f"  CANARY  {c['ticker']:<6} {c['name']:<18}  Momentum {tip_s:>8}  →  {c['status']}")
    print(f"          {c['begruendung']}\n")

    print(f"  OFFENSIV — Ranking ({len(data['offensiv'])} Kandidaten, Top-{sig.get('top_n') or len(sig.get('picks') or [])} gewählt)")
    print(f"  {'Rang':>4} {'Ticker':<6} {'Name':<18} {'Mom13612U':>10} {'Ziel':>6} {'Status':<10} Begründung")
    print("  " + "─" * 90)
    for r in data["offensiv"]:
        mom = f"{r['momentum_13612u']:+.1%}" if r.get("momentum_13612u") is not None else f"{'—':>10}"
        ziel = f"{r['ziel_gewicht']:.0%}" if r["ziel_gewicht"] > 0.001 else "—"
        print(
            f"  {r['rang']:>4} {r['ticker']:<6} {str(r['name'])[:18]:<18} {mom:>10} {ziel:>6} "
            f"{r['status']:<10} {r['begruendung']}"
        )

    print(f"\n  DEFENSIV — Cash-Fallback ({len(data['defensiv'])} Kandidaten)")
    print(f"  {'Rang':>4} {'Ticker':<6} {'Name':<18} {'Mom13612U':>10} {'Ziel':>6} {'Status':<10} Begründung")
    print("  " + "─" * 90)
    for r in data["defensiv"]:
        mom = f"{r['momentum_13612u']:+.1%}" if r.get("momentum_13612u") is not None else f"{'—':>10}"
        ziel = f"{r['ziel_gewicht']:.0%}" if r["ziel_gewicht"] > 0.001 else "—"
        print(
            f"  {r['rang']:>4} {r['ticker']:<6} {str(r['name'])[:18]:<18} {mom:>10} {ziel:>6} "
            f"{r['status']:<10} {r['begruendung']}"
        )
    print("═" * 96)
    return data


def alloc_haa(monthly, i):
    """HAA-Balanced (TuringTrader Keller_HAA_v2)."""
    return haa_signal_detail(monthly, i)["weights"]


def alloc_baa_g12(monthly, i):
    """BAA-G12 balanced (TO=6, TD=3, B=1)."""
    o_m = mom_table(monthly, BAA_O, i, mom_sma13)
    d_m = mom_table(monthly, BAA_D, i, mom_sma13)
    c_m = mom_table(monthly, BAA_P, i, mom13612w)
    if not o_m or not d_m:
        return {}
    bad = sum(1 for v in c_m.values() if v is not None and v < 0)
    p_def = min(1.0, bad / 1.0)
    p_off = 1.0 - p_def
    cash = "SHY"
    shy_m = d_m.get(cash, -999)
    d_rank = sorted(d_m.items(), key=lambda x: x[1] if x[1] is not None else -999, reverse=True)
    d_pick = [t if (d_m.get(t, -999) >= shy_m) else cash for t, _ in d_rank[:3]]
    w = {}
    for t in sorted(o_m, key=lambda x: o_m.get(x, -999), reverse=True)[:6]:
        w[t] = w.get(t, 0) + p_off / 6
    for t in d_pick:
        w[t] = w.get(t, 0) + p_def / 3
    return _w(w)


def alloc_daa_g12(monthly, i):
    """DAA-G12 (T=6, B=2, Canary VWO+BND)."""
    r_m = mom_table(monthly, DAA_R, i, mom13612w)
    c_m = mom_table(monthly, DAA_C, i, mom13612w)
    p_m = mom_table(monthly, DAA_P, i, mom13612w)
    if not r_m or not c_m:
        return {}
    b = sum(1 for v in p_m.values() if v is not None and v < 0)
    T, B = 6, 2
    cf = min(1.0, (1.0 / T) * np.floor(b * T / B))
    t_n = int(round((1.0 - cf) * T))
    cash = max(c_m.items(), key=lambda x: x[1] if x[1] is not None else -999)[0]
    w = {cash: cf}
    for t, _ in sorted(r_m.items(), key=lambda x: x[1] if x[1] is not None else -999, reverse=True)[: max(t_n, 0)]:
        w[t] = w.get(t, 0) + (1.0 - cf) / max(t_n, 1)
    return _w(w)


def alloc_vaa_g12(monthly, i):
    """VAA-G12 breadth (B=1): ein negatives Asset → defensiv."""
    r_m = mom_table(monthly, VAA_R, i, mom13612w)
    c_m = mom_table(monthly, DAA_C, i, mom13612w)
    if not r_m or not c_m:
        return {}
    b = sum(1 for v in r_m.values() if v is not None and v < 0)
    T, B = 6, 1
    cf = min(1.0, b / B)
    t_n = max(0, int(round((1.0 - cf) * T)))
    cash = max(c_m.items(), key=lambda x: x[1] if x[1] is not None else -999)[0]
    if cf >= 1.0 or t_n == 0:
        return _w({cash: 1.0})
    w = {}
    for t, _ in sorted(r_m.items(), key=lambda x: x[1] if x[1] is not None else -999, reverse=True)[:t_n]:
        w[t] = (1.0 - cf) / t_n
    w[cash] = w.get(cash, 0) + cf
    return _w(w)


def alloc_faber_gtaa(monthly, i):
    """Faber GTAA — 5 Asset-Klassen, je 20 %, 10M-SMA Filter."""
    safe = "SHY"
    w = {}
    for t in FABER_G:
        s = monthly[t].iloc[: i + 1].dropna() if t in monthly.columns else pd.Series(dtype=float)
        if len(s) < 11:
            continue
        if s.iloc[-1] > s.iloc[-10:].mean():
            w[t] = 0.2
        else:
            w[safe] = w.get(safe, 0) + 0.2
    return _w(w)


def alloc_dual_momentum_balanced(monthly, i):
    """Antonacci Global Balanced Momentum (vereinfacht)."""
    bil_m = mom_at(monthly, "SHY", i, mom12m)
    eq_c = {"SPY": "SPY", "ACWX": "ACWX", "SHY": "SHY"}
    bd_c = {"TLT": "TLT", "BWX": "BWX", "HYG": "HYG", "SHY": "SHY"}
    safe = "AGG" if "AGG" in monthly.columns else "TLT"

    def _pick(cands):
        m = {k: mom_at(monthly, t, i, mom12m) for k, t in cands.items() if t in monthly.columns}
        if not m:
            return safe
        best = max(m.items(), key=lambda x: x[1] if x[1] is not None else -999)[0]
        if m.get(best, -999) < (bil_m if bil_m is not None and not np.isnan(bil_m) else 0):
            return safe
        return cands[best]

    eq_t = _pick(eq_c)
    bd_t = _pick(bd_c)
    return _w({eq_t: 0.7, bd_t: 0.3})


def alloc_spy(monthly, i):
    return {"SPY": 1.0}


def alloc_6040(monthly, i):
    return {"SPY": 0.5, "IEF": 0.5}


STRATEGIES = [
    ("HAA-Balanced", alloc_haa),
    ("BAA-G12", alloc_baa_g12),
    ("DAA-G12", alloc_daa_g12),
    ("VAA-G12", alloc_vaa_g12),
    ("Faber GTAA", alloc_faber_gtaa),
    ("Dual Momentum", alloc_dual_momentum_balanced),
    ("SPY B&H", alloc_spy),
    ("60/40", alloc_6040),
]


def run_backtest(monthly, alloc_fn, name=""):
    dates = monthly.index[WARMUP_MONTHS:]
    pf = float(INITIAL_CAPITAL)
    prev_w = {}
    rows = []

    for di, dt in enumerate(dates):
        if di == 0:
            prev_w = alloc_fn(monthly, monthly.index.get_loc(dt))
            continue
        prev_dt = dates[di - 1]
        mr = 0.0
        for t, w in prev_w.items():
            if t not in monthly.columns or w <= 0:
                continue
            p0 = monthly[t].loc[:prev_dt].dropna()
            p1 = monthly[t].loc[:dt].dropna()
            if len(p0) and len(p1) and p0.iloc[-1] > 0:
                mr += w * (p1.iloc[-1] / p0.iloc[-1] - 1)

        new_w = alloc_fn(monthly, monthly.index.get_loc(dt))
        if not new_w:
            new_w = prev_w
        turnover = sum(abs(new_w.get(t, 0) - prev_w.get(t, 0)) for t in set(new_w) | set(prev_w)) / 2
        net = mr - turnover * TRANSACTION_COST
        pf *= 1 + net
        rows.append({"date": dt, "portfolio_value": pf, "monthly_return": net})
        prev_w = new_w

    return pd.DataFrame(rows).set_index("date")


def metrics(eq, rf=0.02):
    eq = eq["portfolio_value"].dropna()
    if len(eq) < 6:
        return None
    mr = eq.pct_change().dropna()
    yrs = max(len(eq) / 12, 0.5)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    vol = mr.std() * np.sqrt(12)
    sharpe = (mr.mean() * 12 - rf) / vol if vol > 0 else np.nan
    mar = cagr / abs(dd) if dd < 0 else np.nan
    return dict(cagr=cagr, maxdd=dd, sharpe=sharpe, mar=mar, end=eq.iloc[-1], eq=eq)


def slice_metrics(bt, from_date, capital=INITIAL_CAPITAL):
    sub = bt.loc[pd.Timestamp(from_date):]
    if sub.empty:
        return None
    base = sub.iloc[0]["portfolio_value"]
    scaled = sub["portfolio_value"] / base * capital
    return metrics(scaled.to_frame("portfolio_value"))


def yearly_metrics(bt, rf=0.02):
    """Kalenderjahres-Kennzahlen aus Monats-Backtest."""
    if bt.empty:
        return pd.DataFrame()
    rows = []
    for year in sorted(bt.index.year.unique()):
        sub = bt.loc[bt.index.year == year]
        if len(sub) < 2:
            continue
        eq = sub["portfolio_value"]
        rets = sub["monthly_return"].dropna()
        yr_ret = eq.iloc[-1] / eq.iloc[0] - 1
        dd = (eq / eq.cummax() - 1).min()
        vol = rets.std() * np.sqrt(12) if len(rets) > 1 else np.nan
        sharpe = (rets.mean() * 12 - rf) / vol if vol and vol > 0 else np.nan
        mar = yr_ret / abs(dd) if dd < 0 else np.nan
        rows.append({
            "Jahr": int(year),
            "Rendite": yr_ret,
            "Vola": vol,
            "Sharpe": sharpe,
            "MaxDD": dd,
            "MAR": mar,
        })
    return pd.DataFrame(rows)


def print_yearly_table(df, title):
    print("\n" + "═" * 88)
    print(f"  {title}")
    print("═" * 88)
    print(f"  {'Jahr':>6} {'Rendite':>9} {'Vola':>8} {'Sharpe':>8} {'MaxDD':>9} {'MAR':>8}")
    print("  " + "─" * 56)
    if df.empty:
        print("  (keine Daten)")
    for _, r in df.iterrows():
        print(
            f"  {int(r['Jahr']):>6} {r['Rendite']*100:+8.1f}% {r['Vola']*100:7.1f}% "
            f"{r['Sharpe']:8.2f} {r['MaxDD']*100:+8.1f}% {r['MAR']:8.2f}"
        )
    print("═" * 88)


def run_haa_backtest_report(from_periods=None, rf=0.02, plot=True):
    """Erweiterter HAA-Backtest: Pool, Datenfrische, Gesamt + Jahres-Tabelle."""
    if not EODHD_TOKEN:
        raise RuntimeError("EODHD_API_KEY fehlt.")

    from_periods = from_periods or [
        ("2008-01-01", "Gesamt ab 2008"),
        ("2018-01-01", "Ab 2018"),
    ]

    print_haa_pool_info()
    monthly, daily = load_monthly(HAA_POOL, refresh_stale=True, return_daily=True)
    print_ticker_freshness(daily, HAA_POOL)

    print(f"\n  Monatsbars Backtest: {monthly.index[0].date()} → {monthly.index[-1].date()} ({len(monthly)})\n")

    haa_bt = run_backtest(monthly, alloc_haa, "HAA-Balanced")
    spy_bt = run_backtest(monthly, alloc_spy, "SPY")

    for from_d, label in from_periods:
        rows = []
        for name, bt in [("HAA-Balanced", haa_bt), ("SPY B&H", spy_bt)]:
            m = slice_metrics(bt, from_d)
            if m:
                rows.append((name, m))
        print_table(rows, f"{label}  ({from_d} → {END_DATE})")

    print_yearly_table(yearly_metrics(haa_bt, rf), "HAA-BALANCED — Performance pro Kalenderjahr")
    print_yearly_table(yearly_metrics(spy_bt, rf), "SPY B&H — Performance pro Kalenderjahr")

    haa_m = slice_metrics(haa_bt, "2018-01-01")
    spy_m = slice_metrics(spy_bt, "2018-01-01")
    if plot and haa_m and spy_m:
        plot_results(
            [("HAA-Balanced", haa_m["eq"]), ("SPY B&H", spy_m["eq"])],
            f"HAA-Balanced vs. SPY ab 2018 (€{INITIAL_CAPITAL:,})",
            f"haa_backtest_{pd.Timestamp.now():%Y%m%d}.png",
        )

    return dict(monthly=monthly, daily=daily, haa_bt=haa_bt, spy_bt=spy_bt)


def print_table(results, title):
    print("\n" + "═" * 105)
    print(f"  {title}")
    print("═" * 105)
    hdr = f"  {'Strategie':<18} {'CAGR':>8} {'Sharpe':>7} {'MaxDD':>8} {'MAR':>7} {'Endwert':>12}"
    print(hdr)
    print("  " + "─" * 101)
    for name, m in sorted(results, key=lambda x: -(x[1]["mar"] if x[1] and x[1].get("mar") is not None else -999)):
        if not m:
            print(f"  {name:<18} {'—':>8} {'—':>7} {'—':>8} {'—':>7} {'—':>12}")
            continue
        print(
            f"  {name:<18} {m['cagr']*100:+7.2f}% {m['sharpe']:7.2f} "
            f"{m['maxdd']*100:+7.2f}% {m['mar']:7.2f} €{m['end']:,.0f}"
        )
    print("═" * 105)


def plot_results(curves, title, out_name):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(14, 9), gridspec_kw={"height_ratios": [2, 1]})
    ax0, ax1 = axes
    for label, eq in curves:
        if eq is None or eq.empty:
            continue
        norm = eq / eq.iloc[0] * INITIAL_CAPITAL
        norm.plot(ax=ax0, label=label, linewidth=1.6 if "SPY" not in label else 1.0, alpha=0.9)
    ax0.set_title(title)
    ax0.legend(loc="upper left", fontsize=8)
    ax0.grid(True, alpha=0.3)
    ax0.set_ylabel("Portfoliowert")
    for label, eq in curves:
        if eq is None or eq.empty:
            continue
        norm = eq / eq.iloc[0] * INITIAL_CAPITAL
        (norm / norm.cummax() - 1).plot(ax=ax1, label=label, alpha=0.75)
    ax1.set_ylabel("Drawdown")
    ax1.grid(True, alpha=0.3)
    plt.tight_layout()
    out = NOTEBOOK_DIR / out_name
    try:
        fig.savefig(out, dpi=120)
        print(f"📊 Chart: {out}")
    except Exception:
        pass
    plt.show()


COMPARE_PERIODS = [
    ("2008-01-01", "Gesamt ab 2008 (inkl. Finanzkrise)"),
    ("2018-01-01", "Ab 2018 (wie dein Strategie-Vergleich)"),
]


def run_taa_vergleich():
    if not EODHD_TOKEN:
        raise RuntimeError("EODHD_API_KEY fehlt (Colab Secrets oder Umgebungsvariable).")

    print("═" * 72)
    print("  TAA-STRATEGIE-VERGLEICH — fair · gleiche Daten · gleiche Kosten")
    print("═" * 72)
    print(f"  Daten: {START_DATE} → {END_DATE}  ·  TC {TRANSACTION_COST:.1%}/Monat")

    monthly = load_monthly()
    print(f"  Monatsbars: {monthly.index[0].date()} → {monthly.index[-1].date()} ({len(monthly)})\n")

    full_bts = {}
    for name, fn in STRATEGIES:
        print(f"  ⟳ {name} …", flush=True)
        full_bts[name] = run_backtest(monthly, fn, name)

    for from_d, label in COMPARE_PERIODS:
        rows = []
        curves = []
        for name, bt in full_bts.items():
            m = slice_metrics(bt, from_d)
            rows.append((name, m))
            if m:
                curves.append((name, m["eq"]))
        print_table(rows, f"{label}  ({from_d} → {END_DATE})")
        if from_d == "2018-01-01":
            plot_results(
                curves,
                f"TAA-Vergleich ab {from_d} (€{INITIAL_CAPITAL:,})",
                f"taa_vergleich_{pd.Timestamp.now():%Y%m%d}.png",
            )

    print("\n  ℹ️  Regeln: TuringTrader BooksAndPubs (Keller HAA/BAA/DAA, Faber, Antonacci)")
    print("  ℹ️  MAR = CAGR ÷ |Max Drawdown|  ·  Alle Strategien im SELBEN Zeitraum vergleichbar")
    print("\n✅ Fertig.")


if __name__ == "__main__":
    run_taa_vergleich()
