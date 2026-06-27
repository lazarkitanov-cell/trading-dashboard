# ═══════════════════════════════════════════════════════════════
#  Tägliche Stop-/Exit-Signale — gemeinsam für app.py + stop_check.py
#  Live EODHD + Fallback JSON (Colab handelsanweisungen)
# ═══════════════════════════════════════════════════════════════

from datetime import date, timedelta

import requests

SOFORT_GRUND_KEYS = (
    "TRAILING STOP", "EMA100", "CRASH", "RSL-PEAK", "RSL-TRAIL",
    "STOP AUS", "STOP AUSGEL", "ALLE VERKAUF",
)


def safe_float(x):
    if x is None:
        return None
    try:
        v = float(x)
        if v != v or v == float("inf") or v == float("-inf") or v <= 0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def ticker_fix(ticker):
    t = (ticker or "").strip()
    if t.endswith(".L"):
        return t[:-2] + ".LSE"
    if "." not in t:
        return t + ".US"
    return t


def ticker_variants(ticker):
    """Mehrere EODHD-Symbole probieren (.ST, .LSE, …)."""
    t = (ticker or "").strip()
    out = []

    def add(x):
        if x and x not in out:
            out.append(x)

    add(ticker_fix(t))
    add(t)
    if t.endswith(".ST"):
        base = t[:-3]
        add(f"{base}.ST")
        add(f"{base}.STOCKHOLM")
    if t.endswith(".L"):
        add(t[:-2] + ".LSE")
    return out


def _fetch_realtime(api_key, symbol, timeout=10):
    try:
        r = requests.get(
            f"https://eodhd.com/api/real-time/{symbol}",
            params={"api_token": api_key, "fmt": "json"},
            timeout=timeout,
        )
        d = r.json()
        close = safe_float(d.get("close") or d.get("previousClose"))
        if not close:
            return None
        return {"close": close, "source": "RT", "symbol": symbol}
    except Exception:
        return None


def _fetch_eod_last(api_key, symbol, timeout=15):
    try:
        start = (date.today() - timedelta(days=14)).isoformat()
        r = requests.get(
            f"https://eodhd.com/api/eod/{symbol}",
            params={
                "api_token": api_key,
                "fmt": "json",
                "period": "d",
                "from": start,
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        if not rows:
            return None
        last = rows[-1]
        close = safe_float(last.get("adjusted_close") or last.get("close"))
        if not close:
            return None
        qd = last.get("date")
        return {
            "close": close,
            "source": "EOD",
            "symbol": symbol,
            "quote_date": date.fromisoformat(qd[:10]) if qd else None,
        }
    except Exception:
        return None


def fetch_quote(api_key, ticker, fallback_price=None, timeout=10):
    """Bester Kurs: EODHD RT → EOD → JSON-Fallback."""
    for sym in ticker_variants(ticker):
        q = _fetch_realtime(api_key, sym, timeout=timeout)
        if q:
            return q
    for sym in ticker_variants(ticker):
        q = _fetch_eod_last(api_key, sym, timeout=timeout)
        if q:
            return q
    fb = safe_float(fallback_price)
    if fb:
        return {"close": fb, "source": "JSON", "symbol": ticker, "quote_date": None}
    return None


def is_sofort_rec(rec):
    """Täglich handeln (Stop/Exit) — nicht Plan-Rebalancing."""
    if not isinstance(rec, dict):
        return False
    if str(rec.get("prioritaet") or "").strip().lower() == "sofort":
        return True
    act = str(rec.get("aktion") or rec.get("action") or "").upper()
    grund = str(rec.get("grund") or "").upper()
    if "VERKAUF" not in act and "ALLE VERKAUF" not in act:
        return False
    return any(k in grund for k in SOFORT_GRUND_KEYS)


def json_kurs_hints(raw):
    """Ticker/ISIN → kurs_eur aus handelsanweisungen (Colab-Fallback)."""
    hints = {}
    if not isinstance(raw, dict):
        return hints
    for rec in raw.get("handelsanweisungen") or []:
        if not isinstance(rec, dict):
            continue
        kurs = safe_float(rec.get("kurs_eur") or rec.get("kurs"))
        if not kurs:
            continue
        for key in (rec.get("ticker"), rec.get("isin")):
            if key:
                hints[str(key).upper()] = kurs
    return hints


def collect_json_sofort_exits(raw, strategie_label):
    """Sofort-VERKAUFEN aus Colab-JSON (wenn Live-Check fehlt)."""
    out = []
    if not isinstance(raw, dict):
        return out
    for rec in raw.get("handelsanweisungen") or []:
        if not isinstance(rec, dict):
            continue
        if not is_sofort_rec(rec):
            continue
        act = str(rec.get("aktion") or rec.get("action") or "").upper()
        if "VERKAUF" not in act and "ALLE VERKAUF" not in act:
            continue
        ticker = rec.get("ticker") or rec.get("isin") or "—"
        name = rec.get("name") or ""
        ticker_s = f"{ticker} — {name}" if name else str(ticker)
        kurs = safe_float(rec.get("kurs_eur") or rec.get("kurs"))
        pnl = rec.get("pnl_pct")
        out.append({
            "strategie": strategie_label,
            "ticker": ticker_s,
            "ticker_key": str(ticker).upper(),
            "kurs": kurs if kurs else "—",
            "stop": "—",
            "puffer": 0.0,
            "pnl_s": f"{pnl:+.1f}%" if pnl is not None else "",
            "grund": rec.get("grund") or "Sofort-Exit (Colab JSON)",
            "json_sofort": True,
        })
    return out


def smallcap_stop_row(isin, pos, trailing_pct, api_key, kurs_hints=None):
    """Small-Cap Stop-Zeile — Live + JSON-Fallback."""
    kauf = safe_float(pos.get("buy_price") or pos.get("einstieg"))
    if not kauf:
        return None
    ticker = pos.get("ticker") or isin
    hints = kurs_hints or {}
    fb = hints.get(str(ticker).upper()) or hints.get(str(isin).upper())
    q = fetch_quote(api_key, ticker, fallback_price=fb)
    if not q:
        return None
    kurs = q["close"]
    hw = safe_float(pos.get("high_water") or pos.get("hoch") or kauf) or kauf
    hw = max(hw, kurs)
    stop = round(hw * (1 - trailing_pct), 2)
    puffer = round((kurs / stop - 1) * 100, 1) if stop > 0 else 0.0
    return {
        "ticker": ticker,
        "isin": isin,
        "pos": pos,
        "kurs": kurs,
        "hw": hw,
        "stop": stop,
        "puffer": puffer,
        "quote_source": q.get("source", "?"),
        "triggered": kurs <= stop,
    }


def merge_stop_alerts(live_alerts, json_alerts):
    """JSON-Sofort-Signale ergänzen, ohne Duplikate."""
    seen = set()
    for a in live_alerts:
        tk = a.get("ticker_key") or str(a.get("ticker", ""))[:40].upper()
        seen.add((a.get("strategie"), tk))
    out = list(live_alerts)
    for ja in json_alerts:
        tk = ja.get("ticker_key") or str(ja.get("ticker", ""))[:40].upper()
        key = (ja.get("strategie"), tk)
        if key in seen:
            continue
        out.append(ja)
        seen.add(key)
    return out


JSON_STRATEGIES = (
    ("smallcap_positionen.json", "🇪🇺 Small Cap EU"),
    ("kassandra_positionen.json", "🌍 Kassandra"),
    ("sp100_positionen.json", "📈 S&P 100"),
    ("ivy_portfolio.json", "🏛 IVY/RAA"),
    ("etf_eingabe.json", "📊 ETF Aktien"),
    ("regime_momentum_positionen.json", "🚀 Regime Momentum"),
)
