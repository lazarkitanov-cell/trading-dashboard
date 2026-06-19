"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  KASSANDRA REGIME — Live-Signal + stufenweiser Backtest (Stufe 0–5)          ║
║  Ampel steuert Investitionsquote im Index-ETF vs. Buy & Hold                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

REGIME_VERSION = 5

import itertools
import json
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

# ── Pfade & API ───────────────────────────────────────────────────────────────

DRIVE_CANDIDATES = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
]
_UNIVERSE_REL = Path("Meine Aktienlisten") / "S&P 500 nach Marktkapitalisierung.csv"


def _resolve_notebook_dir() -> Path:
    try:
        script_dir = Path(__file__).resolve().parent
        if (script_dir / _UNIVERSE_REL).exists():
            return script_dir
    except NameError:
        pass
    try:
        from google.colab import drive
        if not Path("/content/drive/MyDrive").exists():
            drive.mount("/content/drive")
    except ImportError:
        pass
    for p in DRIVE_CANDIDATES:
        if (p / _UNIVERSE_REL).exists():
            return p
    local = Path(r"h:\Meine Ablage\Colab Notebooks")
    if (local / _UNIVERSE_REL).exists():
        return local
    return Path(".").resolve()


NOTEBOOK_DIR = _resolve_notebook_dir()
UNIVERSE_CSV = NOTEBOOK_DIR / _UNIVERSE_REL
CACHE_DIR = NOTEBOOK_DIR / "regime_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "2008-01-01"
END_DATE = dt_date.today().strftime("%Y-%m-%d")
INITIAL_CAPITAL = 100_000
TRANSACTION_COST = 0.001
BREADTH_SAMPLE = 80
BREADTH_MA = 200
BREADTH_THRESHOLD = 0.50
VIX_THRESHOLD = 30.0
EMA_SPAN = 200
SMA_SPAN = 21
EODHD_DELAY = 0.2

try:
    from google.colab import userdata
    EODHD_TOKEN = userdata.get("EODHD_API_KEY") or userdata.get("EODHD_TOKEN") or ""
except ImportError:
    EODHD_TOKEN = os.environ.get("EODHD_API_KEY", "")

EODHD_BASE = "https://eodhd.com/api/eod"
MARKET_SYMS = {
    "SPY": "SPY.US",
    "VIX": "VIX.INDX",
    "TNX": "TNX.INDX",
    "IRX": "IRX.INDX",
}

BENCHMARKS = {
    "SPY": {"ticker": "SPY.US", "label": "S&P 500"},
    "QQQ": {"ticker": "QQQ.US", "label": "Nasdaq 100"},
    "EXSA": {"ticker": "EXSA.XETRA", "label": "STOXX Europe 600"},
    "VT": {"ticker": "VT.US", "label": "World (VT)"},
    "AAXJ": {"ticker": "AAXJ.US", "label": "Asia ex-Japan"},
}

COMPONENTS = {
    "C1_SPY_EMA200": "SPY > EMA200",
    "C2_VIX_30": f"VIX < {VIX_THRESHOLD:.0f}",
    "C3_YIELD_SPREAD": "10Y − 3M Spread > 0",
    "C4_SPY_SMA21": "SPY > SMA21",
    "C5_BREADTH_50": f"Breadth ≥ {BREADTH_THRESHOLD:.0%} über MA200",
}

ISIN_MAP_FILE = CACHE_DIR / "isin_to_ticker.pkl"
BREADTH_CACHE = CACHE_DIR / "breadth_panel.pkl"
MARKET_CACHE = CACHE_DIR / "market_panel.pkl"


def _require_api():
    if not EODHD_TOKEN:
        raise RuntimeError("EODHD_API_KEY fehlt (Colab Secrets oder Umgebungsvariable).")


def fetch_eod(symbol: str, start: str = START_DATE, end: str = END_DATE) -> pd.Series:
    _require_api()
    try:
        r = requests.get(
            f"{EODHD_BASE}/{symbol}",
            params={"api_token": EODHD_TOKEN, "fmt": "json", "from": start, "to": end},
            timeout=30,
        )
        if r.status_code != 200:
            return pd.Series(dtype=float)
        rows = r.json()
        if not rows:
            return pd.Series(dtype=float)
        df = pd.DataFrame(rows)
        col = "adjusted_close" if "adjusted_close" in df.columns else "close"
        return df.set_index(pd.to_datetime(df["date"]))[col].astype(float).sort_index()
    except Exception:
        return pd.Series(dtype=float)


def resolve_isin(isin: str, cache: dict) -> str | None:
    isin = isin.strip().upper()
    if isin in cache:
        return cache[isin]
    try:
        r = requests.get(
            f"https://eodhd.com/api/search/{isin}",
            params={"api_token": EODHD_TOKEN, "limit": 10},
            timeout=20,
        )
        if r.status_code != 200:
            cache[isin] = None
            return None
        hits = r.json() or []
        for h in hits:
            code = (h.get("Code") or "").upper()
            ex = (h.get("Exchange") or "").upper()
            if ex in ("US", "NYSE", "NASDAQ", "BATS", "AMEX") and code:
                cache[isin] = code
                time.sleep(EODHD_DELAY)
                return code
        if hits:
            code = (hits[0].get("Code") or "").upper()
            if code:
                cache[isin] = code
                time.sleep(EODHD_DELAY)
                return code
    except Exception:
        pass
    cache[isin] = None
    time.sleep(EODHD_DELAY)
    return None


def _load_sp500_tickers(n: int = BREADTH_SAMPLE) -> list[str]:
    rows = []
    if not UNIVERSE_CSV.is_file():
        print(f"  ⚠ Universe-CSV fehlt: {UNIVERSE_CSV}")
        return []
    with UNIVERSE_CSV.open(encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if i == 0 and line.upper().startswith("ISIN"):
            continue
        parts = line.split(";", 1)
        if parts and parts[0].strip():
            rows.append(parts[0].strip().upper())
    rows = rows[:n]
    isin_map: dict = {}
    if ISIN_MAP_FILE.exists():
        try:
            isin_map = pickle.loads(ISIN_MAP_FILE.read_bytes())
        except Exception:
            pass
    tickers = []
    for j, isin in enumerate(rows, 1):
        tk = resolve_isin(isin, isin_map)
        if tk:
            tickers.append(tk)
        if j % 25 == 0:
            print(f"     ISIN→Ticker [{j}/{len(rows)}] · {len(tickers)} OK", flush=True)
    try:
        ISIN_MAP_FILE.write_bytes(pickle.dumps(isin_map, protocol=4))
    except Exception:
        pass
    return tickers


def _load_breadth_panel(force: bool = False) -> pd.DataFrame:
    if not force and BREADTH_CACHE.exists():
        try:
            df = pickle.loads(BREADTH_CACHE.read_bytes())
            if isinstance(df, pd.DataFrame) and len(df.columns) >= 20:
                print(f"  📂 Breadth-Panel aus Cache ({len(df.columns)} Titel)")
                return df
        except Exception:
            pass
    tickers = _load_sp500_tickers()
    if not tickers:
        return pd.DataFrame()
    print(f"  ⏳ Lade {len(tickers)} S&P-Titel für Breadth (einmalig, ~{len(tickers)//4} Min.)…")
    series = {}
    for i, tk in enumerate(tickers, 1):
        sym = f"{tk}.US" if "." not in tk else tk
        s = fetch_eod(sym)
        if len(s) > BREADTH_MA + 10:
            series[tk] = s
        if i % 10 == 0:
            print(f"     Kurse [{i}/{len(tickers)}] · {len(series)} OK", flush=True)
        time.sleep(EODHD_DELAY)
    df = pd.DataFrame(series).sort_index()
    try:
        BREADTH_CACHE.write_bytes(pickle.dumps(df, protocol=4))
        print(f"  💾 Breadth-Cache: {BREADTH_CACHE}")
    except Exception:
        pass
    return df


def build_market_panel(force: bool = False) -> dict:
    if not force and MARKET_CACHE.exists():
        try:
            data = pickle.loads(MARKET_CACHE.read_bytes())
            if data.get("end") == END_DATE and data.get("spy") is not None:
                print(f"  📂 Markt-Panel aus Cache")
                return data
        except Exception:
            pass
    print("  ⏳ Markt-Daten laden (SPY, VIX, Zinsen, Breadth)…")
    spy = fetch_eod(MARKET_SYMS["SPY"])
    vix = fetch_eod(MARKET_SYMS["VIX"])
    tnx = fetch_eod(MARKET_SYMS["TNX"])
    irx = fetch_eod(MARKET_SYMS["IRX"])
    breadth_panel = _load_breadth_panel(force=force)
    breadth = _compute_breadth_series(breadth_panel)
    data = {
        "end": END_DATE,
        "spy": spy,
        "vix": vix,
        "tnx": tnx,
        "irx": irx,
        "breadth": breadth,
    }
    try:
        MARKET_CACHE.write_bytes(pickle.dumps(data, protocol=4))
    except Exception:
        pass
    return data


def _compute_breadth_series(panel: pd.DataFrame) -> pd.Series:
    if panel.empty:
        return pd.Series(dtype=float)
    above = pd.DataFrame(index=panel.index)
    for col in panel.columns:
        ma = panel[col].rolling(BREADTH_MA, min_periods=BREADTH_MA).mean()
        above[col] = (panel[col] > ma).astype(float)
    return above.mean(axis=1)


def build_component_frame(market: dict) -> pd.DataFrame:
    spy = market["spy"]
    vix = market["vix"]
    tnx = market["tnx"]
    irx = market["irx"]
    breadth = market["breadth"]
    idx = spy.index.intersection(vix.index)
    df = pd.DataFrame(index=idx)
    ema200 = spy.rolling(EMA_SPAN, min_periods=EMA_SPAN).mean()
    sma21 = spy.rolling(SMA_SPAN, min_periods=SMA_SPAN).mean()
    spread = tnx.reindex(idx).ffill() - irx.reindex(idx).ffill()
    br = breadth.reindex(idx).ffill()
    df["C1_SPY_EMA200"] = (spy.reindex(idx) > ema200.reindex(idx)).astype(int)
    df["C2_VIX_30"] = (vix.reindex(idx) < VIX_THRESHOLD).astype(int)
    df["C3_YIELD_SPREAD"] = (spread > 0).astype(int)
    df["C4_SPY_SMA21"] = (spy.reindex(idx) > sma21.reindex(idx)).astype(int)
    df["C5_BREADTH_50"] = (br >= BREADTH_THRESHOLD).astype(int)
    return df.dropna(how="all")


def score_row(row: pd.Series, active: list[str]) -> float:
    if not active:
        return 100.0
    vals = [int(row[c]) for c in active if c in row.index]
    return float(sum(vals) / len(vals) * 100) if vals else 0.0


def invest_pct_from_score(score: float, mode: str = "single") -> float:
    """single: Stufe 1 (100 oder 50). combo: Stufe 2 (100/75/50)."""
    if mode == "single":
        return 1.0 if score >= 100 else 0.5
    if score >= 100:
        return 1.0
    if score >= 50:
        return 0.75
    return 0.5


def invest_series(components: pd.DataFrame, active: list[str], mode: str = "combo") -> pd.Series:
    scores = components[active].apply(lambda r: score_row(r, active), axis=1)
    return scores.apply(lambda s: invest_pct_from_score(s, mode))


def invest_series_tuned(
    components: pd.DataFrame,
    active: list[str],
    *,
    score_green: float = 100,
    score_yellow: float = 50,
    q_green: float = 1.0,
    q_yellow: float = 0.75,
    q_red: float = 0.5,
    smooth: int = 0,
) -> pd.Series:
    scores = components[active].apply(lambda r: score_row(r, active), axis=1)
    if smooth > 0:
        scores = scores.rolling(smooth, min_periods=1).mean()

    def _pct(s: float) -> float:
        if s >= score_green:
            return q_green
        if s >= score_yellow:
            return q_yellow
        return q_red

    return scores.apply(_pct)


def _load_winner_components() -> list[str]:
    for name in ("regime_final.json", "regime_winner.json"):
        p = CACHE_DIR / name
        if p.is_file():
            w = json.loads(p.read_text(encoding="utf-8"))
            comp = w.get("components")
            if comp:
                return comp
    return ["C5_BREADTH_50", "C1_SPY_EMA200"]


def _load_final_config() -> dict:
    for name in ("regime_final.json", "regime_winner.json"):
        p = CACHE_DIR / name
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    return {
        "components": ["C5_BREADTH_50", "C1_SPY_EMA200"],
        "score_green": 100,
        "score_yellow": 50,
        "q_green": 1.0,
        "q_yellow": 0.75,
        "q_red": 0.5,
        "smooth": 0,
        "crash_overlay": [],
        "crash_cap": 0.5,
    }


def build_crash_flags(market: dict, comp: pd.DataFrame, triggers: list[str]) -> pd.Series:
    """Tägliche Crash-Flags für Overlay (True = Risiko hoch)."""
    spy = market["spy"]
    vix = market["vix"]
    breadth = market["breadth"]
    idx = comp.index
    flags = pd.Series(False, index=idx)
    spy_r = spy.reindex(idx).ffill().pct_change()
    sma21 = spy.reindex(idx).ffill().rolling(SMA_SPAN, min_periods=SMA_SPAN).mean()
    spy_px = spy.reindex(idx).ffill()
    if "spy_crash" in triggers:
        flags |= spy_r <= -0.03
    if "vix_spike" in triggers:
        flags |= vix.reindex(idx).ffill() > VIX_THRESHOLD
    if "breadth_collapse" in triggers:
        flags |= breadth.reindex(idx).ffill() < 0.30
    if "spy_breakdown" in triggers:
        low20 = spy_px.rolling(20, min_periods=20).min()
        flags |= (spy_px < sma21) & (spy_px <= low20)
    return flags.fillna(False)


def apply_crash_overlay(
    invest_pct: pd.Series,
    market: dict,
    comp: pd.DataFrame,
    triggers: list[str],
    cap: float = 0.5,
) -> pd.Series:
    if not triggers:
        return invest_pct
    flags = build_crash_flags(market, comp, triggers)
    out = invest_pct.reindex(flags.index).ffill().copy()
    out[flags] = np.minimum(out[flags], cap)
    return out


def run_regime_backtest(
    index_prices: pd.Series,
    invest_pct: pd.Series,
    initial_capital: float = INITIAL_CAPITAL,
    tc: float = TRANSACTION_COST,
) -> pd.Series:
    idx = index_prices.index.intersection(invest_pct.index)
    prices = index_prices.reindex(idx).ffill()
    w = invest_pct.reindex(idx).ffill().clip(0, 1)
    ret = prices.pct_change().fillna(0)
    port_ret = w.shift(1).fillna(1.0) * ret
    turnover = w.diff().abs().fillna(0)
    port_ret -= turnover.shift(1).fillna(0) * tc
    equity = (1 + port_ret).cumprod() * initial_capital
    equity.name = "equity"
    return equity


def run_buy_hold(index_prices: pd.Series, initial_capital: float = INITIAL_CAPITAL) -> pd.Series:
    ret = index_prices.pct_change().fillna(0)
    return (1 + ret).cumprod() * initial_capital


def compute_metrics(equity: pd.Series) -> dict:
    if equity is None or len(equity) < 2:
        return {}
    eq = equity.dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    dd = eq / eq.cummax() - 1
    maxdd = float(dd.min())
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    mar = cagr / abs(maxdd) if maxdd < 0 else 0.0
    return {
        "cagr": round(cagr, 4),
        "maxdd": round(maxdd, 4),
        "sharpe": round(sharpe, 3),
        "mar": round(mar, 3),
        "end_value": round(float(eq.iloc[-1]), 0),
    }


def _load_benchmark_prices() -> dict[str, pd.Series]:
    out = {}
    for key, meta in BENCHMARKS.items():
        s = fetch_eod(meta["ticker"])
        if len(s) > 100:
            out[key] = s
        time.sleep(EODHD_DELAY)
    return out


def _print_table(df: pd.DataFrame, title: str):
    print("\n" + "═" * 88)
    print(f"  {title}")
    print("═" * 88)
    if df.empty:
        print("  ⚠ Keine Ergebnisse.")
        return
    cols = [c for c in df.columns if c in (
        "label", "benchmark", "cagr", "maxdd", "mar", "sharpe", "end_value",
        "bh_cagr", "bh_mar", "delta_mar", "active",
    )]
    print(df[cols].to_string(index=False))
    print("═" * 88)


def run_stage_0() -> pd.DataFrame:
    """Baseline: Buy & Hold je Benchmark."""
    print("═" * 70)
    print("  STUFE 0 — Buy & Hold Baseline")
    print("═" * 70)
    prices = _load_benchmark_prices()
    rows = []
    for key, s in prices.items():
        m = compute_metrics(run_buy_hold(s))
        rows.append({
            "benchmark": key,
            "label": BENCHMARKS[key]["label"],
            "active": "B&H 100%",
            **m,
        })
    df = pd.DataFrame(rows)
    df.to_csv(CACHE_DIR / "stage0_baseline.csv", index=False)
    _print_table(df, "STUFE 0 — Buy & Hold")
    return df


def run_stage_1(primary: str = "SPY") -> pd.DataFrame:
    """Einzelkomponenten vs. SPY (primary)."""
    print("═" * 70)
    print("  STUFE 1 — Einzelkomponenten (Top-3 nach MAR vs. B&H)")
    print("═" * 70)
    market = build_market_panel()
    comp = build_component_frame(market)
    prices = _load_benchmark_prices()
    if primary not in prices:
        raise RuntimeError(f"Benchmark {primary} fehlt")
    idx_s = prices[primary]
    bh = compute_metrics(run_buy_hold(idx_s))
    rows = []
    for cid, label in COMPONENTS.items():
        inv = invest_series(comp, [cid], mode="single")
        eq = run_regime_backtest(idx_s, inv)
        m = compute_metrics(eq)
        rows.append({
            "component": cid,
            "label": label,
            "active": cid,
            "benchmark": primary,
            "bh_cagr": bh["cagr"],
            "bh_mar": bh["mar"],
            "delta_mar": round(m["mar"] - bh["mar"], 3),
            **m,
        })
    df = pd.DataFrame(rows).sort_values(["delta_mar", "mar"], ascending=False)
    top3 = df.head(3)["component"].tolist()
    meta = {"top3": top3, "primary": primary, "bh": bh}
    (CACHE_DIR / "stage1_top3.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    df.to_csv(CACHE_DIR / "stage1_components.csv", index=False)
    _print_table(df, f"STUFE 1 — Einzelkomponenten ({BENCHMARKS[primary]['label']})")
    print(f"\n  🏆 Top-3 für Stufe 2: {', '.join(top3)}")
    return df


def run_stage_2(primary: str = "SPY") -> pd.DataFrame:
    """Kombinationen aus Top-3 der Stufe 1."""
    print("═" * 70)
    print("  STUFE 2 — Kombinationen (Top-3 aus Stufe 1)")
    print("═" * 70)
    meta_path = CACHE_DIR / "stage1_top3.json"
    if not meta_path.is_file():
        print("  ⚠ Stufe 1 fehlt — starte run_stage_1() …")
        run_stage_1(primary)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    top3 = meta["top3"]
    market = build_market_panel()
    comp = build_component_frame(market)
    prices = _load_benchmark_prices()
    idx_s = prices[primary]
    bh = compute_metrics(run_buy_hold(idx_s))
    combos = []
    for r in (2, 3):
        if r <= len(top3):
            combos.extend(list(itertools.combinations(top3, r)))
    rows = []
    for combo in combos:
        active = list(combo)
        inv = invest_series(comp, active, mode="combo")
        eq = run_regime_backtest(idx_s, inv)
        m = compute_metrics(eq)
        names = " + ".join(active)
        rows.append({
            "active": names,
            "components": active,
            "n_comp": len(active),
            "benchmark": primary,
            "bh_mar": bh["mar"],
            "delta_mar": round(m["mar"] - bh["mar"], 3),
            **m,
        })
    df = pd.DataFrame(rows).sort_values(["delta_mar", "mar"], ascending=False)
    if df.empty:
        return df
    winner = df.iloc[0].to_dict()
    winner["quotes"] = {"100": 1.0, "50": 0.75, "0": 0.5}
    winner["mode"] = "combo"
    (CACHE_DIR / "regime_winner.json").write_text(
        json.dumps(winner, indent=2, default=str), encoding="utf-8",
    )
    df.to_csv(CACHE_DIR / "stage2_combos.csv", index=False)
    _print_table(df, f"STUFE 2 — Kombinationen ({BENCHMARKS[primary]['label']})")
    print(f"\n  🏆 Gewinner: {winner['active']}  MAR {winner['mar']:.2f} (Δ {winner['delta_mar']:+.2f} vs B&H)")
    return df


QUOTE_GRIDS = [
    {"name": "100/75/50", "q_green": 1.0, "q_yellow": 0.75, "q_red": 0.5},
    {"name": "100/50/25", "q_green": 1.0, "q_yellow": 0.50, "q_red": 0.25},
    {"name": "100/50/0", "q_green": 1.0, "q_yellow": 0.50, "q_red": 0.0},
]

SMOOTH_GRID = [0, 3, 5]

CRASH_SETS = [
    {"name": "kein Overlay", "triggers": [], "cap": 0.5},
    {"name": "SPY −3%", "triggers": ["spy_crash"], "cap": 0.5},
    {"name": "VIX >30", "triggers": ["vix_spike"], "cap": 0.5},
    {"name": "Breadth <30%", "triggers": ["breadth_collapse"], "cap": 0.5},
    {"name": "SPY Breakdown", "triggers": ["spy_breakdown"], "cap": 0.5},
    {"name": "SPY+VIX", "triggers": ["spy_crash", "vix_spike"], "cap": 0.5},
    {"name": "Alle 4", "triggers": ["spy_crash", "vix_spike", "breadth_collapse", "spy_breakdown"], "cap": 0.5},
    {"name": "Alle 4 (Cap 25%)", "triggers": ["spy_crash", "vix_spike", "breadth_collapse", "spy_breakdown"], "cap": 0.25},
]


def run_stage_3(primary: str = "SPY") -> pd.DataFrame:
    """Schwellen, Quoten & Glättung auf Gewinner-Kombi aus Stufe 2."""
    print("═" * 70)
    print("  STUFE 3 — Quoten & Glättung (Gewinner aus Stufe 2)")
    print("═" * 70)
    winner_path = CACHE_DIR / "regime_winner.json"
    if not winner_path.is_file():
        print("  ⚠ Stufe 2 fehlt — starte run_stage_2() …")
        run_stage_2(primary)
    active = _load_winner_components()
    market = build_market_panel()
    comp = build_component_frame(market)
    prices = _load_benchmark_prices()
    idx_s = prices[primary]
    bh = compute_metrics(run_buy_hold(idx_s))
    rows = []
    for qg in QUOTE_GRIDS:
        for smooth in SMOOTH_GRID:
            inv = invest_series_tuned(
                comp, active,
                q_green=qg["q_green"], q_yellow=qg["q_yellow"], q_red=qg["q_red"],
                smooth=smooth,
            )
            eq = run_regime_backtest(idx_s, inv)
            m = compute_metrics(eq)
            rows.append({
                "quotes": qg["name"],
                "smooth": smooth,
                "active": " + ".join(active),
                "benchmark": primary,
                "bh_mar": bh["mar"],
                "delta_mar": round(m["mar"] - bh["mar"], 3),
                **m,
            })
    df = pd.DataFrame(rows).sort_values(["delta_mar", "mar"], ascending=False)
    if df.empty:
        return df
    best = df.iloc[0]
    tuned = {
        "components": active,
        "active": best["active"],
        "quotes": best["quotes"],
        "q_green": next(q["q_green"] for q in QUOTE_GRIDS if q["name"] == best["quotes"]),
        "q_yellow": next(q["q_yellow"] for q in QUOTE_GRIDS if q["name"] == best["quotes"]),
        "q_red": next(q["q_red"] for q in QUOTE_GRIDS if q["name"] == best["quotes"]),
        "smooth": int(best["smooth"]),
        "score_green": 100,
        "score_yellow": 50,
        "mar": float(best["mar"]),
        "delta_mar": float(best["delta_mar"]),
    }
    (CACHE_DIR / "regime_tuned.json").write_text(json.dumps(tuned, indent=2), encoding="utf-8")
    df.to_csv(CACHE_DIR / "stage3_quotes.csv", index=False)
    _print_table(df, f"STUFE 3 — Quoten & Glättung ({BENCHMARKS[primary]['label']})")
    print(f"\n  🏆 Beste Quote: {best['quotes']}  Glättung {int(best['smooth'])}d  MAR {best['mar']:.2f}")
    return df


def run_stage_4(primary: str = "SPY") -> pd.DataFrame:
    """Crash-Overlay auf Stufe-3-Konfiguration."""
    print("═" * 70)
    print("  STUFE 4 — Crash-Overlay")
    print("═" * 70)
    tuned_path = CACHE_DIR / "regime_tuned.json"
    if not tuned_path.is_file():
        print("  ⚠ Stufe 3 fehlt — starte run_stage_3() …")
        run_stage_3(primary)
    tuned = json.loads(tuned_path.read_text(encoding="utf-8"))
    active = tuned["components"]
    market = build_market_panel()
    comp = build_component_frame(market)
    prices = _load_benchmark_prices()
    idx_s = prices[primary]
    bh = compute_metrics(run_buy_hold(idx_s))
    base_inv = invest_series_tuned(
        comp, active,
        q_green=tuned["q_green"], q_yellow=tuned["q_yellow"], q_red=tuned["q_red"],
        smooth=tuned.get("smooth", 0),
    )
    rows = []
    for cs in CRASH_SETS:
        inv = apply_crash_overlay(base_inv, market, comp, cs["triggers"], cap=cs["cap"])
        eq = run_regime_backtest(idx_s, inv)
        m = compute_metrics(eq)
        rows.append({
            "overlay": cs["name"],
            "triggers": ",".join(cs["triggers"]) or "—",
            "cap": cs["cap"],
            "benchmark": primary,
            "bh_mar": bh["mar"],
            "delta_mar": round(m["mar"] - bh["mar"], 3),
            **m,
        })
    df = pd.DataFrame(rows).sort_values(["delta_mar", "mar"], ascending=False)
    if df.empty:
        return df
    best = df.iloc[0]
    cs_match = next(c for c in CRASH_SETS if c["name"] == best["overlay"])
    final = {**tuned, **{
        "crash_overlay": cs_match["triggers"],
        "crash_cap": cs_match["cap"],
        "overlay_name": best["overlay"],
        "mar": float(best["mar"]),
        "delta_mar": float(best["delta_mar"]),
    }}
    (CACHE_DIR / "regime_final.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
    df.to_csv(CACHE_DIR / "stage4_crash.csv", index=False)
    _print_table(df, f"STUFE 4 — Crash-Overlay ({BENCHMARKS[primary]['label']})")
    print(f"\n  🏆 Overlay: {best['overlay']}  MAR {best['mar']:.2f}")
    return df


def run_stage_5() -> pd.DataFrame:
    """Multi-Index-Validierung mit gesperrter Final-Konfiguration."""
    print("═" * 70)
    print("  STUFE 5 — Multi-Index-Validierung")
    print("═" * 70)
    final_path = CACHE_DIR / "regime_final.json"
    if not final_path.is_file():
        print("  ⚠ Stufe 4 fehlt — starte run_stage_4() …")
        run_stage_4()
    cfg = json.loads(final_path.read_text(encoding="utf-8"))
    active = cfg["components"]
    market = build_market_panel()
    comp = build_component_frame(market)
    base_inv = invest_series_tuned(
        comp, active,
        q_green=cfg["q_green"], q_yellow=cfg["q_yellow"], q_red=cfg["q_red"],
        smooth=cfg.get("smooth", 0),
    )
    inv = apply_crash_overlay(
        base_inv, market, comp,
        cfg.get("crash_overlay", []),
        cap=cfg.get("crash_cap", 0.5),
    )
    prices = _load_benchmark_prices()
    rows = []
    for key, s in prices.items():
        bh_m = compute_metrics(run_buy_hold(s))
        eq = run_regime_backtest(s, inv)
        m = compute_metrics(eq)
        rows.append({
            "benchmark": key,
            "label": BENCHMARKS[key]["label"],
            "active": cfg.get("active", " + ".join(active)),
            "quotes": cfg.get("quotes", ""),
            "overlay": cfg.get("overlay_name", "—"),
            "bh_cagr": bh_m["cagr"],
            "bh_mar": bh_m["mar"],
            "delta_mar": round(m["mar"] - bh_m["mar"], 3),
            **m,
        })
    df = pd.DataFrame(rows).sort_values(["delta_mar", "mar"], ascending=False)
    df.to_csv(CACHE_DIR / "stage5_multindex.csv", index=False)
    (CACHE_DIR / "regime_final.json").write_text(
        json.dumps({**cfg, "validated": df.to_dict(orient="records")}, indent=2, default=str),
        encoding="utf-8",
    )
    _print_table(df, "STUFE 5 — Multi-Index (gesperrte Parameter)")
    wins = (df["delta_mar"] > 0).sum()
    print(f"\n  ✅ Regime schlägt B&H bei {wins}/{len(df)} Indizes (nach MAR)")
    return df


def _signal_label(score: float, cfg: dict | None = None) -> tuple[str, float]:
    cfg = cfg or {}
    green = cfg.get("score_green", 100)
    yellow = cfg.get("score_yellow", 50)
    qg = cfg.get("q_green", 1.0)
    qy = cfg.get("q_yellow", 0.75)
    qr = cfg.get("q_red", 0.5)
    if score >= green:
        return "GRÜN", qg
    if score >= yellow:
        return "GELB", qy
    return "ROT", qr


def _live_invest_series(market: dict, comp: pd.DataFrame, cfg: dict) -> tuple[float, bool]:
    active = cfg.get("components") or _load_winner_components()
    inv = invest_series_tuned(
        comp, active,
        q_green=cfg.get("q_green", 1.0),
        q_yellow=cfg.get("q_yellow", 0.75),
        q_red=cfg.get("q_red", 0.5),
        smooth=cfg.get("smooth", 0),
    )
    inv = apply_crash_overlay(
        inv, market, comp,
        cfg.get("crash_overlay", []),
        cap=cfg.get("crash_cap", 0.5),
    )
    pct = float(inv.iloc[-1])
    crash_today = bool(
        build_crash_flags(market, comp, cfg.get("crash_overlay", [])).iloc[-1]
    ) if cfg.get("crash_overlay") else False
    return pct, crash_today


def live_signal() -> dict:
    """Heutiges Regime-Signal (regime_final.json → regime_tuned → regime_winner)."""
    _require_api()
    cfg = _load_final_config()
    active = cfg.get("components") or _load_winner_components()
    market = build_market_panel()
    comp = build_component_frame(market)
    if comp.empty:
        raise RuntimeError("Keine Komponenten-Daten")
    row = comp.iloc[-1]
    score = score_row(row, active)
    signal, _ = _signal_label(score, cfg)
    invest_pct, crash_today = _live_invest_series(market, comp, cfg)
    if crash_today and cfg.get("crash_overlay"):
        signal = f"{signal} + CRASH"
    dt = comp.index[-1]
    comp_detail = {COMPONENTS.get(c, c): bool(int(row[c])) for c in active if c in row.index}
    out = {
        "datum": dt.strftime("%Y-%m-%d"),
        "score": int(score),
        "signal": signal,
        "invest_pct": round(invest_pct, 4),
        "crash_overlay_active": crash_today,
        "active_components": active,
        "quotes": cfg.get("quotes", "100/75/50"),
        "overlay": cfg.get("overlay_name", "—"),
        "components": comp_detail,
    }
    print("═" * 60)
    print(f"  KASSANDRA REGIME  —  {out['datum']}")
    print("═" * 60)
    print(f"  Score:     {out['score']}/100  →  {signal}  →  {int(invest_pct*100)}% investieren")
    print(f"  Quoten:    {out['quotes']}  |  Overlay: {out['overlay']}")
    if crash_today:
        print(f"  ⚠ Crash-Overlay aktiv heute (Cap {int(cfg.get('crash_cap', 0.5)*100)}%)")
    print(f"  Aktiv:     {', '.join(active)}")
    print()
    for name, ok in comp_detail.items():
        print(f"    {'✅' if ok else '❌'}  {name}")
    print("═" * 60)
    payload = json.dumps(out, indent=2, ensure_ascii=False)
    for p in (
        CACHE_DIR / "kassandra_regime_live.json",
        NOTEBOOK_DIR / "kassandra_regime_live.json",
        NOTEBOOK_DIR / "trading-dashboard" / "kassandra_regime_live.json",
    ):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(payload, encoding="utf-8")
        except Exception:
            pass
    return out


def run_all_stages(primary: str = "SPY", through: int = 5):
    run_stage_0()
    run_stage_1(primary)
    run_stage_2(primary)
    if through >= 3:
        run_stage_3(primary)
    if through >= 4:
        run_stage_4(primary)
    if through >= 5:
        return run_stage_5()
    return None
