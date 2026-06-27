"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  KASSANDRA AMPEL — Research Phase A (pandas) + Phase B (vectorbt)            ║
║  Daten: yfinance → CSV-Cache | Breadth: regime_cache/breadth_panel.pkl       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import itertools
import os
import pickle
import warnings
from datetime import date as dt_date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

RESEARCH_VERSION = 3

DRIVE_CANDIDATES = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
]


def _resolve_notebook_dir() -> Path:
    try:
        script_dir = Path(__file__).resolve().parent
        if (script_dir / "regime_cache").is_dir():
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
        if p.is_dir():
            return p
    local = Path(r"h:\Meine Ablage\Colab Notebooks")
    if local.is_dir():
        return local
    return Path(".").resolve()


NOTEBOOK_DIR = _resolve_notebook_dir()
CACHE_DIR = NOTEBOOK_DIR / "regime_cache"
ETF_PRICE_DIR = CACHE_DIR / "etf_prices"
GRID_RESULT_CSV = CACHE_DIR / "ampel_grid_spy.csv"
VBT_AMPEL_CSV = CACHE_DIR / "ampel_grid_vbt.csv"
VBT_TRAIL_CSV = CACHE_DIR / "trailing_grid_vbt.csv"

START_DATE = "2008-01-01"
END_DATE = dt_date.today().strftime("%Y-%m-%d")
INITIAL_CAPITAL = 10_000
TRANSACTION_COST = 0.001
SLIPPAGE = 0.0005
TOTAL_COST = TRANSACTION_COST + SLIPPAGE

BREADTH_CACHE = CACHE_DIR / "breadth_panel.pkl"
BREADTH_MA = 200

DEFAULT_ETFS = ("SPY", "EFA", "EEM", "SHY", "IEF", "TLT")

# Phase-A-Grid (kompakt, SPY)
PHASE_A_GRID = {
    "sma_period": [150, 200, 250],
    "adx_threshold": [20, 25],
    "vola_window": [20],
    "vola_threshold": [0.20, 0.25],
    "breadth_threshold": [50],
    "use_breadth": [True, False],
    "use_adx": [True, False],
    "use_vola": [True, False],
}

TRAILING_VBT_GRID = (0.15, 0.18, 0.20, 0.22, 0.25, 0.30)
TRAILING_VBT_TICKERS = ("SPY", "EFA", "EEM", "IWM")

FILTER_MAR = 1.0
FILTER_SHARPE = 0.5
FILTER_MIN_TRADES = 50

try:
    from google.colab import userdata
    EODHD_TOKEN = userdata.get("EODHD_API_KEY") or userdata.get("EODHD_TOKEN") or ""
except ImportError:
    EODHD_TOKEN = os.environ.get("EODHD_API_KEY", "")

EODHD_BASE = "https://eodhd.com/api/eod"
YF_TO_EODHD = {
    "SPY": "SPY.US", "EFA": "EFA.US", "EEM": "EEM.US",
    "MTUM": "MTUM.US", "VLUE": "VLUE.US", "QUAL": "QUAL.US",
    "USMV": "USMV.US", "IWM": "IWM.US",
    "SHY": "SHY.US", "IEF": "IEF.US", "TLT": "TLT.US", "BIL": "BIL.US",
}


# ── Daten ─────────────────────────────────────────────────────────────────────

def _fetch_eodhd_ohlcv(ticker: str, start: str = START_DATE, end: str = END_DATE) -> pd.DataFrame:
    sym = YF_TO_EODHD.get(ticker.upper(), f"{ticker}.US")
    if not EODHD_TOKEN:
        return pd.DataFrame()
    try:
        r = requests.get(
            f"{EODHD_BASE}/{sym}",
            params={"api_token": EODHD_TOKEN, "fmt": "json", "from": start, "to": end},
            timeout=45,
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
            "Open": df.get("open", close),
            "High": df.get("high", close),
            "Low": df.get("low", close),
            "Close": close.astype(float),
            "Volume": df.get("volume", 0).astype(float),
        })
        return out
    except Exception:
        return pd.DataFrame()


def _ensure_yfinance():
    try:
        import yfinance as yf  # noqa: F401
        return yf
    except ImportError:
        import subprocess
        import sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "yfinance"])
        import yfinance as yf
        return yf


def download_etf_ohlcv(ticker: str, start: str = START_DATE, end: str = END_DATE) -> pd.DataFrame:
    yf = _ensure_yfinance()
    raw = None
    try:
        raw = yf.download(
            ticker, start=start, end=end, auto_adjust=True,
            progress=False, threads=False,
        )
    except Exception:
        raw = None
    if raw is None or raw.empty:
        try:
            raw = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
        except Exception:
            raw = None
    if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
        eod = _fetch_eodhd_ohlcv(ticker, start=start, end=end)
        if not eod.empty:
            print(f"  📡 {ticker} via EODHD ({len(eod)} Zeilen)")
            return eod
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    raw = raw.rename(columns=str.title)
    need = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in raw.columns]
    if "Close" not in need:
        return pd.DataFrame()
    out = raw[need].copy()
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


def load_etf_ohlcv(ticker: str, start: str = START_DATE, force: bool = False) -> pd.DataFrame:
    ETF_PRICE_DIR.mkdir(parents=True, exist_ok=True)
    path = ETF_PRICE_DIR / f"{ticker}.csv"
    if path.is_file() and not force:
        try:
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
            if len(df) >= 252:
                return df.sort_index()
        except Exception:
            pass
    df = download_etf_ohlcv(ticker, start=start)
    if df.empty:
        df = _fetch_eodhd_ohlcv(ticker, start=start)
        if not df.empty:
            print(f"  📡 {ticker} via EODHD ({len(df)} Zeilen)")
    if not df.empty:
        df.index.name = "Date"
        df.to_csv(path)
    return df


def load_breadth_fraction() -> pd.Series:
    """Anteil S&P-Titel über SMA200 (0–1), aus bestehendem Breadth-Cache."""
    if not BREADTH_CACHE.is_file():
        print(f"  ⚠ Breadth-Cache fehlt: {BREADTH_CACHE}")
        print("     Einmal Kassandra_Regime.ipynb → kassandra_regime(2) ausführen.")
        return pd.Series(dtype=float)
    try:
        panel = pickle.loads(BREADTH_CACHE.read_bytes())
    except Exception as e:
        print(f"  ⚠ Breadth-Cache lesen fehlgeschlagen: {e}")
        return pd.Series(dtype=float)
    if not isinstance(panel, pd.DataFrame) or panel.empty:
        return pd.Series(dtype=float)
    above = pd.DataFrame(index=panel.index)
    for col in panel.columns:
        ma = panel[col].rolling(BREADTH_MA, min_periods=BREADTH_MA).mean()
        above[col] = (panel[col] > ma).astype(float)
    s = above.mean(axis=1)
    s.name = "breadth_frac"
    print(f"  📂 Breadth aus Cache ({len(panel.columns)} Titel)")
    return s


# ── Indikatoren (pandas) ──────────────────────────────────────────────────────

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def add_indicators(ohlcv: pd.DataFrame, sma_periods: tuple[int, ...] = (50, 200)) -> pd.DataFrame:
    df = ohlcv.copy()
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"].fillna(0)

    for p in sma_periods:
        df[f"SMA_{p}"] = c.rolling(p, min_periods=p).mean()

    ret = c.pct_change()
    for w in (10, 20, 30):
        df[f"HV_{w}"] = ret.rolling(w, min_periods=w).std() * np.sqrt(252)

    df["ADX_14"] = _adx(h, l, c, 14)
    df["RSI_14"] = _rsi(c, 14)
    df["ATR_14"] = _atr(h, l, c, 14)
    df["MOM_12M"] = c.pct_change(252)

    fi = (c - c.shift(1)) * v
    df["Force_1"] = fi
    df["Force_EMA13"] = fi.ewm(span=13, min_periods=13, adjust=False).mean()

    mid = c.rolling(20, min_periods=20).mean()
    std = c.rolling(20, min_periods=20).std()
    upper, lower = mid + 2 * std, mid - 2 * std
    df["BB_width"] = (upper - lower) / mid.replace(0, np.nan)

    return df


def build_feature_frame(
    ticker: str = "SPY",
    start: str = START_DATE,
    force_download: bool = False,
    sma_periods: tuple[int, ...] = (50, 100, 150, 200, 250),
) -> pd.DataFrame:
    ohlcv = load_etf_ohlcv(ticker, start=start, force=force_download)
    if ohlcv.empty:
        raise RuntimeError(f"Keine Kursdaten für {ticker}")
    df = add_indicators(ohlcv, sma_periods=sma_periods)
    breadth = load_breadth_fraction()
    if not breadth.empty:
        df["Pct_Above_SMA200"] = (breadth.reindex(df.index).ffill() * 100).astype(float)
        for w in (50, 100, 150):
            df[f"Breadth_SMA_{w}"] = df["Pct_Above_SMA200"].rolling(w, min_periods=1).mean()
    else:
        df["Pct_Above_SMA200"] = np.nan
    return df


# ── Ampel ─────────────────────────────────────────────────────────────────────

def kassandra_ampel(
    df: pd.DataFrame,
    *,
    sma_period: int = 200,
    adx_threshold: float = 20,
    vola_window: int = 20,
    vola_threshold: float = 0.25,
    breadth_threshold: float = 50,
    use_adx: bool = True,
    use_vola: bool = True,
    use_breadth: bool = True,
) -> pd.Series:
    """
    Grün 1.0 | Gelb 0.5 | Rot 0.0 (Cash).
    Trend: Close > SMA{sma_period} — sonst Rot.
    """
    sma_col = f"SMA_{sma_period}"
    if sma_col not in df.columns:
        raise KeyError(f"{sma_col} fehlt — build_feature_frame() mit passendem sma_period aufrufen")

    hv_col = f"HV_{vola_window}"
    close = df["Close"]
    trend_up = close > df[sma_col]
    red = ~trend_up

    green = trend_up.copy()
    if use_adx and "ADX_14" in df.columns:
        green &= df["ADX_14"] > adx_threshold
    if use_vola and hv_col in df.columns:
        green &= df[hv_col] < vola_threshold
    if use_breadth and "Pct_Above_SMA200" in df.columns:
        br = df["Pct_Above_SMA200"]
        if br.isna().all():
            green &= False
        else:
            green &= br > breadth_threshold

    yellow = ~green & ~red

    signal = pd.Series(0.0, index=df.index, name="ampel")
    signal.loc[green] = 1.0
    signal.loc[yellow] = 0.5
    return signal


# ── Backtest ──────────────────────────────────────────────────────────────────

def run_backtest(
    prices: pd.Series,
    weights: pd.Series,
    initial_capital: float = INITIAL_CAPITAL,
    tc: float = TOTAL_COST,
) -> pd.Series:
    idx = prices.index.intersection(weights.index)
    px = prices.reindex(idx).ffill()
    w = weights.reindex(idx).ffill().clip(0, 1)
    ret = px.pct_change().fillna(0)
    port_ret = w.shift(1).fillna(0) * ret
    turnover = w.diff().abs().fillna(0)
    port_ret -= turnover * tc
    equity = (1 + port_ret).cumprod() * initial_capital
    equity.name = "equity"
    return equity


def run_buy_hold(prices: pd.Series, initial_capital: float = INITIAL_CAPITAL) -> pd.Series:
    ret = prices.pct_change().fillna(0)
    return (1 + ret).cumprod() * initial_capital


def trailing_stop_weights(
    close: pd.Series,
    trail_pct: float,
    reentry_sma: pd.Series | None = None,
) -> pd.Series:
    """
    Ohne Ampel: start 100 % investiert.
    Ausstieg wenn Close <= Hoch seit Einstieg × (1 − trail_pct).
    Wieder ein wenn Close > SMA200 (falls reentry_sma gesetzt).
    """
    w = np.zeros(len(close), dtype=float)
    invested = True
    hwm = float(close.iloc[0])

    for i in range(len(close)):
        px = float(close.iloc[i])
        if invested:
            hwm = max(hwm, px)
            if px <= hwm * (1.0 - trail_pct):
                invested = False
                w[i] = 0.0
            else:
                w[i] = 1.0
        elif reentry_sma is not None:
            ma = reentry_sma.iloc[i]
            if not np.isnan(ma) and px > float(ma):
                invested = True
                hwm = px
                w[i] = 1.0
            else:
                w[i] = 0.0
        else:
            w[i] = 0.0

    return pd.Series(w, index=close.index, name="weight")


def count_stop_exits(weights: pd.Series) -> int:
    """Anzahl Ausstiege (1 → 0)."""
    prev = weights.shift(1).fillna(1.0)
    return int(((prev > 0.5) & (weights < 0.5)).sum())


def run_trailing_compare(
    ticker: str = "SPY",
    trail_pcts: tuple[float, ...] = (0.15, 0.20, 0.25),
    reentry_sma_period: int = 200,
) -> dict:
    """
  Vergleich: Buy & Hold (ohne Ampel, ohne TS) vs. reiner Trailing Stop.
    """
    print("═" * 70)
    print(f"  TRAILING STOP — {ticker}  (ohne Ampel, v{RESEARCH_VERSION})")
    print("═" * 70)
    print(f"  Regel: 100 % investiert · TS vom Hoch · Re-Entry Close > SMA{reentry_sma_period}")

    periods = tuple({50, 100, 150, 200, 250, reentry_sma_period})
    df = build_feature_frame(ticker, sma_periods=periods)
    close = df["Close"]
    sma_col = f"SMA_{reentry_sma_period}"
    reentry_sma = df[sma_col] if sma_col in df.columns else None

    rows = []
    equities: dict[str, pd.Series] = {}
    weights_map: dict[str, pd.Series] = {}

    bh_w = pd.Series(1.0, index=close.index, name="weight")
    bh_eq = run_backtest(close, bh_w)
    bh_m = compute_metrics(bh_eq, bh_w)
    bh_m.update({"label": "Buy & Hold (ohne TS)", "trail_pct": None, "stop_exits": 0})
    rows.append(bh_m)
    equities["Buy & Hold"] = bh_eq
    weights_map["Buy & Hold"] = bh_w

    for tp in trail_pcts:
        label = f"TS {int(tp * 100)} %"
        w = trailing_stop_weights(close, tp, reentry_sma=reentry_sma)
        eq = run_backtest(close, w)
        m = compute_metrics(eq, w)
        m.update({
            "label": label,
            "trail_pct": tp,
            "stop_exits": count_stop_exits(w),
        })
        rows.append(m)
        equities[label] = eq
        weights_map[label] = w

    table = pd.DataFrame(rows)
    cols = ["label", "trail_pct", "cagr", "sharpe", "mar", "maxdd", "trades", "stop_exits",
            "avg_hold_days", "avg_invest_pct", "end_value"]
    cols = [c for c in cols if c in table.columns]

    print("\n" + "═" * 88)
    print(f"  ERGEBNIS — {ticker}")
    print("═" * 88)
    print(table[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("═" * 88)

    return {
        "ticker": ticker,
        "table": table,
        "equities": equities,
        "weights": weights_map,
        "close": close,
    }


def plot_trailing_compare(ts_result: dict, title: str | None = None):
    """Equity-Kurven für run_trailing_compare (matplotlib)."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5))
    for label, eq in ts_result["equities"].items():
        lw = 2.0 if label == "Buy & Hold" else 1.3
        alpha = 1.0 if label in ("Buy & Hold",) else 0.85
        eq.plot(ax=ax, label=label, linewidth=lw, alpha=alpha)
    t = title or ts_result.get("ticker", "SPY")
    ax.set_title(f"{t} — ohne Ampel · Buy & Hold vs. Trailing Stop")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    return fig, ax


def count_trades(weights: pd.Series, threshold: float = 0.05) -> int:
    return int((weights.diff().abs().fillna(0) > threshold).sum())


def avg_holding_days(weights: pd.Series) -> float:
    w = (weights.fillna(0) > 0).astype(int)
    runs: list[int] = []
    in_pos, start = False, None
    for dt, v in w.items():
        if v and not in_pos:
            in_pos, start = True, dt
        elif not v and in_pos and start is not None:
            runs.append((dt - start).days)
            in_pos, start = False, None
    if in_pos and start is not None:
        runs.append((w.index[-1] - start).days)
    return float(np.mean(runs)) if runs else 0.0


def compute_metrics(equity: pd.Series, weights: pd.Series | None = None) -> dict:
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
    out = {
        "cagr": round(cagr, 4),
        "maxdd": round(maxdd, 4),
        "sharpe": round(sharpe, 3),
        "mar": round(mar, 3),
        "end_value": round(float(eq.iloc[-1]), 0),
    }
    if weights is not None:
        out["trades"] = count_trades(weights)
        out["avg_hold_days"] = round(avg_holding_days(weights), 1)
        out["avg_invest_pct"] = round(float(weights.mean()) * 100, 1)
    return out


# ── Grid & Auswertung ─────────────────────────────────────────────────────────

def _grid_combos(grid: dict | None = None) -> list[dict]:
    g = grid or PHASE_A_GRID
    keys = list(g.keys())
    return [dict(zip(keys, vals)) for vals in itertools.product(*(g[k] for k in keys))]


def _rank_score(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    cagr_r = df["cagr"].rank(ascending=False, pct=True)
    sharpe_r = df["sharpe"].rank(ascending=False, pct=True)
    mar_r = df["mar"].rank(ascending=False, pct=True)
    return 0.3 * cagr_r + 0.4 * sharpe_r + 0.3 * mar_r


def run_single_backtest(
    ticker: str = "SPY",
    params: dict | None = None,
    df: pd.DataFrame | None = None,
) -> tuple[pd.Series, pd.Series, dict]:
    p = {
        "sma_period": 200,
        "adx_threshold": 20,
        "vola_window": 20,
        "vola_threshold": 0.25,
        "breadth_threshold": 50,
        "use_adx": True,
        "use_vola": True,
        "use_breadth": True,
    }
    if params:
        p.update(params)
    if df is None:
        periods = tuple({50, 100, 150, 200, 250, p["sma_period"]})
        df = build_feature_frame(ticker, sma_periods=periods)
    signal = kassandra_ampel(df, **p)
    eq = run_backtest(df["Close"], signal)
    m = compute_metrics(eq, signal)
    m.update(p)
    m["ticker"] = ticker
    return eq, signal, m


def run_grid(
    ticker: str = "SPY",
    grid: dict | None = None,
    save: bool = True,
) -> pd.DataFrame:
    periods = tuple({50, 100, 150, 200, 250} | set((grid or PHASE_A_GRID).get("sma_period", [])))
    print(f"  ⏳ Feature-Frame {ticker} …")
    df = build_feature_frame(ticker, sma_periods=periods)
    bh = compute_metrics(run_buy_hold(df["Close"]))
    combos = _grid_combos(grid)
    print(f"  🔍 Grid: {len(combos)} Kombinationen auf {ticker}")

    rows = []
    for i, params in enumerate(combos, 1):
        try:
            signal = kassandra_ampel(df, **params)
        except KeyError:
            continue
        eq = run_backtest(df["Close"], signal)
        m = compute_metrics(eq, signal)
        rows.append({**params, **m, "bh_cagr": bh.get("cagr"), "bh_mar": bh.get("mar")})
        if i % 24 == 0 or i == len(combos):
            print(f"     [{i}/{len(combos)}]", flush=True)

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    result["delta_mar"] = result["mar"] - bh.get("mar", 0)
    result["score"] = _rank_score(result)

    filt = result[
        (result["mar"] > FILTER_MAR)
        & (result["sharpe"] > FILTER_SHARPE)
        & (result["trades"] >= FILTER_MIN_TRADES)
    ].copy()
    result = result.sort_values(["score", "mar"], ascending=False)
    filt = filt.sort_values(["score", "mar"], ascending=False)

    print(f"\n  Buy & Hold {ticker}: CAGR {bh.get('cagr', 0):.1%}  MAR {bh.get('mar', 0):.2f}")
    print(f"  Nach Filter (MAR>{FILTER_MAR}, Sharpe>{FILTER_SHARPE}, Trades≥{FILTER_MIN_TRADES}): "
          f"{len(filt)}/{len(result)}")

    _print_results(filt.head(15) if not filt.empty else result.head(15), f"TOP — {ticker}")

    if save:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        result.to_csv(GRID_RESULT_CSV, index=False)
        print(f"  💾 {GRID_RESULT_CSV}")

    return result


def _print_results(df: pd.DataFrame, title: str):
    print("\n" + "═" * 96)
    print(f"  {title}")
    print("═" * 96)
    if df.empty:
        print("  ⚠ Keine Ergebnisse.")
        return
    cols = [
        "sma_period", "adx_threshold", "vola_threshold",
        "use_adx", "use_vola", "use_breadth",
        "cagr", "sharpe", "mar", "maxdd", "trades", "avg_hold_days", "delta_mar", "score",
    ]
    cols = [c for c in cols if c in df.columns]
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("═" * 96)


def run_phase_a(ticker: str = "SPY", force_download: bool = False):
    """Phase A: Daten laden, Demo-Backtest, Grid auf SPY."""
    print("═" * 70)
    print(f"  KASSANDRA AMPEL — Phase A  (v{RESEARCH_VERSION})")
    print("═" * 70)
    print(f"  Ordner: {NOTEBOOK_DIR}")
    print(f"  Kurs-CSV: {ETF_PRICE_DIR}")

    if force_download:
        load_etf_ohlcv(ticker, force=True)

    print("\n── 1) Demo-Backtest (Standard-Parameter) ──")
    eq, sig, m = run_single_backtest(ticker)
    bh_m = compute_metrics(run_buy_hold(
        build_feature_frame(ticker)["Close"],
    ))
    print(f"  Strategie: CAGR {m['cagr']:.1%}  Sharpe {m['sharpe']:.2f}  MAR {m['mar']:.2f}  "
          f"Trades {m.get('trades', 0)}  Ø Invest {m.get('avg_invest_pct', 0):.0f}%")
    print(f"  Buy&Hold:  CAGR {bh_m['cagr']:.1%}  MAR {bh_m['mar']:.2f}")

    print("\n── 2) Grid-Suche ──")
    grid_df = run_grid(ticker=ticker)

    print("\n── 3) Nächste Schritte ──")
    print("  · Phase B: run_phase_b() — vectorbt Grid + Heatmaps")
    print("  · Walk-Forward 6J/1J (Phase C)")
    print("═" * 70)
    return {"demo": m, "bh": bh_m, "grid": grid_df, "equity": eq, "signal": sig}


# ── Phase B: vectorbt ───────────────────────────────────────────────────────────

def _ensure_vectorbt():
    try:
        import vectorbt as vbt
        return vbt
    except ImportError:
        import subprocess
        import sys
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "vectorbt"],
        )
        import vectorbt as vbt
        return vbt


def _batch_equity_numpy(close: pd.Series, weights_df: pd.DataFrame) -> pd.DataFrame:
    """Vektorisierter Multi-Strategie-Backtest (NumPy, identisch zu run_backtest)."""
    r = close.pct_change().fillna(0).to_numpy()
    w = weights_df.fillna(0).to_numpy(dtype=float)
    w_lag = np.empty_like(w)
    w_lag[0] = 0.0
    w_lag[1:] = w[:-1]
    turn = np.abs(np.diff(w, axis=0, prepend=w[:1]))
    port_r = w_lag * r[:, None] - turn * TOTAL_COST
    eq = INITIAL_CAPITAL * np.cumprod(1.0 + port_r, axis=0)
    return pd.DataFrame(eq, index=close.index, columns=weights_df.columns)


def _vbt_batch_equity(close: pd.Series, weights_df: pd.DataFrame) -> pd.DataFrame:
    """vectorbt Portfolio je Spalte; Fallback NumPy bei Fehler."""
    try:
        vbt = _ensure_vectorbt()
        w = weights_df.shift(1).fillna(0).astype(float)
        pf = vbt.Portfolio.from_orders(
            close,
            size=w,
            size_type="targetpercent",
            fees=TOTAL_COST,
            init_cash=INITIAL_CAPITAL,
            freq="1D",
        )
        val = pf.value()
        if isinstance(val, pd.Series):
            return val.to_frame(weights_df.columns[0])
        return val
    except Exception as exc:
        print(f"  ⚠ vectorbt Fallback NumPy: {exc}")
        return _batch_equity_numpy(close, weights_df)


def _metrics_table(
    close: pd.Series,
    weights_df: pd.DataFrame,
    meta_df: pd.DataFrame,
) -> pd.DataFrame:
    eq_df = _vbt_batch_equity(close, weights_df)
    rows = []
    for col in weights_df.columns:
        m = compute_metrics(eq_df[col], weights_df[col])
        m["combo_id"] = col
        rows.append(m)
    stats = pd.DataFrame(rows)
    out = meta_df.merge(stats, on="combo_id", how="inner")
    bh_eq = run_backtest(close, pd.Series(1.0, index=close.index))
    bh_mar = compute_metrics(bh_eq).get("mar", 0)
    if "mar" in out.columns:
        out["delta_mar"] = out["mar"] - bh_mar
        out["score"] = _rank_score(out)
    return out.sort_values(["score", "mar"], ascending=False, na_position="last")


def plot_param_heatmap(
    df: pd.DataFrame,
    x: str,
    y: str,
    metric: str = "mar",
    title: str = "",
    filter_eq: dict | None = None,
):
    """Heatmap für Grid-Ergebnisse (matplotlib)."""
    import matplotlib.pyplot as plt

    sub = df.copy()
    if filter_eq:
        for k, v in filter_eq.items():
            if k in sub.columns:
                sub = sub[sub[k] == v]
    if sub.empty or x not in sub.columns or y not in sub.columns:
        print("  ⚠ Heatmap: keine Daten")
        return None, None
    pivot = sub.pivot_table(index=y, columns=x, values=metric, aggfunc="mean")
    fig, ax = plt.subplots(figsize=(max(6, len(pivot.columns) * 0.9), max(4, len(pivot) * 0.5)))
    im = ax.imshow(pivot.values, aspect="auto", cmap="RdYlGn")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([str(c) for c in pivot.columns], rotation=45, ha="right")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([str(i) for i in pivot.index])
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    fig.colorbar(im, ax=ax, label=metric)
    ax.set_title(title or f"{metric} — {y} × {x}")
    plt.tight_layout()
    return fig, ax


def run_vectorbt_ampel_grid(
    ticker: str = "SPY",
    grid: dict | None = None,
    save: bool = True,
) -> dict:
    """Ampel-Parameter-Grid via vectorbt (alle Kombis parallel)."""
    print("═" * 70)
    print(f"  vectorbt AMPEL-GRID — {ticker}  (v{RESEARCH_VERSION})")
    print("═" * 70)

    periods = tuple({50, 100, 150, 200, 250} | set((grid or PHASE_A_GRID).get("sma_period", [])))
    df = build_feature_frame(ticker, sma_periods=periods)
    close = df["Close"]
    combos = _grid_combos(grid)

    weights: dict[str, pd.Series] = {}
    meta_rows: list[dict] = []
    for i, params in enumerate(combos):
        try:
            sig = kassandra_ampel(df, **params)
        except KeyError:
            continue
        cid = f"c{i:03d}"
        weights[cid] = sig
        meta_rows.append({**params, "combo_id": cid, "ticker": ticker})

    if not weights:
        return {"ticker": ticker, "results": pd.DataFrame()}

    weights_df = pd.DataFrame(weights)
    meta_df = pd.DataFrame(meta_rows)
    print(f"  🔍 {len(weights_df.columns)} Kombinationen · vectorbt …")
    results = _metrics_table(close, weights_df, meta_df)

    filt = results[
        (results["mar"] > FILTER_MAR)
        & (results["sharpe"] > FILTER_SHARPE)
        & (results["trades"] >= FILTER_MIN_TRADES)
    ]
    print(f"  Filter bestanden: {len(filt)}/{len(results)}")
    _print_results(filt.head(12) if not filt.empty else results.head(12), f"VBT AMPEL TOP — {ticker}")

    if save:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        results.to_csv(VBT_AMPEL_CSV, index=False)
        print(f"  💾 {VBT_AMPEL_CSV}")

    return {
        "ticker": ticker,
        "results": results,
        "weights": weights_df,
        "close": close,
    }


def run_vectorbt_trailing_grid(
    tickers: tuple[str, ...] = TRAILING_VBT_TICKERS,
    trail_pcts: tuple[float, ...] = TRAILING_VBT_GRID,
    reentry_sma_period: int = 200,
    save: bool = True,
) -> dict:
    """Trailing-Stop-Grid (ohne Ampel) für mehrere ETFs via vectorbt."""
    print("═" * 70)
    print(f"  vectorbt TRAILING-GRID  (v{RESEARCH_VERSION})")
    print("═" * 70)
    print(f"  ETFs: {', '.join(tickers)} · TS: {[int(t * 100) for t in trail_pcts]} %")

    all_weights: dict[str, pd.Series] = {}
    meta_rows: list[dict] = []
    close_map: dict[str, pd.Series] = {}

    for ticker in tickers:
        periods = tuple({50, 100, 150, 200, 250, reentry_sma_period})
        try:
            df = build_feature_frame(ticker, sma_periods=periods)
        except RuntimeError as e:
            print(f"  ⚠ {ticker}: {e}")
            continue
        close = df["Close"]
        close_map[ticker] = close
        sma_col = f"SMA_{reentry_sma_period}"
        reentry_sma = df[sma_col] if sma_col in df.columns else None

        cid_bh = f"{ticker}_BH"
        all_weights[cid_bh] = pd.Series(1.0, index=close.index)
        meta_rows.append({
            "combo_id": cid_bh,
            "ticker": ticker,
            "trail_pct": None,
            "label": "Buy & Hold",
        })

        for tp in trail_pcts:
            cid = f"{ticker}_TS{int(tp * 100)}"
            all_weights[cid] = trailing_stop_weights(close, tp, reentry_sma=reentry_sma)
            meta_rows.append({
                "combo_id": cid,
                "ticker": ticker,
                "trail_pct": tp,
                "label": f"TS {int(tp * 100)}%",
            })

    if not all_weights:
        return {"results": pd.DataFrame()}

    # Pro Ticker separat backtesten (unterschiedliche Datumsindizes)
    result_parts: list[pd.DataFrame] = []
    meta_df = pd.DataFrame(meta_rows)
    for ticker in tickers:
        sub_meta = meta_df[meta_df["ticker"] == ticker]
        if sub_meta.empty or ticker not in close_map:
            continue
        cols = sub_meta["combo_id"].tolist()
        wdf = pd.DataFrame({c: all_weights[c] for c in cols})
        part = _metrics_table(close_map[ticker], wdf, sub_meta)
        result_parts.append(part)

    results = pd.concat(result_parts, ignore_index=True)
    results = results.sort_values(["ticker", "mar"], ascending=[True, False])

    print("\n" + "═" * 96)
    print("  VBT TRAILING — alle ETFs")
    print("═" * 96)
    cols = ["ticker", "label", "trail_pct", "cagr", "sharpe", "mar", "maxdd",
            "stop_exits", "trades", "avg_invest_pct", "end_value"]
    cols = [c for c in cols if c in results.columns]
    if "stop_exits" not in results.columns and "combo_id" in results.columns:
        for i, row in results.iterrows():
            cid = row["combo_id"]
            if cid in all_weights:
                results.at[i, "stop_exits"] = count_stop_exits(all_weights[cid])
    cols = [c for c in cols if c in results.columns]
    print(results[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("═" * 96)

    if save:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        results.to_csv(VBT_TRAIL_CSV, index=False)
        print(f"  💾 {VBT_TRAIL_CSV}")

    return {
        "results": results,
        "weights": all_weights,
        "close": close_map,
    }


def run_vectorbt_grid(
    ticker: str = "SPY",
    ampel_grid: dict | None = None,
    trail_tickers: tuple[str, ...] = TRAILING_VBT_TICKERS,
) -> dict:
    """Phase B Kurzform: Ampel-Grid + Trailing-Multi-ETF."""
    amp = run_vectorbt_ampel_grid(ticker, grid=ampel_grid)
    trail = run_vectorbt_trailing_grid(tickers=trail_tickers)
    return {"ampel": amp, "trailing": trail}


def run_phase_b(
    ticker: str = "SPY",
    trail_tickers: tuple[str, ...] = TRAILING_VBT_TICKERS,
):
    """Phase B: vectorbt Grids + Heatmap-Hinweise."""
    print("═" * 70)
    print(f"  KASSANDRA AMPEL — Phase B vectorbt  (v{RESEARCH_VERSION})")
    print("═" * 70)

    out = run_vectorbt_grid(ticker=ticker, trail_tickers=trail_tickers)

    amp_res = out["ampel"]["results"]
    if not amp_res.empty:
        print("\n── Heatmap Ampel: MAR nach sma_period × vola_threshold ──")
        print("  (nur use_adx=True, use_vola=True, use_breadth=False)")
        fig, _ = plot_param_heatmap(
            amp_res,
            x="vola_threshold",
            y="sma_period",
            metric="mar",
            title=f"{ticker} Ampel MAR",
            filter_eq={"use_adx": True, "use_vola": True, "use_breadth": False},
        )
        out["ampel_heatmap"] = (fig, _)

    trail_res = out["trailing"]["results"]
    if not trail_res.empty and "trail_pct" in trail_res.columns:
        sub = trail_res[trail_res["trail_pct"].notna()]
        if not sub.empty:
            print("\n── Heatmap Trailing: MAR nach ETF × TS % ──")
            fig2, _ = plot_param_heatmap(
                sub,
                x="trail_pct",
                y="ticker",
                metric="mar",
                title="Trailing Stop MAR (ohne Ampel)",
            )
            out["trail_heatmap"] = (fig2, _)

    print("\n── Fertig — CSV in regime_cache/ ──")
    print("═" * 70)
    return out


def prefetch_etfs(tickers: tuple[str, ...] = DEFAULT_ETFS):
    """Optional: ETF-Kurse in CSV-Cache laden."""
    for tk in tickers:
        print(f"  ⬇ {tk} …", flush=True)
        load_etf_ohlcv(tk)
    print(f"  ✅ {len(tickers)} ETFs im Cache")
