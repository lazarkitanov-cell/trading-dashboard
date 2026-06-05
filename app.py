# ═══════════════════════════════════════════════════════════════════════════
#  TRADING DASHBOARD v3 — Kompakt
#  Nächster Check + Trailing-Stop (5 Strategien, JSON von GitHub / Colab)
# ═══════════════════════════════════════════════════════════════════════════

import json
import math
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📈",
    layout="wide",
)

try:
    API_KEY = st.secrets["EODHD_API_KEY"]
except Exception:
    API_KEY = "69c0f8ad5ac198.37699109"

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def eodhd_kurs(ticker):
    try:
        r = requests.get(
            f"https://eodhd.com/api/real-time/{ticker}",
            params={"api_token": API_KEY, "fmt": "json"},
            timeout=10,
        )
        data = r.json()
        k = float(data.get("close") or data.get("previousClose") or 0)
        return k if k > 0 else None
    except Exception:
        return None


def lade_json(pfad):
    p = Path(pfad)
    return json.loads(p.read_text()) if p.exists() else None


def ticker_fix(ticker):
    if ticker.endswith(".L"):
        return ticker[:-2] + ".LSE"
    if "." not in ticker:
        return ticker + ".US"
    return ticker


def naechster_wochentag(weekday):
    heute = date.today()
    tage = (weekday - heute.weekday()) % 7
    if tage == 0:
        tage = 7
    return heute + timedelta(days=tage)


def letzter_wochentag(weekday):
    heute = date.today()
    tage = (heute.weekday() - weekday) % 7
    if tage == 0:
        tage = 7
    return heute - timedelta(days=tage)


def letzter_handelstag_monat():
    heute = date.today()
    if heute.month == 12:
        naechster_monat = date(heute.year + 1, 1, 1)
    else:
        naechster_monat = date(heute.year, heute.month + 1, 1)
    letzter = naechster_monat - timedelta(days=1)
    while letzter.weekday() >= 5:
        letzter -= timedelta(days=1)
    return letzter


def naechster_monatscheck():
    heute = date.today()
    letzter = letzter_handelstag_monat()
    if heute >= letzter:
        if heute.month == 12:
            erster = date(heute.year + 1, 2, 1)
        else:
            erster = date(heute.year, heute.month + 2, 1)
        letzter = erster - timedelta(days=1)
        while letzter.weekday() >= 5:
            letzter -= timedelta(days=1)
    return letzter


def format_datum(d):
    tage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return f"{d.strftime('%d.%m.%Y')} ({tage[d.weekday()]})"


def tage_bis(ziel):
    return (ziel - date.today()).days


def status_icon(puffer, warn=5):
    if puffer is None:
        return "—"
    if puffer <= 0:
        return "🔴 STOP"
    if puffer < warn:
        return "🟡 Gefahr"
    return "🟢 OK"


# Abgestimmt mit Colab-Hauptscripts (Stand Jun 2026)
CHECK_ZEITEN = {
    "kassandra": {
        "label": "🌍 Kassandra",
        "frequenz": "2-wöchentlich",
        "check_tag": 2,       # Mi Script/Check
        "handel_tag": 3,      # Do 09:00 EU
        "handel_uhrzeit": "09:00",
        "hinweis": "Mi EOD → Do 09:00 (alle 2 Wochen)",
    },
    "sp100": {
        "label": "📈 S&P 100",
        "frequenz": "wöchentlich",
        "check_tag": 2,       # Mi Signal
        "handel_tag": 3,      # Do 15:30 US
        "handel_uhrzeit": "15:30",
        "hinweis": "Mi EOD → Do 15:30 US",
    },
    "smallcap": {
        "label": "🇪🇺 Small Cap EU",
        "frequenz": "wöchentlich",
        "check_tag": 1,       # Di Check (Notebook: Tag vor REBAL)
        "handel_tag": 2,      # Mi 09:00 Xetra (REBAL_WEEKDAY=2)
        "handel_uhrzeit": "09:00",
        "hinweis": "Di Check → Mi 09:00 Xetra",
    },
    "ivy": {
        "label": "🏛 IVY/RAA",
        "frequenz": "monatlich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "09:00 / 15:30",
        "hinweis": "Monatsende → 1. Handelstag EU/US",
    },
    "etf": {
        "label": "📊 ETF Aktien",
        "frequenz": "monatlich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "15:30",
        "hinweis": "Monatsende → 1. Handelstag 15:30 US",
    },
}

STOP_CFG = {
    "kassandra": {
        "pct": 0.20, "typ": "Trailing", "basis": "hoch", "active": True,
        "regel": "20% Trailing Stop (vom Hoch)",
    },
    "sp100": {
        "pct": 0.35, "typ": "RSL-Trail", "basis": "rsl_peak", "active": True,
        "regel": "35% RSL-Peak-Trail",
    },
    "ivy": {
        "pct": 0.15, "typ": "Trailing", "basis": "peak", "active": True,
        "regel": "15% Trailing Stop (vom Peak)",
    },
    "etf": {
        "pct": 0.10, "typ": "Trailing", "basis": "hoch", "active": True,
        "regel": "10% Trailing Stop (vom Hoch, native Währung)",
    },
    "smallcap": {
        "pct": None, "typ": None, "basis": None, "active": False,
        "regel": "Kein Trailing Stop (Exit: EMA100 −5%, Kassandra ROT, Rebalancing)",
    },
}


def stop_regel(key):
    """Anzeige-Text für die Stop-Regel je Strategie."""
    if key == "etf":
        return f"{int(ETF_TS * 100)}% Trailing Stop (vom Hoch, native Währung)"
    return STOP_CFG[key]["regel"]


def check_info(key):
    cfg = CHECK_ZEITEN[key]
    if cfg["frequenz"] == "monatlich":
        daten = letzter_handelstag_monat()
        heute = date.today()
        if heute > daten:
            daten = naechster_monatscheck()
        handel = daten + timedelta(days=1)
        while handel.weekday() >= 5:
            handel += timedelta(days=1)
    else:
        check_wd = cfg["check_tag"]
        handel_wd = cfg["handel_tag"]
        daten = naechster_wochentag(check_wd)
        handel = naechster_wochentag(handel_wd)
        if handel <= daten:
            handel = daten + timedelta(days=1)
            while handel.weekday() >= 5:
                handel += timedelta(days=1)
    return {
        "label": cfg["label"],
        "frequenz": cfg["frequenz"],
        "check_datum": daten,
        "handel_datum": handel,
        "handel_uhrzeit": cfg["handel_uhrzeit"],
        "tage_bis": tage_bis(handel),
        "hinweis": cfg["hinweis"],
    }


TICKER_MAP_IVY = {
    "LYTR.XETRA": "LYTR.XETRA",
    "IFX.DE": "IFX.XETRA",
    "ASM.AS": "ASM.AS",
    "RWE.DE": "RWE.XETRA",
    "ABBN.SW": "ABBN.SW",
    "TSEM.US": "TSEM.US",
    "FN.US": "FN.US",
    "CVE.TO": "CVE.TO",
    "FLEX.US": "FLEX.US",
    "LRCX": "LRCX.US",
    "CIEN": "CIEN.US",
    "FIX": "FIX.US",
}


def safe_float(x):
    """None, NaN, ≤0 → None (JSON-NaN aus Colab abfangen)."""
    if x is None:
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v) or v <= 0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def puffer_pct(kurs, stop):
    kurs = safe_float(kurs)
    stop = safe_float(stop)
    if not kurs or not stop:
        return None
    return round((kurs / stop - 1) * 100, 1)


# ── JSON laden ────────────────────────────────────────────────────────────────

KASSANDRA_POS = lade_json("kassandra_positionen.json") or {}
SP100_POS = lade_json("sp100_positionen.json") or {}
IVY_POS = lade_json("ivy_portfolio.json") or {}
_etf_raw = lade_json("etf_eingabe.json") or {}
if isinstance(_etf_raw, dict) and "positionen" in _etf_raw:
    ETF_POS = {
        p["ticker"]: p
        for p in _etf_raw.get("positionen", [])
        if isinstance(p, dict) and p.get("ticker")
    }
    ETF_TS = _etf_raw.get("trailing_pct", 0.10)
else:
    ETF_POS = _etf_raw if isinstance(_etf_raw, dict) else {}
    ETF_TS = 0.10
ETF_STATE = lade_json("portfolio_state.json") or {}
SMALLCAP_POS = lade_json("smallcap_positionen.json") or {}

# ── Trailing-Stop Zeilen ──────────────────────────────────────────────────────

def build_stop_rows():
    rows = []

    # Kassandra — 20% Trailing (hoch aus JSON)
    ci = check_info("kassandra")
    for ticker, p in KASSANDRA_POS.items():
        kauf = p.get("einstieg", 0)
        hoch = p.get("hoch", kauf)
        if not kauf:
            continue
        tk = ticker_fix(ticker)
        kurs = eodhd_kurs(tk) or kauf
        stop = round(hoch * (1 - STOP_CFG["kassandra"]["pct"]), 2)
        puf = puffer_pct(kurs, stop)
        rows.append({
            "Strategie": ci["label"],
            "Stop-Regel": stop_regel("kassandra"),
            "Nächster Check": format_datum(ci["check_datum"]),
            "Handeln am": f"{format_datum(ci['handel_datum'])} {ci['handel_uhrzeit']}",
            "Ticker": ticker,
            "Akt. Kurs": round(kurs, 2),
            "Stop-Kurs": stop,
            "% zum Stop": f"{puf:+.1f}%" if puf is not None else "—",
            "Status": status_icon(puf),
        })

    # S&P 100 — RSL-Peak-Trail 35% (aus Notebook-Export rsl_data)
    ci = check_info("sp100")
    rsl_data = SP100_POS.get("rsl_data", {})
    for ticker, info in rsl_data.items():
        trail = info.get("trail")
        rsl_now = info.get("rsl", 0)
        puf = info.get("puffer")
        if trail is None:
            continue
        rows.append({
            "Strategie": ci["label"],
            "Stop-Regel": stop_regel("sp100"),
            "Nächster Check": format_datum(ci["check_datum"]),
            "Handeln am": f"{format_datum(ci['handel_datum'])} {ci['handel_uhrzeit']}",
            "Ticker": ticker,
            "Akt. Kurs": f"RSL {rsl_now:.3f}",
            "Stop-Kurs": trail,
            "% zum Stop": f"{puf:+.1f}%" if puf is not None else "—",
            "Status": info.get("status", status_icon(puf)),
        })

    # IVY — 15% Trailing unter Peak (wie Ivy_2.1.ipynb TS_LIVE_STOP)
    ci = check_info("ivy")
    for tk, p in IVY_POS.items():
        if tk == "FIX" or not p.get("entry_price"):
            continue
        kauf = float(p["entry_price"])
        peak = float(p.get("peak_price") or kauf)
        eodhd_tk = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
        kurs = eodhd_kurs(eodhd_tk) or kauf
        stop = round(peak * (1 - STOP_CFG["ivy"]["pct"]), 2)
        puf = puffer_pct(kurs, stop)
        rows.append({
            "Strategie": ci["label"],
            "Stop-Regel": stop_regel("ivy"),
            "Nächster Check": format_datum(ci["check_datum"]),
            "Handeln am": f"{format_datum(ci['handel_datum'])} {ci['handel_uhrzeit']}",
            "Ticker": tk,
            "Akt. Kurs": round(kurs, 2),
            "Stop-Kurs": stop,
            "% zum Stop": f"{puf:+.1f}%" if puf is not None else "—",
            "Status": status_icon(puf),
        })

    # ETF Aktien — 10% Trailing (native Währung, wie ETF Ampel_2)
    ci = check_info("etf")
    state_pos = ETF_STATE.get("positionen", {})
    for ticker, pos in ETF_POS.items():
        if not isinstance(pos, dict):
            continue
        kauf_eur = pos.get("kauf_kurs", 0)
        if not kauf_eur or kauf_eur < 0.01:
            continue
        kurs = safe_float(eodhd_kurs(ticker))
        st = state_pos.get(ticker, {})
        hoch = (
            safe_float(st.get("hoch_kurs"))
            or safe_float(pos.get("hoch_kurs"))
            or kurs
        )
        stop = safe_float(st.get("stop_level")) or safe_float(pos.get("stop_nativ"))
        if stop is None and hoch:
            stop = round(hoch * (1 - ETF_TS), 2)
        kurs_f = kurs or hoch
        puf = puffer_pct(kurs_f, stop)
        rows.append({
            "Strategie": ci["label"],
            "Stop-Regel": stop_regel("etf"),
            "Nächster Check": format_datum(ci["check_datum"]),
            "Handeln am": f"{format_datum(ci['handel_datum'])} {ci['handel_uhrzeit']}",
            "Ticker": ticker.replace(".US", "").replace(".TO", ""),
            "Akt. Kurs": round(kurs_f, 2),
            "Stop-Kurs": stop,
            "% zum Stop": f"{puf:+.1f}%" if puf is not None else "—",
            "Status": status_icon(puf, 3),
        })

    # Small Cap EU: kein Trailing Stop im Live-Betrieb (nur Rebalancing / EMA100 / Kassandra)

    return rows


def build_check_rows():
    rows = []
    for key in ("kassandra", "sp100", "smallcap", "ivy", "etf"):
        ci = check_info(key)
        rows.append({
            "Strategie": ci["label"],
            "Stop-Regel": stop_regel(key),
            "Rhythmus": ci["frequenz"],
            "Nächster Check": format_datum(ci["check_datum"]),
            "Handeln am": f"{format_datum(ci['handel_datum'])} {ci['handel_uhrzeit']}",
            "in Tagen": ci["tage_bis"],
            "Hinweis": ci["hinweis"],
        })
    return rows


# ── UI ────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📈 Trading Dashboard")
    st.caption(f"Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    if st.button("🔄 Kurse aktualisieren"):
        st.cache_data.clear()
        st.rerun()
    st.caption("EODHD · Cache 5 Min.")

st.title("📅 Handel & Trailing-Stop")
st.caption("Signale aus Colab-JSON auf GitHub · Live-Kurse via EODHD")

st.subheader("Nächste Check-Termine")
st.dataframe(pd.DataFrame(build_check_rows()), use_container_width=True, hide_index=True)

st.divider()
st.subheader("Trailing-Stop Monitor")

with st.spinner("Live-Kurse laden..."):
    stop_rows = build_stop_rows()

if not stop_rows:
    st.warning("Keine Positionen oder keine Stop-Daten gefunden.")
else:
    df = pd.DataFrame(stop_rows)
    st.dataframe(
        df.style.map(
            lambda v: (
                "color:#ff1744;font-weight:bold"
                if "STOP" in str(v)
                else (
                    "color:#ffd600"
                    if "Gefahr" in str(v)
                    else "color:#00c853" if "OK" in str(v) else ""
                )
            ),
            subset=["Status"],
        ),
        use_container_width=True,
        hide_index=True,
    )

# Hinweise bei fehlenden Daten
hinweise = []
if SP100_POS.get("tickers") and not SP100_POS.get("rsl_data"):
    hinweise.append(
        "📈 **S&P 100:** `sp100_positionen.json` enthält keine `rsl_data` — "
        "Notebook ausführen und JSON erneut auf GitHub laden."
    )
if not SMALLCAP_POS:
    hinweise.append(
        "🇪🇺 **Small Cap:** `smallcap_positionen.json` fehlt/leer — "
        "aus `live_positions.json` hochladen."
    )
for h in hinweise:
    st.warning(h)

if SMALLCAP_POS:
    st.info(
        f"🇪🇺 **Small Cap EU:** {len(SMALLCAP_POS)} Position(en) — "
        f"{stop_regel('smallcap')}. "
        "Positionen erscheinen nicht im Trailing-Stop Monitor."
    )

st.caption(
    "Trailing-Stops: Kassandra 20% · S&P 100 RSL-Trail 35% · IVY 15% · ETF 10% · "
    "Small Cap EU ohne Trailing Stop · Alerts: GitHub Actions (stop_check.py)"
)
