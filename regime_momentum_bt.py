"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  REGIME MOMENTUM — Einzelaktien + Kassandra Regime v5 (Stufe 1)              ║
║  Universe: S&P 500 (ISIN-CSV) · EODHD · wöchentlich Freitag · Faktor 1.0     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import pickle
import time
import warnings
from datetime import date as dt_date, datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

BT_VERSION = 10

DRIVE_CANDIDATES = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
]

UNIVERSE_PROFILES = {
    "sp500": {
        "label": "S&P 500",
        "rel": Path("Meine Aktienlisten") / "S&P 500 nach Marktkapitalisierung.csv",
        "bundled": Path("trading-dashboard") / "data" / "sp500_universe_by_mcap.csv",
        "github": (
            "https://raw.githubusercontent.com/lazarkitanov-cell/trading-dashboard/main/"
            "data/sp500_universe_by_mcap.csv"
        ),
        "index_sym": None,
        "cache_dir": "regime_momentum_cache",
        "quick_n": 80,
    },
    "r1000": {
        "label": "Russell 1000",
        "rel": Path("Meine Aktienlisten") / "Russell 1000 nach Marktkapitalisierung.csv",
        "bundled": Path("trading-dashboard") / "data" / "r1000_universe.csv",
        "github": None,
        "github_urls": [
            "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/all.csv",
            "https://raw.githubusercontent.com/lazarkitanov-cell/trading-dashboard/main/data/r1000_universe.csv",
        ],
        "index_syms": ["RUI.INDX", "RUI", "IWB.US"],
        "ishares_url": (
            "https://www.ishares.com/us/products/239710/ishares-russell-1000-etf/"
            "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
        ),
        "cache_dir": "regime_momentum_cache_r1000",
        "quick_n": 120,
    },
}

UNIVERSE_KEY = "sp500"
SP500_CACHE_NAME = "regime_momentum_cache"


def _resolve_notebook_dir() -> Path:
    try:
        script_dir = Path(__file__).resolve().parent
        if (script_dir / "regime_cache").is_dir():
            return script_dir
        for key in UNIVERSE_PROFILES:
            if (script_dir / UNIVERSE_PROFILES[key]["rel"]).exists():
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
        if (p / "regime_cache").is_dir():
            return p
        for key in UNIVERSE_PROFILES:
            if (p / UNIVERSE_PROFILES[key]["rel"]).exists():
                return p
    local = Path(r"h:\Meine Ablage\Colab Notebooks")
    if (local / "regime_cache").is_dir():
        return local
    return Path(".").resolve()


def _active_profile() -> dict:
    return UNIVERSE_PROFILES[UNIVERSE_KEY]


def _sp500_cache_dir() -> Path:
    return NOTEBOOK_DIR / SP500_CACHE_NAME


def _rebind_cache_paths() -> None:
    """Cache-Pfade nach Universe-Wechsel aktualisieren."""
    global CACHE_DIR, ISIN_MAP_FILE, PRICES_CACHE, PRICES_CACHE_QUICK
    global RESULT_CSV, RESULT_STUFE2_CSV, STUFE2_WINNER_JSON, RESULT_STUFE3_CSV
    global STUFE3_WINNER_JSON, RESULT_STUFE4_CSV, STUFE4_WINNER_JSON, RESULT_R1000_CSV

    prof = _active_profile()
    CACHE_DIR = NOTEBOOK_DIR / prof["cache_dir"]
    ISIN_MAP_FILE = CACHE_DIR / "isin_to_ticker.pkl"
    PRICES_CACHE = CACHE_DIR / "prices_panel.pkl"
    PRICES_CACHE_QUICK = CACHE_DIR / "prices_panel_quick.pkl"
    RESULT_CSV = CACHE_DIR / "stufe1_results.csv"
    RESULT_STUFE2_CSV = CACHE_DIR / "stufe2_grid_results.csv"
    STUFE2_WINNER_JSON = CACHE_DIR / "stufe2_winner.json"
    RESULT_STUFE3_CSV = CACHE_DIR / "stufe3_wf_results.csv"
    STUFE3_WINNER_JSON = CACHE_DIR / "stufe3_winner.json"
    RESULT_STUFE4_CSV = CACHE_DIR / "stufe4_factor_results.csv"
    STUFE4_WINNER_JSON = CACHE_DIR / "stufe4_winner.json"
    RESULT_R1000_CSV = CACHE_DIR / "r1000_robustness.csv"


def set_universe(key: str = "sp500") -> str:
    """Universum umschalten: 'sp500' | 'r1000'."""
    global UNIVERSE_KEY, UNIVERSE_CSV, _UNIVERSE_RESOLVED, _DATA_CACHE
    if key not in UNIVERSE_PROFILES:
        raise ValueError(f"Unbekanntes Universum: {key!r} — erlaubt: {list(UNIVERSE_PROFILES)}")
    UNIVERSE_KEY = key
    _UNIVERSE_RESOLVED = None
    _DATA_CACHE = None
    UNIVERSE_CSV = NOTEBOOK_DIR / _active_profile()["rel"]
    _rebind_cache_paths()
    return key


def _find_universe_on_drive(filename: str) -> Path | None:
    root = Path("/content/drive/MyDrive")
    if not root.is_dir():
        return None
    for hit in root.rglob(filename):
        try:
            if hit.is_file() and hit.stat().st_size > 500:
                return hit
        except OSError:
            continue
    return None


def _write_ticker_universe(dest: Path, pairs: list[tuple[str, str]], min_n: int = 400) -> Path:
    if len(pairs) < min_n:
        raise RuntimeError(f"Zu wenige Titel ({len(pairs)} < {min_n})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = ["TICKER;NAME"] + [f"{tk};{nm.replace(';', ' ')}" for tk, nm in pairs]
    dest.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓ {len(pairs)} Titel → {dest.name}")
    return dest


def _fetch_eodhd_index_components(symbols: list[str], dest: Path) -> Path:
    """EODHD Fundamentals Components (benötigt ggf. höheres Abo)."""
    _require_token()
    last_err = None
    for index_sym in symbols:
        print(f"  📡 EODHD Index {index_sym} …")
        try:
            r = requests.get(
                f"https://eodhd.com/api/fundamentals/{index_sym}",
                params={"api_token": EODHD_TOKEN, "fmt": "json"},
                timeout=90,
            )
            if r.status_code == 403:
                last_err = f"{index_sym}: HTTP 403 (Fundamentals/Index evtl. nicht im Abo)"
                continue
            if r.status_code != 200:
                last_err = f"{index_sym}: HTTP {r.status_code}"
                continue
            data = r.json()
            components = data.get("Components") or data.get("Holdings") or {}
            pairs = []
            for comp in components.values():
                if not isinstance(comp, dict):
                    continue
                code = (comp.get("Code") or comp.get("code") or "").strip().upper()
                name = (comp.get("Name") or comp.get("name") or "").replace(";", " ")
                ex = (comp.get("Exchange") or comp.get("exchange") or "US").upper()
                if code and ex in ("US", "NYSE", "NASDAQ", "BATS", "AMEX"):
                    pairs.append((code, name))
            if len(pairs) >= 400:
                return _write_ticker_universe(dest, pairs)
            last_err = f"{index_sym}: nur {len(pairs)} Titel"
        except Exception as e:
            last_err = f"{index_sym}: {e}"
    raise RuntimeError(last_err or "EODHD Index fehlgeschlagen")


def _fetch_ishares_iwb(url: str, dest: Path) -> Path:
    """iShares Russell 1000 ETF (IWB) Holdings-CSV — öffentlich, kein EODHD-Abo."""
    print("  📡 iShares IWB Holdings …")
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KassandraResearch/1.0)"},
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    pairs = []
    started = False
    for line in r.text.splitlines():
        row = [c.strip().strip('"') for c in line.split(",")]
        if not row:
            continue
        head = ",".join(row).upper()
        if not started and "TICKER" in head:
            started = True
            continue
        if not started:
            continue
        ticker = row[0].upper() if row else ""
        name = row[1] if len(row) > 1 else ""
        if ticker and ticker.replace(".", "").isalnum() and len(ticker) <= 6:
            pairs.append((ticker, name))
    if len(pairs) < 400:
        raise RuntimeError(f"Zu wenige IWB-Holdings ({len(pairs)})")
    return _write_ticker_universe(dest, pairs)


def _fetch_github_universe_list(url: str, dest: Path, top_n: int = 1000) -> Path:
    """GitHub-CSV: Ate329 all.csv (NASDAQ, nach Marktkapitalisierung) oder TICKER;NAME."""
    print(f"  ⬇️  GitHub Universe …")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    text = r.text
    if "ISIN;" in text[:200] or "TICKER;" in text[:200].upper():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        n = max(0, text.count("\n") - 1)
        print(f"  ✓ {n} Zeilen → {dest.name}")
        return dest
    df = pd.read_csv(StringIO(text))
    sym_col = next(
        (c for c in df.columns if str(c).lower() in ("symbol", "ticker", "code")),
        df.columns[0],
    )
    name_col = next((c for c in df.columns if "name" in str(c).lower()), None)
    mcap_col = next((c for c in df.columns if "market" in str(c).lower()), None)
    work = df.copy()
    if mcap_col is not None:
        work[mcap_col] = pd.to_numeric(
            work[mcap_col].astype(str).str.replace(r"[$,]", "", regex=True),
            errors="coerce",
        )
        work = work.sort_values(mcap_col, ascending=False)
    pairs = []
    for _, row in work.head(top_n).iterrows():
        tk = str(row[sym_col]).strip().upper()
        nm = str(row[name_col]).strip() if name_col else ""
        if tk and tk not in ("NAN", "NONE"):
            pairs.append((tk, nm))
    return _write_ticker_universe(dest, pairs, min_n=400)


def _build_r1000_universe(nb: Path, dest: Path) -> Path:
    """Russell 1000: EODHD → iShares IWB → GitHub → Fehler mit Hinweis."""
    prof = _active_profile()
    errors = []

    for label, fn in (
        ("EODHD Index/ETF", lambda: _fetch_eodhd_index_components(prof.get("index_syms", []), dest)),
        ("iShares IWB", lambda: _fetch_ishares_iwb(prof["ishares_url"], dest)),
    ):
        try:
            print(f"  → {label}")
            return fn()
        except Exception as e:
            errors.append(f"{label}: {e}")
            print(f"     ⚠ {e}")

    for url in prof.get("github_urls") or []:
        try:
            print(f"  → GitHub: {url.split('/')[-1]}")
            return _fetch_github_universe_list(url, dest)
        except Exception as e:
            errors.append(f"GitHub: {e}")
            print(f"     ⚠ {e}")

    raise FileNotFoundError(
        f"Russell-1000-Universum konnte nicht geladen werden.\n"
        f"  Versuche:\n"
        f"  1) CSV manuell: {dest}\n"
        f"  2) iShares IWB Holdings exportieren (TICKER;NAME)\n"
        f"  3) EODHD-Abo mit Index Components\n"
        f"  Fehler: {' | '.join(errors)}"
    )


def _ensure_universe_csv(nb: Path) -> Path:
    """Universe-CSV: Standardpfad, Drive-Suche, Bundled, GitHub oder EODHD-Index."""
    global _UNIVERSE_RESOLVED, UNIVERSE_CSV
    if _UNIVERSE_RESOLVED is not None and _UNIVERSE_RESOLVED.is_file():
        return _UNIVERSE_RESOLVED

    import shutil
    import urllib.request

    prof = _active_profile()
    primary = nb / prof["rel"]
    filename = prof["rel"].name

    if primary.is_file():
        _UNIVERSE_RESOLVED = primary
        UNIVERSE_CSV = primary
        return primary

    found = _find_universe_on_drive(filename)
    if found is not None and found.resolve() != primary.resolve():
        _UNIVERSE_RESOLVED = found
        UNIVERSE_CSV = found
        return found

    for src in (nb / prof["bundled"], nb / "data" / prof["bundled"].name):
        if src.is_file():
            primary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, primary)
            print(f"  📋 Universe kopiert nach {primary.relative_to(nb)}")
            _UNIVERSE_RESOLVED = primary
            UNIVERSE_CSV = primary
            return primary

    if prof.get("github"):
        try:
            data = urllib.request.urlopen(prof["github"], timeout=60).read()
            if len(data) > 500 and b"ISIN" in data:
                primary.parent.mkdir(parents=True, exist_ok=True)
                primary.write_bytes(data)
                print(f"  ⬇️  Universe von GitHub → {primary.relative_to(nb)}")
                _UNIVERSE_RESOLVED = primary
                UNIVERSE_CSV = primary
                return primary
        except Exception:
            pass

    if UNIVERSE_KEY == "r1000" and not primary.is_file():
        for src in (nb / prof["bundled"], nb / "data" / prof["bundled"].name):
            if src.is_file() and src.stat().st_size > 3000:
                primary.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, primary)
                print(f"  📋 Universe kopiert nach {primary.relative_to(nb)}")
                _UNIVERSE_RESOLVED = primary
                UNIVERSE_CSV = primary
                return primary

    if UNIVERSE_KEY == "r1000":
        built = _build_r1000_universe(nb, primary)
        _UNIVERSE_RESOLVED = built
        UNIVERSE_CSV = built
        return built

    raise FileNotFoundError(
        f"Universe-CSV fehlt: {primary}\n"
        f"  → {prof['label']}-Liste auf Drive syncen\n"
        f"  → oder trading-dashboard/data/ legen"
    )


NOTEBOOK_DIR = _resolve_notebook_dir()
UNIVERSE_CSV = NOTEBOOK_DIR / UNIVERSE_PROFILES["sp500"]["rel"]
CACHE_DIR = NOTEBOOK_DIR / SP500_CACHE_NAME
ISIN_MAP_FILE = CACHE_DIR / "isin_to_ticker.pkl"
PRICES_CACHE = CACHE_DIR / "prices_panel.pkl"
PRICES_CACHE_QUICK = CACHE_DIR / "prices_panel_quick.pkl"
RESULT_CSV = CACHE_DIR / "stufe1_results.csv"
RESULT_STUFE2_CSV = CACHE_DIR / "stufe2_grid_results.csv"
STUFE2_WINNER_JSON = CACHE_DIR / "stufe2_winner.json"
RESULT_STUFE3_CSV = CACHE_DIR / "stufe3_wf_results.csv"
STUFE3_WINNER_JSON = CACHE_DIR / "stufe3_winner.json"
RESULT_STUFE4_CSV = CACHE_DIR / "stufe4_factor_results.csv"
STUFE4_WINNER_JSON = CACHE_DIR / "stufe4_winner.json"
FINAL_PARAMS_JSON = NOTEBOOK_DIR / SP500_CACHE_NAME / "final_params.json"

_rebind_cache_paths()

STUFE4_FACTORS = [1.0, 1.25, 1.5, 2.0]

_UNIVERSE_RESOLVED: Path | None = None
_DATA_CACHE: dict | None = None

START_DATE = "2010-01-01"
END_DATE = dt_date.today().strftime("%Y-%m-%d")
INITIAL_CAPITAL = 100_000
EODHD_DELAY = 0.22
QUICK_MODE = False
QUICK_N = 80

# ── Stufe 1 Parameter ─────────────────────────────────────────────────────────
BASELINE_PARAMS = dict(
    top_n=15,
    exit_rank=20,
    low_vol_pct=0.30,
    vol_lookback=100,
    mom_6m=126,
    mom_12m=252,
    mom_w6=0.7,
    mom_w12=0.3,
    w_mom=0.40,
    w_rs=0.25,
    w_lv=0.20,
    w_trend=0.15,
    min_price=10.0,
    min_dollar_vol=5_000_000,
    dollar_vol_window=20,
    sma_trend=200,
    sma_fast=50,
    exit_sma=100,
    use_regime=True,
    transaction_cost=0.0,
    exposure_factor=1.0,
    margin_rate=0.045,
)

STUFE2_TC = 0.001
FINAL_PARAMS_DEFAULT = dict(
    top_n=20,
    exit_rank=25,
    low_vol_pct=0.25,
    use_regime=True,
    transaction_cost=STUFE2_TC,
    exposure_factor=1.0,
    margin_rate=0.045,
)

STUFE2_GRID = dict(
    top_n=[10, 15, 20],
    exit_rank=[15, 20, 25],
    low_vol_pct=[0.20, 0.25, 0.30, 0.35],
)

WF_TRAIN_YEARS = 3
WF_TEST_YEARS = 2
WF_OBJECTIVE = "mar"
PLATEAU_MAR_PCT = 0.05

try:
    from google.colab import userdata
    EODHD_TOKEN = userdata.get("EODHD_API_KEY") or userdata.get("EODHD_TOKEN") or ""
except ImportError:
    EODHD_TOKEN = os.environ.get("EODHD_API_KEY", "")

EODHD_BASE = "https://eodhd.com/api/eod"
MARKET_EXTRA = {"SPY": "SPY.US", "^VIX": "VIX.INDX"}


# ── Kassandra Regime (dynamischer Import) ─────────────────────────────────────

def _import_kassandra_regime():
    for rel in ("_kassandra_regime.py", Path("trading-dashboard") / "_kassandra_regime.py"):
        p = NOTEBOOK_DIR / rel
        if p.is_file() and "build_market_panel" in p.read_text(encoding="utf-8"):
            spec = importlib.util.spec_from_file_location("kassandra_regime_bt", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError(
        "_kassandra_regime.py fehlt — Kassandra_Regime.ipynb Zelle 1 oder Datei auf Drive."
    )


def build_kass_invest_pct(kr=None) -> pd.Series:
    """Tägliche Investitionsquote aus Kassandra Regime v5 (100/50/0)."""
    kr = kr or _import_kassandra_regime()
    cfg = kr._load_final_config()
    components = cfg.get("components") or ["C5_BREADTH_50", "C1_SPY_EMA200"]
    market = kr.build_market_panel()
    comp = kr.build_component_frame(market)
    inv = kr.invest_series_tuned(
        comp,
        list(components),
        score_green=float(cfg.get("score_green", 100)),
        score_yellow=float(cfg.get("score_yellow", 50)),
        q_green=float(cfg.get("q_green", 1.0)),
        q_yellow=float(cfg.get("q_yellow", 0.5)),
        q_red=float(cfg.get("q_red", 0.0)),
        smooth=int(cfg.get("smooth", 3)),
    )
    triggers = cfg.get("crash_overlay") or cfg.get("triggers") or []
    if triggers:
        cap = float(cfg.get("crash_cap", 0.5))
        inv = kr.apply_crash_overlay(inv, market, comp, list(triggers), cap)
    inv.name = "invest_pct"
    return inv.clip(0.0, 1.0)


def invest_pct_on_fridays(daily_inv: pd.Series, trading_idx: pd.DatetimeIndex) -> pd.Series:
    fri = trading_idx[trading_idx.weekday == 4]
    return daily_inv.reindex(fri).ffill().clip(0.0, 1.0)


# ── Universe & EODHD ───────────────────────────────────────────────────────────

def _require_token():
    if not EODHD_TOKEN:
        raise RuntimeError("EODHD_API_KEY fehlt (Colab Secrets).")


def _eodhd_sym(ticker: str) -> str:
    return MARKET_EXTRA.get(ticker, f"{ticker}.US")


def fetch_eod_panel(symbol: str, start: str = START_DATE, end: str = END_DATE) -> pd.DataFrame:
    _require_token()
    try:
        r = requests.get(
            f"{EODHD_BASE}/{symbol}",
            params={"api_token": EODHD_TOKEN, "fmt": "json", "from": start, "to": end},
            timeout=40,
        )
        if r.status_code != 200:
            return pd.DataFrame()
        rows = r.json()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        close = df["adjusted_close"] if "adjusted_close" in df.columns else df["close"]
        out = pd.DataFrame({
            "close": close.astype(float),
            "volume": df.get("volume", 0).astype(float),
        })
        return out
    except Exception:
        return pd.DataFrame()


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
        for h in r.json() or []:
            code = (h.get("Code") or "").upper()
            ex = (h.get("Exchange") or "").upper()
            if ex in ("US", "NYSE", "NASDAQ", "BATS", "AMEX") and code:
                cache[isin] = code
                time.sleep(EODHD_DELAY)
                return code
    except Exception:
        pass
    cache[isin] = None
    time.sleep(EODHD_DELAY)
    return None


def _prices_cache_path() -> Path:
    return PRICES_CACHE_QUICK if QUICK_MODE else PRICES_CACHE


def _is_isin(code: str) -> bool:
    return len(code) == 12 and code[:2].isalpha() and code[2:].isalnum()


def _resolve_ticker(code: str, isin_map: dict) -> str | None:
    code = code.strip().upper()
    if not code:
        return None
    if _is_isin(code):
        return resolve_isin(code, isin_map)
    return code


def load_universe() -> list[tuple[str, str]]:
    global UNIVERSE_CSV
    if not UNIVERSE_CSV.is_file():
        UNIVERSE_CSV = _ensure_universe_csv(NOTEBOOK_DIR)
    rows = []
    with UNIVERSE_CSV.open(encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    hdr = lines[0].upper() if lines else ""
    start = 1 if hdr.startswith("ISIN") or hdr.startswith("TICKER") else 0
    for line in lines[start:]:
        parts = line.split(";", 1)
        if parts and parts[0].strip():
            rows.append((parts[0].strip().upper(), parts[1].strip() if len(parts) > 1 else ""))
    if QUICK_MODE:
        qn = int(_active_profile().get("quick_n", QUICK_N))
        rows = rows[:qn]
        print(f"  ⚡ QUICK_MODE: {qn} Titel")
    isin_map: dict = {}
    if ISIN_MAP_FILE.is_file():
        try:
            isin_map = pickle.loads(ISIN_MAP_FILE.read_bytes())
        except Exception:
            pass
    out = []
    print(f"  📋 Universe ({_active_profile()['label']}): {len(rows)} Einträge")
    for j, (code, name) in enumerate(rows, 1):
        tk = _resolve_ticker(code, isin_map)
        if tk:
            out.append((tk, name))
        if j % 50 == 0:
            print(f"     Ticker [{j}/{len(rows)}] · {len(out)} OK", flush=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        ISIN_MAP_FILE.write_bytes(pickle.dumps(isin_map, protocol=4))
    except Exception:
        pass
    print(f"  ✓ {len(out)} US-Ticker")
    return out


def load_price_panels(tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """close + volume DataFrames (Spalten = Ticker)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _prices_cache_path()
    all_syms = sorted(set(tickers) | {"SPY"})
    if cache_path.is_file():
        try:
            cached = pickle.loads(cache_path.read_bytes())
            if cached.get("start") == START_DATE:
                close = cached["close"]
                vol = cached["volume"]
                cols = [c for c in all_syms if c in close.columns]
                need = max(1, int(len(tickers) * 0.8))
                if len(cols) >= need:
                    age_h = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
                    tag = "QUICK" if QUICK_MODE else "FULL"
                    print(f"  💾 Preis-Cache {tag} ({age_h:.1f}h) · {len(cols)} Titel")
                    return close[cols].copy(), vol[cols].copy()
        except Exception:
            pass

    tag = "QUICK" if QUICK_MODE else "FULL"
    print(f"  📡 EODHD {tag}: {len(all_syms)} Serien … (ca. {len(all_syms) * EODHD_DELAY / 60:.0f} Min)")
    closes, vols = {}, {}
    for i, tk in enumerate(all_syms, 1):
        panel = fetch_eod_panel(_eodhd_sym(tk))
        if not panel.empty:
            closes[tk] = panel["close"]
            vols[tk] = panel["volume"]
        if i % 25 == 0 or i == len(all_syms):
            print(f"     [{i}/{len(all_syms)}] · {len(closes)} OK", flush=True)
        time.sleep(EODHD_DELAY)

    close_df = pd.DataFrame(closes).sort_index().ffill()
    vol_df = pd.DataFrame(vols).sort_index().reindex(close_df.index).fillna(0)
    try:
        cache_path.write_bytes(pickle.dumps({
            "start": START_DATE,
            "end": END_DATE,
            "close": close_df,
            "volume": vol_df,
            "quick": QUICK_MODE,
        }, protocol=4))
    except Exception:
        pass
    return close_df, vol_df


# ── Scoring & Auswahl ─────────────────────────────────────────────────────────

def _percentile_rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average")


def rank_stocks(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    spy: pd.Series,
    dt: pd.Timestamp,
    p: dict,
) -> pd.Series:
    """Gesamtscore je Ticker am Datum dt (absteigend sortiert)."""
    hist = close.loc[:dt]
    if len(hist) < p["sma_trend"] + 5:
        return pd.Series(dtype=float)

    px = hist.iloc[-1]
    vol_hist = volume.loc[:dt].tail(p["dollar_vol_window"])
    dollar_vol = (vol_hist * close.loc[:dt].tail(p["dollar_vol_window"])).mean()

    tickers = [c for c in close.columns if c != "SPY"]
    eligible = []
    for tk in tickers:
        price = px.get(tk)
        dv = dollar_vol.get(tk) if tk in dollar_vol.index else np.nan
        if price is None or np.isnan(price) or price < p["min_price"]:
            continue
        if dv is None or np.isnan(dv) or dv < p["min_dollar_vol"]:
            continue
        eligible.append(tk)
    if not eligible:
        return pd.Series(dtype=float)

    sub = hist[eligible]
    rets = sub.pct_change()
    vol = rets.tail(p["vol_lookback"]).std()
    n_lv = max(1, int(len(eligible) * p["low_vol_pct"]))
    low_vol_set = set(vol.nsmallest(min(n_lv, len(vol))).index)

    mom6 = sub.iloc[-1] / sub.iloc[-p["mom_6m"]] - 1 if len(sub) > p["mom_6m"] else sub.pct_change(p["mom_6m"]).iloc[-1]
    mom12 = sub.iloc[-1] / sub.iloc[-p["mom_12m"]] - 1 if len(sub) > p["mom_12m"] else sub.pct_change(p["mom_12m"]).iloc[-1]
    mom_score = p["mom_w6"] * mom6 + p["mom_w12"] * mom12

    spy_hist = spy.loc[:dt]
    spy_ret6 = spy_hist.iloc[-1] / spy_hist.iloc[-p["mom_6m"]] - 1 if len(spy_hist) > p["mom_6m"] else 0
    rs = mom6 - spy_ret6

    sma200 = sub.rolling(p["sma_trend"]).mean().iloc[-1]
    sma50 = sub.rolling(p["sma_fast"]).mean().iloc[-1]
    trend = ((sub.iloc[-1] > sma200) & (sma50 > sma200)).astype(float)

    lv_rank = pd.Series({tk: 1.0 if tk in low_vol_set else 0.0 for tk in eligible})

    df = pd.DataFrame({
        "mom": mom_score,
        "rs": rs,
        "lv": lv_rank,
        "trend": trend,
    }).dropna(how="all")

    score = (
        p["w_mom"] * _percentile_rank(df["mom"])
        + p["w_rs"] * _percentile_rank(df["rs"])
        + p["w_lv"] * _percentile_rank(df["lv"])
        + p["w_trend"] * _percentile_rank(df["trend"])
    )
    return score.sort_values(ascending=False)


# ── Backtest ──────────────────────────────────────────────────────────────────

_INT_PARAMS = (
    "top_n", "exit_rank", "vol_lookback", "mom_6m", "mom_12m",
    "dollar_vol_window", "sma_trend", "sma_fast", "exit_sma",
)


def _normalize_params(p: dict) -> dict:
    """Pandas-Grid liefert float64 — Slicing/Rolling brauchen int."""
    out = dict(p)
    for k in _INT_PARAMS:
        if k in out and out[k] is not None:
            out[k] = int(out[k])
    return out


def _empty_bt(p: dict) -> dict:
    return {
        "equity": pd.Series(dtype=float, name="equity"),
        "metrics": {},
        "rebalance_log": [],
        "params": p,
    }


def run_stock_backtest(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    invest_daily: pd.Series | None,
    p: dict | None = None,
    eval_start: pd.Timestamp | str | None = None,
    eval_end: pd.Timestamp | str | None = None,
) -> dict:
    p = _normalize_params({**BASELINE_PARAMS, **(p or {})})
    spy = close["SPY"].dropna()
    idx = spy.index
    stock_cols = [c for c in close.columns if c != "SPY"]

    if eval_start is not None:
        t0 = pd.Timestamp(eval_start)
        t1 = pd.Timestamp(eval_end) if eval_end is not None else idx[-1]
        sim_idx = idx[(idx >= t0) & (idx <= t1)]
    else:
        sim_idx = idx

    if len(sim_idx) < 2:
        return _empty_bt(p)

    if p["use_regime"]:
        if invest_daily is None:
            invest_daily = build_kass_invest_pct()
        exposure = invest_daily.reindex(idx).ffill().fillna(1.0)
    else:
        exposure = pd.Series(1.0, index=idx)

    fridays = idx[idx.weekday == 4]
    sma100 = close[stock_cols].rolling(p["exit_sma"]).mean()

    holdings: set[str] = set()
    weights = pd.Series(0.0, index=stock_cols)
    equity = INITIAL_CAPITAL
    curve = []
    turnover_log = []
    rebalance_log = []

    for i, dt in enumerate(sim_idx):
        if i == 0:
            curve.append(equity)
            continue

        day_ret = 0.0
        invested_w = 0.0
        for tk in holdings:
            w = weights.get(tk, 0.0)
            if w <= 0 or tk not in stock_cols:
                continue
            r = close[tk].pct_change().loc[dt]
            if not np.isnan(r):
                day_ret += w * r
            invested_w += w

        margin_rate = float(p.get("margin_rate", 0.0))
        if invested_w > 1.0 and margin_rate > 0:
            day_ret -= (invested_w - 1.0) * margin_rate / 252.0

        equity *= 1.0 + day_ret
        curve.append(equity)

        if dt not in fridays:
            continue

        exp = float(exposure.loc[dt])
        factor = float(p.get("exposure_factor", 1.0))
        gross_target = exp * factor
        scores = rank_stocks(close, volume, spy, dt, p)
        if scores.empty or gross_target <= 0:
            turnover = float(weights.sum()) + float(sum(weights.get(t, 0) for t in holdings))
            weights[:] = 0.0
            holdings.clear()
            turnover_log.append(turnover)
            rebalance_log.append({
                "date": dt, "exposure": exp, "gross_exposure": gross_target,
                "factor": factor, "n": 0, "tickers": [],
            })
            continue

        ranked = list(scores.index)
        tn, er = int(p["top_n"]), int(p["exit_rank"])
        top_n = ranked[:tn]
        keep_zone = set(ranked[:er])

        new_holdings: set[str] = set()
        for tk in holdings:
            if tk not in keep_zone:
                continue
            px = close.at[dt, tk]
            s100 = sma100.at[dt, tk]
            if np.isnan(px) or np.isnan(s100) or px < s100:
                continue
            new_holdings.add(tk)
        for tk in top_n:
            new_holdings.add(tk)

        n = len(new_holdings)
        new_weights = pd.Series(0.0, index=stock_cols)
        if n > 0:
            w_each = gross_target / n
            for tk in new_holdings:
                new_weights[tk] = w_each

        turnover = float((new_weights - weights).abs().sum())
        if p["transaction_cost"] > 0:
            equity *= 1.0 - turnover * p["transaction_cost"]
        weights = new_weights
        holdings = new_holdings
        turnover_log.append(turnover)
        rebalance_log.append({
            "date": dt,
            "exposure": exp,
            "gross_exposure": gross_target,
            "factor": factor,
            "n": n,
            "tickers": sorted(holdings),
        })

    eq = pd.Series(curve, index=sim_idx, name="equity")
    metrics = compute_bt_metrics(eq, turnover_log)
    metrics["avg_positions"] = float(np.mean([r["n"] for r in rebalance_log])) if rebalance_log else 0
    metrics["avg_exposure"] = float(np.mean([r["exposure"] for r in rebalance_log])) if rebalance_log else 0
    metrics["avg_gross_exposure"] = float(
        np.mean([r.get("gross_exposure", r["exposure"]) for r in rebalance_log])
    ) if rebalance_log else 0
    return {
        "equity": eq,
        "metrics": metrics,
        "rebalance_log": rebalance_log,
        "params": p,
    }


def compute_bt_metrics(equity: pd.Series, turnover_log: list | None = None) -> dict:
    if equity is None or len(equity) < 2:
        return {}
    eq = equity.dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    dd = eq / eq.cummax() - 1
    maxdd = float(dd.min())
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    neg = rets[rets < 0]
    sortino = float(rets.mean() / neg.std() * np.sqrt(252)) if len(neg) and neg.std() > 0 else 0.0
    mar = cagr / abs(maxdd) if maxdd < 0 else 0.0
    avg_turn = float(np.mean(turnover_log)) if turnover_log else 0.0
    return {
        "cagr": round(cagr, 4),
        "maxdd": round(maxdd, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "mar": round(mar, 3),
        "calmar": round(mar, 3),
        "end_value": round(float(eq.iloc[-1]), 0),
        "avg_turnover": round(avg_turn, 4),
    }


def _spy_buy_hold(close: pd.DataFrame) -> pd.Series:
    spy = close["SPY"].dropna()
    ret = spy.pct_change().fillna(0)
    return (1 + ret).cumprod() * INITIAL_CAPITAL


def _load_backtest_data(quick: bool = False, refresh: bool = False) -> dict:
    """Universe, Preise und Regime-Quote laden (gemeinsam für Stufe 1/2/3)."""
    global QUICK_MODE, UNIVERSE_CSV, _DATA_CACHE
    QUICK_MODE = bool(quick)
    cache_key = "quick" if QUICK_MODE else "full"
    if not refresh and _DATA_CACHE and _DATA_CACHE.get("key") == cache_key:
        n = len(_DATA_CACHE["close"].columns) - 1
        print(f"  ♻️ Daten im Speicher ({cache_key}) · {n} Titel")
        return _DATA_CACHE

    UNIVERSE_CSV = _ensure_universe_csv(NOTEBOOK_DIR)
    universe = load_universe()
    tickers = [t for t, _ in universe]
    close, volume = load_price_panels(tickers)
    print("\n  ⏳ Kassandra Investitionsquote …")
    inv = build_kass_invest_pct()
    print(f"  Ø Investitionsquote (täglich): {float(inv.mean()):.0%}")
    _DATA_CACHE = {"key": cache_key, "close": close, "volume": volume, "invest_pct": inv}
    return _DATA_CACHE


def _grid_combos(grid: dict | None = None) -> list[dict]:
    grid = grid or STUFE2_GRID
    keys = list(grid.keys())
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]
    return [
        c for c in combos
        if int(c.get("exit_rank", 20)) >= int(c.get("top_n", 15))
    ]


def _wf_fold_ranges(close: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """(train_end, test_start, test_end) — OOS ab train_end inkl."""
    spy = close["SPY"].dropna()
    start, end = spy.index[0], spy.index[-1]
    folds = []
    cursor = start + pd.DateOffset(years=WF_TRAIN_YEARS)
    while cursor + pd.DateOffset(years=WF_TEST_YEARS) <= end:
        train_end = cursor
        test_end = cursor + pd.DateOffset(years=WF_TEST_YEARS)
        folds.append((train_end, train_end, test_end))
        cursor += pd.DateOffset(years=WF_TEST_YEARS)
    return folds


def _objective_value(m: dict | None, key: str = WF_OBJECTIVE) -> float:
    if not m:
        return -999.0
    v = m.get(key)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return -999.0
    return float(v)


def _pick_wf_winner(rows: list[dict], grid_keys: list[str]) -> dict:
    if not rows:
        return {**BASELINE_PARAMS, "use_regime": True, "transaction_cost": STUFE2_TC}
    df = pd.DataFrame(rows)
    score_col = "oos_mar"
    best = float(df[score_col].max())
    cutoff = best * (1 - PLATEAU_MAR_PCT) if best > 0 else best - PLATEAU_MAR_PCT
    cand = df[df[score_col] >= cutoff].copy()
    if cand.empty:
        cand = df.sort_values(score_col, ascending=False).head(1)
    sort_cols = [score_col]
    if "avg_turnover" in cand.columns:
        sort_cols.append("avg_turnover")
    cand = cand.sort_values(sort_cols, ascending=[False] + [True] * (len(sort_cols) - 1))
    row = cand.iloc[0]
    return _normalize_params({
        **BASELINE_PARAMS,
        **{k: row[k] for k in grid_keys if k in row},
        "use_regime": True,
        "transaction_cost": STUFE2_TC,
    })


def _stitched_oos_equity(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    inv: pd.Series,
    params: dict,
    folds: list,
) -> pd.Series:
    parts = []
    level = float(INITIAL_CAPITAL)
    for train_end, _, test_end in folds:
        bt = run_stock_backtest(
            close, volume, inv, params,
            eval_start=train_end, eval_end=test_end,
        )
        eq = bt["equity"]
        if eq is None or len(eq) < 2:
            continue
        normed = eq / float(eq.iloc[0]) * level
        parts.append(normed)
        level = float(normed.iloc[-1])
    if not parts:
        return pd.Series(dtype=float, name="equity")
    return pd.concat(parts)


def run_stufe1(quick: bool = False) -> dict:
    """Stufe 1: Mit vs. ohne Kassandra-Regime, Einzelaktien, keine Fees."""
    print("═" * 72)
    print(f"  REGIME MOMENTUM — Stufe 1  (v{BT_VERSION})")
    print("═" * 72)
    print(f"  Universe: {_ensure_universe_csv(NOTEBOOK_DIR)}")
    print(f"  Ampel:    Kassandra Regime v5 (100/50/0)")
    print(f"  Rebal:    Freitag · Top {BASELINE_PARAMS['top_n']} · Exit Top {BASELINE_PARAMS['exit_rank']} / SMA100")
    print(f"  Hebel:    Faktor 1.0 · TC {BASELINE_PARAMS['transaction_cost']:.1%}")

    data = _load_backtest_data(quick=quick)
    close, volume, inv = data["close"], data["volume"], data["invest_pct"]

    print("\n── Backtest MIT Regime ──")
    with_reg = run_stock_backtest(close, volume, inv, {**BASELINE_PARAMS, "use_regime": True})

    print("\n── Backtest OHNE Regime (immer 100 %) ──")
    no_reg = run_stock_backtest(close, volume, None, {**BASELINE_PARAMS, "use_regime": False})

    spy_eq = _spy_buy_hold(close)
    spy_m = compute_bt_metrics(spy_eq)

    rows = [
        {"label": "Regime + Momentum (Einzelaktien)", **with_reg["metrics"]},
        {"label": "Ohne Regime (Einzelaktien)", **no_reg["metrics"]},
        {"label": "SPY Buy & Hold", **spy_m},
    ]
    table = pd.DataFrame(rows)
    cols = ["label", "cagr", "sharpe", "sortino", "mar", "maxdd", "avg_turnover",
            "avg_exposure", "avg_positions", "end_value"]
    cols = [c for c in cols if c in table.columns]

    print("\n" + "═" * 88)
    print("  ERGEBNIS Stufe 1")
    print("═" * 88)
    print(table[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("═" * 88)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESULT_CSV, index=False)
    print(f"  💾 {RESULT_CSV}")

    delta_mar = with_reg["metrics"].get("mar", 0) - no_reg["metrics"].get("mar", 0)
    print(f"\n  Δ MAR (mit − ohne Regime): {delta_mar:+.3f}")
    if delta_mar > 0:
        print("  → Regime verbessert risikoadjustierte Rendite.")
    else:
        print("  → Regime schadet in Stufe 1 — Grid/Walk-Forward oder Parameter prüfen.")

    print("═" * 72)
    return {
        "with_regime": with_reg,
        "without_regime": no_reg,
        "spy_equity": spy_eq,
        "spy": spy_m,
        "table": table,
        "invest_pct": inv,
        "close": close,
    }


def plot_stufe1(result: dict):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5))
    result["with_regime"]["equity"].plot(ax=ax, label="Regime + Momentum", linewidth=1.5)
    result["without_regime"]["equity"].plot(ax=ax, label="Ohne Regime", linewidth=1.2, alpha=0.85)
    if "spy_equity" in result:
        result["spy_equity"].reindex(result["with_regime"]["equity"].index).plot(
            ax=ax, label="SPY B&H", linewidth=1, alpha=0.75,
        )
    m = result["with_regime"]["metrics"]
    ax.set_title(
        f"Einzelaktien S&P 500 — CAGR {m.get('cagr', 0):.1%}  MAR {m.get('mar', 0):.2f}",
    )
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    return fig, ax


def run_stufe2(quick: bool = False, grid: dict | None = None) -> dict:
    """Stufe 2: 0,1 % Fees + Parameter-Grid (36 Kombinationen), nur mit Regime."""
    combos = _grid_combos(grid)
    n = len(combos)

    print("═" * 72)
    print(f"  REGIME MOMENTUM — Stufe 2  (v{BT_VERSION})")
    print("═" * 72)
    uni = _ensure_universe_csv(NOTEBOOK_DIR)
    print(f"  Universe: {uni}")
    print(f"  Fees:     {STUFE2_TC:.2%} pro Rebalance-Turnover")
    print(f"  Grid:     {n} Kombinationen · Regime immer AN")

    data = _load_backtest_data(quick=quick)
    close, volume, inv = data["close"], data["volume"], data["invest_pct"]

    baseline = run_stock_backtest(
        close, volume, inv,
        {**BASELINE_PARAMS, "use_regime": True, "transaction_cost": STUFE2_TC},
    )
    print(
        f"\n  Baseline (Top {BASELINE_PARAMS['top_n']}, TC {STUFE2_TC:.2%}): "
        f"CAGR {baseline['metrics']['cagr']:.1%}  MAR {baseline['metrics']['mar']:.2f}"
    )

    rows = []
    for i, combo in enumerate(combos, 1):
        p = {
            **BASELINE_PARAMS,
            **combo,
            "use_regime": True,
            "transaction_cost": STUFE2_TC,
        }
        bt = run_stock_backtest(close, volume, inv, p)
        m = bt["metrics"]
        rows.append({**combo, **m})
        if i % 9 == 0 or i == n:
            print(f"     Grid [{i}/{n}] …", flush=True)

    grid_df = pd.DataFrame(rows).sort_values("mar", ascending=False).reset_index(drop=True)
    grid_keys = list((grid or STUFE2_GRID).keys())
    best_row = grid_df.iloc[0]
    best_params = _normalize_params({
        **BASELINE_PARAMS,
        **{k: best_row[k] for k in grid_keys if k in best_row},
        "use_regime": True,
        "transaction_cost": STUFE2_TC,
    })

    best_bt = run_stock_backtest(close, volume, inv, best_params)
    spy_m = compute_bt_metrics(_spy_buy_hold(close))

    show_cols = grid_keys + ["cagr", "mar", "maxdd", "sharpe", "avg_turnover", "avg_exposure"]
    show_cols = [c for c in show_cols if c in grid_df.columns]

    print("\n" + "═" * 88)
    print("  TOP 10 (nach MAR, mit Fees)")
    print("═" * 88)
    print(grid_df[show_cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("═" * 88)
    print(
        f"  Sieger: top_n={int(best_params['top_n'])} exit_rank={int(best_params['exit_rank'])} "
        f"low_vol={best_params['low_vol_pct']:.0%} → MAR {best_row['mar']:.3f}  "
        f"CAGR {best_row['cagr']:.1%}  MaxDD {best_row['maxdd']:.1%}"
    )
    print(f"  vs. Baseline MAR {baseline['metrics']['mar']:.3f}  ·  SPY MAR {spy_m.get('mar', 0):.3f}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    grid_df.to_csv(RESULT_STUFE2_CSV, index=False)
    STUFE2_WINNER_JSON.write_text(
        json.dumps(
            {k: best_params[k] for k in (*grid_keys, "transaction_cost", "use_regime")},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  💾 {RESULT_STUFE2_CSV}")
    print("═" * 72)

    return {
        "grid": grid_df,
        "best_params": best_params,
        "best": best_bt,
        "baseline": baseline,
        "spy": spy_m,
        "close": close,
        "invest_pct": inv,
    }


def plot_stufe2(result: dict):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5))
    result["best"]["equity"].plot(ax=ax, label="Grid-Sieger", linewidth=1.5)
    result["baseline"]["equity"].plot(ax=ax, label="Baseline + Fees", linewidth=1.2, alpha=0.85)
    bp = result["best_params"]
    ax.set_title(
        f"Stufe 2 — Top {int(bp['top_n'])} / Exit {int(bp['exit_rank'])} / "
        f"LowVol {bp['low_vol_pct']:.0%} · MAR {result['best']['metrics']['mar']:.2f}",
    )
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    return fig, ax


def run_stufe3(quick: bool = False, grid: dict | None = None) -> dict:
    """Stufe 3: Walk-Forward (3J Train → 2J Test) auf Stufe-2-Grid, Ziel OOS-MAR."""
    grid_def = grid or STUFE2_GRID
    grid_keys = list(grid_def.keys())
    combos = _grid_combos(grid_def)

    print("═" * 72)
    print(f"  REGIME MOMENTUM — Stufe 3 Walk-Forward  (v{BT_VERSION})")
    print("═" * 72)
    uni = _ensure_universe_csv(NOTEBOOK_DIR)
    print(f"  Universe: {uni}")
    print(f"  Grid:     {len(combos)} Kombinationen · TC {STUFE2_TC:.2%}")
    print(f"  WF:       {WF_TRAIN_YEARS}J Train → {WF_TEST_YEARS}J Test · Ziel {WF_OBJECTIVE.upper()}")

    data = _load_backtest_data(quick=quick)
    close, volume, inv = data["close"], data["volume"], data["invest_pct"]
    folds = _wf_fold_ranges(close)
    n_folds = len(folds)

    if n_folds == 0:
        raise RuntimeError("Zu wenig Historie für Walk-Forward (min. ~5 Jahre).")

    print(f"  Folds:    {n_folds}")
    for fi, (tr, _, te) in enumerate(folds, 1):
        print(f"     Fold {fi}: OOS {tr.date()} → {te.date()}")

    rows = []
    for ci, combo in enumerate(combos, 1):
        p = _normalize_params({
            **BASELINE_PARAMS,
            **combo,
            "use_regime": True,
            "transaction_cost": STUFE2_TC,
        })
        oos_scores, oos_cagrs = [], []
        for train_end, _, test_end in folds:
            bt_oos = run_stock_backtest(
                close, volume, inv, p,
                eval_start=train_end, eval_end=test_end,
            )
            m = bt_oos["metrics"]
            oos_scores.append(_objective_value(m))
            if m.get("cagr") is not None:
                oos_cagrs.append(float(m["cagr"]))

        bt_full = run_stock_backtest(close, volume, inv, p)
        m_full = bt_full["metrics"]
        rows.append({
            **combo,
            "oos_mar": float(np.mean(oos_scores)) if oos_scores else np.nan,
            "oos_cagr": float(np.mean(oos_cagrs)) if oos_cagrs else np.nan,
            "folds": len(oos_scores),
            **{f"is_{k}": m_full.get(k) for k in ("cagr", "mar", "maxdd", "sharpe", "avg_turnover")},
        })
        if ci % 6 == 0 or ci == len(combos):
            print(f"     WF-Grid [{ci}/{len(combos)}] …", flush=True)

    wf_df = pd.DataFrame(rows).sort_values("oos_mar", ascending=False).reset_index(drop=True)
    winner = _pick_wf_winner(rows, grid_keys)
    winner_bt = run_stock_backtest(close, volume, inv, winner)
    oos_eq = _stitched_oos_equity(close, volume, inv, winner, folds)
    oos_m = compute_bt_metrics(oos_eq)

    stufe2_params = None
    stufe2_bt = None
    if STUFE2_WINNER_JSON.is_file():
        try:
            stufe2_params = _normalize_params({
                **BASELINE_PARAMS,
                **json.loads(STUFE2_WINNER_JSON.read_text(encoding="utf-8")),
            })
            stufe2_bt = run_stock_backtest(close, volume, inv, stufe2_params)
        except Exception:
            stufe2_params = None

    show_cols = grid_keys + ["oos_mar", "oos_cagr", "is_mar", "is_cagr", "is_maxdd", "folds"]
    show_cols = [c for c in show_cols if c in wf_df.columns]

    print("\n" + "═" * 88)
    print("  TOP 10 (nach Ø OOS-MAR)")
    print("═" * 88)
    print(wf_df[show_cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("═" * 88)
    wr = wf_df.iloc[0]
    print(
        f"  WF-Sieger: top_n={int(winner['top_n'])} exit_rank={int(winner['exit_rank'])} "
        f"low_vol={winner['low_vol_pct']:.0%} → Ø OOS-MAR {wr['oos_mar']:.3f}  "
        f"IS-MAR {wr.get('is_mar', 0):.3f}"
    )
    print(
        f"  Gestitchte OOS-Kurve: CAGR {oos_m.get('cagr', 0):.1%}  "
        f"MAR {oos_m.get('mar', 0):.3f}  MaxDD {oos_m.get('maxdd', 0):.1%}"
    )
    if stufe2_bt:
        print(
            f"  Stufe-2-Sieger (IS): MAR {stufe2_bt['metrics'].get('mar', 0):.3f}  "
            f"(top_n={int(stufe2_params['top_n'])}, exit={int(stufe2_params['exit_rank'])})"
        )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wf_df.to_csv(RESULT_STUFE3_CSV, index=False)
    STUFE3_WINNER_JSON.write_text(
        json.dumps({k: winner[k] for k in (*grid_keys, "transaction_cost", "use_regime")}, indent=2),
        encoding="utf-8",
    )
    print(f"  💾 {RESULT_STUFE3_CSV}")
    print("═" * 72)

    return {
        "grid": wf_df,
        "winner_params": winner,
        "winner": winner_bt,
        "oos_equity": oos_eq,
        "oos_metrics": oos_m,
        "folds": folds,
        "stufe2_params": stufe2_params,
        "stufe2": stufe2_bt,
        "close": close,
        "invest_pct": inv,
    }


def plot_stufe3(result: dict):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5))
    if len(result.get("oos_equity", ())) > 1:
        result["oos_equity"].plot(ax=ax, label="WF OOS (gestitcht)", linewidth=1.5)
    result["winner"]["equity"].plot(ax=ax, label="WF-Sieger IS", linewidth=1.0, alpha=0.65)
    if result.get("stufe2") and result["stufe2"].get("equity") is not None:
        result["stufe2"]["equity"].plot(ax=ax, label="Stufe-2-Sieger IS", linewidth=1.0, alpha=0.55)
    om = result.get("oos_metrics") or {}
    wp = result["winner_params"]
    ax.set_title(
        f"Stufe 3 — Top {int(wp['top_n'])}/{int(wp['exit_rank'])} · "
        f"OOS MAR {om.get('mar', 0):.2f}",
    )
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    return fig, ax


def run_full_validation(quick: bool = False, run_wf: bool = True) -> dict:
    """
    Vollständige Validierung: Stufe 1 → 2 → 3.
    quick=False: ~500 S&P-Titel (erster Download 30–60 Min, danach Cache).
    """
    set_universe("sp500")
    global QUICK_MODE, _DATA_CACHE
    QUICK_MODE = bool(quick)
    _DATA_CACHE = None
    mode = f"QUICK ({QUICK_N})" if quick else "FULL (~500)"

    print("█" * 72)
    print(f"  REGIME MOMENTUM — Voll-Validierung  (v{BT_VERSION})  ·  {mode}")
    print("█" * 72)

    r1 = run_stufe1(quick=quick)
    r2 = run_stufe2(quick=quick)
    r3 = run_stufe3(quick=quick) if run_wf else None

    print("\n" + "█" * 72)
    print("  ZUSAMMENFASSUNG")
    print("█" * 72)
    m1 = r1["with_regime"]["metrics"]
    print(
        f"  Stufe 1 Regime:     CAGR {m1.get('cagr', 0):.1%}  "
        f"MAR {m1.get('mar', 0):.2f}  MaxDD {m1.get('maxdd', 0):.1%}"
    )
    bp = r2["best_params"]
    print(
        f"  Stufe 2 IS-Sieger:  Top {int(bp['top_n'])}/{int(bp['exit_rank'])}  "
        f"LowVol {bp['low_vol_pct']:.0%}  MAR {r2['best']['metrics'].get('mar', 0):.2f}"
    )
    if r3:
        wp = r3["winner_params"]
        om = r3.get("oos_metrics") or {}
        print(
            f"  Stufe 3 WF-Sieger:  Top {int(wp['top_n'])}/{int(wp['exit_rank'])}  "
            f"LowVol {wp['low_vol_pct']:.0%}  Ø OOS-MAR {r3['grid'].iloc[0]['oos_mar']:.2f}"
        )
        print(
            f"  Gestitchte OOS:     CAGR {om.get('cagr', 0):.1%}  "
            f"MAR {om.get('mar', 0):.2f}  MaxDD {om.get('maxdd', 0):.1%}"
        )
    print("█" * 72)

    return {"stufe1": r1, "stufe2": r2, "stufe3": r3}


def load_winner_params(stage: str = "stufe3") -> dict:
    """Lädt WF-/Final-Parameter (final_params.json liegt immer im S&P-500-Cache)."""
    paths = {
        "final": NOTEBOOK_DIR / SP500_CACHE_NAME / "final_params.json",
        "stufe4": NOTEBOOK_DIR / SP500_CACHE_NAME / "stufe4_winner.json",
        "stufe3": NOTEBOOK_DIR / SP500_CACHE_NAME / "stufe3_winner.json",
    }
    path = paths.get(stage, paths["stufe3"])
    params = _normalize_params({**BASELINE_PARAMS, **FINAL_PARAMS_DEFAULT})
    params["exposure_factor"] = 1.0
    if path.is_file():
        try:
            params = _normalize_params({
                **BASELINE_PARAMS,
                **FINAL_PARAMS_DEFAULT,
                **json.loads(path.read_text(encoding="utf-8")),
            })
            params["exposure_factor"] = 1.0
        except Exception:
            pass
    return params


def run_stufe4(
    quick: bool = False,
    factors: list[float] | None = None,
    base_params: dict | None = None,
) -> dict:
    """Stufe 4: Faktor-Grid auf WF-Sieger-Parametern, Bewertung per Ø OOS-MAR."""
    factors = factors or STUFE4_FACTORS
    base = _normalize_params({**load_winner_params("stufe3"), **(base_params or {})})
    base["transaction_cost"] = STUFE2_TC

    print("═" * 72)
    print(f"  REGIME MOMENTUM — Stufe 4 Faktor  (v{BT_VERSION})")
    print("═" * 72)
    print(
        f"  Basis:    Top {int(base['top_n'])}/{int(base['exit_rank'])}  "
        f"LowVol {base['low_vol_pct']:.0%}  TC {STUFE2_TC:.2%}"
    )
    print(f"  Faktoren: {factors}")
    print(f"  Margin:   {base.get('margin_rate', 0):.1%} p.a. auf borrowed (>100 %)")

    data = _load_backtest_data(quick=quick)
    close, volume, inv = data["close"], data["volume"], data["invest_pct"]
    folds = _wf_fold_ranges(close)
    if not folds:
        raise RuntimeError("Zu wenig Historie für Walk-Forward.")

    rows = []
    for fi, fac in enumerate(factors, 1):
        p = {**base, "exposure_factor": float(fac)}
        oos_scores, oos_cagrs = [], []
        for train_end, _, test_end in folds:
            bt_oos = run_stock_backtest(
                close, volume, inv, p,
                eval_start=train_end, eval_end=test_end,
            )
            m = bt_oos["metrics"]
            oos_scores.append(_objective_value(m))
            if m.get("cagr") is not None:
                oos_cagrs.append(float(m["cagr"]))

        bt_full = run_stock_backtest(close, volume, inv, p)
        m_full = bt_full["metrics"]
        oos_eq = _stitched_oos_equity(close, volume, inv, p, folds)
        oos_m = compute_bt_metrics(oos_eq)
        rows.append({
            "exposure_factor": fac,
            "oos_mar": float(np.mean(oos_scores)) if oos_scores else np.nan,
            "oos_cagr": float(np.mean(oos_cagrs)) if oos_cagrs else np.nan,
            "stitched_cagr": oos_m.get("cagr"),
            "stitched_mar": oos_m.get("mar"),
            "stitched_maxdd": oos_m.get("maxdd"),
            "folds": len(oos_scores),
            **{f"is_{k}": m_full.get(k) for k in ("cagr", "mar", "maxdd", "sharpe", "avg_gross_exposure")},
        })
        print(
            f"     Faktor {fac:.2f}  Ø OOS-MAR {rows[-1]['oos_mar']:.3f}  "
            f"gestitcht MAR {oos_m.get('mar', 0):.3f}",
            flush=True,
        )

    fac_df = pd.DataFrame(rows).sort_values("oos_mar", ascending=False).reset_index(drop=True)
    best_fac = float(fac_df.iloc[0]["exposure_factor"])
    final_p = {**base, "exposure_factor": best_fac}
    final_bt = run_stock_backtest(close, volume, inv, final_p)
    final_oos = _stitched_oos_equity(close, volume, inv, final_p, folds)
    final_oos_m = compute_bt_metrics(final_oos)

    fac1 = next((r for r in rows if r["exposure_factor"] == 1.0), rows[0])

    print("\n" + "═" * 88)
    print("  FAKTOR-VERGLEICH (nach Ø OOS-MAR)")
    print("═" * 88)
    cols = ["exposure_factor", "oos_mar", "oos_cagr", "stitched_mar", "stitched_maxdd",
            "is_mar", "is_cagr", "is_avg_gross_exposure"]
    cols = [c for c in cols if c in fac_df.columns]
    print(fac_df[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("═" * 88)
    print(
        f"  Sieger: Faktor {best_fac:.2f}  Ø OOS-MAR {fac_df.iloc[0]['oos_mar']:.3f}  "
        f"gestitcht CAGR {final_oos_m.get('cagr', 0):.1%}  MAR {final_oos_m.get('mar', 0):.3f}"
    )
    if fac1:
        print(
            f"  vs. Faktor 1.0:  Ø OOS-MAR {fac1['oos_mar']:.3f}  "
            f"gestitcht MAR {fac1.get('stitched_mar', 0):.3f}"
        )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fac_df.to_csv(RESULT_STUFE4_CSV, index=False)
    save_p = {k: final_p[k] for k in (
        "top_n", "exit_rank", "low_vol_pct", "use_regime",
        "transaction_cost", "exposure_factor", "margin_rate",
    )}
    STUFE4_WINNER_JSON.write_text(json.dumps(save_p, indent=2), encoding="utf-8")
    FINAL_PARAMS_JSON.write_text(json.dumps(save_p, indent=2), encoding="utf-8")
    print(f"  💾 {RESULT_STUFE4_CSV}")
    print(f"  💾 {FINAL_PARAMS_JSON}")
    print("═" * 72)

    return {
        "grid": fac_df,
        "final_params": final_p,
        "winner": final_bt,
        "oos_equity": final_oos,
        "oos_metrics": final_oos_m,
        "base_params": base,
        "close": close,
        "invest_pct": inv,
    }


def plot_stufe4(result: dict):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5))
    if len(result.get("oos_equity", ())) > 1:
        result["oos_equity"].plot(ax=ax, label="OOS (gestitcht)", linewidth=1.5)
    result["winner"]["equity"].plot(ax=ax, label="IS", linewidth=1.0, alpha=0.65)
    fp = result["final_params"]
    om = result.get("oos_metrics") or {}
    ax.set_title(
        f"Stufe 4 — Faktor {fp.get('exposure_factor', 1):.2f}  "
        f"Top {int(fp['top_n'])}/{int(fp['exit_rank'])}  "
        f"OOS MAR {om.get('mar', 0):.2f}",
    )
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    return fig, ax


def run_r1000_robustness(quick: bool = False) -> dict:
    """
    Russell-1000-Robustness: feste S&P-500-Final-Parameter, Faktor 1.0, kein Grid.
    Vergleich Regime vs. ohne Regime + WF OOS (gestitcht).
    """
    set_universe("r1000")
    global QUICK_MODE, _DATA_CACHE
    QUICK_MODE = bool(quick)
    _DATA_CACHE = None

    params = load_winner_params("final")
    params["exposure_factor"] = 1.0
    params["transaction_cost"] = STUFE2_TC
    params["use_regime"] = True

    print("█" * 72)
    print(f"  RUSSELL 1000 — Robustness  (v{BT_VERSION})")
    print("█" * 72)
    print(f"  Universum: {_active_profile()['label']}")
    print(
        f"  Parameter (S&P-500-final): Top {int(params['top_n'])}/{int(params['exit_rank'])}  "
        f"LowVol {params['low_vol_pct']:.0%}  Faktor 1.0  TC {STUFE2_TC:.2%}"
    )
    print(f"  Regime:   Kassandra v5 (SPY + US-Breadth) — unverändert")

    data = _load_backtest_data(quick=quick)
    close, volume, inv = data["close"], data["volume"], data["invest_pct"]

    print("\n── Backtest MIT Regime ──")
    with_reg = run_stock_backtest(close, volume, inv, params)
    print("\n── Backtest OHNE Regime ──")
    no_reg = run_stock_backtest(close, volume, None, {**params, "use_regime": False})
    spy_m = compute_bt_metrics(_spy_buy_hold(close))

    folds = _wf_fold_ranges(close)
    oos_scores = []
    for train_end, _, test_end in folds:
        bt_oos = run_stock_backtest(
            close, volume, inv, params,
            eval_start=train_end, eval_end=test_end,
        )
        oos_scores.append(_objective_value(bt_oos["metrics"]))
    oos_eq = _stitched_oos_equity(close, volume, inv, params, folds)
    oos_m = compute_bt_metrics(oos_eq)
    avg_oos_mar = float(np.mean(oos_scores)) if oos_scores else 0.0

    rows = [
        {"label": "R1000 Regime + Momentum", **with_reg["metrics"], "oos_mar": avg_oos_mar,
         "stitched_mar": oos_m.get("mar")},
        {"label": "R1000 Ohne Regime", **no_reg["metrics"]},
        {"label": "SPY B&H", **spy_m},
    ]
    table = pd.DataFrame(rows)
    cols = ["label", "cagr", "mar", "maxdd", "sharpe", "oos_mar", "stitched_mar",
            "avg_gross_exposure", "avg_turnover", "end_value"]
    cols = [c for c in cols if c in table.columns]

    print("\n" + "═" * 88)
    print("  ERGEBNIS Russell 1000 (feste Params)")
    print("═" * 88)
    print(table[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("═" * 88)
    delta = with_reg["metrics"].get("mar", 0) - no_reg["metrics"].get("mar", 0)
    print(f"  Δ MAR (mit − ohne Regime): {delta:+.3f}")
    print(
        f"  WF Ø OOS-MAR: {avg_oos_mar:.3f}  ·  gestitcht "
        f"CAGR {oos_m.get('cagr', 0):.1%}  MAR {oos_m.get('mar', 0):.3f}  "
        f"MaxDD {oos_m.get('maxdd', 0):.1%}"
    )
    if avg_oos_mar >= 0.8:
        print("  → Robust: Edge übersteht breiteres Universum.")
    elif avg_oos_mar >= 0.5:
        print("  → Schwächer als S&P 500, aber noch tragfähig — ggf. Params anpassen.")
    else:
        print("  → Edge bricht auf R1000 ein — nur S&P 500 handeln.")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESULT_R1000_CSV, index=False)
    print(f"  💾 {RESULT_R1000_CSV}")
    print("█" * 72)

    set_universe("sp500")
    return {
        "with_regime": with_reg,
        "without_regime": no_reg,
        "spy": spy_m,
        "oos_equity": oos_eq,
        "oos_metrics": oos_m,
        "avg_oos_mar": avg_oos_mar,
        "table": table,
        "params": params,
        "close": close,
    }


def plot_r1000(result: dict):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5))
    if len(result.get("oos_equity", ())) > 1:
        result["oos_equity"].plot(ax=ax, label="WF OOS (gestitcht)", linewidth=1.5)
    result["with_regime"]["equity"].plot(ax=ax, label="Regime IS", linewidth=1.0, alpha=0.65)
    result["without_regime"]["equity"].plot(ax=ax, label="Ohne Regime", linewidth=1.0, alpha=0.5)
    p = result["params"]
    om = result.get("oos_metrics") or {}
    ax.set_title(
        f"Russell 1000 — Top {int(p['top_n'])}/{int(p['exit_rank'])}  "
        f"OOS MAR {om.get('mar', 0):.2f}",
    )
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    return fig, ax


# ── Live-Signal (Colab → JSON → Dashboard) ────────────────────────────────────

LIVE_JSON_NAME = "regime_momentum_positionen.json"
LIVE_JSON = NOTEBOOK_DIR / LIVE_JSON_NAME
LIVE_POSITIONS_TXT = NOTEBOOK_DIR / "regime_momentum_positionen.txt"
GITHUB_REPO = "lazarkitanov-cell/trading-dashboard"


def _ticker_name_map() -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        for code, name in load_universe():
            if name:
                names[code] = name
    except Exception:
        pass
    return names


def _parse_meine_positionen(
    meine: list[str] | str | None = None,
    txt_path: Path | None = None,
) -> list[str]:
    if isinstance(meine, str) and meine.strip():
        return [t.strip().upper() for t in meine.replace(";", ",").split(",") if t.strip()]
    if isinstance(meine, list):
        return [str(t).strip().upper() for t in meine if str(t).strip()]
    path = txt_path or LIVE_POSITIONS_TXT
    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            return [t.strip().upper() for t in raw.replace(";", ",").split(",") if t.strip()]
    return []


def _save_meine_positionen(tickers: list[str], txt_path: Path | None = None) -> None:
    path = txt_path or LIVE_POSITIONS_TXT
    try:
        path.write_text(", ".join(tickers), encoding="utf-8")
    except Exception:
        pass


def refresh_prices_for_live(stale_days: int = 2) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Preis-Cache aktualisieren (inkrementell wenn möglich)."""
    global END_DATE, QUICK_MODE, UNIVERSE_CSV
    QUICK_MODE = False
    END_DATE = dt_date.today().strftime("%Y-%m-%d")
    UNIVERSE_CSV = _ensure_universe_csv(NOTEBOOK_DIR)
    universe = load_universe()
    tickers = [t for t, _ in universe]
    cache_path = PRICES_CACHE
    all_syms = sorted(set(tickers) | {"SPY"})
    closes, vols = {}, {}
    cached_close, cached_vol = None, None

    if cache_path.is_file():
        try:
            cached = pickle.loads(cache_path.read_bytes())
            if cached.get("start") == START_DATE and not cached.get("quick"):
                cached_close = cached["close"]
                cached_vol = cached["volume"]
        except Exception:
            pass

    if cached_close is not None and len(cached_close) > 0:
        last_dt = cached_close.index[-1]
        age_days = (pd.Timestamp(END_DATE) - last_dt).days
        if age_days <= stale_days:
            cols = [c for c in all_syms if c in cached_close.columns]
            print(f"  💾 Preis-Cache LIVE ({age_days}d alt) · {len(cols)} Titel")
            return cached_close[cols].copy(), cached_vol[cols].copy()

        from_dt = (last_dt - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        print(f"  📡 EODHD Update ab {from_dt} · {len(all_syms)} Serien …")
        closes = {c: cached_close[c] for c in all_syms if c in cached_close.columns}
        vols = {c: cached_vol[c] for c in all_syms if c in cached_vol.columns}
        for i, tk in enumerate(all_syms, 1):
            panel = fetch_eod_panel(_eodhd_sym(tk), start=from_dt, end=END_DATE)
            if panel.empty:
                continue
            if tk in closes:
                merged_c = pd.concat([closes[tk], panel["close"]])
                merged_c = merged_c[~merged_c.index.duplicated(keep="last")].sort_index()
                closes[tk] = merged_c
                merged_v = pd.concat([vols.get(tk, pd.Series(dtype=float)), panel["volume"]])
                merged_v = merged_v[~merged_v.index.duplicated(keep="last")].sort_index()
                vols[tk] = merged_v.reindex(merged_c.index).fillna(0)
            else:
                closes[tk] = panel["close"]
                vols[tk] = panel["volume"]
            if i % 50 == 0 or i == len(all_syms):
                print(f"     [{i}/{len(all_syms)}] · {len(closes)} OK", flush=True)
            time.sleep(EODHD_DELAY)
    else:
        print("  📡 EODHD FULL (kein Cache) …")
        return load_price_panels(tickers)

    close_df = pd.DataFrame(closes).sort_index().ffill()
    vol_df = pd.DataFrame(vols).sort_index().reindex(close_df.index).fillna(0)
    try:
        cache_path.write_bytes(pickle.dumps({
            "start": START_DATE,
            "end": END_DATE,
            "close": close_df,
            "volume": vol_df,
            "quick": False,
        }, protocol=4))
    except Exception:
        pass
    return close_df, vol_df


def _regime_label(invest_pct: float) -> str:
    if invest_pct >= 0.99:
        return "🟢 GRÜN — Volle Quote"
    if invest_pct >= 0.45:
        return f"🟡 GELB — Quote {invest_pct:.0%}"
    if invest_pct > 0.01:
        return f"🔴 ROT — Quote {invest_pct:.0%}"
    return "🔴 ROT — Cash (0%)"


def upload_live_json(
    path: Path | None = None,
    github_token: str | None = None,
    repo_path: str | None = None,
) -> bool:
    """JSON auf GitHub (trading-dashboard) hochladen."""
    import base64

    path = path or LIVE_JSON
    repo_path = repo_path or LIVE_JSON_NAME
    token = github_token or os.environ.get("GITHUB_TOKEN", "")
    if not token or not path.is_file():
        return False
    content = base64.b64encode(path.read_bytes()).decode()
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    r = requests.get(url, headers=headers, timeout=30)
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {
        "message": f"Regime Momentum live {datetime.now():%Y-%m-%d %H:%M}",
        "content": content,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=60)
    if r.status_code in (200, 201):
        print(f"  ✅ GitHub: {GITHUB_REPO}/{repo_path}")
        return True
    print(f"  ⚠ GitHub Upload: {r.status_code} {r.text[:200]}")
    return False


def run_live_signal(
    meine_positionen: list[str] | str | None = None,
    kapital_eur: float = 100_000,
    upload_github: bool = False,
    github_token: str | None = None,
) -> dict:
    """
    Aktuelles Ziel-Portfolio nach letztem Freitags-Rebalancing.
    Schreibt regime_momentum_positionen.json (optional GitHub-Upload).
    """
    global _DATA_CACHE
    set_universe("sp500")
    _DATA_CACHE = None

    params = load_winner_params("final")
    params["use_regime"] = True
    params["exposure_factor"] = 1.0
    params["transaction_cost"] = STUFE2_TC

    print("═" * 72)
    print(f"  REGIME MOMENTUM — LIVE  (v{BT_VERSION})")
    print("═" * 72)
    print(
        f"  Parameter: Top {int(params['top_n'])}/{int(params['exit_rank'])}  "
        f"LowVol {params['low_vol_pct']:.0%}  Regime an  Faktor 1.0"
    )

    close, volume = refresh_prices_for_live()
    print("\n  ⏳ Kassandra Investitionsquote …")
    inv = build_kass_invest_pct()
    bt = run_stock_backtest(close, volume, inv, params)
    if not bt["rebalance_log"]:
        raise RuntimeError("Kein Rebalancing — zu wenig Preisdaten.")

    last = bt["rebalance_log"][-1]
    as_of = pd.Timestamp(last["date"])
    holdings = sorted(last["tickers"])
    exposure = float(last["exposure"])
    gross = float(last.get("gross_exposure", exposure))
    inv_now = float(inv.reindex(close.index).ffill().iloc[-1])
    names = _ticker_name_map()
    spy = close["SPY"].dropna()
    scores = rank_stocks(close, volume, spy, as_of, params)

    n = len(holdings)
    w_each = gross / n if n > 0 else 0.0
    cash_pct = max(0.0, 1.0 - gross)

    meine = _parse_meine_positionen(meine_positionen)
    if isinstance(meine_positionen, str) and meine_positionen.strip() and meine:
        _save_meine_positionen(meine)
    elif isinstance(meine_positionen, list) and meine:
        _save_meine_positionen(meine)

    holdings_set = set(holdings)
    meine_set = set(meine)
    kaufen = sorted(holdings_set - meine_set)
    verkaufen = sorted(meine_set - holdings_set)

    dt = datetime.now()
    regime_lbl = _regime_label(inv_now)

    print(f"\n  Signal-Datum : {as_of.date()} (letztes Fr-Rebal)")
    print(f"  Regime heute : {regime_lbl}")
    print(f"  Brutto-Quote : {gross:.0%}  ({n} Aktien · je {w_each:.1%})")
    if cash_pct > 0.01:
        print(f"  Cash-Anteil  : {cash_pct:.0%}")

    print(f"\n  Ziel-Portfolio ({n} US-Aktien):")
    for i, tk in enumerate(holdings, 1):
        sc = scores.get(tk)
        sc_s = f"Score {sc:.3f}" if sc is not None and not np.isnan(sc) else "—"
        print(
            f"    {i:2}. {tk:6}  {names.get(tk, '—')[:28]:28}  "
            f"{w_each:5.1%}  (~€{kapital_eur * w_each:,.0f})  {sc_s}"
        )

    if meine:
        print(f"\n  Mein Depot ({len(meine)} Ticker): {', '.join(meine)}")
        if kaufen:
            print(f"  🟢 KAUFEN   : {', '.join(kaufen)}")
        if verkaufen:
            print(f"  🔴 VERKAUFEN: {', '.join(verkaufen)}")
        if not kaufen and not verkaufen:
            print("  ✅ Keine Ticker-Wechsel — Depot = Ziel")
    else:
        print("\n  ℹ️  Kein Depot — MEINE_POSITIONEN setzen für Kauf/Verkauf-Vergleich")

    rankings = []
    for rk, (tk, sc) in enumerate(scores.items(), 1):
        in_port = tk in holdings_set
        rankings.append({
            "rang": rk,
            "ticker": tk,
            "name": names.get(tk, "—"),
            "score": round(float(sc), 4) if not np.isnan(sc) else None,
            "im_portfolio": in_port,
            "exit_zone": rk <= int(params["exit_rank"]),
            "top_n": rk <= int(params["top_n"]),
        })

    def _ha_row(aktion, tk, grund, ziel_eur=None):
        row = {
            "aktion": aktion,
            "ticker": tk,
            "name": names.get(tk, tk),
            "grund": grund,
            "gewicht": w_each,
            "prioritaet": "Plan",
        }
        if ziel_eur is not None:
            row["ziel_eur"] = ziel_eur
        return row

    ha = []
    for tk in verkaufen:
        ha.append(_ha_row(
            "🔴 VERKAUFEN", tk,
            f"Fr-Rebal · Rank-Exit oder nicht Top-{int(params['top_n'])}",
        ))
    for tk in holdings:
        ziel_eur = round(kapital_eur * w_each)
        if tk in meine_set and tk not in kaufen:
            ha.append(_ha_row(
                "⚪ HALTEN", tk,
                f"Im Ziel-Portfolio · {w_each:.1%}",
                ziel_eur,
            ))
        elif tk in kaufen:
            ha.append(_ha_row(
                "🟢 KAUFEN", tk,
                f"Neu Top-{int(params['top_n'])} · Regime {gross:.0%}",
                ziel_eur,
            ))

    payload = {
        "datum": dt.strftime("%Y-%m-%d"),
        "stand": dt.strftime("%Y-%m-%d %H:%M"),
        "sync_ts": dt.strftime("%Y-%m-%d %H:%M"),
        "signal_datum": as_of.strftime("%Y-%m-%d"),
        "strategie": "Regime-Momentum",
        "bt_version": BT_VERSION,
        "params": {
            k: (float(v) if isinstance(v, (np.floating, float)) else v)
            for k, v in params.items()
            if k in (
                "top_n", "exit_rank", "low_vol_pct", "use_regime",
                "exposure_factor", "transaction_cost",
            )
        },
        "regime_label": regime_lbl,
        "invest_pct": inv_now,
        "gross_exposure": gross,
        "cash_pct": cash_pct,
        "n_positions": n,
        "ziel": [
            {
                "ticker": tk,
                "name": names.get(tk, "—"),
                "gewicht": w_each,
                "ziel_eur": round(kapital_eur * w_each),
                "score": scores.get(tk),
            }
            for tk in holdings
        ],
        "ziel_ticker": holdings,
        "rankings": rankings[:50],
        "verkaufen": verkaufen,
        "kaufen": kaufen,
        "meine_aktien": meine,
        "kapital_eur": kapital_eur,
        "handelsanweisungen": ha,
        "rebal_freq": "weekly_friday",
        "regel_text": (
            f"Wöchentlich Freitag: Top-{int(params['top_n'])} Momentum+RS+LowVol+Trend, "
            f"halten bis Rank>{int(params['exit_rank'])} oder Close<SMA100. "
            f"Kassandra Regime steuert Brutto-Quote (100/50/0)."
        ),
        "hinweis": "Do EOD-Signal → Fr 15:30 US · S&P 500 · final_params.json",
        "ampel_source": "kassandra_regime",
    }

    LIVE_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"\n  💾 {LIVE_JSON.name}")
    if upload_github:
        upload_live_json(github_token=github_token)
    print("\n✅ Fertig.")
    return payload
