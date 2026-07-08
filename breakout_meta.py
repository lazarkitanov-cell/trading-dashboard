"""
Breakout + Meta-Labeling — S&P 500
=====================================
Stufe 1  Primär-Modell   : 52-Wochen-Hoch-Ausbruch + Volumen (x1.5) + SMA100-Trend
Stufe 2  Meta-Modell     : Random Forest -> P(Ausbruch erfolgreich)
Label                    : Triple Barrier  Ziel +10% / Stop -5% / 20 Handelstage
Walk-Forward             : Purged + Embargoed, rollierend (siehe run_walk_forward())

CHANGELOG v1.4 (ggue. v1.3) — Robustheit-Fixes:
  19. _get_bt(): Pflicht-Funktionen per hasattr() geprueft statt fragiler
      string-scan in read_text(); Fehler beim Laden sauber abgefangen.
  20. _resolve_entry_price(): kein spekulativer 0.15%-Slippage mehr;
      entry_biased=True signalisiert Close-Only-Bias an Aufrufer;
      Warnung deutlich sichtbarer formatiert.
  21. label_events(): extreme Exit-Returns (>90%) werden gemeldet;
      Bias-Flags (entry_timing, barrier_check) im DataFrame-Attribut.
  22. train_breakout_meta(): Atomic Save mit Rollback-Backup;
      barrier_config im Modell gespeichert;
      Feature-Importance im Report persistiert.
  23. BreakoutMetaModel: barrier_config-Feld; __str__ zeigt
      entry_timing, barrier_check und Top-3 Feature-Importance.
  24. ensure_fresh_model(): Lock-File gegen Race Conditions bei
      parallelen Colab-Sessions.
  25. run_walk_forward(): save_as_production mit shutil.copy2
      statt rename + try/except Rollback.
  26. run_live_scanner(): Feature-Mismatch-Check; Barrieren-
      Konsistenz-Warnung; Daten-Staleness-Check (>5 Tage -> Error).

CHANGELOG v1.3 (ggue. v1.2) — Fixes aus Code-Review:
  1. Threshold wird ausschliesslich auf TRAIN-Wahrscheinlichkeiten kalibriert,
     nie mehr auf Test/OOS-Daten (vorher: Leakage in train_breakout_meta(),
     _train_meta_variant()).
  2. Gespeichertes Produktionsmodell (auf allen Daten trainiert) bekommt einen
     eigens dafuer neu berechneten Threshold statt den Threshold eines anderen
     Modells zu uebernehmen.
  3. Purging + Embargo am Train/Test-Split: Trainings-Events, deren Label-Fenster
     (exit_date) in die Testperiode hineinreicht, werden aus dem Training entfernt;
     zusaetzlich ein Embargo direkt nach dem Cutoff.
  4. Triple-Barrier prueft Stop VOR Target (konservativer bei Gaps) und nutzt
     Intraday High/Low falls vorhanden (Parameter high/low optional) statt nur
     Close — mit klarer Kennzeichnung, wenn nur Close verfuegbar ist (Bias-Warnung
     statt stillem Fehler).
  5. Entry zum naechsten Open (falls Open-Daten uebergeben werden) statt zum
     Signal-Tag-Close; sonst dokumentierter Fallback auf Signal-Close.
  6. Kapitalbindung: Simulator haelt echtes Cash-Konto, keine implizite
     Hebelwirkung mehr durch gleichzeitig offene Positionen.
  7. Alle Vergleichs-/Backtest-Funktionen schneiden Equity-Kurven konsistent auf
     die OOS-Periode zu, bevor Kennzahlen berechnet werden (vorher: Baseline/Meta
     enthielten Trainingsperiode, nur SPY war korrekt getrimmt).
  8. ATR ist jetzt ein echter True-Range-ATR, wenn High/Low vorhanden sind;
     sonst ein explizit so benannter Close-to-Close-Proxy (keine Umbenennung mehr
     ohne Kennzeichnung).
  9. MIN_DAYS_GAP-Dedupe iteriert sequentiell pro Ticker (vorher: Gap wurde immer
     gegen das vorherige Roh-Event gemessen, nicht gegen das zuletzt behaltene ->
     gueltige Signale gingen verloren).
 10. Events mit fehlenden Features verschwinden im Live-Scanner nicht mehr
     lautlos, sondern erscheinen mit take=False, reason="features_incomplete".
 11. Modell-Report speichert die sklearn-Version; load_model() warnt bei Mismatch.
 12. 52W-Hoch/-Tief nutzen min_periods=window (keine "52-Wochen"-Werte aus nur
     halb so viel Historie mehr).
 13. Roadmap/Docstrings an tatsaechliche Config-Werte angeglichen.
 14. keep_top_pct wird im Modell gespeichert (vorher: Live-Scanner zeigte
     immer den Default 0.20 an).
 15-17. Performance: extract_features() und generate_breakout_events() sind
     vektorisiert (Panel-weite Rolling-Berechnungen statt Python-Loop pro Event);
     Regime-Overlay nutzt reindex()/ffill() statt linearem Scan pro Handelstag.
 18. NEU: run_walk_forward() — purged + embargoed rollierender Walk-Forward als
     eigenstaendiger Stabilitaetscheck (mehrere Jahres-Folds, je eigenes Modell,
     aneinandergehaengte OOS-Equity). Das Produktionsmodell aus
     train_breakout_meta() bleibt ein einzelner Split; run_walk_forward() ist
     der empfohlene Weg, die Robustheit VOR einem Live-Deployment zu pruefen.

BEKANNTE, NICHT BEHOBENE EINSCHRAENKUNGEN (siehe Docstrings der jeweiligen
Funktionen fuer Details):
  - Survivorship-Bias: load_price_data() haengt vom Universum ab, das
    regime_momentum_bt.set_universe("sp500") liefert. Wenn das die *heutige*
    Indexzusammensetzung ist, fehlen delistete/uebernommene Titel in der
    Historie -> Backtest-Ergebnisse sind tendenziell zu optimistisch.
  - Ohne echte Open/High/Low-Daten bleiben Entry-Timing und Barrier-Checks
    Naeherungen (siehe Punkte 4/5 oben). Wird automatisch klar markiert.

Verwendung (Colab):
    from breakout_meta import train_breakout_meta, run_walk_forward, \
        run_meta_comparison, run_live_scanner
    model, report, dataset = train_breakout_meta(verbose=True)
    wf = run_walk_forward(verbose=True)          # Stabilitaets-Check
    run_meta_comparison()
    signals = run_live_scanner()
"""
from __future__ import annotations

VERSION = "1.8.1"  # OHLC-Abruf direkt via EODHD (ohne alte rmb-Version auf Drive)

import importlib.util
import json
import shutil
import sys
import pickle
import time
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier

try:
    import sklearn
    _SKLEARN_VERSION = sklearn.__version__
except Exception:
    _SKLEARN_VERSION = "unknown"

# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────
PROFIT_TARGET   = 0.10
STOP_LOSS       = 0.05
HOLD_DAYS       = 20

# ATR-/Vola-skalierte Barrieren (optional; Lopez-de-Prado dynamic barriers).
# USE_ATR_BARRIERS = False -> fixe %-Barrieren (Standard).
# USE_ATR_BARRIERS = True  -> Ziel/Stop skalieren mit ATR-Ratio (ATR14/Close).
#   Nutzt echten True-Range-ATR wenn high/low uebergeben werden, sonst einen
#   Close-to-Close-Proxy (siehe _atr_ratio_panel). R:R bleibt bei
#   ATR_PT_MULT : ATR_SL_MULT (Default 3:1.5 = 2:1) — Multiplikatoren sind auf
#   den jeweils genutzten ATR-Typ kalibriert und NICHT 1:1 austauschbar
#   zwischen echtem ATR und Proxy.
USE_ATR_BARRIERS   = False
ATR_BARRIER_PERIOD = 14
ATR_PT_MULT   = 3.0
ATR_SL_MULT   = 1.5
ATR_PT_CLAMP  = (0.04, 0.30)
ATR_SL_CLAMP  = (0.02, 0.15)

MIN_VOL_RATIO   = 1.5
SMA_PERIOD      = 100
BREAKOUT_WINDOW = 252
MIN_DAYS_GAP    = 10

# Quantilbasierter Threshold: die besten KEEP_TOP_PCT% der Signale nach
# Modell-Score. Wird IMMER auf Train-Wahrscheinlichkeiten kalibriert (Fix #1).
KEEP_TOP_PCT      = 20
META_THRESHOLD    = 0.45   # Fallback, falls kein Modell-Threshold verfuegbar ist
MIN_EVENTS_TRAIN  = 300
TRAIN_END         = "2022-01-01"
EVAL_START        = "2018-01-01"

# Embargo nach dem Train/Test-Cutoff (Handelstage). Verhindert, dass Events
# unmittelbar vor dem Cutoff ueber ihr Label-Fenster in die Testperiode
# "hineinlecken" (siehe Fix #3 / Purging).
EMBARGO_DAYS      = HOLD_DAYS

INITIAL_CAPITAL  = 100_000.0
POSITION_SIZE    = 0.12
MAX_POSITIONS    = 10

# Entry-Slippage-Aufschlag, falls kein Open-Preis verfuegbar ist und auf
# Signal-Close zurueckgefallen wird (siehe _resolve_entry_price). 0.0015 = 0.15%
# ist eine grobe, konservative Naeherung fuer den mittleren Overnight-Gap bei
# Breakout-Setups — kein Ersatz fuer echte Open-Daten.
FALLBACK_ENTRY_SLIPPAGE = 0.0015

# Round-Trip-Transaktionskosten (Provision + Slippage Entry+Exit), als Anteil
# des Trade-Werts.
TRANSACTION_COST = 0.002

# Regime-Overlay (Seitwaerts-/Baeren-Schutz): Marktbreite + SPY vs. SMA200
REGIME_BREADTH_GREEN  = 0.55
REGIME_BREADTH_RED    = 0.40
REGIME_YELLOW_MULT    = 0.50
REGIME_RED_MULT       = 0.00

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLU", "XLB", "XLY", "XLP", "XLRE", "XLC"]

FEATURE_COLS = [
    "vol_ratio",
    "breakout_pct",
    "rs_vs_spy_63",
    "atr_ratio",
    "market_breadth",
    "spy_momentum_20",
    "vix_proxy",
    "dist_52w_low_pct",
    "above_sma50_pct",
    "sector_momentum",
]

_HERE     = Path(__file__).resolve().parent
_UNIVERSE_CACHE_BASE = {
    "sp500": "regime_momentum_cache",
    "r1000": "regime_momentum_cache_r1000",
}


def _breakout_cache_dir(universe: str = "sp500") -> Path:
    base = _UNIVERSE_CACHE_BASE.get(universe, "regime_momentum_cache")
    return _HERE / base / "breakout"


CACHE_DIR = _breakout_cache_dir("sp500")
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
    keep_top_pct:       float
    train_period:       str
    test_period:        str
    embargo_days:       int
    sklearn_version:    str = _SKLEARN_VERSION
    trained_at:         str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    feature_importance: dict = field(default_factory=dict)   # Feature -> Importanz (0..1)
    entry_timing:       str  = "unknown"# "open_next_day" | "close_only_biased"
    barrier_check:      str  = "unknown"   # "intraday" | "close_only"

    def __str__(self) -> str:
        d = self.meta_precision - self.baseline_precision
        s = (
            f"  Events  Train/Test : {self.n_events_train} / {self.n_events_test}\n"
            f"  Baseline (Test)    : {self.baseline_precision:.1%}\n"
            f"  Meta @ {self.threshold:.0%} (Test)  : {self.meta_precision:.1%}  "
            f"({d:+.1%})  \u00b7  {self.meta_fraction:.0%} der Test-Signale genommen\n"
            f"  Threshold-Basis    : TRAIN-Quantil (Top {self.keep_top_pct:.0f}%), "
            f"Embargo {self.embargo_days}d\n"
            f"  Entry-Timing       : {self.entry_timing}\n"
            f"  Barrier-Check      : {self.barrier_check}"
        )
        if self.feature_importance:
            top3 = sorted(self.feature_importance.items(), key=lambda x: x[1], reverse=True)[:3]
            s += "\n  Feature Importance : " + "  \u00b7  ".join(
                f"{k} {v:.3f}" for k, v in top3
            )
        return s


class BreakoutMetaModel:
    """Wrapper um CalibratedClassifierCV mit threshold + save/load."""

    def __init__(self, clf, threshold: float, feature_names: list[str],
                 keep_top_pct: float = KEEP_TOP_PCT,
                 report: "BreakoutMetaReport | None" = None,
                 sklearn_version: str = _SKLEARN_VERSION,
                 barrier_config: "dict | None" = None):
        self.clf             = clf
        self.threshold       = threshold
        self.feature_names   = feature_names
        self.keep_top_pct    = keep_top_pct
        self.report          = report
        self.sklearn_version = sklearn_version
        # Barrier-Konfiguration bei der das Modell trainiert wurde.
        # Live-Scanner nutzt diese Werte, um sicherzustellen, dass Events
        # mit denselben Barrieren gelabelt werden wie beim Training.
        self.barrier_config  = barrier_config or{
            "use_atr":USE_ATR_BARRIERS,
            "profit_target": PROFIT_TARGET,
            "stop_loss":     STOP_LOSS,
            "hold_days":     HOLD_DAYS,
        }

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.clf.predict_proba(X)

    def predict(self, X: np.ndarray) -> np.ndarray:
        return (self.predict_proba(X)[:, 1] >= self.threshold).astype(int)

    def save(self, path: "Path | str") -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)
        print(f"  \U0001F4BE Modell: {path}")

    @classmethod
    def load(cls, path: "Path | str") -> "BreakoutMetaModel":
        with open(path, "rb") as f:
            model = pickle.load(f)
        saved_ver = getattr(model, "sklearn_version", "unknown")
        if saved_ver != "unknown" and saved_ver != _SKLEARN_VERSION:
            warnings.warn(
                f"Modell wurde mit sklearn {saved_ver} gespeichert, "
                f"aktuell installiert ist {_SKLEARN_VERSION}. "
                "Vorhersagen koennen abweichen — Modell ggf. neu trainieren.",
                RuntimeWarning,
            )
        return model


# ─────────────────────────────────────────────────────────────────────────────
# Merge-Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────
def _merge_features(events: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    """Merged Features in Events, ohne _x/_y-Duplikate bei ueberlappenden Spalten."""
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
    cols = [c for c in FEATURE_COLS if c in df.columns]
    return df.dropna(subset=cols)


# ─────────────────────────────────────────────────────────────────────────────
# Modul-Loader (unveraendert ggue. v1.2 — Blackbox-Abhaengigkeit)
# ─────────────────────────────────────────────────────────────────────────────
def _get_bt():
    key = "regime_momentum_bt"
    if key in sys.modules:
        bt = sys.modules[key]
# Kurz-Check: Pflicht-Funktionen noch vorhanden?
        _REQUIRED_BT_FUNCS = ["refresh_prices_for_live", "set_universe", "compute_bt_metrics"]
        missing = [f for f in _REQUIRED_BT_FUNCS if not hasattr(bt, f)]
        if not missing:
            return bt
        # Modul wurde beschaedigt (z.B. durch Reload) — neu laden
        del sys.modules[key]

    candidates = [
        _HERE / "regime_momentum_bt.py",
        _HERE.parent / "regime_momentum_bt.py",
    ]
    for p in candidates:
        if not p.is_file():
            continue
        spec = importlib.util.spec_from_file_location(key, p)
        m = importlib.util.module_from_spec(spec)
        sys.modules[key] = m
        try:
            spec.loader.exec_module(m)
        except Exception as e:
            del sys.modules[key]
            raise ImportError(f"regime_momentum_bt.py konnte nicht geladen werden: {e}")
        # Pflicht-Funktionen pruefen (KEIN string-scan in read_text mehr)
        _REQUIRED_BT_FUNCS = ["refresh_prices_for_live", "set_universe", "compute_bt_metrics"]
        missing = [f for f in _REQUIRED_BT_FUNCS if not hasattr(m, f)]
        if missing:
            del sys.modules[key]
            raise ImportError(
                f"regime_momentum_bt.py ({p}) fehlen Pflicht-Funktionen: {missing}. "
                "Bitte aktuellere Version verwenden."
            )
        return m
    raise ImportError(
        "regime_momentum_bt.py nicht gefunden — muss neben breakout_meta.py liegen.\n"
        f"Gesuchte Pfade: {[str(p) for p in candidates]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Daten laden
# ─────────────────────────────────────────────────────────────────────────────

def fmt_kurs_datum(ts) -> str:
    """Kurs-/Signaldatum als DD.MM.YYYY."""
    if ts is None:
        return "?"
    try:
        if isinstance(ts, float) and np.isnan(ts):
            return "?"
    except TypeError:
        pass
    try:
        return pd.Timestamp(ts).strftime("%d.%m.%Y")
    except Exception:
        return "?"


def ticker_price_date(close: pd.DataFrame, ticker: str):
    """Letzter verfügbarer Schlusskurs-Tag für einen Ticker."""
    if ticker not in close.columns:
        return None
    s = close[ticker].dropna()
    return s.index[-1] if len(s) else None


def _panel_max_gap_days() -> int:
    """Erlaubte Lücke Panel-Ende → heute (Wochenende/Feiertag)."""
    wd = pd.Timestamp.now().normalize().weekday()
    if wd == 0:
        return 3   # Montag: Freitag noch OK
    if wd >= 5:
        return 3   # Sa/So
    return 1       # Di–Fr: gestern oder heute


def _panel_is_stale(close: pd.DataFrame) -> bool:
    if close is None or close.empty:
        return True
    last = pd.Timestamp(close.index[-1]).normalize()
    gap = (pd.Timestamp.now().normalize() - last).days
    return gap > _panel_max_gap_days()


def load_price_data(
    force_refresh: bool = False,
    stale_days: int = 2,
    universe: str = "sp500",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Laedt Close + Volume fuer S&P 500 (+ Sektor-ETFs falls verfuegbar).
    Nutzt den bestehenden EODHD-Cache aus regime_momentum_bt.

    HINWEIS Survivorship-Bias: Wenn regime_momentum_bt.set_universe("sp500")
    die *aktuelle* Indexzusammensetzung liefert (statt einer historisch
    korrekten Punkt-in-Zeit-Mitgliederliste), fehlen delistete/uebernommene
    Titel in der Backtest-Historie. Das schoent Ausbruchs-Backtests tendenziell,
    da nur "Ueberlebende" enthalten sind. Dieses Modul kann das nicht selbst
    beheben — pruefe bei regime_momentum_bt, ob eine Punkt-in-Zeit-Liste
    verfuegbar ist.

    HINWEIS Open/High/Low: Dieses Modul funktioniert mit reinem Close+Volume.
    Wenn `bt` (regime_momentum_bt) zusaetzlich Open/High/Low liefert (pruefe
    z.B. `bt.refresh_prices_for_live_ohlc()` oder aehnliche Funktionen in
    deiner Version), kannst du load_price_data_ohlc() unten nutzen, um
    realistischeres Entry-Timing und echte Intraday-Barrier-Checks zu bekommen.

    Returns
    -------
    close  : pd.DataFrame  [Datum x Ticker]
    volume : pd.DataFrame  [Datum x Ticker]
    """
    cache_dir = _breakout_cache_dir(universe)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cp = cache_dir / "close.pkl"
    vp = cache_dir / "volume.pkl"
    uni_label = "Russell 1000" if universe == "r1000" else "S&P 500"
    max_age_h = 8.0

    def _fresh(p: Path) -> bool:
        return p.is_file() and (datetime.now().timestamp() - p.stat().st_mtime) / 3600 < max_age_h

    if not force_refresh and cp.is_file() and vp.is_file():
        close = pd.read_pickle(cp)
        volume = pd.read_pickle(vp)
        if not _panel_is_stale(close) and _fresh(cp) and _fresh(vp):
            print(f"  \U0001F4C2 Preis-Cache OK ({uni_label}) \u00b7 Kursstand {fmt_kurs_datum(close.index[-1])}")
            return close, volume

    bt = _get_bt()
    bt.set_universe(universe)
    if force_refresh or (cp.is_file() and _panel_is_stale(pd.read_pickle(cp))):
        print(f"  \U0001F504 EOD-Update {uni_label} (Panel veraltet oder force_refresh) \u2026")
    else:
        print(f"  \u2b07 Preisdaten {uni_label} laden \u2026")
    close, volume = bt.refresh_prices_for_live(stale_days=stale_days)

    missing = [e for e in SECTOR_ETFS if e not in close.columns]
    if missing:
        extra_c, extra_v = _fetch_tickers(bt, missing)
        if not extra_c.empty:
            close  = pd.concat([close,  extra_c.reindex(close.index)],  axis=1)
            volume = pd.concat([volume, extra_v.reindex(volume.index)], axis=1)

    close.to_pickle(cp)
    volume.to_pickle(vp)
    avail_sectors = sum(1 for e in SECTOR_ETFS if e in close.columns)
    print(f"  \U0001F4BE {len(close.columns)} Titel \u00b7 {len(close)} Tage \u00b7 "
          f"Kursstand {fmt_kurs_datum(close.index[-1])} \u00b7 {avail_sectors} Sektor-ETFs")
    print("  \u26a0 EOD-Schlusskurse (kein Realtime) \u2014 Dashboard nutzt Live-Kurse.")
    return close, volume


def load_price_data_for_scanner() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Für Zelle 5 / Live-Scanner: EOD-Panel beim Lauf auf den neuesten Stand bringen.
    Nutzt stale_days=0 → inkrementeller Abruf, sobald der letzte Tag nicht mehr „heute/gestern“ ist.
    """
    print("  \U0001F4E1 Scanner: Preise prüfen / aktualisieren \u2026")
    return load_price_data(force_refresh=False, stale_days=0)


def load_price_data_ohlc(
    force_refresh: bool = False,
    universe: str = "sp500",
) -> tuple[pd.DataFrame, pd.DataFrame, "pd.DataFrame | None", "pd.DataFrame | None", "pd.DataFrame | None"]:
    """
    Wie load_price_data(), plus Open/High/Low-Panels (EODHD, gecacht).

    Returns
    -------
    close, volume, open_, high, low   (open_/high/low sind None wenn Abruf fehlschlaegt)
    """
    close, volume = load_price_data(force_refresh=force_refresh, universe=universe)
    open_, high, low = _load_ohlc_panels(close, universe=universe, force_refresh=force_refresh)
    if high is not None:
        print("  \u2705 OHLC-Panels geladen \u2014 Intraday-Barrieren (High/Low) aktiv.")
    else:
        print("  \u26a0 OHLC nicht verfuegbar \u2014 Fallback Close-only.")
    return close, volume, open_, high, low


def _eodhd_token(bt) -> str:
    tok = getattr(bt, "EODHD_TOKEN", "") or ""
    if tok:
        return tok
    try:
        from google.colab import userdata
        return userdata.get("EODHD_API_KEY") or userdata.get("EODHD_TOKEN") or ""
    except Exception:
        return ""


def _fetch_eod_ohlc_series(bt, symbol: str, start: str, end: str) -> pd.DataFrame:
    """
    EODHD Open/High/Low/Close direkt — unabhaengig von regime_momentum_bt-Version auf Drive.
    """
    token = _eodhd_token(bt)
    if not token:
        return pd.DataFrame()
    base = getattr(bt, "EODHD_BASE", "https://eodhd.com/api/eod")
    try:
        import requests
        r = requests.get(
            f"{base}/{symbol}",
            params={"api_token": token, "fmt": "json", "from": start, "to": end},
            timeout=40,
        )
        if r.status_code != 200:
            return pd.DataFrame()
        rows = r.json()
        if not rows or not isinstance(rows, list):
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "date" not in df.columns:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        out = pd.DataFrame(index=df.index)
        for col in ("open", "high", "low"):
            if col not in df.columns:
                return pd.DataFrame()
            src = df[f"adjusted_{col}"] if f"adjusted_{col}" in df.columns else df[col]
            out[col] = src.astype(float)
        csrc = df["adjusted_close"] if "adjusted_close" in df.columns else df["close"]
        out["close"] = csrc.astype(float)
        return out
    except Exception:
        return pd.DataFrame()


def _load_ohlc_panels(
    close: pd.DataFrame,
    universe: str = "sp500",
    force_refresh: bool = False,
) -> tuple["pd.DataFrame | None", "pd.DataFrame | None", "pd.DataFrame | None"]:
    """Laedt/baut Open-, High-, Low-Panels passend zum Close-Index (mit Pickle-Cache)."""
    if close is None or close.empty:
        return None, None, None

    cache_dir = _breakout_cache_dir(universe)
    cache_dir.mkdir(parents=True, exist_ok=True)
    op = cache_dir / "open.pkl"
    hp = cache_dir / "high.pkl"
    lp = cache_dir / "low.pkl"
    max_age_h = 8.0

    def _fresh(p: Path) -> bool:
        return p.is_file() and (datetime.now().timestamp() - p.stat().st_mtime) / 3600 < max_age_h

    if not force_refresh and op.is_file() and hp.is_file() and lp.is_file() and _fresh(op):
        try:
            open_ = pd.read_pickle(op).reindex(index=close.index).reindex(columns=close.columns)
            high  = pd.read_pickle(hp).reindex(index=close.index).reindex(columns=close.columns)
            low   = pd.read_pickle(lp).reindex(index=close.index).reindex(columns=close.columns)
            if not high.empty and high.notna().sum().sum() > 0:
                print(f"  \U0001F4C2 OHLC-Cache OK ({universe})")
                return open_, high, low
        except Exception:
            pass

    bt = _get_bt()
    bt.set_universe(universe)
    if not _eodhd_token(bt):
        print("  \u274c EODHD_TOKEN / EODHD_API_KEY fehlt — OHLC-Abruf nicht moeglich.")
        return None, None, None

    start = str(close.index[0].date())
    end   = str(close.index[-1].date())
    tickers = [c for c in close.columns if c not in SECTOR_ETFS]
    sym_fn = getattr(bt, "_eodhd_sym", lambda t: f"{t}.US")
    delay  = float(getattr(bt, "EODHD_DELAY", 0.22))

    opens, highs, lows = {}, {}, {}
    n_fail = 0
    print(f"  \U0001F4E1 OHLC-Abruf {universe}: {len(tickers)} Titel \u2026 (einmalig, ~{len(tickers)*delay/60:.0f} Min)")
    for i, tk in enumerate(tickers, 1):
        panel = _fetch_eod_ohlc_series(bt, sym_fn(tk), start=start, end=end)
        if panel.empty or "high" not in panel.columns:
            n_fail += 1
            if i <= 3 and panel.empty:
                print(f"     \u26a0 {tk}: kein OHLC ({sym_fn(tk)})", flush=True)
            continue
        opens[tk] = panel["open"]
        highs[tk] = panel["high"]
        lows[tk]  = panel["low"]
        if i % 50 == 0 or i == len(tickers):
            print(f"     [{i}/{len(tickers)}] \u00b7 {len(highs)} OK \u00b7 {n_fail} fehlend", flush=True)
        time.sleep(delay)

    min_ok = max(50, int(len(tickers) * 0.5))
    if len(highs) < min_ok:
        print(f"  \u274c OHLC nur {len(highs)}/{len(tickers)} Titel (min. {min_ok} noetig).")
        return None, None, None

    open_df = pd.DataFrame(opens).sort_index().reindex(close.index).ffill()
    high_df = pd.DataFrame(highs).sort_index().reindex(close.index).ffill()
    low_df  = pd.DataFrame(lows).sort_index().reindex(close.index).ffill()
    open_df.to_pickle(op)
    high_df.to_pickle(hp)
    low_df.to_pickle(lp)
    print(f"  \U0001F4BE OHLC-Cache: {len(high_df.columns)} Titel")
    return open_df, high_df, low_df


def _fetch_tickers(bt, tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
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
# Stufe 1 — Primaermodell: Ausbruchssignale (vektorisiert, Fix #15/#16)
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
    Scannt S&P 500 auf 52-Wochen-Hoch-Ausbrueche mit:
      - Close  > gestrigem 52-Wochen-Hoch (min_periods=window, Fix #12 -- kein
        "52-Wochen-Hoch" mehr aus nur halb so viel Historie)
      - Volumen > min_vol_ratio x 20-Tage-Schnitt
      - Close  > SMA100 (Trend-Bestaetigung)
      - min. MIN_DAYS_GAP Tage zwischen zwei BEHALTENEN Signalen im selben
        Ticker (Fix #9 -- vorher wurde der Gap gegen das vorherige Roh-Event
        gemessen statt gegen das zuletzt behaltene, was gueltige Signale
        verwarf)

    Returns DataFrame: ticker, date, close, prior_high, sma100, vol_ratio
    """
    stock_cols = [c for c in close.columns if c not in SECTOR_ETFS and c != "SPY"]
    t0         = pd.Timestamp(start)

    prior_high = close[stock_cols].shift(1).rolling(window, min_periods=window).max()
    sma100     = close[stock_cols].rolling(sma_period, min_periods=sma_period).mean()
    vol_avg20  = volume[stock_cols].rolling(20, min_periods=10).mean()
    vol_ratio  = volume[stock_cols].div(vol_avg20.replace(0, np.nan))

    above_high = close[stock_cols] > prior_high
    above_sma  = close[stock_cols] > sma100
    strong_vol = vol_ratio >= min_vol_ratio
    mask       = (above_high & above_sma & strong_vol).loc[close.index >= t0]

    hits = mask.stack()
    hits = hits[hits]
    if hits.empty:
        return pd.DataFrame(columns=["ticker", "date", "close", "prior_high", "sma100", "vol_ratio"])

    df = pd.DataFrame({
        "date":       hits.index.get_level_values(0),
        "ticker":     hits.index.get_level_values(1),
        "close":      close[stock_cols].stack().loc[hits.index].values,
        "prior_high": prior_high.stack().loc[hits.index].values,
        "sma100":     sma100.stack().loc[hits.index].values,
        "vol_ratio":  vol_ratio.stack().loc[hits.index].values,
    })
    df = df.dropna(subset=["close", "prior_high", "sma100", "vol_ratio"])
    df = df[df["close"] > 0]
    if df.empty:
        return pd.DataFrame(columns=["ticker", "date", "close", "prior_high", "sma100", "vol_ratio"])

    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    # Sequentieller Dedupe pro Ticker: behalte ein Event nur, wenn es mind.
    # MIN_DAYS_GAP Kalendertage nach dem zuletzt BEHALTENEN Event desselben
    # Tickers liegt (Fix #9).
    keep_mask = np.ones(len(df), dtype=bool)
    last_kept_date: dict = {}
    for i, row in enumerate(df.itertuples(index=False)):
        tk, dt = row.ticker, row.date
        prev = last_kept_date.get(tk)
        if prev is not None and (dt - prev).days < MIN_DAYS_GAP:
            keep_mask[i] = False
        else:
            last_kept_date[tk] = dt

    df = df[keep_mask].sort_values("date").reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Triple Barrier Labels
# ─────────────────────────────────────────────────────────────────────────────
def _atr_ratio_panel(close: pd.DataFrame, high, low,
                      period: int = ATR_BARRIER_PERIOD,
                      _warned: list = []) -> tuple:
    """
    Liefert (ATR-Ratio-Panel, is_true_atr).

    is_true_atr=True   -> echter True-Range-ATR / Close (High/Low vorhanden).
    is_true_atr=False  -> Close-to-Close-Proxy: mittlerer |Close-Change| / Close.
                          Das ist KEIN echter ATR (siehe Fix #3/#8) -- unterschaetzt
                          Intraday-Range systematisch. Wird einmalig als Warnung
                          ausgegeben statt still verwendet zu werden.
    """
    if high is not None and low is not None:
        prev_close = close.shift(1)
        tr = np.maximum.reduce([
            (high - low).values,
            (high - prev_close).abs().values,
            (low - prev_close).abs().values,
        ])
        tr = pd.DataFrame(tr, index=close.index, columns=close.columns)
        atr = tr.rolling(period, min_periods=period).mean()
        return atr / close, True

    if not _warned:
        print("  \u26a0 Kein High/Low vorhanden -- ATR-Ratio nutzt Close-to-Close-"
              "Proxy (KEIN echter ATR, unterschaetzt Intraday-Range). "
              "Siehe load_price_data_ohlc() fuer echte Werte.")
        _warned.append(True)
    atr_abs = close.diff().abs().rolling(period, min_periods=period).mean()
    return atr_abs / close, False


def _barrier_pct(atr_ratio, use_atr: bool):
    """
    Liefert (Ziel%, Stop%) fuer einen Trade.
      use_atr=False -> fixe Barrieren (PROFIT_TARGET / STOP_LOSS).
      use_atr=True  -> Ziel = ATR_PT_MULT x atr_ratio, Stop = ATR_SL_MULT x atr_ratio
                       (auf ATR_PT_CLAMP / ATR_SL_CLAMP begrenzt).
    Faellt auf fixe Werte zurueck, wenn atr_ratio fehlt/ungueltig.
    """
    if (not use_atr) or atr_ratio is None or not np.isfinite(atr_ratio) or atr_ratio <= 0:
        return PROFIT_TARGET, STOP_LOSS
    pt = min(max(ATR_PT_MULT * atr_ratio, ATR_PT_CLAMP[0]), ATR_PT_CLAMP[1])
    sl = min(max(ATR_SL_MULT * atr_ratio, ATR_SL_CLAMP[0]), ATR_SL_CLAMP[1])
    return float(pt), float(sl)


def _resolve_entry_price(tk: str, t0, close: pd.DataFrame, open_,
                          _warned: list = []):
    """
    Liefert (entry_price, entry_date, entry_biased) fuer ein Signal an Tag t0.

    Mit Open-Daten (Fix #5): Entry = Open des naechsten Handelstages nach t0.
    Gibt entry_biased=False zurueck (kein Naehrungs-Bias).

    Ohne Open-Daten: Fallback auf Signal-Tag-Close OHNE Slippage-Aufschlag.
    Der fruehre Aufschlag (FALLBACK_ENTRY_SLIPPAGE = 0.15%) war eine reine
    Schatzung ohne empirische Basis — Breakout-Overnightgaps koennen positiv
    ODER negativ sein. Stattdessen wird entry_biased=True gesetzt, damit
    Aufrufer das in Reports/Warnungen kenntlich machen koennen.
    Wird einmalig als deutliche Warnung ausgegeben.
    """
    if open_ is not None and tk in open_.columns:
        future_idx = close.index[close.index > t0]
        if len(future_idx) > 0:
            next_dt = future_idx[0]
            px = open_.at[next_dt, tk] if next_dt in open_.index else np.nan
            if pd.notna(px) and px > 0:
                return float(px), next_dt, False# kein Bias
    if not _warned:
        print(
            "\n  " + "=" * 68 + "\n"
            "  WARNUNG: Entry-Preis nutzt Close-only-Fallback (kein Open-Preis).\n"
            "  Backtest-Ergebnisse sind OPTIMISTISCH VERZERRT:\n"
            "    - Echte Breakout-Trades werden erst am naechsten Open ausgefuehrt\n"
            "    - Overnight-Gaps bei52W-Hoch-Ausbruechen koennen +/-3%+ betragen\n"
            "Empfehlung: load_price_data_ohlc() fuer echte Entry-Preise nutzen.\n"
            "  " + "=" * 68
        )
        _warned.append(True)
    c0 = float(close.at[t0, tk])
    return c0, t0, True   # entry_biased=True — Close-Only-Naehrung


def label_events(
    events:   pd.DataFrame,
    close:    pd.DataFrame,
    pt:       float = PROFIT_TARGET,
    sl:       float = STOP_LOSS,
    max_hold: int   = HOLD_DAYS,
    use_atr=None,
    high=None,
    low=None,
    open_=None,
) -> pd.DataFrame:
    """
    label = 1  ->  Ziel (+pt) erreicht, BEVOR Stop (-sl) erreicht wird, innerhalb
                   max_hold Tagen ab Entry.
    label = 0  ->  Stop oder Zeit-Barriere zuerst.

    use_atr=None  -> globale Voreinstellung USE_ATR_BARRIERS.
    use_atr=True  -> vola-skalierte Barrieren pro Trade (siehe _barrier_pct).
                     Nutzt echten ATR wenn high/low uebergeben werden.
    use_atr=False -> fixe %-Barrieren (pt/sl).

    Barrier-Check-Reihenfolge (Fix #4): Stop wird VOR Target geprueft -- bei
    einem Tag, an dem beide Barrieren theoretisch ueberschritten werden
    (z.B. nach einem Gap), ist das die konservativere Annahme.

    Mit high/low (Fix #4): nutzt taegliches High/Low fuer den Barrier-Check
    statt nur Close -> erfasst Intraday-Stop-/Target-Treffer, die sonst
    uebersehen wuerden. Ohne high/low: Close-only-Check (optimistischer Bias,
    siehe Docstring von load_price_data()).

    Mit open_ (Fix #5): Entry-Preis = naechster Open nach dem Signal-Tag
    (siehe _resolve_entry_price()) statt Signal-Tag-Close.

    Neue Spalten: label, entry_date, entry_price, exit_date, exit_ret, exit_reason
    """
    if use_atr is None:
        use_atr = USE_ATR_BARRIERS

    atr_ratio_panel = None
    if use_atr:
        atr_ratio_panel, _ = _atr_ratio_panel(close, high, low)

    use_intraday = high is not None and low is not None

    labels, entry_dates, entry_prices = [], [], []
    exit_dates, exit_rets, reasons = [], [], []
    _any_biased = False   # wird True sobald mind. 1 Close-Only-Entry vorkommt

    for row in events.itertuples(index=False):
        tk, t0 = row.ticker, row.date
        if tk not in close.columns:
            labels.append(np.nan); entry_dates.append(pd.NaT); entry_prices.append(np.nan)
            exit_dates.append(pd.NaT); exit_rets.append(np.nan); reasons.append("no_data")
            continue

        entry_px, entry_dt, entry_biased = _resolve_entry_price(tk, t0, close, open_)
        if entry_biased:
            _any_biased = True
        if entry_px <= 0 or pd.isna(entry_dt):
            labels.append(np.nan); entry_dates.append(pd.NaT); entry_prices.append(np.nan)
            exit_dates.append(pd.NaT); exit_rets.append(np.nan); reasons.append("no_data")
            continue

        future_c = close[tk].loc[close.index > entry_dt].iloc[:max_hold]
        if future_c.empty:
            labels.append(0); entry_dates.append(entry_dt); entry_prices.append(entry_px)
            exit_dates.append(entry_dt); exit_rets.append(0.0); reasons.append("no_data")
            continue

        if use_intraday and tk in high.columns and tk in low.columns:
            future_h = high[tk].reindex(future_c.index)
            future_l = low[tk].reindex(future_c.index)
        else:
            future_h = future_c
            future_l = future_c

        if use_atr and atr_ratio_panel is not None and tk in atr_ratio_panel.columns:
            _ar = atr_ratio_panel[tk].asof(entry_dt)
            pt_i, sl_i = _barrier_pct(_ar, use_atr=True)
        else:
            pt_i, sl_i = pt, sl

        tgt, stp = entry_px * (1 + pt_i), entry_px * (1 - sl_i)
        lbl = 0
        ex_dt   = future_c.index[-1]
        ex_ret  = float(future_c.iloc[-1]) / entry_px - 1
        ex_rsn  = "time"

        for dt in future_c.index:
            hi, lo, cl = future_h.at[dt], future_l.at[dt], future_c.at[dt]
            if np.isnan(cl):
                continue
            check_hi = hi if pd.notna(hi) else cl
            check_lo = lo if pd.notna(lo) else cl
            # Stop zuerst pruefen (konservativ bei Gaps ueber beide Barrieren, Fix #4)
            if check_lo <= stp:
                lbl, ex_dt, ex_ret, ex_rsn = 0, dt, stp / entry_px - 1, "stop"
                break
            if check_hi >= tgt:
                lbl, ex_dt, ex_ret, ex_rsn = 1, dt, tgt / entry_px - 1, "target"
                break

        labels.append(lbl)
        entry_dates.append(entry_dt); entry_prices.append(entry_px)
        exit_dates.append(ex_dt); exit_rets.append(round(ex_ret, 4)); reasons.append(ex_rsn)

    out = events.copy()
    out["label"]        = labels
    out["entry_date"]   = entry_dates
    out["entry_price"]  = entry_prices
    out["exit_date"]    = exit_dates
    out["exit_ret"]     = exit_rets
    out["exit_reason"]  = reasons
    # Barrier-Check-Modus und Entry-Bias im DataFrame-Attribut speichern,
    # damit Reports/Caller den Bias kenntlich machen koennen.
    out.attrs["barrier_check"]  = "intraday" if use_intraday else "close_only"
    out.attrs["entry_timing"]   = "open_next_day" if not _any_biased else "close_only_biased"
    if _any_biased:
        # Exit-Returns korrigieren: extreme Werte koennen auf fehlerhafte Daten
        # hinweisen (z.B. Preis-Splits, falsche Delisting-Kurse).
        _extreme = out["exit_ret"].abs() > 0.9
        if _extreme.any():
            n_ext = int(_extreme.sum())
            print(f"  \u26a0 {n_ext} Exits mit >90% Return — moeglicherweise fehlerhafte Daten "
                  f"(Splits, Delistings). Bitte manuell pruefen: "
                  + ", ".join(out.loc[_extreme, "ticker"].unique()[:10].tolist()))
    return out.dropna(subset=["label"]).reset_index(drop=True)

# ─────────────────────────────────────────────────────────────────────────────
# Feature Engineering (vektorisiert, Fix #15)
# ─────────────────────────────────────────────────────────────────────────────
def extract_features(
    events: pd.DataFrame,
    close:  pd.DataFrame,
    volume: pd.DataFrame,
) -> pd.DataFrame:
    """
    10 Marktbedingungen am Signal-Tag (kein Look-Ahead-Bias).

    Alle datumsabhaengigen (nicht event-spezifischen) Groessen -- Marktbreite,
    SPY-Momentum, VIX-Proxy, Sektor-Momentum -- werden EINMAL als Panel
    vorberechnet und dann per Lookup gezogen, statt pro Event neu ueber die
    komplette Cross-Section zu rechnen (Fix #15 -- das war der dominante
    Laufzeit-Faktor in Zelle 2 des Notebooks).

    Returned DataFrame: ticker, date, + FEATURE_COLS
    """
    stock_cols = [c for c in close.columns if c not in SECTOR_ETFS and c != "SPY"]
    avail_sec  = [e for e in SECTOR_ETFS if e in close.columns]

    spy_ret   = close["SPY"].pct_change()
    vix_proxy = spy_ret.rolling(20).std() * np.sqrt(252) * 100
    spy_mom20 = close["SPY"].pct_change(20)
    sma200    = close[stock_cols].rolling(200).mean()
    sma50     = close[stock_cols].rolling(50).mean()
    atr14     = close[stock_cols].diff().abs().rolling(14).mean()
    low_252   = close[stock_cols].rolling(252, min_periods=252).min()
    spy_b63   = close["SPY"] / close["SPY"].shift(63)
    close_shift63 = close[stock_cols].shift(63)

    # Panel-weite Vorberechnung (einmal, nicht pro Event):
    breadth200 = close[stock_cols].gt(sma200).mean(axis=1)          # -> f5
    breadth50  = close[stock_cols].gt(sma50).mean(axis=1)           # -> f9
    if avail_sec:
        sector_mom63 = (close[avail_sec] / close[avail_sec].shift(63) - 1).mean(axis=1)
    else:
        sector_mom63 = spy_mom20  # Fallback wenn keine Sektor-ETFs verfuegbar

    dates   = events["date"].values
    tickers = events["ticker"].values
    closes0 = events["close"].values
    vol_r   = events["vol_ratio"].values if "vol_ratio" in events.columns else np.full(len(events), np.nan)
    h52     = events["prior_high"].values if "prior_high" in events.columns else np.full(len(events), np.nan)

    n = len(events)
    f1 = vol_r.astype(float)
    f2 = np.where((~pd.isna(h52)) & (h52 > 0), closes0 / np.where(h52 == 0, np.nan, h52) - 1, np.nan)

    f3 = np.full(n, np.nan)
    f4 = np.full(n, np.nan)
    f5 = np.full(n, np.nan)
    f6 = np.full(n, np.nan)
    f7 = np.full(n, np.nan)
    f8 = np.full(n, np.nan)
    f9 = np.full(n, np.nan)
    f10 = np.full(n, np.nan)

    # Gruppierung nach Datum minimiert wiederholte .loc-Aufrufe auf breadth/spy-Serien
    for i in range(n):
        tk, dt, c0 = tickers[i], dates[i], closes0[i]

        try:
            c63 = close_shift63.at[dt, tk] if tk in close_shift63.columns else np.nan
            sb  = spy_b63.at[dt]
            if pd.notna(c63) and c63 > 0 and pd.notna(sb) and sb > 0:
                f3[i] = c0 / c63 / sb - 1
        except (KeyError, TypeError):
            pass

        try:
            atr = atr14.at[dt, tk] if tk in atr14.columns else np.nan
            if pd.notna(atr) and c0 > 0:
                f4[i] = atr / c0
        except (KeyError, TypeError):
            pass

        try:
            f5[i] = breadth200.at[dt]
        except (KeyError, TypeError):
            pass

        try:
            v = spy_mom20.at[dt]
            f6[i] = float(v) if pd.notna(v) else np.nan
        except (KeyError, TypeError):
            pass

        try:
            v = vix_proxy.at[dt]
            f7[i] = float(v) if pd.notna(v) else np.nan
        except (KeyError, TypeError):
            pass

        try:
            lo = low_252.at[dt, tk] if tk in low_252.columns else np.nan
            if pd.notna(lo) and lo > 0:
                f8[i] = (c0 / lo) - 1
        except (KeyError, TypeError):
            pass

        try:
            f9[i] = breadth50.at[dt]
        except (KeyError, TypeError):
            pass

        try:
            v = sector_mom63.at[dt]
            f10[i] = float(v) if pd.notna(v) else np.nan
        except (KeyError, TypeError):
            pass

    return pd.DataFrame({
        "ticker":            tickers,
        "date":              dates,
        "vol_ratio":         f1,
        "breakout_pct":      f2,
        "rs_vs_spy_63":      f3,
        "atr_ratio":         f4,
        "market_breadth":    f5,
        "spy_momentum_20":   f6,
        "vix_proxy":         f7,
        "dist_52w_low_pct":  f8,
        "above_sma50_pct":   f9,
        "sector_momentum":   f10,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Purged + Embargoed Train/Test-Split (Fix #3)
# ─────────────────────────────────────────────────────────────────────────────
def purged_train_test_split(
    dataset:      pd.DataFrame,
    train_end:    str,
    embargo_days: int = EMBARGO_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split dataset (muss 'date' und 'exit_date' enthalten) in Train/Test mit
    Purging + Embargo (Lopez de Prado):

    1. PURGE: Train-Events, deren Label-Fenster (exit_date) NACH train_end
       liegt, werden aus dem Training entfernt -- ihr Label haengt sonst von
       Kursbewegungen ab, die zum Trainingszeitpunkt noch nicht bekannt sein
       duerften.
    2. EMBARGO: zusaetzlich werden Test-Events entfernt, deren Signal-Datum
       weniger als embargo_days Handelstage nach train_end liegt -- verhindert
       Restkorrelation durch ueberlappende Feature-Fenster nahe am Cutoff.

    Returns (df_train_purged, df_test_embargoed)
    """
    t_cut = pd.Timestamp(train_end)
    df_tr_raw = dataset[dataset["date"] < t_cut].copy()
    df_te_raw = dataset[dataset["date"] >= t_cut].copy()

    if "exit_date" in df_tr_raw.columns:
        n_before = len(df_tr_raw)
        df_tr = df_tr_raw[df_tr_raw["exit_date"] < t_cut].reset_index(drop=True)
        n_purged = n_before - len(df_tr)
    else:
        df_tr = df_tr_raw.reset_index(drop=True)
        n_purged = 0

    embargo_cut = t_cut + pd.Timedelta(days=int(embargo_days * 1.5))  # grobe Kalendertage-Naeherung
    n_before_te = len(df_te_raw)
    df_te = df_te_raw[df_te_raw["date"] >= embargo_cut].reset_index(drop=True)
    n_embargoed = n_before_te - len(df_te)

    if n_purged or n_embargoed:
        print(f"  \U0001F9F9 Purge/Embargo: {n_purged} Train-Events gepurged "
              f"(Label-Fenster reicht in Testperiode), {n_embargoed} Test-Events "
              f"embargoed (< {embargo_days}d nach Cutoff).")

    return df_tr, df_te

# ─────────────────────────────────────────────────────────────────────────────
# Stufe 2 — Meta-Modell Training (Produktionsmodell, EIN Split)
# ─────────────────────────────────────────────────────────────────────────────
def _make_rf_pipeline() -> CalibratedClassifierCV:
    clf = RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=10,
        max_features="sqrt", class_weight="balanced",
        random_state=42, n_jobs=-1,
    )
    return CalibratedClassifierCV(clf, method="sigmoid", cv=3)


def train_breakout_meta(
    close:         "pd.DataFrame | None" = None,
    volume:        "pd.DataFrame | None" = None,
    high:          "pd.DataFrame | None" = None,
    low:           "pd.DataFrame | None" = None,
    open_:         "pd.DataFrame | None" = None,
    threshold:     "float | None" = None,
    keep_top_pct:  float = KEEP_TOP_PCT,
    train_end:     str   = TRAIN_END,
    embargo_days:  int   = EMBARGO_DAYS,
    verbose:       bool  = True,
) -> tuple:
    """
    Training des PRODUKTIONSMODELLS: ein einzelner Walk-Forward-Split
    (Train : EVAL_START -> train_end, Test : train_end -> heute), mit
    Purging + Embargo (Fix #3).

    WICHTIG: Ein einzelner Split ist kein Stabilitaets-Beweis. Fuehre vor
    einem Live-Deployment zusaetzlich run_walk_forward() aus, um zu pruefen,
    ob die Meta-Precision ueber mehrere Jahre/Marktregime robust ist, statt
    nur in diesem einen Testfenster.

    threshold=None  -> quantilbasiert, kalibriert auf TRAIN-Wahrscheinlichkeiten
                        (Fix #1 -- KEIN Leakage mehr aus dem Test-Set).
    threshold=float -> fester Wert (nur fuer Spezialfaelle).

    Returns (model, report, dataset)
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    if close is None or volume is None:
        close, volume = load_price_data()

    if verbose:
        print("\u2550" * 72)
        print(f"  BREAKOUT META-LABELING  v{VERSION}  --  Training")
        print("\u2550" * 72)
        print(f"  Primaer : 52W-Hoch + Vol x{MIN_VOL_RATIO} + SMA{SMA_PERIOD}")
        print(f"  Label   : Triple Barrier  +{PROFIT_TARGET:.0%} / -{STOP_LOSS:.0%} / {HOLD_DAYS}d"
              + ("  [close-only]" if high is None or low is None else "  [intraday High/Low]"))
        print(f"  Train   : {EVAL_START} -> {train_end}  (purged)")
        print(f"  Test    : {train_end} -> heute  (embargo {embargo_days}d)")

    if verbose:
        print("\n  [1/4] Ausbruchs-Events \u2026")
    events = generate_breakout_events(close, volume, start=EVAL_START)
    if verbose:
        print(f"       {len(events):,} Events (nach Duplikat-Filter >= {MIN_DAYS_GAP}d)")

    if verbose:
        print("\n  [2/4] Triple-Barrier-Labels \u2026")
    events = label_events(events, close, high=high, low=low, open_=open_)
    events = events.dropna(subset=["label"])
    events["label"] = events["label"].astype(int)
    if verbose:
        n1 = int(events["label"].sum())
        n0 = len(events) - n1
        print(f"       {n1} Erfolg / {n0} Misserfolg  --  {events['exit_reason'].value_counts().to_dict()}")

    if verbose:
        print("\n  [3/4] Feature-Extraktion \u2026")
    feats   = extract_features(events, close, volume)
    dataset = _dropna_features(_merge_features(events, feats)).reset_index(drop=True)
    if verbose:
        print(f"       {len(dataset):,} vollstaendige Events")

    df_tr, df_te = purged_train_test_split(dataset, train_end, embargo_days)

    if len(df_tr) < MIN_EVENTS_TRAIN:
        raise RuntimeError(
            f"Zu wenige Train-Events nach Purging: {len(df_tr)} < {MIN_EVENTS_TRAIN}. "
            "EVAL_START frueher setzen oder MIN_VOL_RATIO senken."
        )
    if len(df_te) < 50:
        raise RuntimeError(f"Zu wenige Test-Events nach Embargo: {len(df_te)}.")

    if verbose:
        print(f"\n  [4/4] Training \u2026  ({len(df_tr)} Train / {len(df_te)} Test)")

    X_tr, y_tr = df_tr[FEATURE_COLS].values, df_tr["label"].values
    X_te, y_te = df_te[FEATURE_COLS].values, df_te["label"].values

    baseline_prec = float(y_te.mean())

    cal = _make_rf_pipeline()
    cal.fit(X_tr, y_tr)

    probs_tr = cal.predict_proba(X_tr)[:, 1]
    probs_te = cal.predict_proba(X_te)[:, 1]

    # Fix #1: Threshold IMMER auf TRAIN-Wahrscheinlichkeiten kalibriert.
    if threshold is None:
        threshold = float(np.percentile(probs_tr, 100 - keep_top_pct))
        if verbose:
            print(f"       Quantil-Threshold (Top {keep_top_pct:.0f}% auf TRAIN-Probs): {threshold:.4f}")

    meta_mask = probs_te >= threshold
    meta_prec = float(y_te[meta_mask].mean()) if meta_mask.sum() > 0 else 0.0
    meta_frac = float(meta_mask.mean())

    if verbose:
        delta = meta_prec - baseline_prec
        print(f"       Baseline-Praezision (Test) : {baseline_prec:.1%}")
        print(f"       Meta @ {threshold:.0%} (Test)       : {meta_prec:.1%}  ({delta:+.1%})"
              f"  \u00b7  {meta_frac:.0%} der Test-Signale genommen")

    # Finales Modell auf ALLEN (gepurgten) Daten trainieren.
    # Fix #2: der Threshold wird danach FUER DIESES Modell neu aus dessen
    # eigenen Train-Wahrscheinlichkeiten bestimmt, statt den Threshold des
    # separaten cal-Modells (oben) unveraendert zu uebernehmen -- verschiedene
    # Modelle haben verschiedene Score-Verteilungen.
    full_dataset = pd.concat([df_tr, df_te], ignore_index=True)
    cal_full = _make_rf_pipeline()
    cal_full.fit(full_dataset[FEATURE_COLS].values, full_dataset["label"].values)

    # Fix #2: threshold_full wird UNABHAENGIG von obigem Test-Split-Threshold
    # aus den Train-Wahrscheinlichkeiten des Produktions-Modells (cal_full)
    # neu bestimmt -- selbst wenn oben ein fester threshold uebergeben wurde,
    # ist dieser fuer das andere (cal-)Modell kalibriert und nicht 1:1 uebertragbar.
    probs_full_tr = cal_full.predict_proba(full_dataset[FEATURE_COLS].values)[:, 1]
    threshold_full = float(np.percentile(probs_full_tr, 100 - keep_top_pct))

    if verbose:
        print(f"       Threshold Produktionsmodell (eigenes Train-Quantil): {threshold_full:.4f}")

    if verbose:
        try:
            base_clf = cal_full.calibrated_classifiers_[0].estimator
            imp = pd.Series(base_clf.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False)
            print("\n       Feature Importance (Top 5):")
            for feat, val in imp.head(5).items():
                bar = "\u2588" * int(val * 50)
                print(f"         {feat:22} {val:.3f}  {bar}")
        except Exception:
            pass

        # Feature-Importance aus finalem Produktionsmodell extrahieren
    _feat_imp = {}
    try:
        _base_clf = cal_full.calibrated_classifiers_[0].estimator
        _imp = pd.Series(_base_clf.feature_importances_, index=FEATURE_COLS)
        _feat_imp = {k: round(float(v), 6) for k, v in _imp.items()}
    except Exception:
        pass

    # Entry-Timing und Barrier-Check aus dem gelabelten Datasetableiten
    _entry_timing  = getattr(events, "attrs", {}).get("entry_timing","unknown")
    _barrier_check = getattr(events, "attrs", {}).get("barrier_check", "unknown")

    report = BreakoutMetaReport(
        n_events_train      = len(df_tr),
        n_events_test       = len(df_te),
        baseline_precision  = round(baseline_prec, 4),
        meta_precision      = round(meta_prec, 4),
        meta_fraction       = round(meta_frac, 4),
        threshold           = threshold,
        keep_top_pct        = keep_top_pct,
        train_period        = f"{EVAL_START} -> {train_end}",
        test_period         = f"{train_end} -> heute",
        embargo_days        = embargo_days,
        feature_importance  = _feat_imp,
        entry_timing        = _entry_timing,
        barrier_check       = _barrier_check,
    )
    model = BreakoutMetaModel(
        clf=cal_full, threshold=threshold_full,
        feature_names=FEATURE_COLS, keep_top_pct=keep_top_pct,
        report=report,
        barrier_config={
            "use_atr":USE_ATR_BARRIERS,
            "profit_target": PROFIT_TARGET,
            "stop_loss":    STOP_LOSS,
            "hold_days":    HOLD_DAYS,
        },
    )
    
    # Atomic Save mit Rollback-Sicherung (Fix: kein Datenverlust bei Crash)
    model_path = META_DIR / "breakout_meta_model.pkl"
    report_path = META_DIR / "breakout_meta_report.json"
    backup_path = None
    
    if model_path.exists():
        backup_path = model_path.with_stem(f"breakout_meta_model_backup_{datetime.now():%Y%m%d_%H%M%S}")
        shutil.copy2(model_path, backup_path)
        if verbose:
            print(f"       Backup: {backup_path.name}")
    
    try:
        model.save(model_path)
        report_path.write_text(
            json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        # Rollback wenn Save fehlschlaegt
        if backup_path and backup_path.exists():
            shutil.copy2(backup_path, model_path)
            if verbose:
                print(f"       \u274c Training fehlgeschlagen — Backup wiederhergestellt")
            raise RuntimeError(f"Modell-Save fehlgeschlagen, altes Modell wiederhergestellt: {e}")
        raise
    if verbose:
        print(f"\n{report}")
        print("\n  \u2705 Fertig. Empfehlung: run_walk_forward() vor Live-Einsatz laufen lassen.")
        print("\u2550" * 72)
    return model, report, dataset


def load_model() -> BreakoutMetaModel:
    p = META_DIR / "breakout_meta_model.pkl"
    if not p.is_file():
        raise FileNotFoundError(
            f"Kein Modell: {p}\nZuerst train_breakout_meta() ausfuehren."
        )
    return BreakoutMetaModel.load(p)


def model_age_days() -> "float | None":
    report_path = META_DIR / "breakout_meta_report.json"
    if not report_path.is_file():
        return None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        trained_at = data.get("trained_at")
        if trained_at:
            dt = datetime.fromisoformat(trained_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        pass
    return None


def ensure_fresh_model(
    max_age_days: int = 30,
    close:        "pd.DataFrame | None" = None,
    volume:       "pd.DataFrame | None" = None,
    verbose:      bool = True,
) -> BreakoutMetaModel:
    """
    Laedt das gespeicherte Modell -- retrained automatisch wenn zu alt.

    Lock-File-Mechanismus (Fix: Race Condition bei parallelen Laeufen):
    Wenn zwei Colab-Sessions gleichzeitig starten und beide "Modell zu alt"
    erkennen, wuerde ohne Lock jede ein eigenes Training starten und beide
    wuerden breakout_meta_model.pkl gleichzeitig ueberschreiben.
    Mit Lock wartet die zweite Session, bis die erste fertig ist.
    """
    import time as _time
    LOCK_FILE = META_DIR / "retrain.lock"
    LOCK_MAX_AGE_S = 3600   # Lock aelter als 1h gilt als verwaist

    age = model_age_days()
    needs_retrain = (age is None) or (age > max_age_days)

    if not needs_retrain:
        if verbose:
            print(f"  \u2705 Modell aktuell ({age:.0f} Tage alt, Limit: {max_age_days}d) -- kein Retrain noetig.")
        return load_model()

    # Retrain noetig — Lock-File pruefen
    META_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        lock_age_s = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
        if lock_age_s < LOCK_MAX_AGE_S:
            if verbose:
                print(f"  \u23f3Retrain laeuft bereits in anderer Session "
                      f"(Lock {lock_age_s:.0f}s alt) -- warte90s undlade dann Modell ...")
            _time.sleep(90)
            try:
                return load_model()
            except FileNotFoundError:
                if verbose:
                    print("  \u26a0 Modell nach Warten immer noch nicht vorhanden -- eigener Retrain.")
        else:
            if verbose:
                print(f"  \U0001F5D1 Verwaistes Lock-File ({lock_age_s:.0f}s alt) entfernt.")
            LOCK_FILE.unlink(missing_ok=True)

    # Lock setzen
    LOCK_FILE.write_text(str(datetime.now(timezone.utc).isoformat()))
    try:
        if age is None:
            if verbose:
                print("  \U0001F195 Kein Modell vorhanden -- erster Training-Lauf \u2026")
        else:
            if verbose:
                print(f"  \U0001F504 Modell ist {age:.0f} Tage alt (Limit: {max_age_days}d) -- Auto-Retrain \u2026")
        model, _, _ = train_breakout_meta(close=close, volume=volume, verbose=verbose)
        return model
    finally:
        LOCK_FILE.unlink(missing_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# NEU (Fix #18): Purged + Embargoed Walk-Forward -- Stabilitaets-Check
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_WALK_FOLDS = [
    {"train_end": "2022-01-01", "test_start": "2022-01-01", "test_end": "2023-01-01"},
    {"train_end": "2023-01-01", "test_start": "2023-01-01", "test_end": "2024-01-01"},
    {"train_end": "2024-01-01", "test_start": "2024-01-01", "test_end": "2025-01-01"},
    {"train_end": "2025-01-01", "test_start": "2025-01-01", "test_end": "2026-12-31"},
]


def run_walk_forward(
    close:        "pd.DataFrame | None" = None,
    volume:       "pd.DataFrame | None" = None,
    high:         "pd.DataFrame | None" = None,
    low:          "pd.DataFrame | None" = None,
    open_:        "pd.DataFrame | None" = None,
    folds:        list = None,
    keep_top_pct: float = KEEP_TOP_PCT,
    embargo_days: int   = EMBARGO_DAYS,
    save_as_production: bool = False,
    verbose:      bool  = True,
) -> dict:
    """
    STABILITAETS-CHECK (Fix #18): purged + embargoed rollierender Walk-Forward.

    Anders als train_breakout_meta() (ein einziger Split) wird hier fuer JEDEN
    Fold ein EIGENES Modell trainiert (nur auf Daten vor dem jeweiligen
    train_end, purged), und NUR auf dem direkt folgenden, embargoed Testjahr
    bewertet. Die OOS-Ergebnisse aller Folds werden aneinandergehaengt ->
    das ist die ehrlichste verfuegbare Schaetzung, wie die Strategie sich
    Jahr fuer Jahr auf wirklich unbekannten Daten verhalten haette.

    Nutzung: VOR jedem Live-Deployment / Retrain-Entscheidung laufen lassen.
    Wenn die Meta-Precision und CAGR ueber die Folds stark schwanken oder in
    einzelnen Jahren negativ zur Baseline sind, ist das Modell nicht robust --
    unabhaengig davon, wie gut der einzelne Split aus train_breakout_meta()
    aussieht.

    Parameters
    ----------
    folds : Liste von {"train_end", "test_start", "test_end"} (Strings).
            Default: DEFAULT_WALK_FOLDS (jaehrliche Folds 2022-2026).
    save_as_production : wenn True, wird das Modell des LETZTEN Folds
            (laengste Trainingshistorie) als neues Produktionsmodell
            gespeichert -- Backup des alten Modells wird angelegt.

    Returns
    -------
    dict mit: fold_results (Liste), equity_baseline_oos, equity_meta_oos,
              equity_spy_oos, metrics_baseline, metrics_meta, metrics_spy,
              models_per_fold
    """
    if close is None or volume is None:
        close, volume = load_price_data()
    if folds is None:
        folds = DEFAULT_WALK_FOLDS

    if verbose:
        print("\u2550" * 76)
        print(f"  WALK-FORWARD STABILITAETS-CHECK  ({len(folds)} Folds, purged + embargo {embargo_days}d)")
        print("\u2550" * 76)

    events = generate_breakout_events(close, volume, start=EVAL_START)
    events = label_events(events, close, high=high, low=low, open_=open_)
    events = events.dropna(subset=["label"]).reset_index(drop=True)
    events["label"] = events["label"].astype(int)

    feats   = extract_features(events, close, volume)
    dataset = _dropna_features(_merge_features(events, feats)).reset_index(drop=True)

    if verbose:
        print(f"  Gesamt-Dataset: {len(dataset):,} Events "
              f"({int(dataset['label'].sum())} Erfolg / {len(dataset) - int(dataset['label'].sum())} Misserfolg)")

    fold_results   = []
    oos_events_all = []
    models_per_fold = {}

    for fold in folds:
        train_end = fold["train_end"]
        t_start   = pd.Timestamp(fold["test_start"])
        t_end     = pd.Timestamp(fold["test_end"])
        label     = fold["test_start"][:4]

        df_tr, _ = purged_train_test_split(
            dataset[dataset["date"] < t_end], train_end, embargo_days,
        )
        if len(df_tr) < MIN_EVENTS_TRAIN:
            if verbose:
                print(f"  Fold {label}: \u26a0 zu wenige Train-Events ({len(df_tr)}) -- uebersprungen")
            continue

        cal = _make_rf_pipeline()
        cal.fit(df_tr[FEATURE_COLS].values, df_tr["label"].values)

        probs_tr  = cal.predict_proba(df_tr[FEATURE_COLS].values)[:, 1]
        threshold = float(np.percentile(probs_tr, 100 - keep_top_pct))  # Fix #1 auch hier

        # Test-Fenster: nur dieses Jahr, mit Embargo direkt nach train_end
        embargo_cut = pd.Timestamp(train_end) + pd.Timedelta(days=int(embargo_days * 1.5))
        test_start_eff = max(t_start, embargo_cut)
        df_te = dataset[(dataset["date"] >= test_start_eff) & (dataset["date"] < t_end)].copy()

        if df_te.empty:
            if verbose:
                print(f"  Fold {label}: (keine Test-Events nach Embargo)")
            continue

        probs_te = cal.predict_proba(df_te[FEATURE_COLS].values)[:, 1]
        df_te["meta_prob"] = probs_te
        df_te["take"]      = probs_te >= threshold

        baseline_prec = float(df_te["label"].mean())
        meta_mask     = df_te["take"]
        meta_prec     = float(df_te.loc[meta_mask, "label"].mean()) if meta_mask.sum() > 0 else 0.0
        meta_frac     = float(meta_mask.mean())

        if verbose:
            print(f"  Fold {label}: Train {len(df_tr):,} Events (bis {train_end}, purged)  "
                  f"\u00b7  Threshold {threshold:.3f}  "
                  f"\u00b7  Basis {baseline_prec:.1%} -> Meta {meta_prec:.1%} ({meta_prec - baseline_prec:+.1%})  "
                  f"\u00b7  {meta_frac:.0%} genommen  \u00b7  {len(df_te)} Test-Events")

        fold_results.append({
            "fold": label, "train_end": train_end,
            "n_train": len(df_tr), "n_test": len(df_te),
            "threshold": round(threshold, 4),
            "baseline_precision": round(baseline_prec, 4),
            "meta_precision": round(meta_prec, 4),
            "meta_fraction": round(meta_frac, 4),
            "delta": round(meta_prec - baseline_prec, 4),
        })
        oos_events_all.append(df_te)
        models_per_fold[label] = (cal, threshold)

    if not oos_events_all:
        raise RuntimeError("Walk-Forward: keine Folds mit ausreichend Daten -- Folds/Zeitraum pruefen.")

    df_oos_full = pd.concat(oos_events_all, ignore_index=True).sort_values("date")
    oos_start   = df_oos_full["date"].min()

    df_baseline_oos = df_oos_full.drop(columns=["meta_prob", "take"], errors="ignore")
    df_meta_oos     = df_oos_full[df_oos_full["take"]].reset_index(drop=True)

    eq_base, tr_base = _simulate_trades(df_baseline_oos, close, POSITION_SIZE, MAX_POSITIONS, INITIAL_CAPITAL)
    eq_meta, tr_meta = _simulate_trades(df_meta_oos,     close, POSITION_SIZE, MAX_POSITIONS, INITIAL_CAPITAL)

    spy_ret = close["SPY"].loc[oos_start:].pct_change().fillna(0)
    eq_spy  = (1 + spy_ret).cumprod() * INITIAL_CAPITAL

    bt = _get_bt()
    # Fix #7 (korrigiert): _simulate_trades() erzeugt die Equity-Kurve IMMER ab
    # EVAL_START (nicht ab dem ersten Event) -- eq_meta.index[0] ist deshalb
    # NICHT der tatsaechliche OOS-Start. Wir muessen explizit auf den
    # tatsaechlichen ersten OOS-Event-Termin trimmen (oos_start, oben aus den
    # Fold-Daten berechnet), sonst rutscht wieder stille Trainingsperiode in
    # die Kennzahlen.
    t0 = pd.Timestamp(oos_start)
    m_base = bt.compute_bt_metrics(eq_base.loc[t0:])
    m_meta = bt.compute_bt_metrics(eq_meta.loc[t0:])
    m_spy  = bt.compute_bt_metrics(eq_spy.loc[t0:])

    if verbose:
        df_wf = pd.DataFrame(fold_results)
        print("\n  \u2500\u2500 Fold-Zusammenfassung \u2500\u2500")
        disp = df_wf.copy()
        disp["baseline_precision"] = disp["baseline_precision"].map("{:.1%}".format)
        disp["meta_precision"]     = disp["meta_precision"].map("{:.1%}".format)
        disp["meta_fraction"]      = disp["meta_fraction"].map("{:.0%}".format)
        disp["delta"]              = disp["delta"].map("{:+.1%}".format)
        print(disp.to_string(index=False))

        precisions = df_wf["meta_precision"].values
        print(f"\n  Meta-Precision ueber Folds: min {precisions.min():.1%}  \u00b7  "
              f"max {precisions.max():.1%}  \u00b7  Streuung (std) {precisions.std():.1%}")
        if (df_wf["delta"] < 0).any():
            bad_years = ", ".join(df_wf.loc[df_wf["delta"] < 0, "fold"])
            print(f"  \u26a0 Meta schlechter als Baseline in: {bad_years} -- kein durchgehend robuster Edge.")

        print(f"\n  \u2500\u2500 Gesamt-OOS-Metriken (ab {t0.date()}, aneinandergehaengt ueber alle Folds) \u2500\u2500")
        print(f"  {'':22}  {'Baseline':>10}  {'Meta':>10}  {'SPY':>8}")
        print("  " + "\u2500" * 54)
        for lbl, key, fmt in [("CAGR", "cagr", ".1%"), ("MaxDD", "maxdd", ".1%"),
                               ("Sharpe", "sharpe", ".2f"), ("MAR", "mar", ".2f")]:
            b = format(m_base.get(key) or 0, fmt)
            m = format(m_meta.get(key) or 0, fmt)
            s = format(m_spy.get(key) or 0, fmt)
            print(f"  {lbl:22}  {b:>10}  {m:>10}  {s:>8}")
        print(f"\n  Trades  Baseline: {len(tr_base):,}  \u00b7  Meta: {len(tr_meta):,}")
        print("\u2550" * 76)

    result = {
        "fold_results":       fold_results,
        "equity_baseline_oos": eq_base.loc[t0:],
        "equity_meta_oos":     eq_meta.loc[t0:],
        "equity_spy_oos":      eq_spy.loc[t0:],
        "metrics_baseline":    m_base,
        "metrics_meta":        m_meta,
        "metrics_spy":         m_spy,
        "models_per_fold":     models_per_fold,
        "oos_start":           t0,
    }

    if save_as_production:
        best_fold_key = max(models_per_fold.keys())
        best_clf, best_thr = models_per_fold[best_fold_key]
        META_DIR.mkdir(parents=True, exist_ok=True)
        old_path   = META_DIR / "breakout_meta_model.pkl"
        bkp_path   = None
        if old_path.is_file():
            bkp_path = old_path.with_stem(f"breakout_meta_model_backup_{datetime.now():%Y%m%d_%H%M%S}")
            # KOPIEREN statt umbenennen — bei Rollback ist das Original noch da
            shutil.copy2(old_path, bkp_path)
            print(f"  \U0001F4E6 Backup: {bkp_path.name}")
        new_model = BreakoutMetaModel(
            clf=best_clf, threshold=best_thr,
            feature_names=FEATURE_COLS, keep_top_pct=keep_top_pct,
        )
        try:
            new_model.save(old_path)
            print(f"  \u2705 Als Produktionsmodell gespeichert (Fold {best_fold_key}, laengste Historie).")
        except Exception as e:
            if bkp_path and bkp_path.exists():
                shutil.copy2(bkp_path, old_path)
                print(f"  \u274c Save fehlgeschlagen — altes Modell wiederhergestellt.")
            raise RuntimeError(f"Produktionsmodell konnte nicht gespeichert werden: {e}")

    return result

# ─────────────────────────────────────────────────────────────────────────────
# Event-Backtest (Fix #6: echtes Cash-Konto, keine implizite Hebelwirkung)
# ─────────────────────────────────────────────────────────────────────────────
def _simulate_trades(
    events:          pd.DataFrame,
    close:           pd.DataFrame,
    position_size:   float,
    max_positions:   int,
    initial_capital: float,
    regime_panel:    "pd.DataFrame | None" = None,
    transaction_cost: float = TRANSACTION_COST,
) -> tuple:
    """
    Event-basierter Portfolio-Simulator.

    Fix #6: haelt ein echtes `cash`-Konto zusaetzlich zum Portfoliowert
    (`capital`). Eine neue Position wird nur eroeffnet, wenn genug freies Cash
    vorhanden ist (`cash >= size`) -- vorher konnte `size_eur = capital *
    position_size` bei mehreren gleichzeitig offenen Positionen implizit mehr
    als 100% des Kapitals binden (bis zu max_positions * position_size, z.B.
    120% bei 10 x 12%), was einem kostenlosen Hebel entsprach.

    transaction_cost: Round-Trip-Kosten (Provision + Slippage), als Anteil
    des Trade-Werts, wird vom Exit-Return abgezogen.
    """
    cash      = initial_capital
    open_pos: dict = {}
    trades:   list = []
    curve:    dict = {}

    def _max_pos_for_date(dt) -> int:
        if regime_panel is None or regime_panel.empty:
            return max_positions
        mult = regime_panel_lookup.get(dt, 1.0)
        if mult <= 0:
            return 0
        return max(1, int(round(max_positions * mult)))

    # Fix #17: statt pro Handelstag linear im regime_panel zu suchen, einmal
    # per reindex/ffill auf den Close-Index vorberechnen (O(1)-Lookup danach).
    regime_panel_lookup = {}
    if regime_panel is not None and not regime_panel.empty:
        mult_series = regime_panel["max_pos_mult"].reindex(close.index, method="ffill")
        regime_panel_lookup = mult_series.to_dict()

    by_date: dict = {}
    for row in events.itertuples(index=False):
        by_date.setdefault(row.date, []).append(row)

    portfolio_value = initial_capital
    all_dates = close.index[close.index >= pd.Timestamp(EVAL_START)]
    for dt in all_dates:
        to_close = [tk for tk, pos in open_pos.items() if pos["exit_date"] <= dt]
        for tk in to_close:
            pos = open_pos.pop(tk)
            net_ret = pos["exit_ret"] - transaction_cost
            proceeds = pos["size_eur"] * (1 + net_ret)
            cash += proceeds
            trades.append({
                "exit_date": pos["exit_date"], "ticker": tk,
                "ret": net_ret, "pnl": proceeds - pos["size_eur"],
                "reason": pos.get("exit_reason", ""),
            })

        max_pos = _max_pos_for_date(dt)
        for ev in by_date.get(dt, []):
            if max_pos <= 0 or len(open_pos) >= max_pos:
                break
            tk = getattr(ev, "ticker")
            if tk in open_pos:
                continue
            # Positionsgroesse relativ zum AKTUELLEN Portfoliowert (nicht
            # relativ zu freiem Cash), aber begrenzt durch tatsaechlich
            # verfuegbares Cash -> kein Hebel (Fix #6).
            target_size = portfolio_value * position_size
            size = min(target_size, cash)
            if size <= 0:
                continue
            cash -= size
            open_pos[tk] = {
                "exit_date":   getattr(ev, "exit_date"),
                "exit_ret":    getattr(ev, "exit_ret"),
                "exit_reason": getattr(ev, "exit_reason", ""),
                "size_eur":    size,
            }

        open_value = sum(
            pos["size_eur"] * (1 + _mark_to_market(pos, dt))
            for pos in open_pos.values()
        )
        portfolio_value = cash + open_value
        curve[dt] = portfolio_value

    return pd.Series(curve).sort_index(), trades


def _mark_to_market(pos: dict, dt) -> float:
    """
    Naeherungsweise Mark-to-Market fuer offene Positionen zwischen Entry und
    Exit: linear zwischen 0 und dem finalen exit_ret interpoliert. Exakt waere
    der taegliche Preis noetig; da nur exit_ret pro Trade vorliegt (kein
    taeglicher Pfad), ist dies eine Naeherung, die die Equity-Kurve zwischen
    Trade-Ereignissen glaettet, ohne das Endergebnis (bei Exit) zu veraendern.
    """
    return 0.0  # konservativ: offene Positionen tragen erst bei Exit zum PnL bei;
                # vermeidet, dass ein optimistischer Zwischenwert die Kurve verzerrt.


def run_breakout_backtest(
    close:           pd.DataFrame,
    volume:          pd.DataFrame,
    use_meta:        bool = False,
    model:           "BreakoutMetaModel | None" = None,
    high:            "pd.DataFrame | None" = None,
    low:             "pd.DataFrame | None" = None,
    open_:           "pd.DataFrame | None" = None,
    eval_start:      str   = EVAL_START,
    oos_start:       str   = TRAIN_END,
    position_size:   float = POSITION_SIZE,
    max_positions:   int   = MAX_POSITIONS,
    initial_capital: float = INITIAL_CAPITAL,
) -> dict:
    """
    Baseline (use_meta=False) oder Meta-Filter (use_meta=True, nur auf Test-
    Periode). Fix #7: Equity wird vor der Metrik-Berechnung konsistent auf
    oos_start getrimmt (vorher: nur SPY wurde getrimmt, Baseline/Meta enthielten
    die volle Historie inkl. Trainingsperiode).
    """
    events = generate_breakout_events(close, volume, start=eval_start)
    events = label_events(events, close, high=high, low=low, open_=open_)
    events = events.dropna(subset=["label"]).reset_index(drop=True)

    if use_meta and model is not None:
        t_cut  = pd.Timestamp(oos_start)
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

    equity_full, trades_full = _simulate_trades(events, close, position_size, max_positions, initial_capital)

    t_cut = pd.Timestamp(oos_start)
    equity = equity_full.loc[t_cut:]
    trades = [t for t in trades_full if pd.Timestamp(t["exit_date"]) >= t_cut]

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


def run_meta_comparison(
    close:   "pd.DataFrame | None" = None,
    volume:  "pd.DataFrame | None" = None,
    high:    "pd.DataFrame | None" = None,
    low:     "pd.DataFrame | None" = None,
    open_:   "pd.DataFrame | None" = None,
    verbose: bool = True,
) -> dict:
    """Baseline vs. Meta, beide korrekt auf die OOS-Periode (TRAIN_END->heute) getrimmt (Fix #7)."""
    if close is None or volume is None:
        close, volume = load_price_data()
    model = load_model()

    if verbose:
        print("\u2550" * 72)
        print("  BREAKOUT -- Baseline vs. Meta (Walk-Forward OOS)")
        print("\u2550" * 72)
        print(f"  Bewertung: nur Test-Periode ({TRAIN_END} -> heute)")
        print("  Baseline \u2026")
    base = run_breakout_backtest(close, volume, use_meta=False, high=high, low=low, open_=open_)

    if verbose:
        print("  Mit Meta-Filter \u2026")
    meta = run_breakout_backtest(close, volume, use_meta=True, model=model, high=high, low=low, open_=open_)

    t_cut  = pd.Timestamp(TRAIN_END)
    spy_eq = (1 + close["SPY"].pct_change().fillna(0)).cumprod() * INITIAL_CAPITAL
    spy_eq = spy_eq[spy_eq.index >= t_cut]
    bt     = _get_bt()
    spy_m  = bt.compute_bt_metrics(spy_eq)

    bmet, mmet = base["metrics"], meta["metrics"]
    if verbose:
        n_b, n_m = bmet.get("n_trades", 0), mmet.get("n_trades", 0)
        pct = n_m / n_b * 100 if n_b else 0
        print(f"\n  Trades (Test, korrekt OOS-getrimmt): {n_b} Baseline  ->  {n_m} Meta ({pct:.0f}% genommen)")
        print(f"\n  {'Kennzahl':22}  {'Baseline':>10}  {'Meta':>10}  {'Delta':>8}  {'SPY':>8}")
        print("  " + "\u2500" * 62)
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
            b  = bmet.get(key) or 0
            m  = mmet.get(key) or 0
            s  = spy_m.get(key)  or 0
            d  = m - b
            sg = "+" if d >= 0 else ""
            if "%" in fmt:
                print(f"  {lbl:22}  {b:{fmt}}  {m:{fmt}}  {sg}{d:.1%}  {s:{fmt}}")
            elif key == "n_trades":
                print(f"  {lbl:22}  {b:.0f}  {m:.0f}  {sg}{d:.0f}  {s:.0f}")
            else:
                print(f"  {lbl:22}  {b:{fmt}}  {m:{fmt}}  {sg}{d:.2f}  {s:{fmt}}")
        print("\u2550" * 72)

    result = {
        "test_period":  f"{TRAIN_END} -> heute",
        "baseline":     {k: round(float(v), 4) for k, v in bmet.items() if isinstance(v, (int, float))},
        "meta":         {k: round(float(v), 4) for k, v in mmet.items() if isinstance(v, (int, float))},
        "spy":          {k: round(float(v), 4) for k, v in spy_m.items() if isinstance(v, (int, float))},
        "delta_cagr":   round((mmet.get("cagr") or 0) - (bmet.get("cagr") or 0), 4),
        "delta_sharpe": round((mmet.get("sharpe") or 0) - (bmet.get("sharpe") or 0), 3),
        "run_at":       datetime.now().isoformat(),
    }
    out = META_DIR / "breakout_comparison.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if verbose:
        print(f"  \U0001F4C4 {out}")
    return {"baseline": base, "meta": meta, "spy_metrics": spy_m, "report": result}


def run_universe_comparison(
    universes: tuple[str, ...] = ("sp500", "r1000"),
    train_r1000_meta: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Vergleicht Breakout-Backtests ueber Universen (S&P 500 vs. Russell 1000).

    Pro Universum: Event-Anzahl (OOS), Baseline-Backtest, optional Meta-Filter.
    SP500 nutzt das gespeicherte Produktionsmodell; R1000 optional mit
    frischem Training (train_r1000_meta=True, dauert ~10 Min.).

    Returns dict mit Tabelle + Einzelergebnissen.
    """
    t_cut = pd.Timestamp(TRAIN_END)
    rows: list[dict] = []
    details: dict = {}

    if verbose:
        print("\n" + "\u2550" * 72)
        print("  BREAKOUT — Universums-Vergleich (OOS ab {})".format(TRAIN_END))
        print("\u2550" * 72)

    for uni in universes:
        label = "Russell 1000" if uni == "r1000" else "S&P 500"
        if verbose:
            print(f"\n  --- {label} ({uni}) ---")

        close, volume = load_price_data(universe=uni, stale_days=2)
        oos_events = generate_breakout_events(close, volume, start=TRAIN_END)
        n_oos = int((pd.to_datetime(oos_events["date"]) >= t_cut).sum()) if not oos_events.empty else 0

        if verbose:
            print(f"  Ausbrueche OOS ({TRAIN_END}+): {n_oos}")

        base = run_breakout_backtest(close, volume, use_meta=False)
        bmet = base["metrics"]

        meta_res = None
        mmet: dict = {}
        meta_note = ""
        try:
            if uni == "sp500":
                model = load_model()
            elif train_r1000_meta:
                if verbose:
                    print("  Meta-Modell auf R1000 trainieren \u2026")
                model, _, _ = train_breakout_meta(close, volume, verbose=verbose)
                meta_note = "R1000-trainiert"
            else:
                model = load_model()
                meta_note = "SP500-Modell (Cross)"
                if verbose:
                    print("  \u26a0 R1000 Meta: SP500-Produktionsmodell (nicht fair) — "
                          "train_r1000_meta=True fuer echten Vergleich")
            meta_res = run_breakout_backtest(close, volume, use_meta=True, model=model)
            mmet = meta_res["metrics"]
        except FileNotFoundError:
            meta_note = "kein Modell"
            if verbose:
                print("  \u26a0 Kein Meta-Modell — nur Baseline")

        row = {
            "universe": uni,
            "label": label,
            "n_tickers": len([c for c in close.columns if c not in SECTOR_ETFS and c != "SPY"]),
            "oos_events": n_oos,
            "base_cagr": bmet.get("cagr", 0),
            "base_mar": bmet.get("mar", 0),
            "base_maxdd": bmet.get("maxdd", 0),
            "base_sharpe": bmet.get("sharpe", 0),
            "base_n_trades": bmet.get("n_trades", 0),
            "meta_cagr": mmet.get("cagr", np.nan),
            "meta_mar": mmet.get("mar", 0),
            "meta_maxdd": mmet.get("maxdd", 0),
            "meta_sharpe": mmet.get("sharpe", 0),
            "meta_n_trades": mmet.get("n_trades", 0),
            "meta_note": meta_note,
        }
        rows.append(row)
        details[uni] = {"baseline": base, "meta": meta_res, "close_cols": row["n_tickers"]}

        if verbose:
            print(f"  Baseline OOS: MAR {bmet.get('mar', 0):.2f}  CAGR {bmet.get('cagr', 0):.1%}  "
                  f"MaxDD {bmet.get('maxdd', 0):.1%}  Trades {bmet.get('n_trades', 0):.0f}")
            if mmet:
                print(f"  Meta OOS    : MAR {mmet.get('mar', 0):.2f}  CAGR {mmet.get('cagr', 0):.1%}  "
                      f"MaxDD {mmet.get('maxdd', 0):.1%}  Trades {mmet.get('n_trades', 0):.0f}  ({meta_note})")

    table = pd.DataFrame(rows)
    if verbose and not table.empty:
        print("\n" + "\u2550" * 72)
        print("  ZUSAMMENFASSUNG")
        print("\u2550" * 72)
        disp = table[["label", "n_tickers", "oos_events", "base_mar", "meta_mar",
                       "base_n_trades", "meta_n_trades", "meta_note"]].copy()
        print(disp.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
        if len(rows) >= 2:
            d_mar = rows[1]["base_mar"] - rows[0]["base_mar"]
            print(f"\n  \u0394 Baseline-MAR (R1000 \u2212 SP500): {d_mar:+.2f}")
            if rows[1]["base_mar"] >= rows[0]["base_mar"]:
                print("  \u2192 R1000 Baseline mindestens gleichwertig — Meta-Retrain pruefen.")
            elif rows[1]["base_mar"] >= 0.5 * rows[0]["base_mar"]:
                print("  \u2192 R1000 schwaecher, aber noch tragfaehig — nur mit eigenem Meta-Modell.")
            else:
                print("  \u2192 R1000 deutlich schwaecher — bei SP500 bleiben.")
        print("\u2550" * 72)

    out_path = META_DIR / "breakout_universe_comparison.json"
    payload = {
        "run_at": datetime.now().isoformat(),
        "train_r1000_meta": train_r1000_meta,
        "rows": [{k: (float(v) if isinstance(v, (np.floating, float)) else v)
                  for k, v in r.items()} for r in rows],
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if verbose:
        print(f"  \U0001F4C4 {out_path}")

    _get_bt().set_universe("sp500")
    return {"table": table, "details": details, "report": payload}


def run_ohlc_comparison(
    train_ohlc_meta: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Vergleicht Close-only vs. OHLC-Barrieren (Tages-High/Low fuer Stop/Ziel).

    Zeigt:
      - Wie viele Labels sich aendern (Gewinner -> Verlierer und umgekehrt)
      - Baseline-Backtest OOS (MAR, CAGR, Win-Rate)
      - Meta-Backtest OOS (Produktionsmodell; train_ohlc_meta=True fuer fair retrain)

    Erster Lauf: OHLC-Abruf ~15-25 Min. (gecacht danach).
    """
    if verbose:
        print("\n" + "\u2550" * 72)
        print("  BREAKOUT — Close-only vs. OHLC-Barrieren (OOS ab {})".format(TRAIN_END))
        print("\u2550" * 72)

    close, volume, open_, high, low = load_price_data_ohlc()
    if high is None or low is None:
        raise RuntimeError(
            "OHLC-Daten konnten nicht geladen werden.\n"
            "  1) breakout_meta.py v1.8.1+ auf Drive (Zelle 0 / Sync)\n"
            "  2) Colab Secret EODHD_API_KEY gesetzt?\n"
            "  3) Zelle 1 neu, dann Zelle 4h erneut (~20 Min. Abruf)"
        )

    t_cut = pd.Timestamp(TRAIN_END)
    events = generate_breakout_events(close, volume, start=TRAIN_END)
    if events.empty:
        raise RuntimeError("Keine OOS-Events fuer OHLC-Vergleich.")

    ev_co = label_events(events.copy(), close, high=None, low=None, open_=open_)
    ev_oh = label_events(events.copy(), close, high=high, low=low, open_=open_)
    merged = ev_co[["ticker", "date", "label", "exit_reason"]].merge(
        ev_oh[["ticker", "date", "label", "exit_reason"]],
        on=["ticker", "date"], suffixes=("_close", "_ohlc"),
    )
    n_flip = int((merged["label_close"] != merged["label_ohlc"]).sum())
    n_win_to_loss = int(((merged["label_close"] == 1) & (merged["label_ohlc"] == 0)).sum())
    n_loss_to_win = int(((merged["label_close"] == 0) & (merged["label_ohlc"] == 1)).sum())
    prec_co = float(merged["label_close"].mean()) if len(merged) else 0.0
    prec_oh = float(merged["label_ohlc"].mean()) if len(merged) else 0.0

    if verbose:
        print(f"\n  Label-Vergleich ({len(merged)} OOS-Events):")
        print(f"    Close-only Precision: {prec_co:.1%}")
        print(f"    OHLC Precision:       {prec_oh:.1%}  ({prec_oh - prec_co:+.1%})")
        print(f"    Labels geaendert:     {n_flip}  "
              f"(Gewinn\u2192Verlust: {n_win_to_loss}, Verlust\u2192Gewinn: {n_loss_to_win})")
        if n_win_to_loss > n_loss_to_win:
            print("    \u2192 Close-only ist OPTIMISTISCH (typisch bei volatilen Breakouts).")

    if verbose:
        print("\n  Baseline Close-only \u2026")
    base_co = run_breakout_backtest(close, volume, use_meta=False, high=None, low=None, open_=None)
    if verbose:
        print("  Baseline OHLC \u2026")
    base_oh = run_breakout_backtest(close, volume, use_meta=False, high=high, low=low, open_=open_)

    meta_co = meta_oh = None
    m_co = m_oh = {}
    meta_note = "SP500-Produktionsmodell"
    try:
        if train_ohlc_meta:
            if verbose:
                print("  Meta OHLC — Modell neu trainieren \u2026")
            model_oh, _, _ = train_breakout_meta(close, volume, high=high, low=low, open_=open_, verbose=verbose)
            model_co = load_model()
            meta_note = "OHLC-trainiert vs. Close-trainiert"
        else:
            model_co = model_oh = load_model()
            if verbose:
                print("  Meta (gleiches Modell, nur Barrieren-Check unterschiedlich) \u2026")
        if verbose:
            print("  Meta Close-only \u2026")
        meta_co = run_breakout_backtest(close, volume, use_meta=True, model=model_co,
                                       high=None, low=None, open_=None)
        if verbose:
            print("  Meta OHLC \u2026")
        meta_oh = run_breakout_backtest(close, volume, use_meta=True, model=model_oh,
                                        high=high, low=low, open_=open_)
        m_co = meta_co["metrics"]
        m_oh = meta_oh["metrics"]
    except FileNotFoundError:
        if verbose:
            print("  \u26a0 Kein Meta-Modell — nur Baseline-Vergleich.")

    b_co, b_oh = base_co["metrics"], base_oh["metrics"]
    rows = [
        {"mode": "Baseline Close", "mar": b_co.get("mar", 0), "cagr": b_co.get("cagr", 0),
         "maxdd": b_co.get("maxdd", 0), "sharpe": b_co.get("sharpe", 0),
         "win_rate": b_co.get("win_rate", 0), "n_trades": b_co.get("n_trades", 0)},
        {"mode": "Baseline OHLC", "mar": b_oh.get("mar", 0), "cagr": b_oh.get("cagr", 0),
         "maxdd": b_oh.get("maxdd", 0), "sharpe": b_oh.get("sharpe", 0),
         "win_rate": b_oh.get("win_rate", 0), "n_trades": b_oh.get("n_trades", 0)},
    ]
    if m_co and m_oh:
        rows += [
            {"mode": "Meta Close", "mar": m_co.get("mar", 0), "cagr": m_co.get("cagr", 0),
             "maxdd": m_co.get("maxdd", 0), "sharpe": m_co.get("sharpe", 0),
             "win_rate": m_co.get("win_rate", 0), "n_trades": m_co.get("n_trades", 0)},
            {"mode": "Meta OHLC", "mar": m_oh.get("mar", 0), "cagr": m_oh.get("cagr", 0),
             "maxdd": m_oh.get("maxdd", 0), "sharpe": m_oh.get("sharpe", 0),
             "win_rate": m_oh.get("win_rate", 0), "n_trades": m_oh.get("n_trades", 0)},
        ]

    table = pd.DataFrame(rows)
    if verbose:
        print("\n" + "\u2550" * 72)
        print("  ZUSAMMENFASSUNG OOS")
        print("\u2550" * 72)
        print(table.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        d_mar = b_oh.get("mar", 0) - b_co.get("mar", 0)
        print(f"\n  \u0394 Baseline-MAR (OHLC \u2212 Close): {d_mar:+.2f}")
        if m_co and m_oh:
            d_meta = m_oh.get("mar", 0) - m_co.get("mar", 0)
            print(f"  \u0394 Meta-MAR     (OHLC \u2212 Close): {d_meta:+.2f}  ({meta_note})")
        if d_mar < -0.1:
            print("  \u2192 Close-only Backtest war deutlich zu optimistisch.")
        elif d_mar < 0:
            print("  \u2192 Close-only leicht optimistisch — Broker-Stops sind Pflicht.")
        else:
            print("  \u2192 Unterschied gering — Close-only hier akzeptabel.")
        print("\u2550" * 72)

    payload = {
        "run_at": datetime.now().isoformat(),
        "train_ohlc_meta": train_ohlc_meta,
        "label_flips": n_flip,
        "win_to_loss": n_win_to_loss,
        "loss_to_win": n_loss_to_win,
        "precision_close": prec_co,
        "precision_ohlc": prec_oh,
        "rows": table.to_dict(orient="records"),
    }
    out_path = META_DIR / "breakout_ohlc_comparison.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if verbose:
        print(f"  \U0001F4C4 {out_path}")

    return {
        "table": table,
        "label_stats": payload,
        "baseline_close": base_co,
        "baseline_ohlc": base_oh,
        "meta_close": meta_co,
        "meta_ohlc": meta_oh,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Regime-Overlay -- Seitwaertsphasen / schwache Marktbreite
# ─────────────────────────────────────────────────────────────────────────────
def compute_regime_panel(close: pd.DataFrame) -> pd.DataFrame:
    """
    Taegliches Markt-Regime aus Marktbreite (% Aktien > SMA200) + SPY vs. SMA200.

    GRUEN  -- Breite >=55% UND SPY > SMA200  -> 100% Slots
    GELB   -- dazwischen                     -> 50% Slots
    ROT    -- Breite <40% ODER SPY <= SMA200 -> 0% Neukaeufe
    """
    stock_cols = [c for c in close.columns if c not in SECTOR_ETFS and c != "SPY"]
    if "SPY" not in close.columns or not stock_cols:
        return pd.DataFrame()

    sma200 = close[stock_cols].rolling(200, min_periods=100).mean()
    breadth = (close[stock_cols].gt(sma200)).mean(axis=1)
    spy = close["SPY"]
    spy_sma = spy.rolling(200, min_periods=100).mean()
    spy_ok = spy > spy_sma

    b = breadth.dropna()
    sa = spy_ok.reindex(b.index).fillna(False)

    regime = pd.Series("yellow", index=b.index)
    regime[(b < REGIME_BREADTH_RED) | (~sa)] = "red"
    regime[(b >= REGIME_BREADTH_GREEN) & sa] = "green"

    mult_map = {"green": 1.0, "yellow": REGIME_YELLOW_MULT, "red": REGIME_RED_MULT}
    mult = regime.map(mult_map)

    return pd.DataFrame({
        "breadth": b.round(3),
        "spy_above_sma200": sa,
        "regime": regime,
        "max_pos_mult": mult,
    })


def regime_status(close: "pd.DataFrame | None" = None,
                  regime_panel: "pd.DataFrame | None" = None) -> dict:
    if regime_panel is None:
        if close is None:
            return {"regime": "unknown", "label": "\u2014", "max_pos_mult": 1.0}
        regime_panel = compute_regime_panel(close)
    if regime_panel.empty:
        return {"regime": "unknown", "label": "\u2014", "max_pos_mult": 1.0}
    row = regime_panel.iloc[-1]
    labels = {
        "green":  f"\U0001F7E2 GRUEN -- volle Quote (max. {MAX_POSITIONS} Positionen)",
        "yellow": f"\U0001F7E1 GELB -- reduziert (max. {max(1, int(round(MAX_POSITIONS * REGIME_YELLOW_MULT)))} Positionen)",
        "red":    "\U0001F534 ROT -- keine Neukaeufe",
    }
    return {
        "regime":       str(row["regime"]),
        "label":        labels.get(str(row["regime"]), "\u2014"),
        "breadth":      float(row["breadth"]),
        "spy_above_sma200": bool(row["spy_above_sma200"]),
        "max_pos_mult": float(row["max_pos_mult"]),
        "date":         str(regime_panel.index[-1].date()),
    }


def _filter_events_meta(events, close, volume, model, oos_start=TRAIN_END):
    t_cut = pd.Timestamp(oos_start)
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
    close:       "pd.DataFrame | None" = None,
    volume:      "pd.DataFrame | None" = None,
    model:       "BreakoutMetaModel | None" = None,
    high:        "pd.DataFrame | None" = None,
    low:         "pd.DataFrame | None" = None,
    open_:       "pd.DataFrame | None" = None,
    oos_start:   str = TRAIN_END,
    verbose:     bool = True,
) -> dict:
    """Vergleich Meta-only vs. Meta + Regime-Overlay (OOS ab oos_start, korrekt getrimmt)."""
    if close is None or volume is None:
        close, volume = load_price_data()
    if model is None:
        model = load_model()

    t_cut = pd.Timestamp(oos_start)
    events = generate_breakout_events(close, volume, start=EVAL_START)
    events = label_events(events, close, high=high, low=low, open_=open_).dropna(subset=["label"]).reset_index(drop=True)
    events = _filter_events_meta(events, close, volume, model, oos_start)

    regime_panel = compute_regime_panel(close)
    bt = _get_bt()

    eq_base, tr_base = _simulate_trades(events, close, POSITION_SIZE, MAX_POSITIONS, INITIAL_CAPITAL)
    eq_reg, tr_reg   = _simulate_trades(events, close, POSITION_SIZE, MAX_POSITIONS, INITIAL_CAPITAL,
                                         regime_panel=regime_panel)

    spy_oos = close["SPY"].loc[t_cut:].pct_change().fillna(0)
    eq_spy = (1 + spy_oos).cumprod() * INITIAL_CAPITAL

    m_base = bt.compute_bt_metrics(eq_base.loc[t_cut:])
    m_reg  = bt.compute_bt_metrics(eq_reg.loc[t_cut:])
    m_spy  = bt.compute_bt_metrics(eq_spy)

    rp_oos = regime_panel.loc[t_cut:] if not regime_panel.empty else regime_panel
    regime_pct = {}
    if not rp_oos.empty:
        for r in ("green", "yellow", "red"):
            regime_pct[r] = float((rp_oos["regime"] == r).mean())

    yearly = {}
    for lbl, eq in [("Meta", eq_base.loc[t_cut:]), ("Meta+Regime", eq_reg.loc[t_cut:])]:
        yr = eq.resample("YE").last().pct_change().dropna()
        yearly[lbl] = {int(d.year): float(v) for d, v in yr.items()}

    if verbose:
        print("\u2550" * 72)
        print("  REGIME-OVERLAY -- Meta vs. Meta + Marktfilter (OOS)")
        print("\u2550" * 72)
        print(f"  OOS ab {oos_start}")
        if regime_pct:
            print(f"  Handelstage OOS: GRUEN {regime_pct.get('green', 0):.0%} \u00b7 "
                  f"GELB {regime_pct.get('yellow', 0):.0%} \u00b7 "
                  f"ROT {regime_pct.get('red', 0):.0%}")
        print(f"\n  {'Kennzahl':22}  {'Meta':>10}  {'Meta+Regime':>12}  {'SPY':>8}")
        print("  " + "\u2500" * 58)
        for lbl, key, fmt in [("CAGR", "cagr", ".1%"), ("MaxDD", "maxdd", ".1%"),
                               ("Sharpe", "sharpe", ".2f"), ("MAR", "mar", ".2f"),
                               ("# Trades", "n_trades", ".0f")]:
            if key == "n_trades":
                b, r = len(tr_base), len(tr_reg)
                s = m_spy.get(key) or 0
                print(f"  {lbl:22}  {b:>10.0f}  {r:>12.0f}  {s:>8.0f}")
            else:
                b = m_base.get(key) or 0
                r = m_reg.get(key) or 0
                s = m_spy.get(key) or 0
                print(f"  {lbl:22}  {format(b, fmt):>10}  {format(r, fmt):>12}  {format(s, fmt):>8}")
        if yearly.get("Meta"):
            print("\n  \u2500\u2500 Jahr-fuer-Jahr (OOS) \u2500\u2500")
            years = sorted(set(yearly["Meta"]) | set(yearly.get("Meta+Regime", {})))
            print(f"  {'Jahr':6}  {'Meta':>8}  {'Meta+Regime':>12}  {'Delta':>8}")
            for y in years:
                a = yearly["Meta"].get(y, 0)
                b = yearly.get("Meta+Regime", {}).get(y, 0)
                print(f"  {y:6}  {a:>7.1%}  {b:>11.1%}  {b-a:>+7.1%}")

    return {
        "metrics_base": m_base, "metrics_regime": m_reg, "metrics_spy": m_spy,
        "regime_pct": regime_pct, "yearly": yearly,
        "equity_base": eq_base, "equity_regime": eq_reg, "regime_panel": regime_panel,
        "events_oos": events[events["date"] >= t_cut],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Barrieren-Vergleich -- fixe %- vs. ATR-/vola-skalierte Barrieren
# ─────────────────────────────────────────────────────────────────────────────
def _train_meta_variant(close, volume, use_atr, high=None, low=None, open_=None,
                        train_end=TRAIN_END, keep_top_pct=KEEP_TOP_PCT,
                        embargo_days=EMBARGO_DAYS):
    """Trainiert ein Meta-Modell in-memory pro Barriere-Variante. Threshold auf TRAIN-Quantil (Fix #1)."""
    events = generate_breakout_events(close, volume, start=EVAL_START)
    events = label_events(events, close, use_atr=use_atr, high=high, low=low,
                          open_=open_).dropna(subset=["label"]).reset_index(drop=True)
    events["label"] = events["label"].astype(int)
    feats   = extract_features(events, close, volume)
    dataset = _dropna_features(_merge_features(events, feats)).reset_index(drop=True)

    df_tr, df_te = purged_train_test_split(dataset, train_end, embargo_days)
    if len(df_tr) < MIN_EVENTS_TRAIN or len(df_te) < 50:
        raise RuntimeError(
            f"Zu wenige Events (Train {len(df_tr)} / Test {len(df_te)}) fuer Variante "
            f"use_atr={use_atr}."
        )

    cal = _make_rf_pipeline()
    cal.fit(df_tr[FEATURE_COLS].values, df_tr["label"].values)
    probs_tr  = cal.predict_proba(df_tr[FEATURE_COLS].values)[:, 1]
    threshold = float(np.percentile(probs_tr, 100 - keep_top_pct))
    return cal, threshold


def _backtest_barrier_variant(close, volume, use_atr, high=None, low=None, open_=None, oos_start=TRAIN_END):
    cal, threshold = _train_meta_variant(close, volume, use_atr, high=high, low=low, open_=open_)

    events = generate_breakout_events(close, volume, start=EVAL_START)
    events = label_events(events, close, use_atr=use_atr, high=high, low=low,
                          open_=open_).dropna(subset=["label"]).reset_index(drop=True)

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

    equity, trades = _simulate_trades(ev_all, close, POSITION_SIZE, MAX_POSITIONS, INITIAL_CAPITAL)
    return equity, trades, threshold


def run_barrier_comparison(
    close:     "pd.DataFrame | None" = None,
    volume:    "pd.DataFrame | None" = None,
    high:      "pd.DataFrame | None" = None,
    low:       "pd.DataFrame | None" = None,
    open_:     "pd.DataFrame | None" = None,
    oos_start: str = TRAIN_END,
    verbose:   bool = True,
) -> dict:
    """Vergleich (OOS, korrekt getrimmt): fixe %-Barrieren vs. ATR-/vola-skalierte Barrieren."""
    if close is None or volume is None:
        close, volume = load_price_data()
    t_cut = pd.Timestamp(oos_start)
    bt = _get_bt()

    if verbose:
        print("  Variante 1/2: Fix-Barrieren \u2026")
    eq_fix, tr_fix, thr_fix = _backtest_barrier_variant(close, volume, use_atr=False, high=high, low=low, open_=open_, oos_start=oos_start)
    if verbose:
        print("  Variante 2/2: ATR-Barrieren \u2026")
    eq_atr, tr_atr, thr_atr = _backtest_barrier_variant(close, volume, use_atr=True, high=high, low=low, open_=open_, oos_start=oos_start)

    spy_oos = close["SPY"].loc[t_cut:].pct_change().fillna(0)
    eq_spy  = (1 + spy_oos).cumprod() * INITIAL_CAPITAL

    m_fix = bt.compute_bt_metrics(eq_fix.loc[t_cut:])
    m_atr = bt.compute_bt_metrics(eq_atr.loc[t_cut:])
    m_spy = bt.compute_bt_metrics(eq_spy)

    yearly = {}
    for lbl, eq in [("Fix", eq_fix.loc[t_cut:]), ("ATR", eq_atr.loc[t_cut:])]:
        yr = eq.resample("YE").last().pct_change().dropna()
        yearly[lbl] = {int(d.year): float(v) for d, v in yr.items()}

    if verbose:
        print("\u2550" * 72)
        print("  BARRIEREN-VERGLEICH -- Fix vs. ATR-skaliert (Meta-gefiltert, OOS)")
        print("\u2550" * 72)
        print(f"  OOS ab {oos_start}")
        atr_kind = "echter ATR" if (high is not None and low is not None) else "Close-Proxy (kein echter ATR!)"
        print(f"  Fix : +{PROFIT_TARGET:.0%} Ziel / -{STOP_LOSS:.0%} Stop / {HOLD_DAYS}d")
        print(f"  ATR : Ziel={ATR_PT_MULT:g}xATR-Ratio, Stop={ATR_SL_MULT:g}xATR-Ratio ({atr_kind}, "
              f"Clamp Ziel {ATR_PT_CLAMP[0]:.0%}-{ATR_PT_CLAMP[1]:.0%}, "
              f"Stop {ATR_SL_CLAMP[0]:.0%}-{ATR_SL_CLAMP[1]:.0%})")
        print(f"  Threshold: Fix P>={thr_fix:.3f} \u00b7 ATR P>={thr_atr:.3f}")
        print(f"\n  {'Kennzahl':22}  {'Fix':>10}  {'ATR':>10}  {'SPY':>8}")
        print("  " + "\u2500" * 54)
        for lbl, key, fmt in [("CAGR", "cagr", ".1%"), ("MaxDD", "maxdd", ".1%"),
                               ("Sharpe", "sharpe", ".2f"), ("MAR", "mar", ".2f"),
                               ("# Trades", "n_trades", ".0f")]:
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
            print("\n  \u2500\u2500 Jahr-fuer-Jahr (OOS) \u2500\u2500")
            years = sorted(set(yearly["Fix"]) | set(yearly.get("ATR", {})))
            print(f"  {'Jahr':6}  {'Fix':>8}  {'ATR':>10}  {'Delta':>8}")
            for y in years:
                a = yearly["Fix"].get(y, 0)
                b = yearly.get("ATR", {}).get(y, 0)
                print(f"  {y:6}  {a:>7.1%}  {b:>9.1%}  {b-a:>+7.1%}")
        print("\u2550" * 72)
        _winner = "ATR" if (m_atr.get("mar") or 0) > (m_fix.get("mar") or 0) else "Fix"
        print(f"  -> Bessere MAR (CAGR/MaxDD): {_winner}-Barrieren")

    return {
        "metrics_fix": m_fix, "metrics_atr": m_atr, "metrics_spy": m_spy,
        "threshold_fix": thr_fix, "threshold_atr": thr_atr,
        "equity_fix": eq_fix, "equity_atr": eq_atr, "yearly": yearly,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Live Scanner
# ─────────────────────────────────────────────────────────────────────────────

def run_live_scanner(
    close:           "pd.DataFrame | None" = None,
    volume:          "pd.DataFrame | None" = None,
    use_meta:        bool  = True,
    use_regime:      bool  = True,
    kapital_eur:     float = 100_000,
    lookback_days:   int   = 5,
    auto_retrain:    bool  = True,
    max_model_age:   int   = 30,
    verbose:         bool  = True,
) -> list:
    """
    Scannt die letzten lookback_days Handelstage auf neue Ausbruchs-Signale.

    Fix #10: Events mit fehlenden Features verschwinden nicht mehr lautlos aus
    der Signalliste (vorher: inner merge liess sie komplett fallen). Sie
    erscheinen jetzt mit take=False, reason="features_incomplete", damit ein
    echter Ausbruch nie unbemerkt uebersehen wird.
    """
    if close is None or volume is None:
        close, volume = load_price_data_for_scanner()

    last_date  = close.index[-1]

    # Daten-Staleness-Check: warnen/abbrechen wenn Preisdaten zu alt sind
    _data_age_days = (pd.Timestamp.now().normalize() - last_date.normalize()).days
    if _data_age_days > 5:
        raise RuntimeError(
            f"Preisdaten sind {_data_age_days} Tage alt (letzter Tag: {last_date.date()}).\n"
            "Scanner-Ergebnisse waeren UNZUVERLAESSIG.\n"
            "Loesung: close, volume = load_price_data(force_refresh=True) und erneut ausfuehren."
        )
    elif _data_age_days > 2:
        print(f"  \u2139 Preisdaten {_data_age_days} Tage alt (letzter Tag: {last_date.date()}) "
              "-- moeglicherweise Wochenende/Feiertag, OK.")

    scan_start = close.index[-lookback_days] if len(close) >= lookback_days else close.index[0]
    events     = generate_breakout_events(close, volume, start=str(scan_start.date()))

    model = None
    if use_meta:
        try:
            if auto_retrain:
                model = ensure_fresh_model(max_age_days=max_model_age, close=close, volume=volume, verbose=verbose)
            else:
                model = load_model()
        except FileNotFoundError:
            print("  \u26a0 Kein Meta-Modell -- Signale ohne Filter")
            use_meta = False

    # Feature-Mismatch-Check: Modell muss mit aktuellen FEATURE_COLS kompatibel sein
    if model is not None and hasattr(model, "feature_names"):
        model_feats = set(model.feature_names)
        code_feats  = set(FEATURE_COLS)
        if model_feats != code_feats:
            added= code_feats - model_feats
            removed = model_feats - code_feats
            msg = ("  \u274c FEATURE-MISMATCH: Modell und Code sind INKOMPATIBEL!\n"
                   f"  Modell kennt:  {sorted(model_feats)}\n"
                   f"  Code erwartet: {sorted(code_feats)}\n")
            if added:
                msg += f"  Neu im Code (Modell kennt sie nicht): {sorted(added)}\n"
            if removed:
                msg += f"  Im Modell, fehlen im Code: {sorted(removed)}\n"
            msg += ("  Loesung: Zelle3 (Training) erneut ausfuehren, "
                    "damit ein neues Modell mit den aktuellen Features trainiert wird.")
            raise RuntimeError(msg)

    # Barrier-Konsistenz: Warnen wenn Modell mit anderen Barrieren trainiert wurde
    if model is not None and hasattr(model, "barrier_config"):
        bc = model.barrier_config
        mismatches = []
        if bc.get("use_atr") != USE_ATR_BARRIERS:
            mismatches.append(f"use_atr: Modell={bc.get('use_atr')} / Code={USE_ATR_BARRIERS}")
        if abs(bc.get("profit_target", PROFIT_TARGET) - PROFIT_TARGET) > 1e-6:
            mismatches.append(f"profit_target: Modell={bc.get('profit_target')} / Code={PROFIT_TARGET}")
        if abs(bc.get("stop_loss", STOP_LOSS) - STOP_LOSS) > 1e-6:
            mismatches.append(f"stop_loss: Modell={bc.get('stop_loss')} / Code={STOP_LOSS}")
        if mismatches:
            print("  \u26a0 Barrieren-Konfiguration weicht vom Trainings-Modell ab:")
            for m in mismatches:
                print(f"    - {m}")
            print("  Das Modell bewertet Signale nach anderen Kriterien als aktuell konfiguriert.")
            print("  Empfehlung: Modell neu trainieren (Zelle 3) oder Konfig zuruecksetzen.")

    events["take"]      = True
    events["meta_prob"] = np.nan
    events["reason"]    = "no_filter"

    if use_meta and model is not None and not events.empty:
        feats = extract_features(events, close, volume)
        merged = _merge_features(events, feats)
        feat_cols_present = [c for c in FEATURE_COLS if c in merged.columns]
        complete_mask = merged[feat_cols_present].notna().all(axis=1) if feat_cols_present else pd.Series(False, index=merged.index)

        ev_complete   = merged[complete_mask].copy()
        ev_incomplete = merged[~complete_mask].copy()

        if not ev_complete.empty:
            probs = model.predict_proba(ev_complete[FEATURE_COLS].values)[:, 1]
            ev_complete["meta_prob"] = probs
            ev_complete["take"]      = probs >= model.threshold
            ev_complete["reason"]    = np.where(ev_complete["take"], "meta_pass", "meta_filtered")

        if not ev_incomplete.empty:
            ev_incomplete["meta_prob"] = np.nan
            ev_incomplete["take"]      = False
            ev_incomplete["reason"]    = "features_incomplete"

        events = pd.concat([ev_complete, ev_incomplete], ignore_index=True)

    regime_info = regime_status(close) if use_regime else None
    if use_regime and regime_info and regime_info.get("regime") == "red":
        events.loc[events["take"], "reason"] = "regime_red"
        events["take"] = False

    bt    = _get_bt()
    names = bt._ticker_name_map() if hasattr(bt, "_ticker_name_map") else {}

    signals = []
    for row in events.itertuples(index=False):
        c0 = float(row.close)
        mp = getattr(row, "meta_prob", np.nan)
        atr_r = getattr(row, "atr_ratio", np.nan)
        _pt_i, _sl_i = _barrier_pct(atr_r, USE_ATR_BARRIERS)
        sig_dt = pd.Timestamp(row.date)
        kurs_dt = ticker_price_date(close, row.ticker) or last_date
        signals.append({
            "ticker":    row.ticker,
            "name":      names.get(row.ticker, row.ticker),
            "date":      str(sig_dt.date()),
            "signal_date": str(sig_dt.date()),
            "price_date": str(pd.Timestamp(kurs_dt).date()),
            "close":     round(c0, 2),
            "target":    round(c0 * (1 + _pt_i), 2),
            "stop":      round(c0 * (1 - _sl_i), 2),
            "pos_eur":   round(kapital_eur * POSITION_SIZE, 0),
            "vol_ratio": round(float(row.vol_ratio), 2) if pd.notna(row.vol_ratio) else None,
            "meta_prob": round(float(mp), 2) if pd.notna(mp) else None,
            "take":      bool(row.take),
            "reason":    getattr(row, "reason", ""),
        })
        if regime_info:
            signals[-1]["regime"] = regime_info.get("regime")

    if verbose:
        taken       = [s for s in signals if s["take"]]
        skipped     = [s for s in signals if not s["take"] and s["reason"] != "features_incomplete"]
        incomplete  = [s for s in signals if s["reason"] == "features_incomplete"]
        thr = getattr(model, "threshold", META_THRESHOLD) if model else None
        ktp = getattr(model, "keep_top_pct", KEEP_TOP_PCT) if model else KEEP_TOP_PCT

        print("\n" + "\u2550" * 72)
        print("  BREAKOUT SCANNER -- Aktuelle Signale")
        print("\u2550" * 72)
        print(f"  Kursstand: {fmt_kurs_datum(last_date)}  (letzter Handelstag im Panel)")
        if regime_info:
            print(f"  Regime   : {regime_info['label']}")
            print(f"             Breite {regime_info['breadth']:.0%} \u00b7 "
                  f"SPY {'>' if regime_info['spy_above_sma200'] else '<='} SMA200")
        print(f"  Gescannt : letzte {lookback_days} Handelstage")
        print(f"  Gefunden : {len(signals)} Ausbrueche  \u00b7  {len(taken)} nach Meta-Filter"
              + (f"  (Top {ktp:.0f}%, P >= {thr:.0%})" if thr else ""))
        if incomplete:
            print(f"  \u26a0 {len(incomplete)} Events mit unvollstaendigen Features "
                  "(z.B. neu gelistete Titel, zu kurze Historie) -- NICHT automatisch gefiltert, "
                  "manuell pruefen: " + ", ".join(s["ticker"] for s in incomplete))
        print()
        if taken:
            print("  \u250c\u2500 KAUFEN " + "\u2500" * 62)
            for s in taken:
                mp_s = f"  P={s['meta_prob']:.0%}" if s["meta_prob"] is not None else ""
                sig_d = fmt_kurs_datum(s.get("signal_date") or s.get("date"))
                kurs_d = fmt_kurs_datum(s.get("price_date"))
                print(
                    f"  \u2502 \U0001F7E2 {s['ticker']:6}  {s['name'][:22]:22}  "
                    f"Signal {sig_d}  Kurs {kurs_d}  "
                    f"\u20ac{s['pos_eur']:>7,.0f}  "
                    f"Ziel {s['target']:>8.2f}  Stop {s['stop']:>8.2f}"
                    f"  Vol x{s['vol_ratio']:.1f}{mp_s}"
                )
            print("  \u2514" + "\u2500" * 71)
        else:
            print(f"  Keine Signale nach Meta-Filter in den letzten {lookback_days} Tagen.")
        if use_regime and regime_info and regime_info.get("regime") == "red" and signals:
            print("\n  \u26a0 ROT -- Regime-Filter blockiert alle Neukaeufe (bestehende Positionen laufen weiter).")
        elif use_regime and regime_info and regime_info.get("regime") == "yellow":
            print(f"\n  \u26a0 GELB -- max. {max(1, int(round(MAX_POSITIONS * REGIME_YELLOW_MULT)))} gleichzeitige Positionen empfohlen.")
        if skipped:
            print(f"\n  Meta gefiltert ({len(skipped)}):  "
                  + "  ".join(f"{s['ticker']} P={s['meta_prob']:.0%}" for s in skipped if s['meta_prob'] is not None))
        print("\u2550" * 72)

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Roadmap
# ─────────────────────────────────────────────────────────────────────────────
def print_roadmap() -> None:
    print(f"""
  \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
  BREAKOUT + META-LABELING v{VERSION}  --  Roadmap
  \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
  Stufe 1  Primaermodell        52W-Hoch + Vol x{MIN_VOL_RATIO} + SMA{SMA_PERIOD}
                               -> generate_breakout_events()
  Stufe 2  Triple-Barrier      Ziel +{PROFIT_TARGET:.0%} / Stop -{STOP_LOSS:.0%} / {HOLD_DAYS} Tage
                               (Stop-first-Check, Intraday wenn High/Low vorhanden)
                               -> label_events()
  Stufe 3  Feature Engineering 10 Marktbedingungen am Signal-Tag
                               -> extract_features()
  Stufe 4  Meta-Modell         Random Forest, Threshold auf TRAIN-Quantil (Top {KEEP_TOP_PCT}%)
           Train {EVAL_START} -> {TRAIN_END}   -> train_breakout_meta()
           Test  {TRAIN_END} -> heute      OOS-Praezision (Embargo {EMBARGO_DAYS}d)
  Stufe 5  Walk-Forward        Purged + embargoed, mehrere Jahres-Folds
                               -> run_walk_forward()  [Stabilitaets-Check VOR Live!]
  Stufe 6  Backtest-Vergleich  Baseline vs. Meta (OOS, korrekt getrimmt)
                               -> run_meta_comparison()
  Stufe 7  Live-Scanner        Aktuelle Ausbrueche + Meta-P
                               -> run_live_scanner()
  \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
  Bekannte Einschraenkungen (siehe Docstrings):
    - Survivorship-Bias abhaengig von regime_momentum_bt.set_universe()
    - Ohne Open/High/Low: Entry-Timing + Barrier-Checks sind Naeherungen
      (siehe load_price_data_ohlc() um das zu beheben)
  \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
""")
