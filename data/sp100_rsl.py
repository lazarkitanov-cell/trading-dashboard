# ═══════════════════════════════════════════════════════════════
# S&P 100 — Live RSL-Peak-Trail (wie S&P 100 Strategie.ipynb)
# RSL = Kurs / 30-Tage-Durchschnitt · Trail = RSL-Peak × (1 − 35%)
# ═══════════════════════════════════════════════════════════════

from datetime import date, timedelta

import pandas as pd
import requests

RSL_WINDOW = 30
RSL_PEAK_TRAIL = 0.35


def ticker_fix(ticker):
    t = (ticker or "").strip()
    if t.endswith(".L"):
        return t[:-2] + ".LSE"
    if "." not in t:
        return t + ".US"
    return t


def fetch_eod_series(ticker, api_key, days=None):
    """Tägliche adjusted_close-Serie von EODHD."""
    lookback = days or (RSL_WINDOW + 15)
    try:
        start = (date.today() - timedelta(days=lookback)).isoformat()
        r = requests.get(
            f"https://eodhd.com/api/eod/{ticker_fix(ticker)}",
            params={"api_token": api_key, "fmt": "json", "period": "d", "from": start},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        if not rows:
            return None
        idx, vals = [], []
        for row in rows:
            d = row.get("date")
            c = row.get("adjusted_close") or row.get("close")
            if d and c:
                idx.append(pd.Timestamp(d))
                vals.append(float(c))
        return pd.Series(vals, index=idx).sort_index() if vals else None
    except Exception:
        return None


def compute_rsl_from_series(prices, rsl_peak_stored=None):
    """Berechnet RSL, Trail und Puffer aus Preis-Serie."""
    if prices is None or len(prices) < RSL_WINDOW:
        return None
    ma = float(prices.iloc[-RSL_WINDOW:].mean())
    if ma <= 0:
        return None
    rsl_now = float(prices.iloc[-1]) / ma
    stored = float(rsl_peak_stored) if rsl_peak_stored else rsl_now
    peak = max(stored, rsl_now)
    trail = round(peak * (1.0 - RSL_PEAK_TRAIL), 4)
    puffer = round((rsl_now / trail - 1) * 100, 1) if trail > 0 else 0.0
    if puffer < 10:
        status = "WARNUNG"
    elif puffer < 25:
        status = "Beobachten"
    else:
        status = "OK"
    return {
        "rsl": round(rsl_now, 4),
        "rsl_peak": round(peak, 4),
        "trail": trail,
        "puffer": puffer,
        "status": status,
    }


def sp100_rsl_live(ticker, rsl_peak_stored=None, api_key=None, prices=None):
    """Live-RSL aus EODHD (oder übergebener Serie) + gespeichertem Peak aus JSON."""
    if prices is None:
        if not api_key:
            return None
        prices = fetch_eod_series(ticker, api_key)
    return compute_rsl_from_series(prices, rsl_peak_stored)
