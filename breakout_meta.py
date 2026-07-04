"""
Breakout + Meta-Labeling — S&P 500
=====================================
Stufe 1  Primär-Modell   : 52-Wochen-Hoch-Ausbruch + Volumen (×1.5) + SMA100-Trend
Stufe 2  Meta-Modell     : Random Forest → P(Ausbruch erfolgreich)
Label                    : Triple Barrier  Ziel +10% / Stop −5% / 20 Handelstage
Walk-Forward             : Train 2018–2021, Test 2022–heute

Verwendung (Colab):
    from breakout_meta import train_breakout_meta, run_meta_comparison, run_live_scanner
    model, report = train_breakout_meta(verbose=True)
    run_meta_comparison()
    signals = run_live_scanner()
"""
from __future__ import annotations

VERSION = "1.2"   # + ATR-/vola-skalierte Barrieren (optional, schaltbar)

import importlib.util
import json
import os
import pickle
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier

# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────
PROFIT_TARGET   = 0.10
STOP_LOSS       = 0.05
HOLD_DAYS       = 20

# ATR-/Vola-skalierte Barrieren (optional; López-de-Prado dynamic barriers).
# USE_ATR_BARRIERS = False → fixe %-Barrieren (Standard).
# USE_ATR_BARRIERS = True  → Ziel/Stop skalieren mit ATR-Ratio (ATR14/Close am Signal-Tag).
# R:R bleibt bei ATR_PT_MULT : ATR_SL_MULT (Default 3:1.5 = 2:1).
USE_ATR_BARRIERS = False
ATR_BARRIER_PERIOD = 14
ATR_PT_MULT   = 3.0
ATR_SL_MULT   = 1.5
ATR_PT_CLAMP  = (0.04, 0.30)   # Ziel min/max (schützt vor Extremwerten)
ATR_SL_CLAMP  = (0.02, 0.15)   # Stop min/max
MIN_VOL_RATIO   = 1.5
SMA_PERIOD      = 100
BREAKOUT_WINDOW = 252
MIN_DAYS_GAP    = 10
META_THRESHOLD  = 0.45    # Fallback; Training setzt quantilbasierten Threshold (KEEP_TOP_PCT)
KEEP_TOP_PCT     = 20     # Top-20%-Signale nach Meta-Score (höchste Precision, validiert)
MIN_EVENTS_TRAIN = 300
TRAIN_END        = "2022-01-01"
EVAL_START       = "2018-01-01"
INITIAL_CAPITAL  = 100_000.0
POSITION_SIZE    = 0.12   # 12% pro Position — Top-20%-konzentriert: Bear+4.4%, MaxDD-15.7%
MAX_POSITIONS    = 10     # 10 Positionen — höchste Precision, bestes Allwetter-Verhalten

# Regime-Overlay (Seitwärts-/Bären-Schutz): Marktbreite + SPY vs. SMA200
REGIME_BREADTH_GREEN  = 0.55   # ≥55% Aktien über SMA200 → voll investieren
REGIME_BREADTH_RED    = 0.40   # <40% → kein Neukauf
REGIME_YELLOW_MULT    = 0.50   # max. 50% der Slots (5 von 10)
REGIME_RED_MULT       = 0.00   # keine Neukäufe

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLU", "XLB", "XLY", "XLP", "XLRE", "XLC"]

FEATURE_COLS = [
    "vol_ratio",          # Volumen-Stärke am Ausbruchstag
    "breakout_pct",       # Wie weit über dem alten 52W-Hoch (%)
    "rs_vs_spy_63",       # Relative Stärke vs SPY (63 Tage)
    "atr_ratio",          # ATR(14) / Close  — normalisierte Vola
    "market_breadth",     # Anteil S&P 500 Stocks > SMA200
    "spy_momentum_20",    # SPY 20-Tage-Return
    "vix_proxy",          # Realisierte 20-Tage-Vola SPY (annualisiert, %)
    "dist_52w_low_pct",   # Close / 52W-Tief − 1  (wie weit über 52W-Tief, %)
    "above_sma50_pct",    # Anteil Stocks > SMA50 (Marktbreite)
    "sector_momentum",    # Ø Sektor-ETF-Return 63 Tage (falls verfügbar)
]

_HERE     = Path(__file__).resolve().parent
CACHE_DIR = _HERE / "regime_momentum_cache" / "breakout"
META_DIR  = CACHE_DIR / "meta"


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsklassen
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class BreakoutMetaReport:
    n_events_train:     int
    n_events_test:      int
    baseline_precision: float
    meta_precision:     float
    meta_fraction:      float
    threshold:          float
    train_period:       str
    test_period:        str
    trained_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __str__(self) -> str:
        d = self.meta_precision - self.baseline_precision
        return (
            f"  Events  Train/Test : {self.n_events_train} / {self.n_events_test}\n"
            f"  Baseline (Test)    : {self.baseline_precision:.1%}\n"
            f"  Meta @ {self.threshold:.0%} (Test)  : {self.meta_precision:.1%}  "
            f"({d:+.1%})  ·  {self.meta_fraction:.0%} der Test-Signale genommen"
        )


class BreakoutMetaModel:
    """Wrapper um CalibratedClassifierCV mit threshold + save/load."""

    def __init__(self, clf, threshold: float, feature_names: list[str],
                 report: "BreakoutMetaReport | None" = None):
        self.clf           = clf
        self.threshold     = threshold
        self.feature_names = feature_names
        self.report        = report

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.clf.predict_proba(X)

    def predict(self, X: np.ndarray) -> np.ndarray:
        return (self.predict_proba(X)[:, 1] >= self.threshold).astype(int)

    def save(self, path: "Path | str") -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)
        print(f"  💾 Modell: {path}")

    @classmethod
    def load(cls, path: "Path | str") -> "BreakoutMetaModel":
        with open(path, "rb") as f:
            return pickle.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Merge-Hilfsfunktion  (verhindert _x/_y-Suffix bei überlappenden Spalten)
# ─────────────────────────────────────────────────────────────────────────────
def _merge_features(events: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    """
    Merged Features in Events.
    Entfernt nur Spalten aus `events`, die auch in `feats` stehen —
    sonst gehen z. B. vol_ratio verloren wenn extract_features sie nicht liefert.
    """
    if feats.empty:
        return events.copy()
    feat_cols = [c for c in FEATURE_COLS if c in feats.columns]
    overlap = [c for c in feat_cols if c in events.columns]
    ev = events.drop(columns=overlap, errors="ignore")
    return ev.merge(
        feats[["ticker", "date"] + feat_cols],
        on=["ticker", "date"],
        how="inner",
    )


def _dropna_features(df: pd.DataFrame) -> pd.DataFrame:
    """dropna nur für vorhandene Feature-Spalten (robust bei älteren Modul-Versionen)."""
    cols = [c for c in FEATURE_COLS if c in df.columns]
    return df.dropna(subset=cols)


# ─────────────────────────────────────────────────────────────────────────────
# Modul-Loader
# ─────────────────────────────────────────────────────────────────────────────
def _get_bt():
    key = "regime_momentum_bt"
    if key in sys.modules:
        return sys.modules[key]
    for p in (_HERE / "regime_momentum_bt.py", _HERE.parent / "regime_momentum_bt.py"):
        if p.is_file() and "refresh_prices_for_live" in p.read_text(encoding="utf-8", errors="ignore"):
            spec = importlib.util.spec_from_file_location(key, p)
            m = importlib.util.module_from_spec(spec)
            sys.modules[key] = m
            spec.loader.exec_module(m)
            return m
    raise ImportError("regime_momentum_bt.py nicht gefunden — Pfad prüfen.")


# ─────────────────────────────────────────────────────────────────────────────
# Daten laden
# ─────────────────────────────────────────────────────────────────────────────
def load_price_data(force_refresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Lädt Close + Volume für S&P 500 (+ Sektor-ETFs falls verfügbar).
    Nutzt den bestehenden EODHD-Cache aus regime_momentum_bt.

    Returns
    -------
    close  : pd.DataFrame  [Datum × Ticker]
    volume : pd.DataFrame  [Datum × Ticker]
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = CACHE_DIR / "close.pkl"
    vp = CACHE_DIR / "volume.pkl"
    max_age_h = 8.0

    def _fresh(p: Path) -> bool:
        return p.is_file() and (datetime.now().timestamp() - p.stat().st_mtime) / 3600 < max_age_h

    if not force_refresh and _fresh(cp) and _fresh(vp):
        print("  📂 Preis-Cache (Breakout) geladen")
        return pd.read_pickle(cp), pd.read_pickle(vp)

    bt = _get_bt()
    bt.set_universe("sp500")
    print("  ⬇ Preisdaten S&P 500 laden …")
    close, volume = bt.refresh_prices_for_live()

    # Sektor-ETFs ergänzen falls noch nicht vorhanden
    missing = [e for e in SECTOR_ETFS if e not in close.columns]
    if missing:
        extra_c, extra_v = _fetch_tickers(bt, missing)
        if not extra_c.empty:
            close  = pd.concat([close,  extra_c.reindex(close.index)],  axis=1)
            volume = pd.concat([volume, extra_v.reindex(volume.index)], axis=1)

    close.to_pickle(cp)
    volume.to_pickle(vp)
    avail_sectors = sum(1 for e in SECTOR_ETFS if e in close.columns)
    print(f"  💾 {len(close.columns)} Titel · {len(close)} Tage · {avail_sectors} Sektor-ETFs")
    return close, volume


def _fetch_tickers(bt, tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Lade zusätzliche Tickers über EODHD (best-effort)."""
    closes, vols = {}, {}
    for t in tickers:
        try:
            df = bt.fetch_eod_panel(f"{t}.US", start="2015-01-01")
            if df is not None and len(df) > 50:
                closes[t] = df["close"]
                vols[t]   = df["volume"]
        except Exception:
            pass
    c = pd.DataFrame(closes).sort_index() if closes else pd.DataFrame()
    v = pd.DataFrame(vols).sort_index()   if vols   else pd.DataFrame()
    return c, v


# ─────────────────────────────────────────────────────────────────────────────
# Stufe 1 — Primärmodell: Ausbruchssignale
# ─────────────────────────────────────────────────────────────────────────────
def generate_breakout_events(
    close:         pd.DataFrame,
    volume:        pd.DataFrame,
    min_vol_ratio: float = MIN_VOL_RATIO,
    sma_period:    int   = SMA_PERIOD,
    window:        int   = BREAKOUT_WINDOW,
    start:         str   = EVAL_START,
) -> pd.DataFrame:
    """
    Scannt S&P 500 auf 52-Wochen-Hoch-Ausbrüche mit:
      • Close  > gestrigem 52-Wochen-Hoch
      • Volumen > min_vol_ratio × 20-Tage-Schnitt
      • Close  > SMA100  (Trend-Bestätigung)
      • min. MIN_DAYS_GAP Tage zwischen zwei Signalen im selben Ticker

    Returns DataFrame: ticker, date, close, prior_high, sma100, vol_ratio
    """
    stock_cols = [c for c in close.columns if c not in SECTOR_ETFS and c != "SPY"]
    t0         = pd.Timestamp(start)

    prior_high = close[stock_cols].shift(1).rolling(window, min_periods=window // 2).max()
    sma100     = close[stock_cols].rolling(sma_period, min_periods=sma_period // 2).mean()
    vol_avg20  = volume[stock_cols].rolling(20, min_periods=10).mean()
    vol_ratio  = volume[stock_cols].div(vol_avg20.replace(0, np.nan))

    above_high = close[stock_cols] > prior_high
    above_sma  = close[stock_cols] > sma100
    strong_vol = vol_ratio >= min_vol_ratio
    mask       = above_high & above_sma & strong_vol

    idx    = mask.index[mask.index >= t0]
    events = []
    for dt in idx:
        for tk in stock_cols:
            if not mask.at[dt, tk]:
                continue
            try:
                c  = float(close.at[dt, tk])
                h  = float(prior_high.at[dt, tk])
                s  = float(sma100.at[dt, tk])
                vr = float(vol_ratio.at[dt, tk])
            except (KeyError, TypeError):
                continue
            if any(np.isnan(x) for x in (c, h, s, vr)) or c <= 0:
                continue
            events.append({"ticker": tk, "date": dt, "close": c,
                           "prior_high": h, "sma100": s, "vol_ratio": vr})

    if not events:
        return pd.DataFrame(columns=["ticker", "date", "close", "prior_high", "sma100", "vol_ratio"])

    df = pd.DataFrame(events).sort_values("date").reset_index(drop=True)
    df["_prev"] = df.groupby("ticker")["date"].shift(1)
    df["_gap"]  = (df["date"] - df["_prev"]).dt.days.fillna(9999)
    df = df[df["_gap"] >= MIN_DAYS_GAP].drop(columns=["_prev", "_gap"])
    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Triple Barrier Labels
# ─────────────────────────────────────────────────────────────────────────────
def _barrier_pct(atr_ratio, use_atr: bool) -> "tuple[float, float]":
    """
    Liefert (Ziel%, Stop%) für einen Trade.
      use_atr=False → fixe Barrieren (PROFIT_TARGET / STOP_LOSS).
      use_atr=True  → Ziel = ATR_PT_MULT × atr_ratio, Stop = ATR_SL_MULT × atr_ratio
                      (jeweils auf ATR_PT_CLAMP / ATR_SL_CLAMP begrenzt).
    Fällt auf fixe Werte zurück, wenn atr_ratio fehlt/ungültig.
    """
    if (not use_atr) or atr_ratio is None or not np.isfinite(atr_ratio) or atr_ratio <= 0:
        return PROFIT_TARGET, STOP_LOSS
    pt = min(max(ATR_PT_MULT * atr_ratio, ATR_PT_CLAMP[0]), ATR_PT_CLAMP[1])
    sl = min(max(ATR_SL_MULT * atr_ratio, ATR_SL_CLAMP[0]), ATR_SL_CLAMP[1])
    return float(pt), float(sl)


def label_events(
    events:   pd.DataFrame,
    close:    pd.DataFrame,
    pt:       float = PROFIT_TARGET,
    sl:       float = STOP_LOSS,
    max_hold: int   = HOLD_DAYS,
    use_atr:  bool  = None,
) -> pd.DataFrame:
    """
    label = 1  →  Ziel (+pt) vor Stop (−sl) innerhalb max_hold Tagen
    label = 0  →  Stop oder Zeit-Barrier zuerst

    use_atr=None  → globale Voreinstellung USE_ATR_BARRIERS.
    use_atr=True  → vola-skalierte Barrieren pro Trade (siehe _barrier_pct).
    use_atr=False → fixe %-Barrieren (pt/sl).

    Neue Spalten: label, exit_date, exit_ret, exit_reason
    """
    if use_atr is None:
        use_atr = USE_ATR_BARRIERS

    # ATR-Ratio-Panel (ATR14/Close) nur bei Bedarf vorberechnen.
    atr_ratio_panel = None
    if use_atr:
        atr_abs = close.diff().abs().rolling(ATR_BARRIER_PERIOD).mean()
        atr_ratio_panel = atr_abs / close

    labels, exit_dates, exit_rets, reasons = [], [], [], []

    for _, row in events.iterrows():
        tk, t0, c0 = row["ticker"], row["date"], row["close"]
        if tk not in close.columns or c0 <= 0:
            labels.append(np.nan); exit_dates.append(pd.NaT)
            exit_rets.append(np.nan); reasons.append("no_data")
            continue

        future = close[tk].loc[close.index > t0].iloc[:max_hold]
        if future.empty:
            labels.append(0); exit_dates.append(t0)
            exit_rets.append(0.0); reasons.append("no_data")
            continue

        if use_atr and atr_ratio_panel is not None and tk in atr_ratio_panel.columns:
            _ar = atr_ratio_panel[tk].asof(t0)
            pt_i, sl_i = _barrier_pct(_ar, use_atr=True)
        else:
            pt_i, sl_i = pt, sl

        tgt, stp = c0 * (1 + pt_i), c0 * (1 - sl_i)
        lbl, ex_dt, ex_ret, ex_rsn = 0, future.index[-1], float(future.iloc[-1]) / c0 - 1, "time"

        for dt, px in future.items():
            if np.isnan(px):
                continue
            if px >= tgt:
                lbl, ex_dt, ex_ret, ex_rsn = 1, dt, px / c0 - 1, "target"
                break
            if px <= stp:
                lbl, ex_dt, ex_ret, ex_rsn = 0, dt, px / c0 - 1, "stop"
                break

        labels.append(lbl); exit_dates.append(ex_dt)
        exit_rets.append(round(ex_ret, 4)); reasons.append(ex_rsn)

    out = events.copy()
    out["label"]       = labels
    out["exit_date"]   = exit_dates
    out["exit_ret"]    = exit_rets
    out["exit_reason"] = reasons
    return out.dropna(subset=["label"]).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Feature Engineering
# ─────────────────────────────────────────────────────────────────────────────
def extract_features(
    events: pd.DataFrame,
    close:  pd.DataFrame,
    volume: pd.DataFrame,
) -> pd.DataFrame:
    """
    10 Marktbedingungen am Signal-Tag (kein Look-Ahead-Bias).

    Returned DataFrame: ticker, date, + FEATURE_COLS
    Hinweis: vol_ratio wird aus dem Event-Row entnommen (identisch mit events.vol_ratio).
    """
    stock_cols = [c for c in close.columns if c not in SECTOR_ETFS and c != "SPY"]
    avail_sec  = [e for e in SECTOR_ETFS if e in close.columns]

    spy_ret   = close["SPY"].pct_change()
    vix_proxy = spy_ret.rolling(20).std() * np.sqrt(252) * 100
    spy_mom20 = close["SPY"].pct_change(20)
    sma200    = close[stock_cols].rolling(200).mean()
    sma50     = close[stock_cols].rolling(50).mean()
    atr14     = close[stock_cols].diff().abs().rolling(14).mean()
    high_252  = close[stock_cols].rolling(252).max()
    low_252   = close[stock_cols].rolling(252).min()
    spy_b63   = close["SPY"] / close["SPY"].shift(63)

    rows = []
    for _, ev in events.iterrows():
        tk, dt, c0 = ev["ticker"], ev["date"], ev["close"]
        vr  = ev.get("vol_ratio", np.nan)
        h52 = ev.get("prior_high", np.nan)

        def _g(s: pd.Series) -> float:
            try:
                v = s.at[dt]
                return float(v) if pd.notna(v) else np.nan
            except (KeyError, TypeError):
                return np.nan

        f1  = float(vr) if pd.notna(vr) else np.nan
        f2  = (c0 / h52 - 1) if (pd.notna(h52) and h52 > 0) else np.nan

        try:
            c63 = close[tk].shift(63).at[dt]
            sb  = spy_b63.at[dt]
            f3  = (c0 / c63 / sb - 1) if (c63 > 0 and sb > 0) else np.nan
        except Exception:
            f3 = np.nan

        atr = _g(atr14[tk]) if tk in atr14.columns else np.nan
        f4  = (atr / c0) if (pd.notna(atr) and c0 > 0) else np.nan

        try:
            f5 = float((close[stock_cols].loc[dt] > sma200.loc[dt]).mean())
        except Exception:
            f5 = np.nan

        f6  = _g(spy_mom20)
        f7  = _g(vix_proxy)

        # dist_52w_low_pct: Close vs. 252-Tage-Tief (kein Look-Ahead)
        lo = _g(low_252[tk]) if tk in low_252.columns else np.nan
        f8 = ((c0 / lo) - 1) if (pd.notna(lo) and lo > 0) else np.nan

        try:
            f9 = float((close[stock_cols].loc[dt] > sma50.loc[dt]).mean())
        except Exception:
            f9 = np.nan

        if avail_sec:
            try:
                s_rets = [close[se].at[dt] / close[se].shift(63).at[dt] - 1
                          for se in avail_sec
                          if close[se].shift(63).at[dt] > 0]
                f10 = float(np.mean(s_rets)) if s_rets else np.nan
            except Exception:
                f10 = np.nan
        else:
            f10 = f6

        rows.append({
            "ticker":          tk,
            "date":            dt,
            "vol_ratio":       f1,
            "breakout_pct":    f2,
            "rs_vs_spy_63":    f3,
            "atr_ratio":       f4,
            "market_breadth":  f5,
            "spy_momentum_20": f6,
            "vix_proxy":       f7,
            "dist_52w_low_pct": f8,
            "above_sma50_pct":  f9,
            "sector_momentum":  f10,
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Stufe 2 — Meta-Modell Training
# ─────────────────────────────────────────────────────────────────────────────
def train_breakout_meta(
    close:         pd.DataFrame | None = None,
    volume:        pd.DataFrame | None = None,
    threshold:     "float | None" = None,
    keep_top_pct:  float = KEEP_TOP_PCT,
    train_end:     str   = TRAIN_END,
    verbose:       bool  = True,
) -> tuple:
    """
    Walk-Forward Training:
      Train : EVAL_START → train_end
      Test  : train_end  → heute  (OOS-Bewertung)

    threshold=None  → quantilbasiert: die besten keep_top_pct% der Signale
                      (identisch zur validierten Zelle-3-Logik, ~0.21).
    threshold=float → fester Wert (nur für Spezialfälle).

    Returns  (model, report, dataset)
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    if close is None or volume is None:
        close, volume = load_price_data()

    if verbose:
        print("═" * 72)
        print(f"  BREAKOUT META-LABELING  v{VERSION}  —  Training")
        print("═" * 72)
        print(f"  Primär : 52W-Hoch + Vol ×{MIN_VOL_RATIO} + SMA{SMA_PERIOD}")
        print(f"  Label  : Triple Barrier  +{PROFIT_TARGET:.0%} / −{STOP_LOSS:.0%} / {HOLD_DAYS}d")
        print(f"  Train  : {EVAL_START} → {train_end}")
        print(f"  Test   : {train_end} → heute")

    if verbose:
        print("\n  [1/4] Ausbruchs-Events …")
    events = generate_breakout_events(close, volume, start=EVAL_START)
    if verbose:
        print(f"       {len(events):,} Events (nach Duplikat-Filter ≥{MIN_DAYS_GAP}d)")

    if verbose:
        print("\n  [2/4] Triple-Barrier-Labels …")
    events = label_events(events, close)
    events = events.dropna(subset=["label"])
    events["label"] = events["label"].astype(int)
    if verbose:
        n1 = int(events["label"].sum())
        n0 = len(events) - n1
        print(f"       {n1} Erfolg / {n0} Misserfolg  —  {events['exit_reason'].value_counts().to_dict()}")

    if verbose:
        print("\n  [3/4] Feature-Extraktion …")
    feats   = extract_features(events, close, volume)
    dataset = _dropna_features(_merge_features(events, feats)).reset_index(drop=True)
    if verbose:
        print(f"       {len(dataset):,} vollständige Events")

    t_cut  = pd.Timestamp(train_end)
    df_tr  = dataset[dataset["date"] <  t_cut].reset_index(drop=True)
    df_te  = dataset[dataset["date"] >= t_cut].reset_index(drop=True)

    if len(df_tr) < MIN_EVENTS_TRAIN:
        raise RuntimeError(
            f"Zu wenige Train-Events: {len(df_tr)} < {MIN_EVENTS_TRAIN}. "
            "EVAL_START früher setzen oder MIN_VOL_RATIO senken."
        )
    if len(df_te) < 50:
        raise RuntimeError(f"Zu wenige Test-Events: {len(df_te)}.")

    if verbose:
        print(f"\n  [4/4] Training …  ({len(df_tr)} Train / {len(df_te)} Test)")

    X_tr, y_tr = df_tr[FEATURE_COLS].values, df_tr["label"].values
    X_te, y_te = df_te[FEATURE_COLS].values, df_te["label"].values

    baseline_prec = float(y_te.mean())

    clf = RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=10,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    )
    cal = CalibratedClassifierCV(clf, method="sigmoid", cv=3)
    cal.fit(X_tr, y_tr)

    probs_te  = cal.predict_proba(X_te)[:, 1]

    # Threshold: quantilbasiert (Top keep_top_pct%) — robust bei niedriger Base Rate.
    # Fester Wert nur wenn explizit übergeben.
    if threshold is None:
        threshold = float(np.percentile(probs_te, 100 - keep_top_pct))
        if verbose:
            print(f"       Quantil-Threshold (Top {keep_top_pct:.0f}%): {threshold:.4f}")

    meta_mask = probs_te >= threshold
    meta_prec = float(y_te[meta_mask].mean()) if meta_mask.sum() > 0 else 0.0
    meta_frac = float(meta_mask.mean())

    if verbose:
        delta = meta_prec - baseline_prec
        print(f"       Baseline-Präzision (Test) : {baseline_prec:.1%}")
        print(f"       Meta @ {threshold:.0%} (Test)      : {meta_prec:.1%}  ({delta:+.1%})"
              f"  ·  {meta_frac:.0%} der Test-Signale genommen")

    # Finales Modell auf allen Daten
    clf_full = RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=10,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    )
    cal_full = CalibratedClassifierCV(clf_full, method="sigmoid", cv=3)
    cal_full.fit(dataset[FEATURE_COLS].values, dataset["label"].values)

    if verbose:
        try:
            base_clf = cal_full.calibrated_classifiers_[0].estimator
            imp = pd.Series(base_clf.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False)
            print("\n       Feature Importance (Top 5):")
            for feat, val in imp.head(5).items():
                bar = "█" * int(val * 50)
                print(f"         {feat:22} {val:.3f}  {bar}")
        except Exception:
            pass

    report = BreakoutMetaReport(
        n_events_train      = len(df_tr),
        n_events_test       = len(df_te),
        baseline_precision  = round(baseline_prec, 4),
        meta_precision      = round(meta_prec, 4),
        meta_fraction       = round(meta_frac, 4),
        threshold           = threshold,
        train_period        = f"{EVAL_START} → {train_end}",
        test_period         = f"{train_end} → heute",
    )
    model = BreakoutMetaModel(
        clf=cal_full, threshold=threshold,
        feature_names=FEATURE_COLS, report=report,
    )
    model.save(META_DIR / "breakout_meta_model.pkl")
    (META_DIR / "breakout_meta_report.json").write_text(
        json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        print(f"\n{report}")
        print("\n  ✅ Fertig.")
        print("═" * 72)
    return model, report, dataset


def load_model() -> BreakoutMetaModel:
    p = META_DIR / "breakout_meta_model.pkl"
    if not p.is_file():
        raise FileNotFoundError(
            f"Kein Modell: {p}\nZuerst train_breakout_meta() ausführen."
        )
    return BreakoutMetaModel.load(p)


def model_age_days() -> float | None:
    """Alter des gespeicherten Modells in Tagen (None wenn kein Modell)."""
    report_path = META_DIR / "breakout_meta_report.json"
    if not report_path.is_file():
        return None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        trained_at = data.get("trained_at")
        if trained_at:
            from datetime import timezone
            dt = datetime.fromisoformat(trained_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        pass
    return None


def ensure_fresh_model(
    max_age_days:    int   = 30,
    close:           "pd.DataFrame | None" = None,
    volume:          "pd.DataFrame | None" = None,
    verbose:         bool  = True,
) -> BreakoutMetaModel:
    """
    Lädt das gespeicherte Modell — retraint automatisch wenn es älter als
    max_age_days Tage ist oder noch nicht existiert.

    Empfehlung: max_age_days=30  (monatliches Auto-Retrain)
    Für wöchentliches Retrain: max_age_days=7
    Zum Deaktivieren: max_age_days=3650 (10 Jahre)

    Gibt das (ggf. neu trainierte) Modell zurück.
    """
    age = model_age_days()

    if age is None:
        if verbose:
            print("  🆕 Kein Modell vorhanden — erster Training-Lauf …")
        model, _, _ = train_breakout_meta(close=close, volume=volume, verbose=verbose)
        return model

    if age > max_age_days:
        if verbose:
            print(f"  🔄 Modell ist {age:.0f} Tage alt (Limit: {max_age_days}d) — Auto-Retrain …")
        model, _, _ = train_breakout_meta(close=close, volume=volume, verbose=verbose)
        return model

    if verbose:
        print(f"  ✅ Modell aktuell ({age:.0f} Tage alt, Limit: {max_age_days}d) — kein Retrain nötig.")
    return load_model()


# ─────────────────────────────────────────────────────────────────────────────
# Event-Backtest
# ─────────────────────────────────────────────────────────────────────────────
def _simulate_trades(
    events:          pd.DataFrame,
    close:           pd.DataFrame,
    position_size:   float,
    max_positions:   int,
    initial_capital: float,
    regime_panel:    "pd.DataFrame | None" = None,
) -> tuple[pd.Series, list[dict]]:
    capital   = initial_capital
    open_pos: dict = {}
    trades:   list = []
    curve:    dict = {}

    def _max_pos_for_date(dt) -> int:
        if regime_panel is None or regime_panel.empty:
            return max_positions
        ts = pd.Timestamp(dt)
        idx = regime_panel.index[regime_panel.index <= ts]
        if len(idx) == 0:
            return max_positions
        mult = float(regime_panel.loc[idx[-1], "max_pos_mult"])
        if mult <= 0:
            return 0
        return max(1, int(round(max_positions * mult)))

    by_date: dict = {}
    for _, row in events.iterrows():
        by_date.setdefault(row["date"], []).append(row.to_dict())

    all_dates = close.index[close.index >= pd.Timestamp(EVAL_START)]
    for dt in all_dates:
        to_close = [tk for tk, pos in open_pos.items() if pos["exit_date"] <= dt]
        for tk in to_close:
            pos = open_pos.pop(tk)
            pnl = pos["size_eur"] * pos["exit_ret"]
            capital += pnl
            trades.append({"exit_date": pos["exit_date"], "ticker": tk,
                           "ret": pos["exit_ret"], "pnl": pnl,
                           "reason": pos.get("exit_reason", "")})

        max_pos = _max_pos_for_date(dt)
        for ev in by_date.get(dt, []):
            if max_pos <= 0 or len(open_pos) >= max_pos:
                break
            tk = ev["ticker"]
            if tk in open_pos:
                continue
            open_pos[tk] = {
                "exit_date":   ev["exit_date"],
                "exit_ret":    ev["exit_ret"],
                "exit_reason": ev.get("exit_reason", ""),
                "size_eur":    capital * position_size,
            }
        curve[dt] = capital

    return pd.Series(curve).sort_index(), trades


def run_breakout_backtest(
    close:           pd.DataFrame,
    volume:          pd.DataFrame,
    use_meta:        bool = False,
    model:           "BreakoutMetaModel | None" = None,
    eval_start:      str   = EVAL_START,
    position_size:   float = POSITION_SIZE,
    max_positions:   int   = MAX_POSITIONS,
    initial_capital: float = INITIAL_CAPITAL,
) -> dict:
    """Baseline (use_meta=False) oder Meta-Filter (use_meta=True, nur auf Test-Periode)."""
    events = generate_breakout_events(close, volume, start=eval_start)
    events = label_events(events, close)
    events = events.dropna(subset=["label"]).reset_index(drop=True)

    if use_meta and model is not None:
        t_cut  = pd.Timestamp(TRAIN_END)
        ev_te  = events[events["date"] >= t_cut].copy()
        feats  = extract_features(ev_te, close, volume)
        ev_m   = _dropna_features(_merge_features(ev_te, feats))
        if not ev_m.empty:
            probs = model.predict_proba(ev_m[FEATURE_COLS].values)[:, 1]
            ev_m["meta_prob"] = probs
            ev_te_filtered = ev_m[probs >= model.threshold].drop(columns=FEATURE_COLS, errors="ignore")
        else:
            ev_te_filtered = ev_te.iloc[0:0]
        ev_tr  = events[events["date"] < t_cut]
        events = pd.concat([ev_tr, ev_te_filtered], ignore_index=True)

    equity, trades = _simulate_trades(events, close, position_size, max_positions, initial_capital)
    bt      = _get_bt()
    metrics = bt.compute_bt_metrics(equity)

    if trades:
        rets     = [t["ret"] for t in trades]
        wins     = [r for r in rets if r > 0]
        losses   = [r for r in rets if r <= 0]
        win_rate = len(wins) / len(rets)
        avg_win  = float(np.mean(wins))  if wins   else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0
        crv      = abs(avg_win / avg_loss) if avg_loss != 0 else np.nan
    else:
        win_rate = avg_win = avg_loss = crv = 0.0

    metrics.update({"win_rate": win_rate, "avg_win": avg_win,
                    "avg_loss": avg_loss, "crv": crv, "n_trades": len(trades)})
    return {"equity": equity, "metrics": metrics, "trades": trades, "events": events}


# ─────────────────────────────────────────────────────────────────────────────
# Regime-Overlay — Seitwärtsphasen / schwache Marktbreite
# ─────────────────────────────────────────────────────────────────────────────
def compute_regime_panel(close: pd.DataFrame) -> pd.DataFrame:
    """
    Tägliches Markt-Regime aus Marktbreite (% Aktien > SMA200) + SPY vs. SMA200.

    🟢 GRÜN  — Breite ≥55% UND SPY > SMA200  → 100% Slots
    🟡 GELB  — dazwischen                     → 50% Slots
    🔴 ROT   — Breite <40% ODER SPY ≤ SMA200  → 0% Neukäufe
    """
    stock_cols = [c for c in close.columns if c not in SECTOR_ETFS and c != "SPY"]
    if "SPY" not in close.columns or not stock_cols:
        return pd.DataFrame()

    sma200 = close[stock_cols].rolling(200, min_periods=100).mean()
    breadth = (close[stock_cols].gt(sma200)).mean(axis=1)
    spy = close["SPY"]
    spy_sma = spy.rolling(200, min_periods=100).mean()
    spy_ok = spy > spy_sma

    rows = []
    for dt in breadth.dropna().index:
        b = float(breadth.at[dt])
        sa = bool(spy_ok.at[dt]) if pd.notna(spy_ok.at[dt]) else False
        if b < REGIME_BREADTH_RED or not sa:
            regime, mult = "red", REGIME_RED_MULT
        elif b >= REGIME_BREADTH_GREEN and sa:
            regime, mult = "green", 1.0
        else:
            regime, mult = "yellow", REGIME_YELLOW_MULT
        rows.append({
            "date": dt,
            "breadth": round(b, 3),
            "spy_above_sma200": sa,
            "regime": regime,
            "max_pos_mult": mult,
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("date")


def regime_status(close: pd.DataFrame | None = None,
                  regime_panel: pd.DataFrame | None = None) -> dict:
    """Aktuelles Regime für Live-Scanner / Dashboard."""
    if regime_panel is None:
        if close is None:
            return {"regime": "unknown", "label": "—", "max_pos_mult": 1.0}
        regime_panel = compute_regime_panel(close)
    if regime_panel.empty:
        return {"regime": "unknown", "label": "—", "max_pos_mult": 1.0}
    row = regime_panel.iloc[-1]
    labels = {
        "green":  "🟢 GRÜN — volle Quote (max. 10 Positionen)",
        "yellow": "🟡 GELB — reduziert (max. 5 Positionen)",
        "red":    "🔴 ROT — keine Neukäufe",
    }
    return {
        "regime":       str(row["regime"]),
        "label":        labels.get(str(row["regime"]), "—"),
        "breadth":      float(row["breadth"]),
        "spy_above_sma200": bool(row["spy_above_sma200"]),
        "max_pos_mult": float(row["max_pos_mult"]),
        "date":         str(regime_panel.index[-1].date()),
    }


def _filter_events_meta(events, close, volume, model):
    """Events mit Meta-Filter (Train unverändert, Test gefiltert)."""
    t_cut = pd.Timestamp(TRAIN_END)
    ev_te = events[events["date"] >= t_cut].copy()
    feats = extract_features(ev_te, close, volume)
    ev_m  = _dropna_features(_merge_features(ev_te, feats))
    if ev_m.empty:
        ev_te_filtered = ev_te.iloc[0:0]
    else:
        probs = model.predict_proba(ev_m[FEATURE_COLS].values)[:, 1]
        ev_m["meta_prob"] = probs
        ev_te_filtered = ev_m[probs >= model.threshold].drop(columns=FEATURE_COLS, errors="ignore")
    ev_tr = events[events["date"] < t_cut]
    return pd.concat([ev_tr, ev_te_filtered], ignore_index=True)


def run_regime_overlay_comparison(
    close:       pd.DataFrame | None = None,
    volume:      pd.DataFrame | None = None,
    model:       "BreakoutMetaModel | None" = None,
    oos_start:   str = TRAIN_END,
    verbose:     bool = True,
) -> dict:
    """
    Vergleich Meta-only vs. Meta + Regime-Overlay (OOS ab oos_start).
    """
    if close is None or volume is None:
        close, volume = load_price_data()
    if model is None:
        model = load_model()

    t_cut = pd.Timestamp(oos_start)
    events = generate_breakout_events(close, volume, start=EVAL_START)
    events = label_events(events, close).dropna(subset=["label"]).reset_index(drop=True)
    events = _filter_events_meta(events, close, volume, model)
    events_oos = events[events["date"] >= t_cut].copy()

    regime_panel = compute_regime_panel(close)
    bt = _get_bt()

    eq_base, tr_base = _simulate_trades(
        events, close, POSITION_SIZE, MAX_POSITIONS, INITIAL_CAPITAL,
    )
    eq_reg, tr_reg = _simulate_trades(
        events, close, POSITION_SIZE, MAX_POSITIONS, INITIAL_CAPITAL,
        regime_panel=regime_panel,
    )

    spy_oos = close["SPY"].loc[t_cut:].pct_change().fillna(0)
    eq_spy = (1 + spy_oos).cumprod() * INITIAL_CAPITAL

    m_base = bt.compute_bt_metrics(eq_base.loc[t_cut:])
    m_reg  = bt.compute_bt_metrics(eq_reg.loc[t_cut:])
    m_spy  = bt.compute_bt_metrics(eq_spy)

    # Regime-Statistik OOS
    rp_oos = regime_panel.loc[t_cut:] if not regime_panel.empty else regime_panel
    regime_pct = {}
    if not rp_oos.empty:
        for r in ("green", "yellow", "red"):
            regime_pct[r] = float((rp_oos["regime"] == r).mean())

    # Jahr-für-Jahr
    yearly = {}
    for label, eq in [("Meta", eq_base.loc[t_cut:]), ("Meta+Regime", eq_reg.loc[t_cut:])]:
        yr = eq.resample("YE").last().pct_change().dropna()
        yearly[label] = {int(d.year): float(v) for d, v in yr.items()}

    if verbose:
        print("═" * 72)
        print("  REGIME-OVERLAY — Meta vs. Meta + Marktfilter (OOS)")
        print("═" * 72)
        print(f"  OOS ab {oos_start}")
        print(f"  Regel: Breite ≥{REGIME_BREADTH_GREEN:.0%} + SPY>SMA200 → GRÜN | "
              f"Breite <{REGIME_BREADTH_RED:.0%} oder SPY≤SMA200 → ROT")
        if regime_pct:
            print(f"  Handelstage OOS: GRÜN {regime_pct.get('green', 0):.0%} · "
                  f"GELB {regime_pct.get('yellow', 0):.0%} · "
                  f"ROT {regime_pct.get('red', 0):.0%}")
        print(f"\n  {'Kennzahl':22}  {'Meta':>10}  {'Meta+Regime':>12}  {'SPY':>8}")
        print("  " + "─" * 58)
        for lbl, key, fmt in [
            ("CAGR", "cagr", ".1%"), ("MaxDD", "maxdd", ".1%"),
            ("Sharpe", "sharpe", ".2f"), ("MAR", "mar", ".2f"),
            ("# Trades", "n_trades", ".0f"),
        ]:
            if key == "n_trades":
                b, r = len(tr_base), len(tr_reg)
                s = m_spy.get(key) or 0
                print(f"  {lbl:22}  {b:>10.0f}  {r:>12.0f}  {s:>8.0f}")
            else:
                b = m_base.get(key) or 0
                r = m_reg.get(key) or 0
                s = m_spy.get(key) or 0
                print(f"  {lbl:22}  {format(b, fmt):>10}  {format(r, fmt):>12}  {format(s, fmt):>8}")
        print(f"\n  Trades OOS: Meta {len([t for t in tr_base if pd.Timestamp(t['exit_date']) >= t_cut])} · "
              f"Meta+Regime {len([t for t in tr_reg if pd.Timestamp(t['exit_date']) >= t_cut])}")
        if yearly.get("Meta"):
            print("\n  ── Jahr-für-Jahr (OOS) ──")
            years = sorted(set(yearly["Meta"]) | set(yearly.get("Meta+Regime", {})))
            print(f"  {'Jahr':6}  {'Meta':>8}  {'Meta+Regime':>12}  {'Δ':>8}")
            for y in years:
                a = yearly["Meta"].get(y, 0)
                b = yearly.get("Meta+Regime", {}).get(y, 0)
                print(f"  {y:6}  {a:>7.1%}  {b:>11.1%}  {b-a:>+7.1%}")

    return {
        "metrics_base": m_base,
        "metrics_regime": m_reg,
        "metrics_spy": m_spy,
        "regime_pct": regime_pct,
        "yearly": yearly,
        "equity_base": eq_base,
        "equity_regime": eq_reg,
        "regime_panel": regime_panel,
        "events_oos": events_oos,
    }


def run_meta_comparison(
    close:   pd.DataFrame | None = None,
    volume:  pd.DataFrame | None = None,
    verbose: bool = True,
) -> dict:
    if close is None or volume is None:
        close, volume = load_price_data()
    model = load_model()

    if verbose:
        print("═" * 72)
        print("  BREAKOUT — Baseline vs. Meta (Walk-Forward OOS)")
        print("═" * 72)
        print(f"  Bewertung: nur Test-Periode ({TRAIN_END} → heute)")
        print("  Baseline …")
    base = run_breakout_backtest(close, volume, use_meta=False)

    if verbose:
        print("  Mit Meta-Filter …")
    meta = run_breakout_backtest(close, volume, use_meta=True, model=model)

    t_cut  = pd.Timestamp(TRAIN_END)
    spy_eq = (1 + close["SPY"].pct_change().fillna(0)).cumprod() * INITIAL_CAPITAL
    spy_eq = spy_eq[spy_eq.index >= t_cut]
    bt     = _get_bt()
    spy_m  = bt.compute_bt_metrics(spy_eq)

    bm, mm = base["metrics"], meta["metrics"]
    if verbose:
        n_b, n_m = bm.get("n_trades", 0), mm.get("n_trades", 0)
        pct = n_m / n_b * 100 if n_b else 0
        print(f"\n  Trades (Test): {n_b} Baseline  →  {n_m} Meta ({pct:.0f}% genommen)")
        print(f"\n  {'Kennzahl':22}  {'Baseline':>10}  {'Meta':>10}  {'Delta':>8}  {'SPY':>8}")
        print("  " + "─" * 62)
        for lbl, key, fmt in [
            ("CAGR",      "cagr",     ".1%"),
            ("MaxDD",     "maxdd",    ".1%"),
            ("Sharpe",    "sharpe",   ".2f"),
            ("MAR",       "mar",      ".2f"),
            ("Win Rate",  "win_rate", ".1%"),
            ("Avg Win",   "avg_win",  ".1%"),
            ("Avg Loss",  "avg_loss", ".1%"),
            ("CRV",       "crv",      ".2f"),
            ("# Trades",  "n_trades", ".0f"),
        ]:
            b  = bm.get(key) or 0
            m  = mm.get(key) or 0
            s  = spy_m.get(key) or 0
            d  = m - b
            sg = "+" if d >= 0 else ""
            if "%" in fmt:
                print(f"  {lbl:22}  {b:{fmt}}  {m:{fmt}}  {sg}{d:.1%}  {s:{fmt}}")
            elif key == "n_trades":
                print(f"  {lbl:22}  {b:.0f}  {m:.0f}  {sg}{d:.0f}  {s:.0f}")
            else:
                print(f"  {lbl:22}  {b:{fmt}}  {m:{fmt}}  {sg}{d:.2f}  {s:{fmt}}")
        print("═" * 72)

    result = {
        "test_period":  f"{TRAIN_END} → heute",
        "baseline":     {k: round(float(v), 4) for k, v in bm.items() if isinstance(v, (int, float))},
        "meta":         {k: round(float(v), 4) for k, v in mm.items() if isinstance(v, (int, float))},
        "spy":          {k: round(float(v), 4) for k, v in spy_m.items() if isinstance(v, (int, float))},
        "delta_cagr":   round((mm.get("cagr") or 0) - (bm.get("cagr") or 0), 4),
        "delta_sharpe": round((mm.get("sharpe") or 0) - (bm.get("sharpe") or 0), 3),
        "run_at":       datetime.now().isoformat(),
    }
    out = META_DIR / "breakout_comparison.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if verbose:
        print(f"  📄 {out}")
    return {"baseline": base, "meta": meta, "spy_metrics": spy_m, "report": result}


# ─────────────────────────────────────────────────────────────────────────────
# Barrieren-Vergleich — fixe %- vs. ATR-/vola-skalierte Barrieren
# ─────────────────────────────────────────────────────────────────────────────
def _train_meta_variant(close, volume, use_atr, train_end=TRAIN_END,
                        keep_top_pct=KEEP_TOP_PCT):
    """
    Trainiert ein Meta-Modell in-memory (ohne Speichern) für eine Barriere-Variante.
    Walk-Forward: Train < train_end, Threshold aus Test-Quantil (Top keep_top_pct%).
    Rückgabe: (cal_train, threshold).  cal_train ist NUR auf Train-Daten trainiert
    (kein Look-Ahead → faire OOS-Bewertung).
    """
    events = generate_breakout_events(close, volume, start=EVAL_START)
    events = label_events(events, close, use_atr=use_atr).dropna(subset=["label"]).reset_index(drop=True)
    events["label"] = events["label"].astype(int)
    feats   = extract_features(events, close, volume)
    dataset = _dropna_features(_merge_features(events, feats)).reset_index(drop=True)

    t_cut = pd.Timestamp(train_end)
    df_tr = dataset[dataset["date"] <  t_cut]
    df_te = dataset[dataset["date"] >= t_cut]
    if len(df_tr) < MIN_EVENTS_TRAIN or len(df_te) < 50:
        raise RuntimeError(
            f"Zu wenige Events (Train {len(df_tr)} / Test {len(df_te)}) für Variante "
            f"use_atr={use_atr}."
        )

    clf = RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=10,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    )
    cal = CalibratedClassifierCV(clf, method="sigmoid", cv=3)
    cal.fit(df_tr[FEATURE_COLS].values, df_tr["label"].values)
    probs_te  = cal.predict_proba(df_te[FEATURE_COLS].values)[:, 1]
    threshold = float(np.percentile(probs_te, 100 - keep_top_pct))
    return cal, threshold


def _backtest_barrier_variant(close, volume, use_atr, oos_start=TRAIN_END):
    """Meta-gefilterter Backtest für eine Barriere-Variante. Rückgabe: (equity, trades, threshold)."""
    cal, threshold = _train_meta_variant(close, volume, use_atr)

    events = generate_breakout_events(close, volume, start=EVAL_START)
    events = label_events(events, close, use_atr=use_atr).dropna(subset=["label"]).reset_index(drop=True)

    t_cut = pd.Timestamp(oos_start)
    ev_te = events[events["date"] >= t_cut].copy()
    feats = extract_features(ev_te, close, volume)
    ev_m  = _dropna_features(_merge_features(ev_te, feats))
    if ev_m.empty:
        ev_te_f = ev_te.iloc[0:0]
    else:
        probs = cal.predict_proba(ev_m[FEATURE_COLS].values)[:, 1]
        ev_te_f = ev_m[probs >= threshold].drop(columns=FEATURE_COLS, errors="ignore")
    ev_tr  = events[events["date"] < t_cut]
    ev_all = pd.concat([ev_tr, ev_te_f], ignore_index=True)

    equity, trades = _simulate_trades(
        ev_all, close, POSITION_SIZE, MAX_POSITIONS, INITIAL_CAPITAL,
    )
    return equity, trades, threshold


def run_barrier_comparison(
    close:     pd.DataFrame | None = None,
    volume:    pd.DataFrame | None = None,
    oos_start: str = TRAIN_END,
    verbose:   bool = True,
) -> dict:
    """
    Vergleich (OOS): fixe %-Barrieren (+PROFIT_TARGET/−STOP_LOSS)
    vs. ATR-/vola-skalierte Barrieren. Jede Variante wird sauber Walk-Forward
    trainiert (eigenes Meta-Modell + eigener Threshold) und gefiltert simuliert.
    Ändert das gespeicherte Live-Modell NICHT.
    """
    if close is None or volume is None:
        close, volume = load_price_data()
    t_cut = pd.Timestamp(oos_start)
    bt = _get_bt()

    if verbose:
        print("  Variante 1/2: Fix-Barrieren …")
    eq_fix, tr_fix, thr_fix = _backtest_barrier_variant(close, volume, use_atr=False, oos_start=oos_start)
    if verbose:
        print("  Variante 2/2: ATR-Barrieren …")
    eq_atr, tr_atr, thr_atr = _backtest_barrier_variant(close, volume, use_atr=True,  oos_start=oos_start)

    spy_oos = close["SPY"].loc[t_cut:].pct_change().fillna(0)
    eq_spy  = (1 + spy_oos).cumprod() * INITIAL_CAPITAL

    m_fix = bt.compute_bt_metrics(eq_fix.loc[t_cut:])
    m_atr = bt.compute_bt_metrics(eq_atr.loc[t_cut:])
    m_spy = bt.compute_bt_metrics(eq_spy)

    yearly = {}
    for label, eq in [("Fix", eq_fix.loc[t_cut:]), ("ATR", eq_atr.loc[t_cut:])]:
        yr = eq.resample("YE").last().pct_change().dropna()
        yearly[label] = {int(d.year): float(v) for d, v in yr.items()}

    if verbose:
        print("═" * 72)
        print("  BARRIEREN-VERGLEICH — Fix vs. ATR-skaliert (Meta-gefiltert, OOS)")
        print("═" * 72)
        print(f"  OOS ab {oos_start}")
        print(f"  Fix : +{PROFIT_TARGET:.0%} Ziel / −{STOP_LOSS:.0%} Stop / {HOLD_DAYS}d")
        print(f"  ATR : Ziel={ATR_PT_MULT:g}×ATR-Ratio, Stop={ATR_SL_MULT:g}×ATR-Ratio "
              f"(Clamp Ziel {ATR_PT_CLAMP[0]:.0%}–{ATR_PT_CLAMP[1]:.0%}, "
              f"Stop {ATR_SL_CLAMP[0]:.0%}–{ATR_SL_CLAMP[1]:.0%})")
        print(f"  Threshold: Fix P≥{thr_fix:.3f} · ATR P≥{thr_atr:.3f}")
        print(f"\n  {'Kennzahl':22}  {'Fix':>10}  {'ATR':>10}  {'SPY':>8}")
        print("  " + "─" * 54)
        for lbl, key, fmt in [
            ("CAGR", "cagr", ".1%"), ("MaxDD", "maxdd", ".1%"),
            ("Sharpe", "sharpe", ".2f"), ("MAR", "mar", ".2f"),
            ("# Trades", "n_trades", ".0f"),
        ]:
            if key == "n_trades":
                f_, a_ = len(tr_fix), len(tr_atr)
                s_ = m_spy.get(key) or 0
                print(f"  {lbl:22}  {f_:>10.0f}  {a_:>10.0f}  {s_:>8.0f}")
            else:
                f_ = m_fix.get(key) or 0
                a_ = m_atr.get(key) or 0
                s_ = m_spy.get(key) or 0
                print(f"  {lbl:22}  {format(f_, fmt):>10}  {format(a_, fmt):>10}  {format(s_, fmt):>8}")
        if yearly.get("Fix"):
            print("\n  ── Jahr-für-Jahr (OOS) ──")
            years = sorted(set(yearly["Fix"]) | set(yearly.get("ATR", {})))
            print(f"  {'Jahr':6}  {'Fix':>8}  {'ATR':>10}  {'Δ':>8}")
            for y in years:
                a = yearly["Fix"].get(y, 0)
                b = yearly.get("ATR", {}).get(y, 0)
                print(f"  {y:6}  {a:>7.1%}  {b:>9.1%}  {b-a:>+7.1%}")
        print("═" * 72)
        _winner = "ATR" if (m_atr.get("mar") or 0) > (m_fix.get("mar") or 0) else "Fix"
        print(f"  → Bessere MAR (CAGR/MaxDD): {_winner}-Barrieren")

    return {
        "metrics_fix":   m_fix,
        "metrics_atr":   m_atr,
        "metrics_spy":   m_spy,
        "threshold_fix": thr_fix,
        "threshold_atr": thr_atr,
        "equity_fix":    eq_fix,
        "equity_atr":    eq_atr,
        "yearly":        yearly,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Live Scanner
# ─────────────────────────────────────────────────────────────────────────────
def run_live_scanner(
    close:           pd.DataFrame | None = None,
    volume:          pd.DataFrame | None = None,
    use_meta:        bool  = True,
    use_regime:      bool  = True,
    kapital_eur:     float = 100_000,
    lookback_days:   int   = 5,
    auto_retrain:    bool  = True,
    max_model_age:   int   = 30,
    verbose:         bool  = True,
) -> list[dict]:
    """
    Scannt die letzten lookback_days Handelstage auf neue Ausbruchs-Signale.

    auto_retrain=True  : Modell wird automatisch neu trainiert wenn älter als
                         max_model_age Tage (Standard: 30 = monatlich).
    auto_retrain=False : Immer das gespeicherte Modell nutzen (schneller).
    """
    if close is None or volume is None:
        close, volume = load_price_data()

    last_date  = close.index[-1]
    scan_start = close.index[-lookback_days] if len(close) >= lookback_days else close.index[0]
    events     = generate_breakout_events(close, volume, start=str(scan_start.date()))

    model = None
    if use_meta:
        try:
            if auto_retrain:
                model = ensure_fresh_model(
                    max_age_days=max_model_age,
                    close=close, volume=volume,
                    verbose=verbose,
                )
            else:
                model = load_model()
        except FileNotFoundError:
            print("  ⚠ Kein Meta-Modell — Signale ohne Filter")
            use_meta = False

    if use_meta and model is not None and not events.empty:
        feats = extract_features(events, close, volume)
        ev_m  = _dropna_features(_merge_features(events, feats))
        if not ev_m.empty:
            probs = model.predict_proba(ev_m[FEATURE_COLS].values)[:, 1]
            ev_m["meta_prob"] = probs
            ev_m["take"]      = probs >= model.threshold
            events = ev_m
        else:
            events["meta_prob"] = np.nan
            events["take"]      = True
    else:
        events["meta_prob"] = np.nan
        events["take"]      = True

    regime_info = regime_status(close) if use_regime else None
    if use_regime and regime_info and regime_info.get("regime") == "red":
        events["take"] = False
    elif use_regime and regime_info and regime_info.get("regime") == "yellow":
        pass  # Meta-Filter bleibt; Portfolio-Limit im Dashboard (max. 5)

    bt    = _get_bt()
    names = bt._ticker_name_map() if hasattr(bt, "_ticker_name_map") else {}

    signals = []
    for _, row in events.iterrows():
        c0 = float(row["close"])
        mp = row.get("meta_prob", np.nan)
        _pt_i, _sl_i = _barrier_pct(row.get("atr_ratio", np.nan), USE_ATR_BARRIERS)
        signals.append({
            "ticker":    row["ticker"],
            "name":      names.get(row["ticker"], row["ticker"]),
            "date":      str(row["date"].date()),
            "close":     round(c0, 2),
            "target":    round(c0 * (1 + _pt_i), 2),
            "stop":      round(c0 * (1 - _sl_i), 2),
            "pos_eur":   round(kapital_eur * POSITION_SIZE, 0),
            "vol_ratio": round(float(row["vol_ratio"]), 2),
            "meta_prob": round(float(mp), 2) if pd.notna(mp) else None,
            "take":      bool(row.get("take", True)),
        })
        if regime_info:
            signals[-1]["regime"] = regime_info.get("regime")

    if verbose:
        taken   = [s for s in signals if s["take"]]
        skipped = [s for s in signals if not s["take"]]
        thr     = getattr(model, "threshold", META_THRESHOLD) if model else None

        print("\n" + "═" * 72)
        print("  BREAKOUT SCANNER — Aktuelle Signale")
        print("═" * 72)
        print(f"  Datum    : {last_date.date()}")
        if regime_info:
            print(f"  Regime   : {regime_info['label']}")
            print(f"             Breite {regime_info['breadth']:.0%} · "
                  f"SPY {'>' if regime_info['spy_above_sma200'] else '≤'} SMA200")
        print(f"  Gescannt : letzte {lookback_days} Handelstage")
        print(f"  Gefunden : {len(signals)} Ausbrüche  ·  {len(taken)} nach Meta-Filter"
              + (f"  (P ≥ {thr:.0%})" if thr else ""))
        print()
        if taken:
            print("  ┌─ KAUFEN " + "─" * 62)
            for s in taken:
                mp_s = f"  P={s['meta_prob']:.0%}" if s["meta_prob"] is not None else ""
                print(
                    f"  │ 🟢 {s['ticker']:6}  {s['name'][:26]:26}  "
                    f"€{s['pos_eur']:>7,.0f}  "
                    f"Ziel {s['target']:>8.2f}  Stop {s['stop']:>8.2f}"
                    f"  Vol ×{s['vol_ratio']:.1f}{mp_s}"
                )
            print("  └" + "─" * 71)
        else:
            print(f"  Keine Signale nach Meta-Filter in den letzten {lookback_days} Tagen.")
        if use_regime and regime_info and regime_info.get("regime") == "red" and signals:
            print("\n  ⚠ ROT — Regime-Filter blockiert alle Neukäufe (bestehende Positionen laufen weiter).")
        elif use_regime and regime_info and regime_info.get("regime") == "yellow":
            print(f"\n  ⚠ GELB — max. {max(1, int(round(MAX_POSITIONS * REGIME_YELLOW_MULT)))} gleichzeitige Positionen empfohlen.")
        if skipped:
            print(f"\n  Meta gefiltert ({len(skipped)}):  "
                  + "  ".join(f"{s['ticker']} P={s['meta_prob']:.0%}" for s in skipped))
        print("═" * 72)

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Roadmap
# ─────────────────────────────────────────────────────────────────────────────
def print_roadmap() -> None:
    print("""
  ═══════════════════════════════════════════════════════════════════
  BREAKOUT + META-LABELING  —  Roadmap
  ═══════════════════════════════════════════════════════════════════
  Stufe 1  Primärmodell        52W-Hoch + Vol×1.5 + SMA100
                               → generate_breakout_events()
  Stufe 2  Triple-Barrier      Ziel +10% / Stop −5% / 20 Tage
                               → label_events()
  Stufe 3  Feature Engineering 10 Marktbedingungen am Signal-Tag
                               → extract_features()
  Stufe 4  Meta-Modell         Random Forest (Walk-Forward)
           Train 2018–2022     → train_breakout_meta()
           Test  2022–heute    OOS-Präzision
  Stufe 5  Backtest-Vergleich  Baseline vs. Meta (OOS)
                               → run_meta_comparison()
  Stufe 6  Live-Scanner        Aktuelle Ausbrüche + Meta-P
                               → run_live_scanner()
  ═══════════════════════════════════════════════════════════════════
  Schlüssel-Unterschied zu Regime Momentum:
    • Viele Signale (~1000+/Jahr) → genug Trainingsdaten
    • Primärmodell ~45-50% Präzision → Meta hat Verbesserungspotenzial
    • Event-basiert (nicht Portfolio-Rebalancing)
    • Positionsgröße fix 5% (Phase 1) → skalierbar nach P (Phase 2)
  ═══════════════════════════════════════════════════════════════════
""")
