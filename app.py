# ═══════════════════════════════════════════════════════════════════════════
#  TRADING DASHBOARD v3.8 — Live-Sync von GitHub
#  Nächster Check + Trailing-Stop (5 Strategien, JSON von GitHub / Colab)
# ═══════════════════════════════════════════════════════════════════════════

APP_VERSION = "3.8"
GITHUB_REPO = "lazarkitanov-cell/trading-dashboard"
GITHUB_BRANCH = "main"
GITHUB_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/"

import json
import math
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

try:
    from name_lookup import lookup_name as _lookup_name
except ImportError:
    _lookup_name = None

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


@st.cache_data(ttl=86400)
def cached_lookup_name(ticker, pos_key):
    """pos_key = JSON-String der Positions-Daten (für Cache)."""
    pos = json.loads(pos_key) if pos_key else None
    if _lookup_name:
        return _lookup_name(ticker, pos, API_KEY)
    # Fallback wenn name_lookup.py auf Streamlit fehlt
    n = (pos or {}).get("name", "") if isinstance(pos, dict) else ""
    if n and len(n) > 5 and n.upper() != ticker.split(".")[0].upper():
        return n
    return ticker.split(".")[0].replace(".US", "").replace(".TO", "")


def position_name(ticker, pos=None):
    pos_key = json.dumps(pos or {}, sort_keys=True, default=str)
    return cached_lookup_name(ticker, pos_key)


def lade_json(pfad):
    p = Path(pfad)
    return json.loads(p.read_text()) if p.exists() else None


@st.cache_data(ttl=120)
def lade_json_github(dateiname):
    """JSON live von GitHub (Colab-Upload) — Fallback auf Repo-Datei."""
    try:
        r = requests.get(
            GITHUB_RAW + dateiname,
            timeout=15,
            headers={"Cache-Control": "no-cache"},
        )
        if r.status_code == 200 and r.text.strip():
            return json.loads(r.text)
    except Exception:
        pass
    return lade_json(dateiname)


def sp100_erlaubte_ticker(sp100_pos):
    """Nur Positionen anzeigen, die noch im Portfolio oder Strategie-Signal sind."""
    if not sp100_pos:
        return None
    meine = sp100_pos.get("meine_aktien")
    if meine is None:
        return None
    return set(meine) | set(sp100_pos.get("tickers") or [])


def json_sync_hinweis(label, data):
    if not isinstance(data, dict):
        return f"{label}: —"
    ts = (
        data.get("sync_ts")
        or data.get("_sync_ts")
        or data.get("stand")
        or data.get("datum")
        or data.get("datum_heute")
    )
    if not ts:
        pos_dates = [
            v.get("entry_date") or v.get("datum") or v.get("kaufdatum")
            for v in data.values()
            if isinstance(v, dict)
        ]
        pos_dates = [d for d in pos_dates if d]
        if pos_dates:
            ts = f"Daten ({max(pos_dates)})"
    return f"{label}: {ts or '—'}"


def portfolio_ohne_meta(data):
    """Entfernt _sync_ts / Meta-Keys aus Positions-JSON."""
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if not str(k).startswith("_") and isinstance(v, dict)
    }


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
        "hinweis": "Di EOD → Mi 09:00 Xetra",
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
        "regel": (
            "35% RSL-Peak-Trail — Verkauf wenn RSL 35% unter dem "
            "eigenen RSL-Hoch fällt (kein Kurs-Trailing-Stop)"
        ),
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
    """Ausführliche Stop-Regel (Hinweise / Info-Box)."""
    if key == "etf":
        return f"{int(ETF_TS * 100)}% Trailing Stop (vom Hoch, native Währung)"
    return STOP_CFG[key]["regel"]


def stop_pct_anzeige(key):
    """Kompakter Trailing-Stop-Wert je Strategie (nur %)."""
    if not STOP_CFG[key].get("active"):
        return "—"
    pct = ETF_TS if key == "etf" else STOP_CFG[key]["pct"]
    if key == "sp100":
        return f"{int(round(pct * 100))}% RSL"
    return f"{int(round(pct * 100))}%"


def format_pruefen_ausfuehren(ci):
    return f"{format_datum(ci['handel_datum'])} {ci['handel_uhrzeit']}"


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

# Wie Ivy_2.1.ipynb TS_LIVE_EXCLUDE — kein Trailing Stop
IVY_TS_EXCLUDE = {"LYTR.XETRA", "VTI", "VEU", "BND", "VNQ", "FIX"}


def ivy_ffm_ticker(pos):
    ffm = (pos.get("ffm_ticker") or "").strip().upper()
    if not ffm:
        return None
    return ffm if ffm.endswith(".F") else ffm + ".F"


def ivy_eur_kurs(tk, pos):
    """EUR-Kurs: Frankfurt (.F) bevorzugt — wie Ivy Live-Stop."""
    ffm = ivy_ffm_ticker(pos)
    if ffm:
        k = safe_float(eodhd_kurs(ffm))
        if k:
            return k
    eodhd_tk = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
    return safe_float(eodhd_kurs(eodhd_tk))


def ivy_peak(pos):
    return safe_float(pos.get("peak_price")) or safe_float(pos.get("entry_price"))


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

_kass_raw = lade_json_github("kassandra_positionen.json") or {}
KASSANDRA_POS = portfolio_ohne_meta(_kass_raw)
SP100_POS = lade_json_github("sp100_positionen.json") or {}
_ivy_raw = lade_json_github("ivy_portfolio.json") or {}
IVY_POS = portfolio_ohne_meta(_ivy_raw)
_etf_raw = lade_json_github("etf_eingabe.json") or {}
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
ETF_STATE = lade_json_github("portfolio_state.json") or {}
_sc_raw = lade_json_github("smallcap_positionen.json") or {}
SMALLCAP_POS = portfolio_ohne_meta(_sc_raw)
SP100_ALLOWED = sp100_erlaubte_ticker(SP100_POS)

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
        name = position_name(ticker, p)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige("kassandra"),
            "Signal (EOD)": format_datum(ci["check_datum"]),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker,
            "Name": name,
            "Akt. Kurs": round(kurs, 2),
            "Stop-Kurs": stop,
            "% zum Stop": f"{puf:+.1f}%" if puf is not None else "—",
            "Status": status_icon(puf),
        })

    # S&P 100 — RSL-Peak-Trail 35% (RSL-Werte, nicht EUR/USD-Kurs!)
    ci = check_info("sp100")
    rsl_data = SP100_POS.get("rsl_data", {})
    for ticker, info in rsl_data.items():
        if SP100_ALLOWED is not None and ticker not in SP100_ALLOWED:
            continue
        trail = info.get("trail")
        rsl_now = info.get("rsl", 0)
        puf = info.get("puffer")
        if trail is None:
            continue
        kurs_live = safe_float(eodhd_kurs(ticker_fix(ticker)))
        abst_hoch = info.get("abst_hoch_pct")
        if abst_hoch is None and kurs_live:
            kurs_hoch = safe_float(info.get("kurs_hoch_usd"))
            if kurs_hoch:
                abst_hoch = round((kurs_live / kurs_hoch - 1) * 100, 1)
        kurs_anzeige = f"RSL {rsl_now:.3f}"
        if kurs_live:
            kurs_anzeige += f"  |  ${kurs_live:.2f}"
        if abst_hoch is not None:
            kurs_anzeige += f"  ({abst_hoch:+.1f}% Hoch)"
        name = position_name(ticker, info)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige("sp100"),
            "Signal (EOD)": format_datum(ci["check_datum"]),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker,
            "Name": name,
            "Akt. Kurs": kurs_anzeige,
            "Stop-Kurs": f"RSL {trail:.3f}",
            "% zum Stop": f"{puf:+.1f}% (RSL)" if puf is not None else "—",
            "Status": info.get("status", status_icon(puf, 10)),
        })

    # IVY — 15% Trailing unter Peak in EUR (wie Ivy_2.1.ipynb)
    ci = check_info("ivy")
    for tk, p in IVY_POS.items():
        if tk in IVY_TS_EXCLUDE or not p.get("entry_price"):
            continue
        peak = ivy_peak(p)
        if not peak:
            continue
        kurs = ivy_eur_kurs(tk, p) or peak
        stop = round(peak * (1 - STOP_CFG["ivy"]["pct"]), 2)
        puf = puffer_pct(kurs, stop)
        name = position_name(tk, p)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige("ivy"),
            "Signal (EOD)": format_datum(ci["check_datum"]),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": tk,
            "Name": name,
            "Akt. Kurs": f"{kurs:.2f} €",
            "Stop-Kurs": f"{stop:.2f} €",
            "% zum Stop": f"{puf:+.1f}%" if puf is not None else "—",
            "Status": status_icon(puf),
        })

    # ETF Aktien — 10% Trailing (native Währung, wie ETF Ampel_2)
    ci = check_info("etf")
    state_pos = ETF_STATE.get("positionen", {})
    for ticker, pos in ETF_POS.items():
        if not isinstance(pos, dict):
            continue
        # Verkaufte Position: oft aus state entfernt, aber noch in etf_eingabe.json
        if state_pos and ticker not in state_pos:
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
            "Trailing Stop %": stop_pct_anzeige("etf"),
            "Signal (EOD)": format_datum(ci["check_datum"]),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker.replace(".US", "").replace(".TO", ""),
            "Name": position_name(ticker, pos),
            "Akt. Kurs": round(kurs_f, 2),
            "Stop-Kurs": stop,
            "% zum Stop": f"{puf:+.1f}%" if puf is not None else "—",
            "Status": status_icon(puf, 3),
        })

    # Small Cap EU: kein Trailing Stop im Live-Betrieb (nur Rebalancing / EMA100 / Kassandra)

    return rows


def build_smallcap_rows():
    """Small Cap Positionen (kein Trailing Stop, nur Übersicht)."""
    rows = []
    ci = check_info("smallcap")
    for isin, p in SMALLCAP_POS.items():
        if not isinstance(p, dict):
            continue
        ticker = p.get("ticker") or isin
        name = position_name(ticker, p)
        rows.append({
            "Strategie": ci["label"],
            "Signal (EOD)": format_datum(ci["check_datum"]),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker,
            "Name": name,
            "Stück": p.get("shares", "—"),
            "Kauf EUR": p.get("buy_price", "—"),
            "Hoch EUR": p.get("high_water", "—"),
        })
    return rows


def build_check_rows():
    rows = []
    for key in ("kassandra", "sp100", "smallcap", "ivy", "etf"):
        ci = check_info(key)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige(key),
            "Rhythmus": ci["frequenz"],
            "Signal (EOD)": format_datum(ci["check_datum"]),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Tage bis Ausführung": ci["tage_bis"],
            "Hinweis": ci["hinweis"],
        })
    return rows


# ── UI ────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📈 Trading Dashboard")
    st.caption(f"v{APP_VERSION} · Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    if st.button("🔄 Kurse & JSON aktualisieren"):
        st.cache_data.clear()
        st.rerun()
    st.caption("JSON von GitHub (2 Min.) · EODHD-Kurse (5 Min.)")
    with st.expander("📡 JSON-Sync (GitHub)"):
        st.caption(json_sync_hinweis("Kassandra", _kass_raw))
        st.caption(json_sync_hinweis("S&P 100", SP100_POS))
        st.caption(json_sync_hinweis("IVY", _ivy_raw))
        st.caption(json_sync_hinweis("ETF", _etf_raw))
        st.caption(json_sync_hinweis("Small Cap", _sc_raw))

st.title("📅 Handel & Trailing-Stop")
st.caption("Signale aus Colab-JSON auf GitHub · Live-Kurse via EODHD")

st.subheader("Strategie-Übersicht")
st.caption("Signal = EOD-Kurs des Vortags · Prüfen & Ausführen = Review + Order am Handelstag")
st.dataframe(pd.DataFrame(build_check_rows()), use_container_width=True, hide_index=True)

st.divider()
st.subheader("Trailing-Stop Monitor")

with st.spinner("Live-Kurse laden..."):
    stop_rows = build_stop_rows()

if not stop_rows:
    st.warning("Keine Positionen oder keine Stop-Daten gefunden.")
else:
    df = pd.DataFrame(stop_rows)
    col_order = [
        "Strategie", "Trailing Stop %", "Signal (EOD)", "Prüfen & Ausführen",
        "Ticker", "Name", "Akt. Kurs", "Stop-Kurs", "% zum Stop", "Status",
    ]
    df = df[[c for c in col_order if c in df.columns]]
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
if SP100_POS.get("rsl_data"):
    sp100_datum = SP100_POS.get("datum", "—")
    st.info(
        f"📈 **S&P 100:** Exit-Regel ist **RSL-Peak-Trail** (Stand JSON: {sp100_datum}), "
        "nicht Kurs-Trailing. "
        "MU −17% vom Kurs-Hoch und +40% RSL-Puffer können gleichzeitig stimmen — "
        "der Stop greift erst, wenn der **RSL** 35% unter seinem **RSL-Hoch** fällt."
    )
if not SMALLCAP_POS:
    hinweise.append(
        "🇪🇺 **Small Cap:** `smallcap_positionen.json` fehlt/leer — "
        "aus `live_positions.json` hochladen."
    )
for h in hinweise:
    st.warning(h)

if SMALLCAP_POS:
    st.divider()
    st.subheader("Small Cap EU — Positionen")
    st.caption(f"{stop_regel('smallcap')}")
    sc_df = pd.DataFrame(build_smallcap_rows())
    if not sc_df.empty:
        st.dataframe(sc_df, use_container_width=True, hide_index=True)
    st.info(
        f"🇪🇺 **Small Cap EU:** {len(SMALLCAP_POS)} Position(en) — "
        "kein Trailing Stop im Monitor (Exit: EMA100 −5%, Kassandra ROT, Rebalancing)."
    )

st.caption("Alerts: GitHub Actions (stop_check.py) · Live-Kurse: EODHD")
