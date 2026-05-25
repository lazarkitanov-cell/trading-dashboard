# ═══════════════════════════════════════════════════════════════════════════
#  TRADING DASHBOARD v2 — Streamlit Cloud
#  Alle 5 Strategien | Live-Kurse via EODHD | Signale | Charts
# ═══════════════════════════════════════════════════════════════════════════

import streamlit as st
import requests
import json
import time
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta, date
from pathlib import Path

st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    div[data-testid="stMetricValue"] { font-size: 1.3rem; }
    .section-header { font-size: 1.1rem; font-weight: bold; margin: 10px 0 5px 0; }
</style>
""", unsafe_allow_html=True)

# ── API Key ───────────────────────────────────────────────────────────────────
try:
    API_KEY = st.secrets["EODHD_API_KEY"]
except Exception:
    API_KEY = "69c0f8ad5ac198.37699109"

# ══════════════════════════════════════════════════════════════════════════════
#  HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300)
def eodhd_kurs(ticker):
    try:
        r    = requests.get("https://eodhd.com/api/real-time/" + ticker,
                            params={"api_token": API_KEY, "fmt": "json"}, timeout=10)
        data = r.json()
        k    = float(data.get("close") or data.get("previousClose") or 0)
        return k if k > 0 else None
    except Exception:
        return None

@st.cache_data(ttl=3600)
def eodhd_name(ticker):
    """Holt den vollen Aktiennamen von EODHD."""
    try:
        r    = requests.get("https://eodhd.com/api/real-time/" + ticker,
                            params={"api_token": API_KEY, "fmt": "json"}, timeout=10)
        data = r.json()
        return data.get("name") or data.get("Name") or ticker
    except Exception:
        return ticker

@st.cache_data(ttl=300)
def eodhd_performance(ticker, kauf_kurs=None):
    """Berechnet Performance für 1T, 1W, 1M, 1J, MAX."""
    try:
        von = (datetime.today() - timedelta(days=400)).strftime("%Y-%m-%d")
        bis = datetime.today().strftime("%Y-%m-%d")
        r   = requests.get("https://eodhd.com/api/eod/" + ticker,
                           params={"api_token": API_KEY, "from": von, "to": bis,
                                   "fmt": "json", "period": "d"}, timeout=15)
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return None
        df      = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df      = df.set_index("date").sort_index()
        close   = df["close"].dropna()
        jetzt   = float(close.iloc[-1])

        def perf(tage):
            if len(close) <= tage:
                return None
            alt = float(close.iloc[-tage-1])
            return round((jetzt/alt - 1)*100, 2) if alt > 0 else None

        result = {
            "1T":  perf(1),
            "1W":  perf(5),
            "1M":  perf(21),
            "1J":  perf(252),
            "Kurs": round(jetzt, 2),
        }
        if kauf_kurs and kauf_kurs > 0:
            result["MAX"] = round((jetzt/kauf_kurs - 1)*100, 2)
        else:
            result["MAX"] = None
        return result
    except Exception:
        return None

def fmt_perf(v):
    """Formatiert Performance-Wert."""
    if v is None: return "—"
    return f"{v:+.1f}%"

def perf_farbe(v):
    if v is None: return ""
    if v > 0:     return "color: #00c853"
    elif v < 0:   return "color: #ff1744"
    return ""


@st.cache_data(ttl=3600)
def eodhd_history(ticker, tage=60):
    try:
        von = (datetime.today() - timedelta(days=tage)).strftime("%Y-%m-%d")
        bis = datetime.today().strftime("%Y-%m-%d")
        r   = requests.get("https://eodhd.com/api/eod/" + ticker,
                           params={"api_token": API_KEY, "from": von, "to": bis,
                                   "fmt": "json", "period": "d"}, timeout=15)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")
    except Exception:
        pass
    return pd.DataFrame()

def lade_json(pfad):
    p = Path(pfad)
    return json.loads(p.read_text()) if p.exists() else None

def balken(puffer, breite=10):
    p = max(0, min(breite, round(puffer / 25 * breite)))
    return "█" * p + "░" * (breite - p) + f"  {puffer:+.1f}%"

def status_icon(puffer, warn_grenze=5):
    if puffer <= 0:             return "🔴 STOP"
    elif puffer < warn_grenze:  return "🟡 Vorsicht"
    else:                       return "🟢 OK"

def naechster_wochentag(weekday):
    """Gibt Datum des nächsten Wochentags zurück (0=Mo, 4=Fr)."""
    heute   = date.today()
    tage    = (weekday - heute.weekday()) % 7
    if tage == 0: tage = 7
    return heute + timedelta(days=tage)

def letzter_wochentag(weekday):
    """Gibt Datum des letzten Wochentags zurück (exkl. heute)."""
    heute = date.today()
    tage  = (heute.weekday() - weekday) % 7
    if tage == 0: tage = 7   # Heute ausschließen
    return heute - timedelta(days=tage)

def letzter_handelstag_monat():
    """Letzter Handelstag des aktuellen Monats."""
    heute = date.today()
    if heute.month == 12:
        erster_naechster = date(heute.year + 1, 1, 1)
    else:
        erster_naechster = date(heute.year, heute.month + 1, 1)
    letzter = erster_naechster - timedelta(days=1)
    while letzter.weekday() >= 5:
        letzter -= timedelta(days=1)
    return letzter

def naechster_monatscheck():
    heute   = date.today()
    letzter = letzter_handelstag_monat()
    if heute >= letzter:
        # Nächsten Monat
        if heute.month == 12:
            erster = date(heute.year + 1, 2, 1)
        else:
            erster = date(heute.year, heute.month + 2, 1)
        letzter = erster - timedelta(days=1)
        while letzter.weekday() >= 5:
            letzter -= timedelta(days=1)
    return letzter

def tage_bis(ziel_datum):
    return (ziel_datum - date.today()).days

def format_datum(d):
    tage_namen = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return f"{d.strftime('%d.%m.%Y')} ({tage_namen[d.weekday()]})"

# ── Check-Zeiten je Strategie ────────────────────────────────────────────────
# ── Check-Zeiten Erklärung ───────────────────────────────────────────────────
# EOD-Workflow: Daten-Tag (Börsenschluss) → Handel-Tag (nächster Morgen)
#
#  Kassandra:  Mi EOD Daten  → Do Morgen handeln
#  S&P 100:    Mi EOD Daten  → Do Morgen handeln (nach US Pre-Market)
#  IVY/RAA:    Monatsende EOD → 1. Handelstag nächster Monat handeln
#  ETF Aktien: Monatsende EOD → 1. Handelstag nächster Monat handeln
#  Small Cap:  Fr EOD Daten  → Mo Morgen handeln

CHECK_ZEITEN = {
    "kassandra": {
        "frequenz":      "2-wöchentlich",
        "daten_tag":     2,         # Mittwoch = EOD Daten
        "handel_tag":    3,         # Donnerstag = handeln
        "daten_uhrzeit": "22:00",   # Mi Börsenschluss
        "handel_uhrzeit":"09:00",   # Do EU-Marktöffnung (ETFs auf LSE/Euronext)
        "handel_uhrzeit2": None,    # Kein US-Anteil
        "stop_pct":      0.20,
        "beschreibung":  "Mi EOD Daten → Do 09:00 handeln (EU ETFs)",
        "markt_info":    "🇪🇺 ETFs auf LSE/Euronext → Do 09:00"
    },
    "sp100": {
        "frequenz":      "wöchentlich",
        "daten_tag":     2,         # Mittwoch = EOD Daten
        "handel_tag":    3,         # Donnerstag = handeln
        "daten_uhrzeit": "22:00",   # Mi US Börsenschluss
        "handel_uhrzeit":"15:30",   # Do US-Marktöffnung
        "handel_uhrzeit2": None,
        "stop_pct":      0.35,
        "beschreibung":  "Mi EOD Daten → Do 15:30 handeln (US Aktien)",
        "markt_info":    "🇺🇸 Nur US Aktien → Do 15:30"
    },
    "ivy": {
        "frequenz":      "monatlich",
        "daten_tag":     None,
        "handel_tag":    None,
        "daten_uhrzeit": "22:00",   # Monatsende Börsenschluss
        "handel_uhrzeit":"09:00",   # EU Aktien zuerst
        "handel_uhrzeit2": "15:30", # US Aktien danach
        "stop_pct":      0.15,
        "beschreibung":  "Monatsende EOD → 1. Handelstag EU 09:00 + US 15:30",
        "markt_info":    "🇪🇺 EU/Asien 09:00 → 🇺🇸 US Aktien 15:30"
    },
    "etf": {
        "frequenz":      "monatlich",
        "daten_tag":     None,
        "handel_tag":    None,
        "daten_uhrzeit": "22:00",
        "handel_uhrzeit":"15:30",   # Alles US Aktien
        "handel_uhrzeit2": None,
        "stop_pct":      0.10,
        "beschreibung":  "Monatsende EOD → 1. Handelstag 15:30 (US Aktien)",
        "markt_info":    "🇺🇸 Nur US Aktien → 15:30"
    },
    "smallcap": {
        "frequenz":      "wöchentlich",
        "daten_tag":     4,         # Freitag = EOD Daten
        "handel_tag":    0,         # Montag = handeln
        "daten_uhrzeit": "17:30",   # Fr EU Börsenschluss
        "handel_uhrzeit":"09:00",   # Mo EU-Marktöffnung
        "handel_uhrzeit2": None,
        "stop_pct":      0.15,
        "beschreibung":  "Fr EOD Daten → Mo 09:00 handeln (EU Aktien)",
        "markt_info":    "🇪🇺 EU Aktien → Mo 09:00"
    },
}

def check_info(strategie_key):
    """
    Gibt Daten-Tag (EOD) und Handel-Tag zurück.
    EOD-Workflow: Daten-Tag Börsenschluss → Handel-Tag Morgen
    """
    cfg = CHECK_ZEITEN[strategie_key]

    if cfg["frequenz"] == "monatlich":
        # Daten: letzter Handelstag des Monats
        # Handel: erster Handelstag nächsten Monats
        daten_tag  = letzter_handelstag_monat()
        heute      = date.today()
        if heute > daten_tag:
            daten_tag = naechster_monatscheck()
        # Handel-Tag = nächster Werktag nach Daten-Tag
        handel_tag = daten_tag + timedelta(days=1)
        while handel_tag.weekday() >= 5:
            handel_tag += timedelta(days=1)
        letzter_daten  = letzter_handelstag_monat()
        letzter_handel = letzter_daten + timedelta(days=1)
        while letzter_handel.weekday() >= 5:
            letzter_handel += timedelta(days=1)
    else:
        daten_wd   = cfg["daten_tag"]
        handel_wd  = cfg["handel_tag"]
        daten_tag  = naechster_wochentag(daten_wd)
        handel_tag = naechster_wochentag(handel_wd)
        # Sicherstellen dass Handel nach Daten
        if handel_tag <= daten_tag:
            handel_tag = daten_tag + timedelta(days=1)
            while handel_tag.weekday() >= 5:
                handel_tag += timedelta(days=1)
        letzter_daten  = letzter_wochentag(daten_wd)
        letzter_handel = letzter_daten + timedelta(days=1)
        while letzter_handel.weekday() >= 5:
            letzter_handel += timedelta(days=1)

    return {
        "naechster":        handel_tag,
        "daten_tag":        daten_tag,
        "letzter":          letzter_handel,
        "letzter_daten":    letzter_daten,
        "tage_bis":         tage_bis(handel_tag),
        "daten_uhrzeit":    cfg["daten_uhrzeit"],
        "handel_uhrzeit":   cfg["handel_uhrzeit"],
        "handel_uhrzeit2":  cfg.get("handel_uhrzeit2"),
        "uhrzeit":          cfg["handel_uhrzeit"],
        "frequenz":         cfg["frequenz"],
        "beschreibung":     cfg["beschreibung"],
        "markt_info":       cfg.get("markt_info", ""),
    }

# ── Ticker-Mapping IVY ────────────────────────────────────────────────────────
TICKER_MAP_IVY = {
    "LYTR.XETRA": "LYTR.XETRA",
    "IFX.DE":     "IFX.XETRA",
    "ASM.AS":     "ASM.AS",
    "RWE.DE":     "RWE.XETRA",
    "ABBN.SW":    "ABBN.SW",
    "TSEM.US":    "TSEM.US",
    "FN.US":      "FN.US",
    "CVE.TO":     "CVE.TO",
    "FLEX.US":    "FLEX.US",
    "LRCX":       "LRCX.US",
    "CIEN":       "CIEN.US",
}

# ── Daten laden ───────────────────────────────────────────────────────────────
KASSANDRA_POS    = lade_json("kassandra_positionen.json") or {}
KASSANDRA_TICKER = lade_json("kassandra_meine_ticker.json") or {}
SP100_POS        = lade_json("sp100_positionen.json") or {}
IVY_POS          = lade_json("ivy_portfolio.json") or {}
ETF_POS          = lade_json("etf_eingabe.json") or {}
SMALLCAP_POS     = lade_json("smallcap_positionen.json") or {}

# ══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.title("📈 Trading Dashboard")
    st.caption(f"Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    st.divider()

    seite = st.radio("Navigation:", [
        "🏠 Übersicht",
        "📅 Signale",
        "📈 Performance",
        "🌍 Kassandra",
        "📈 S&P 100",
        "🏛 IVY / RAA",
        "📊 ETF Aktien",
        "🇪🇺 Small Cap EU",
    ])

    st.divider()
    if st.button("🔄 Kurse aktualisieren"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Kurse: EODHD · Cache: 5 Min.")


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: ÜBERSICHT (neu strukturiert)
# ══════════════════════════════════════════════════════════════════════════════
if seite == "🏠 Übersicht":
    st.title("🏠 Portfolio Übersicht")
    st.caption(f"Alle 5 Strategien — {datetime.now().strftime('%d.%m.%Y %H:%M')}")

    # ── Kennzahlen ────────────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        n      = len(KASSANDRA_POS)
        # Puffer = aktueller Kurs vs. Stop-Level (nicht Einstieg vs. Hoch)
        alerts = 0
        for ticker, p in KASSANDRA_POS.items():
            hoch = p.get("hoch", 0)
            if not hoch: continue
            stop     = hoch * 0.80
            eodhd_tk = ticker if "." in ticker else ticker + ".US"
            if eodhd_tk.endswith(".L"): eodhd_tk = eodhd_tk[:-2] + ".LSE"
            kurs = eodhd_kurs(eodhd_tk)
            if kurs and kurs <= stop:
                alerts += 1
        st.metric("🌍 Kassandra", f"{n} Pos.",
                  delta="🚨 STOP!" if alerts else "✅ OK",
                  delta_color="inverse" if alerts else "normal")
    with col2:
        st.metric("📈 S&P 100", f"{len(SP100_POS.get('tickers', []))} Pos.", delta="RSL-Trail 35%")
    with col3:
        st.metric("🏛 IVY/RAA", f"{len(IVY_POS)} Pos.", delta="Stop 15%")
    with col4:
        st.metric("📊 ETF Aktien", f"{len(ETF_POS)} Pos.", delta="Stop 10%")
    with col5:
        st.metric("🇪🇺 Small Cap", f"{len(SMALLCAP_POS)} Pos.", delta="Stop 15%")

    st.divider()

    # ══ WÖCHENTLICHE STRATEGIEN ══════════════════════════════════════════════
    st.markdown("## 📅 Wöchentliche Strategien")
    st.caption("Kassandra (Mi 07:30) · S&P 100 (Mi 15:30) · Small Cap (Fr 16:00)")

    wochen_rows = []

    # Kassandra
    ci = check_info("kassandra")
    for ticker, p in KASSANDRA_POS.items():
        kauf     = p.get("einstieg", 0)
        hoch     = p.get("hoch", kauf)
        if not kauf: continue
        eodhd_tk = ticker if "." in ticker else ticker + ".US"
        if eodhd_tk.endswith(".L"): eodhd_tk = eodhd_tk[:-2] + ".LSE"
        name     = eodhd_name(eodhd_tk)
        stop     = round(hoch * 0.80, 2)
        kurs_akt = eodhd_kurs(eodhd_tk) or kauf
        puffer   = round((kurs_akt / stop - 1) * 100, 1)
        wochen_rows.append({
            "Strategie":       "🌍 Kassandra",
            "Name":            name,
            "Ticker":          ticker,
            "Stop-Kurs":       stop,
            "Puffer zum Stop": balken(puffer),
            "Status":          status_icon(puffer),
            "Nächster Check":  f"{format_datum(ci['naechster'])} {ci['uhrzeit']} ({ci['tage_bis']}T)",
            "Letzter Check":   format_datum(ci['letzter']),
        })

    # S&P 100
    ci_sp = check_info("sp100")
    for ticker in SP100_POS.get("tickers", []):
        eodhd_tk = ticker + ".US"
        name     = eodhd_name(eodhd_tk)
        kurs     = eodhd_kurs(eodhd_tk)
        wochen_rows.append({
            "Strategie":       "📈 S&P 100",
            "Name":            name,
            "Ticker":          ticker,
            "Stop-Kurs":       "RSL-Trail",
            "Puffer zum Stop": "Im Script berechnet",
            "Status":          "🔵 Aktiv",
            "Nächster Check":  f"{format_datum(ci_sp['naechster'])} {ci_sp['uhrzeit']} ({ci_sp['tage_bis']}T)",
            "Letzter Check":   format_datum(ci_sp['letzter']),
        })

    # Small Cap
    ci_sc = check_info("smallcap")
    for isin, p in SMALLCAP_POS.items():
        tk   = p.get("ticker", isin[:10])
        name = eodhd_name(tk)
        kauf = p.get("buy_price", 0)
        kurs = eodhd_kurs(tk)
        if kurs and kauf:
            puffer = round((kurs/kauf - 1 + 0.15)*100, 1)
            puf_str = balken(puffer)
            st_icon = status_icon(puffer)
        else:
            puf_str = "kein Kurs"
            st_icon = "❓"
        wochen_rows.append({
            "Strategie":       "🇪🇺 Small Cap",
            "Name":            name,
            "Ticker":          tk,
            "Stop-Kurs":       round(kauf*0.85, 2) if kauf else "—",
            "Puffer zum Stop": puf_str,
            "Status":          st_icon,
            "Nächster Check":  f"{format_datum(ci_sc['naechster'])} {ci_sc['uhrzeit']} ({ci_sc['tage_bis']}T)",
            "Letzter Check":   format_datum(ci_sc['letzter']),
        })

    if wochen_rows:
        df_w = pd.DataFrame(wochen_rows)
        st.dataframe(
            df_w.style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) or "Aktiv" in str(v) else ""),
                subset=["Status"]
            ),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("Keine wöchentlichen Positionen")

    st.divider()

    # ══ MONATLICHE STRATEGIEN ════════════════════════════════════════════════
    st.markdown("## 📆 Monatliche Strategien")
    st.caption("IVY/RAA (Monatsende 08:00) · ETF Aktien (Monatsende 15:30)")

    monat_rows = []

    # IVY
    ci_ivy = check_info("ivy")
    for tk, p in IVY_POS.items():
        ep_str    = p.get("entry_price", "")
        kauf_kurs = float(ep_str) if ep_str else None
        eodhd_tk  = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
        name      = eodhd_name(eodhd_tk)
        kurs      = eodhd_kurs(eodhd_tk)
        if kurs and kauf_kurs:
            puffer  = round((kurs/kauf_kurs - 1 + 0.15)*100, 1)
            puf_str = balken(puffer)
            st_icon = status_icon(puffer)
            stop    = round(kauf_kurs*0.85, 2)
        else:
            puf_str = "kein Kurs"
            st_icon = "❓"
            stop    = "—"
        monat_rows.append({
            "Strategie":       "🏛 IVY/RAA",
            "Name":            name,
            "Ticker":          tk,
            "Stop-Kurs":       stop,
            "Puffer zum Stop": puf_str,
            "Status":          st_icon,
            "Nächster Check":  f"{format_datum(ci_ivy['naechster'])} {ci_ivy['uhrzeit']} ({ci_ivy['tage_bis']}T)",
            "Letzter Check":   format_datum(ci_ivy['letzter']),
        })

    # ETF
    ci_etf = check_info("etf")
    for ticker, pos in ETF_POS.items():
        kauf_kurs = pos.get("kauf_kurs", 0)
        waehr     = pos.get("waehrung", "USD")
        if not kauf_kurs: continue
        name      = eodhd_name(ticker)
        kurs      = eodhd_kurs(ticker)
        if kurs:
            puffer  = round((kurs/kauf_kurs - 1 + 0.10)*100, 1)
            puf_str = balken(puffer)
            st_icon = status_icon(puffer, warn_grenze=3)
            stop    = round(kauf_kurs*0.90, 2)
        else:
            puf_str = "kein Kurs"
            st_icon = "❓"
            stop    = round(kauf_kurs*0.90, 2)
        monat_rows.append({
            "Strategie":       "📊 ETF Aktien",
            "Name":            name,
            "Ticker":          ticker.replace(".US","").replace(".TO",""),
            "Währung":         waehr,
            "Stop-Kurs":       stop,
            "Puffer zum Stop": puf_str,
            "Status":          st_icon,
            "Nächster Check":  f"{format_datum(ci_etf['naechster'])} {ci_etf['uhrzeit']} ({ci_etf['tage_bis']}T)",
            "Letzter Check":   format_datum(ci_etf['letzter']),
        })

    if monat_rows:
        df_m = pd.DataFrame(monat_rows)
        st.dataframe(
            df_m.style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("Keine monatlichen Positionen")


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: SIGNALE (neu)
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "📅 Signale":
    st.title("📅 Handelssignale & Check-Kalender")
    st.caption(f"Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

    # ── Nächste Checks ────────────────────────────────────────────────────────
    st.subheader("⏰ Nächste Check-Termine")

    termine = []
    for key, label in [("kassandra","🌍 Kassandra"),("sp100","📈 S&P 100"),
                        ("smallcap","🇪🇺 Small Cap"),("ivy","🏛 IVY/RAA"),
                        ("etf","📊 ETF Aktien")]:
        ci = check_info(key)
        handel_zeit = ci["handel_uhrzeit"]
        if ci.get("handel_uhrzeit2"):
            handel_zeit += " (EU) + " + ci["handel_uhrzeit2"] + " (US)"
        termine.append({
            "Strategie":       label,
            "Frequenz":        ci["frequenz"],
            "📊 Daten (EOD)":  format_datum(ci["daten_tag"]) + " " + ci["daten_uhrzeit"],
            "🛒 Handeln":      format_datum(ci["naechster"]) + " " + handel_zeit,
            "Tage bis":        ci["tage_bis"],
            "Letzter Handel":  format_datum(ci["letzter"]),
            "Markt":           ci["markt_info"],
        })

    df_termine = pd.DataFrame(termine).sort_values("Tage bis")
    st.dataframe(df_termine, use_container_width=True, hide_index=True)

    st.divider()

    # ── Kassandra Handelsanweisungen ──────────────────────────────────────────
    st.subheader("🌍 Kassandra — Handelsanweisungen")
    ticker_soll = KASSANDRA_TICKER.get("ticker", []) if isinstance(KASSANDRA_TICKER, dict) else []
    pos_tickers = list(KASSANDRA_POS.keys())
    gespeichert = KASSANDRA_TICKER.get("gespeichert", "—") if isinstance(KASSANDRA_TICKER, dict) else "—"

    if ticker_soll:
        verkaufen = [t for t in pos_tickers if t not in ticker_soll]
        kaufen    = [t for t in ticker_soll if t not in pos_tickers]
        halten    = [t for t in pos_tickers if t in ticker_soll]

        col1, col2, col3 = st.columns(3)
        with col1:
            st.error(f"**🔴 VERKAUFEN ({len(verkaufen)})**")
            for t in verkaufen:
                name = eodhd_name(t if "." in t else t + ".US")
                st.write(f"• {t} — {name}")
            if not verkaufen:
                st.success("Kein Verkauf nötig")
        with col2:
            st.success(f"**🟢 KAUFEN ({len(kaufen)})**")
            for t in kaufen:
                name = eodhd_name(t if "." in t else t + ".US")
                st.write(f"• {t} — {name}")
            if not kaufen:
                st.info("Kein Kauf nötig")
        with col3:
            st.info(f"**🔵 HALTEN ({len(halten)})**")
            for t in halten:
                st.write(f"• {t}")

        st.caption(f"Modell gespeichert: {gespeichert}")
    else:
        st.info("Kein Kassandra-Modell gefunden")

    st.divider()

    # ── Stop-Alerts ───────────────────────────────────────────────────────────
    st.subheader("🚨 Aktuelle Stop-Alerts")

    alerts = []

    # Kassandra — Puffer = aktueller Kurs vs. Stop-Level
    for ticker, p in KASSANDRA_POS.items():
        kauf     = p.get("einstieg", 0)
        hoch     = p.get("hoch", kauf)
        if not kauf: continue
        eodhd_tk = ticker if "." in ticker else ticker + ".US"
        if eodhd_tk.endswith(".L"): eodhd_tk = eodhd_tk[:-2] + ".LSE"
        stop     = hoch * 0.80
        kurs_akt = eodhd_kurs(eodhd_tk) or kauf
        puffer   = round((kurs_akt / stop - 1) * 100, 1)
        if puffer <= 5:
            name = eodhd_name(eodhd_tk)
            alerts.append({
                "Strategie": "🌍 Kassandra",
                "Ticker":    ticker,
                "Name":      name,
                "Puffer":    f"{puffer:+.1f}%",
                "Status":    status_icon(puffer),
            })

    # ETF
    for ticker, pos in ETF_POS.items():
        kauf = pos.get("kauf_kurs", 0)
        if not kauf: continue
        kurs = eodhd_kurs(ticker)
        if kurs:
            puffer = round((kurs/kauf - 1 + 0.10)*100, 1)
            if puffer <= 5:
                name = eodhd_name(ticker)
                alerts.append({
                    "Strategie": "📊 ETF Aktien",
                    "Ticker":    ticker.replace(".US",""),
                    "Name":      name,
                    "Puffer":    f"{puffer:+.1f}%",
                    "Status":    status_icon(puffer, 3),
                })

    # IVY
    for tk, p in IVY_POS.items():
        ep_str    = p.get("entry_price", "")
        kauf_kurs = float(ep_str) if ep_str else None
        if not kauf_kurs: continue
        eodhd_tk  = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
        kurs      = eodhd_kurs(eodhd_tk)
        if kurs:
            puffer = round((kurs/kauf_kurs - 1 + 0.15)*100, 1)
            if puffer <= 5:
                name = eodhd_name(eodhd_tk)
                alerts.append({
                    "Strategie": "🏛 IVY/RAA",
                    "Ticker":    tk,
                    "Name":      name,
                    "Puffer":    f"{puffer:+.1f}%",
                    "Status":    status_icon(puffer),
                })

    if alerts:
        df_alerts = pd.DataFrame(alerts)
        st.dataframe(
            df_alerts.style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ),
            use_container_width=True, hide_index=True,
        )
    else:
        st.success("✅ Keine Stop-Alerts — alle Positionen sicher")


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: KASSANDRA
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "🌍 Kassandra":
    st.title("🌍 Kassandra — Länder ETF")
    ci = check_info("kassandra")

    # Check-Info Banner
    col1, col2, col3 = st.columns(3)
    handel_str = ci["handel_uhrzeit"]
    if ci.get("handel_uhrzeit2"):
        handel_str += " EU / " + ci["handel_uhrzeit2"] + " US"
    col1.metric("📊 EOD Daten", format_datum(ci["daten_tag"]) + " " + ci["daten_uhrzeit"])
    col2.metric("🛒 Handeln", format_datum(ci["naechster"]) + " " + handel_str,
                delta=f"in {ci['tage_bis']} Tagen")
    col3.metric("Letzter Handel", format_datum(ci["letzter"]))
    st.caption(ci["markt_info"])

    st.divider()

    # Handelsanweisungen
    ticker_soll = KASSANDRA_TICKER.get("ticker", []) if isinstance(KASSANDRA_TICKER, dict) else []
    pos_tickers = list(KASSANDRA_POS.keys())
    if ticker_soll:
        verkaufen = [t for t in pos_tickers if t not in ticker_soll]
        kaufen    = [t for t in ticker_soll if t not in pos_tickers]
        halten    = [t for t in pos_tickers if t in ticker_soll]
        col1, col2, col3 = st.columns(3)
        with col1:
            if verkaufen:
                st.error("🔴 VERKAUFEN\n\n" + "\n".join(verkaufen))
            else:
                st.success("Kein Verkauf nötig")
        with col2:
            if kaufen:
                st.success("🟢 KAUFEN\n\n" + "\n".join(kaufen))
            else:
                st.info("Kein Kauf nötig")
        with col3:
            st.info("🔵 HALTEN\n\n" + "\n".join(halten) if halten else "")

    st.divider()
    st.subheader("🛡 Trailing Stop (20% unter Hoch)")

    for ticker, p in KASSANDRA_POS.items():
        kauf  = p.get("einstieg", 0)
        hoch  = p.get("hoch", kauf)
        datum = p.get("kaufdatum", "-")
        if not kauf: continue
        stop     = round(hoch * 0.80, 2)
        eodhd_tk = ticker if "." in ticker else ticker + ".US"
        if eodhd_tk.endswith(".L"): eodhd_tk = eodhd_tk[:-2] + ".LSE"
        name     = eodhd_name(eodhd_tk)
        kurs_akt = eodhd_kurs(eodhd_tk) or kauf
        puffer   = round((kurs_akt / stop - 1) * 100, 1)
        icon     = "🔴" if puffer <= 0 else ("🟡" if puffer < 5 else "🟢")

        col1, col2 = st.columns([2, 3])
        with col1:
            st.markdown(f"### {icon} {ticker}")
            st.caption(name)
            m1, m2, m3 = st.columns(3)
            m1.metric("Kaufkurs", f"{kauf:.2f}")
            m2.metric("Stop", f"{stop:.2f}")
            m3.metric("Puffer", f"{puffer:+.1f}%",
                      delta_color="inverse" if puffer < 5 else "normal")
            st.progress(min(1.0, max(0.0, puffer/25)),
                        text=f"Abstand zum Stop: {puffer:+.1f}%")
            st.caption(f"Kauf: {datum}  |  Hoch: {hoch:.2f}")
        with col2:
            df_h = eodhd_history(eodhd_tk, 60)
            if not df_h.empty:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=df_h.index, y=df_h["close"],
                    mode="lines", line=dict(color="#00c853", width=2)))
                fig.add_hline(y=kauf, line_dash="dot", line_color="#ffd600",
                              annotation_text="Kauf")
                fig.add_hline(y=stop, line_dash="dash", line_color="#ff1744",
                              annotation_text="Stop")
                fig.update_layout(height=150, margin=dict(l=0,r=0,t=5,b=0),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    showlegend=False,
                    xaxis=dict(showgrid=False, showticklabels=False),
                    yaxis=dict(showgrid=True, gridcolor="#333",
                               tickfont=dict(size=9, color="#aaa")))
                st.plotly_chart(fig, use_container_width=True, key=ticker+"_k")
        st.divider()


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: S&P 100
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "📈 S&P 100":
    st.title("📈 S&P 100 Momentum")
    ci = check_info("sp100")

    col1, col2, col3 = st.columns(3)
    handel_str = ci["handel_uhrzeit"]
    if ci.get("handel_uhrzeit2"):
        handel_str += " EU / " + ci["handel_uhrzeit2"] + " US"
    col1.metric("📊 EOD Daten", format_datum(ci["daten_tag"]) + " " + ci["daten_uhrzeit"])
    col2.metric("🛒 Handeln", format_datum(ci["naechster"]) + " " + handel_str,
                delta=f"in {ci['tage_bis']} Tagen")
    col3.metric("Letzter Handel", format_datum(ci["letzter"]))
    st.caption(ci["markt_info"])

    st.info(f"**{len(SP100_POS.get('tickers', []))} Positionen** | Stop: RSL-Peak-Trail 35%")
    st.divider()

    tickers = SP100_POS.get("tickers", [])
    cols    = st.columns(3)
    for i, ticker in enumerate(tickers):
        with cols[i % 3]:
            kurs = eodhd_kurs(ticker + ".US")
            name = eodhd_name(ticker + ".US")
            if kurs:
                st.metric(f"🔵 {ticker}", f"${kurs:.2f}", delta=name)
            else:
                st.metric(f"🔵 {ticker}", "kein Kurs")

    st.divider()
    st.subheader("📊 Kursverlauf (60 Tage)")
    if tickers:
        ticker_sel = st.selectbox("Ticker wählen:", tickers)
        df = eodhd_history(ticker_sel + ".US", 60)
        if not df.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=df.index, y=df["close"],
                mode="lines", line=dict(color="#00c853", width=2),
                fill="tozeroy", fillcolor="rgba(0,200,83,0.1)"))
            fig.update_layout(height=300, paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#333"),
                yaxis=dict(gridcolor="#333", tickfont=dict(color="#aaa")),
                margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: IVY / RAA
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "🏛 IVY / RAA":
    st.title("🏛 IVY / Hybrid-RAA")
    ci = check_info("ivy")

    col1, col2, col3 = st.columns(3)
    handel_str = ci["handel_uhrzeit"]
    if ci.get("handel_uhrzeit2"):
        handel_str += " EU / " + ci["handel_uhrzeit2"] + " US"
    col1.metric("📊 EOD Daten", format_datum(ci["daten_tag"]) + " " + ci["daten_uhrzeit"])
    col2.metric("🛒 Handeln", format_datum(ci["naechster"]) + " " + handel_str,
                delta=f"in {ci['tage_bis']} Tagen")
    col3.metric("Letzter Handel", format_datum(ci["letzter"]))
    st.caption(ci["markt_info"])

    st.info("Stop: 15% unter Kaufkurs → wechseln zu SHY")
    st.divider()

    TS       = 0.15
    pos_data = []
    with st.spinner("Lade Live-Kurse..."):
        for tk, p in IVY_POS.items():
            ep_str    = p.get("entry_price", "")
            kauf_kurs = float(ep_str) if ep_str else None
            ed        = str(p.get("entry_date", "-"))
            eodhd_tk  = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
            name      = eodhd_name(eodhd_tk)
            kurs      = eodhd_kurs(eodhd_tk)
            time.sleep(0.05)
            if kurs and kauf_kurs:
                stop   = round(kauf_kurs*(1-TS), 2)
                pnl    = round((kurs/kauf_kurs-1)*100, 1)
                puffer = round((kurs/kauf_kurs-1+TS)*100, 1)
                pos_data.append({
                    "Ticker": tk, "Name": name, "Kaufdatum": ed,
                    "Kaufkurs": kauf_kurs, "Jetzt": round(kurs,2),
                    "Stop": stop, "PnL %": pnl, "Puffer %": puffer,
                    "Puffer zum Stop": balken(puffer),
                    "Status": status_icon(puffer),
                    "wert": kauf_kurs, "pnl": pnl
                })
            else:
                pos_data.append({
                    "Ticker": tk, "Name": name, "Kaufdatum": ed,
                    "Kaufkurs": kauf_kurs or 0, "Jetzt": None,
                    "Stop": None, "PnL %": None, "Puffer %": None,
                    "Puffer zum Stop": "kein Kurs",
                    "Status": "❓", "wert": 0, "pnl": 0
                })

    if pos_data:
        col1, col2, col3 = st.columns(3)
        col1.metric("🟢 OK",       sum(1 for p in pos_data if p["Puffer %"] and p["Puffer %"] > 5))
        col2.metric("🟡 Vorsicht", sum(1 for p in pos_data if p["Puffer %"] and 0 < p["Puffer %"] <= 5))
        col3.metric("🔴 Stop",     sum(1 for p in pos_data if p["Puffer %"] and p["Puffer %"] <= 0))
        st.divider()

        df = pd.DataFrame(pos_data)
        disp = ["Ticker","Name","Kaufdatum","Kaufkurs","Jetzt","PnL %","Puffer zum Stop","Status"]
        st.dataframe(
            df[disp].style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ).format({"Kaufkurs": "{:.2f}", "Jetzt": "{:.2f}",
                      "PnL %": "{:+.1f}"}),
            use_container_width=True, hide_index=True,
        )

        # Portfolio Chart
        st.divider()
        st.subheader("📊 Portfolio Verteilung")
        pf_list = [p for p in pos_data if p["wert"] > 0]
        if pf_list:
            labels  = [p.get("Ticker","?") for p in pf_list]
            values  = [abs(p.get("wert", 1)) for p in pf_list]
            colors  = ["#00c853" if p.get("pnl", 0) >= 0 else "#ff1744" for p in pf_list]
            fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.4,
                marker=dict(colors=colors), textinfo="label+percent"))
            fig.update_layout(height=300, margin=dict(l=0,r=0,t=10,b=0),
                paper_bgcolor="rgba(0,0,0,0)", showlegend=False,
                font=dict(color="white"))
            st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: ETF AKTIEN
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "📊 ETF Aktien":
    st.title("📊 ETF Aktien Momentum")
    ci = check_info("etf")

    col1, col2, col3 = st.columns(3)
    handel_str = ci["handel_uhrzeit"]
    if ci.get("handel_uhrzeit2"):
        handel_str += " EU / " + ci["handel_uhrzeit2"] + " US"
    col1.metric("📊 EOD Daten", format_datum(ci["daten_tag"]) + " " + ci["daten_uhrzeit"])
    col2.metric("🛒 Handeln", format_datum(ci["naechster"]) + " " + handel_str,
                delta=f"in {ci['tage_bis']} Tagen")
    col3.metric("Letzter Handel", format_datum(ci["letzter"]))
    st.caption(ci["markt_info"])

    st.info("Stop: 10% Trailing Stop")
    st.divider()

    TS       = 0.10
    pos_data = []
    with st.spinner("Lade Live-Kurse..."):
        for ticker, pos in ETF_POS.items():
            kauf_kurs = pos.get("kauf_kurs", 0)
            waehr     = pos.get("waehrung", "USD")
            if not kauf_kurs: continue
            name      = eodhd_name(ticker)
            kurs      = eodhd_kurs(ticker)
            time.sleep(0.05)
            if kurs:
                stop   = round(kauf_kurs*(1-TS), 2)
                pnl    = round((kurs/kauf_kurs-1)*100, 1)
                puffer = round((kurs/kauf_kurs-1+TS)*100, 1)
                pos_data.append({
                    "Ticker":          ticker.replace(".US","").replace(".TO",""),
                    "Name":            name,
                    "Währung":         waehr,
                    "Kaufkurs":        kauf_kurs,
                    "Jetzt":           round(kurs,2),
                    "Stop":            stop,
                    "PnL %":           pnl,
                    "Puffer zum Stop": balken(puffer),
                    "Status":          status_icon(puffer, 3),
                    "ticker_raw":      ticker,
                    "pnl":             pnl
                })

    if pos_data:
        col1, col2, col3, col4 = st.columns(4)
        ok       = sum(1 for p in pos_data if "OK" in p["Status"])
        vorsicht = sum(1 for p in pos_data if "Vorsicht" in p["Status"])
        stops    = sum(1 for p in pos_data if "STOP" in p["Status"])
        avg_pnl  = sum(p["PnL %"] for p in pos_data) / len(pos_data)
        col1.metric("🟢 OK", ok)
        col2.metric("🟡 Vorsicht", vorsicht)
        col3.metric("🔴 Stop", stops)
        col4.metric("Ø PnL", f"{avg_pnl:+.1f}%")
        st.divider()

        df   = pd.DataFrame(pos_data)
        disp = ["Ticker","Name","Währung","Kaufkurs","Jetzt","Stop","PnL %","Puffer zum Stop","Status"]
        st.dataframe(
            df[disp].style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ).format({"Kaufkurs": "{:.2f}", "Jetzt": "{:.2f}",
                      "Stop": "{:.2f}", "PnL %": "{:+.1f}"}),
            use_container_width=True, hide_index=True,
        )

        # PnL Chart
        st.divider()
        st.subheader("📊 PnL je Position")
        df_c = df.sort_values("PnL %")
        fig  = go.Figure(go.Bar(
            x=df_c["PnL %"], y=df_c["Ticker"], orientation="h",
            marker_color=["#ff1744" if v < 0 else "#00c853" for v in df_c["PnL %"]],
            text=[f"{v:+.1f}%" for v in df_c["PnL %"]], textposition="outside",
        ))
        fig.update_layout(height=350, paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(gridcolor="#333", tickfont=dict(color="#aaa")),
            yaxis=dict(tickfont=dict(color="white")),
            margin=dict(l=0,r=60,t=10,b=0))
        st.plotly_chart(fig, use_container_width=True)

        # Einzel-Chart
        st.divider()
        st.subheader("📈 Kursverlauf")
        ticker_sel = st.selectbox("Ticker:", [p["Ticker"] for p in pos_data])
        sel_pos    = next(p for p in pos_data if p["Ticker"] == ticker_sel)
        df_hist    = eodhd_history(sel_pos["ticker_raw"], 60)
        if not df_hist.empty:
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(x=df_hist.index, y=df_hist["close"],
                mode="lines", line=dict(color="#00c853", width=2),
                fill="tozeroy", fillcolor="rgba(0,200,83,0.1)"))
            fig2.add_hline(y=sel_pos["Kaufkurs"], line_dash="dot",
                           line_color="#ffd600", annotation_text="Kauf")
            fig2.add_hline(y=sel_pos["Stop"], line_dash="dash",
                           line_color="#ff1744", annotation_text="Stop")
            fig2.update_layout(height=300, paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#333"),
                yaxis=dict(gridcolor="#333", tickfont=dict(color="#aaa")),
                margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig2, use_container_width=True)



# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "📈 Performance":
    st.title("📈 Performance Übersicht")
    st.caption(f"Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')} — Kurse via EODHD")

    st.info("Performance wird aus Tagesdaten berechnet. 1T=1 Handelstag, 1W=5T, 1M=21T, 1J=252T, MAX=seit Kauf")

    # Alle Positionen sammeln
    alle_pos = []

    # Kassandra
    for ticker, p in KASSANDRA_POS.items():
        kauf = p.get("einstieg", 0)
        eodhd_tk = ticker if "." in ticker else ticker + ".US"
        alle_pos.append({"gruppe": "🌍 Kassandra", "ticker": eodhd_tk,
                         "anzeige": ticker, "kauf": kauf})

    # ETF Aktien
    for ticker, pos in ETF_POS.items():
        alle_pos.append({"gruppe": "📊 ETF Aktien", "ticker": ticker,
                         "anzeige": ticker.replace(".US","").replace(".TO",""),
                         "kauf": pos.get("kauf_kurs", 0)})

    # IVY
    for tk, p in IVY_POS.items():
        ep_str    = p.get("entry_price", "")
        kauf_kurs = float(ep_str) if ep_str else None
        eodhd_tk  = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
        alle_pos.append({"gruppe": "🏛 IVY/RAA", "ticker": eodhd_tk,
                         "anzeige": tk, "kauf": kauf_kurs})

    # S&P 100
    sp100_detail = SP100_POS.get("positionen", {})
    for ticker in SP100_POS.get("tickers", []):
        kauf_info = sp100_detail.get(ticker, {})
        kauf_kurs = kauf_info.get("kauf_kurs", None)
        alle_pos.append({"gruppe": "📈 S&P 100", "ticker": ticker + ".US",
                         "anzeige": ticker, "kauf": kauf_kurs})

    # Small Cap
    for isin, p in SMALLCAP_POS.items():
        tk = p.get("ticker", isin[:10])
        alle_pos.append({"gruppe": "🇪🇺 Small Cap", "ticker": tk,
                         "anzeige": tk, "kauf": p.get("buy_price", 0)})

    # Strategie-Filter
    gruppen = sorted(set(p["gruppe"] for p in alle_pos))
    sel_gruppen = st.multiselect("Strategien filtern:", gruppen, default=gruppen)
    alle_pos = [p for p in alle_pos if p["gruppe"] in sel_gruppen]

    st.divider()

    rows = []
    progress = st.progress(0, text="Lade Performance-Daten...")
    for i, pos in enumerate(alle_pos):
        progress.progress((i+1)/len(alle_pos), text=f"Lade {pos['anzeige']}...")
        name   = eodhd_name(pos["ticker"])
        result = eodhd_performance(pos["ticker"], pos["kauf"])
        time.sleep(0.05)
        if result:
            rows.append({
                "Strategie": pos["gruppe"],
                "Ticker":    pos["anzeige"],
                "Name":      name,
                "Kurs":      result["Kurs"],
                "1T %":      fmt_perf(result["1T"]),
                "1W %":      fmt_perf(result["1W"]),
                "1M %":      fmt_perf(result["1M"]),
                "1J %":      fmt_perf(result["1J"]),
                "MAX %":     fmt_perf(result["MAX"]),
                "_1T":       result["1T"],
                "_1W":       result["1W"],
                "_1M":       result["1M"],
                "_1J":       result["1J"],
                "_MAX":      result["MAX"],
            })
        else:
            rows.append({
                "Strategie": pos["gruppe"], "Ticker": pos["anzeige"],
                "Name": name, "Kurs": "—",
                "1T %": "—", "1W %": "—", "1M %": "—",
                "1J %": "—", "MAX %": "—",
                "_1T": None, "_1W": None, "_1M": None,
                "_1J": None, "_MAX": None,
            })

    progress.empty()

    if rows:
        df = pd.DataFrame(rows)

        # Zusammenfassung
        st.subheader("📊 Zusammenfassung")
        valide = [r for r in rows if r["_1M"] is not None]
        if valide:
            col1, col2, col3, col4 = st.columns(4)
            avg_1t  = sum(r["_1T"] for r in valide if r["_1T"]) / max(1, sum(1 for r in valide if r["_1T"]))
            avg_1w  = sum(r["_1W"] for r in valide if r["_1W"]) / max(1, sum(1 for r in valide if r["_1W"]))
            avg_1m  = sum(r["_1M"] for r in valide if r["_1M"]) / max(1, sum(1 for r in valide if r["_1M"]))
            avg_max = sum(r["_MAX"] for r in valide if r["_MAX"]) / max(1, sum(1 for r in valide if r["_MAX"]))
            col1.metric("Ø 1 Tag",   f"{avg_1t:+.1f}%",  delta_color="normal" if avg_1t >= 0 else "inverse")
            col2.metric("Ø 1 Woche", f"{avg_1w:+.1f}%",  delta_color="normal" if avg_1w >= 0 else "inverse")
            col3.metric("Ø 1 Monat", f"{avg_1m:+.1f}%",  delta_color="normal" if avg_1m >= 0 else "inverse")
            col4.metric("Ø seit Kauf",f"{avg_max:+.1f}%", delta_color="normal" if avg_max >= 0 else "inverse")

        st.divider()

        # Tabelle
        st.subheader("📋 Alle Positionen")
        disp = ["Strategie","Ticker","Name","Kurs","1T %","1W %","1M %","1J %","MAX %"]

        def farbe_perf(v):
            if v == "—": return ""
            try:
                num = float(v.replace("%","").replace("+",""))
                return "color: #00c853" if num > 0 else "color: #ff1744" if num < 0 else ""
            except Exception:
                return ""

        st.dataframe(
            df[disp].style.map(farbe_perf,
                subset=["1T %","1W %","1M %","1J %","MAX %"]),
            use_container_width=True, hide_index=True,
        )


    st.divider()
    st.subheader("📈 Strategie-Vergleich vs. SPY (seit Kauf)")

    with st.spinner("Lade Kursdaten für Chart..."):

        @st.cache_data(ttl=3600)
        def lade_strategie_kurve(tickers_kaufkurse: tuple, label: str, start_datum: str):
            """
            Berechnet gleichgewichtete Portfolio-Kurve ab start_datum.
            tickers_kaufkurse: tuple von (eodhd_ticker, kauf_kurs) Paaren
            """
            try:
                von = start_datum
                bis = datetime.today().strftime("%Y-%m-%d")
                alle_serien = []
                for ticker, kauf_kurs in tickers_kaufkurse:
                    r = requests.get(
                        "https://eodhd.com/api/eod/" + ticker,
                        params={"api_token": API_KEY, "from": von, "to": bis,
                                "fmt": "json", "period": "d"}, timeout=15)
                    data = r.json()
                    if not isinstance(data, list) or len(data) < 2:
                        continue
                    df_t = pd.DataFrame(data)
                    df_t["date"] = pd.to_datetime(df_t["date"])
                    df_t = df_t.set_index("date")["close"].dropna()
                    # Normalisieren auf Kaufkurs (=100 am Starttag)
                    erste_val = df_t.iloc[0]
                    df_norm   = df_t / erste_val * 100
                    alle_serien.append(df_norm)
                    time.sleep(0.05)
                if not alle_serien:
                    return pd.Series(dtype=float)
                df_portfolio = pd.concat(alle_serien, axis=1).mean(axis=1)
                return df_portfolio
            except Exception as e:
                return pd.Series(dtype=float)

        # Start-Daten bestimmen
        kass_start = min(
            (str(p.get("kaufdatum", "2026-01-01")) for p in KASSANDRA_POS.values()),
            default="2026-01-01"
        )
        etf_start = min(
            (pos.get("kauf_datum", "2026-01-01") for pos in ETF_POS.values()),
            default="2026-01-01"
        )
        ivy_start = min(
            (str(p.get("entry_date", "2026-01-01")) for p in IVY_POS.values()),
            default="2026-01-01"
        )
        sp100_start = SP100_POS.get("live_start", "2026-03-04")
        gesamt_start = min(kass_start, etf_start, ivy_start, sp100_start)

        # Kassandra — Felder: einstieg=Kaufkurs, kaufdatum=Datum
        def kassandra_eodhd_ticker(t):
            """Konvertiert Kassandra-Ticker ins EODHD-Format."""
            if t.endswith(".L"):   return t[:-2] + ".LSE"
            if "." not in t:       return t + ".US"
            return t

        kass_ticker_kauf = tuple(
            (kassandra_eodhd_ticker(t), float(p.get("einstieg", 100)))
            for t, p in KASSANDRA_POS.items()
            if p.get("einstieg")
        )

        # ETF Aktien
        etf_ticker_kauf = tuple(
            (ticker, pos.get("kauf_kurs", 100))
            for ticker, pos in ETF_POS.items()
            if pos.get("kauf_kurs")
        )

        # IVY
        ivy_ticker_kauf = tuple(
            (TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk),
             float(p.get("entry_price", 100)))
            for tk, p in IVY_POS.items()
            if p.get("entry_price")
        )

        # SP100
        sp100_detail   = SP100_POS.get("positionen", {})
        sp100_tk_kauf  = tuple(
            (t + ".US", sp100_detail.get(t, {}).get("kauf_kurs", 100))
            for t in SP100_POS.get("tickers", [])
        )

        # SPY Benchmark
        @st.cache_data(ttl=3600)
        def lade_spy(start_datum):
            try:
                r = requests.get(
                    "https://eodhd.com/api/eod/SPY.US",
                    params={"api_token": API_KEY, "from": start_datum,
                            "to": datetime.today().strftime("%Y-%m-%d"),
                            "fmt": "json", "period": "d"}, timeout=15)
                data = r.json()
                if not isinstance(data, list) or len(data) < 2:
                    return pd.Series(dtype=float)
                df_s = pd.DataFrame(data)
                df_s["date"] = pd.to_datetime(df_s["date"])
                df_s = df_s.set_index("date")["close"].dropna()
                return df_s / df_s.iloc[0] * 100
            except Exception:
                return pd.Series(dtype=float)

        # Alle Kurven laden
        kurven = {}
        if kass_ticker_kauf and kass_start != "2026-01-01":
            k = lade_strategie_kurve(kass_ticker_kauf, "Kassandra", kass_start)
            if not k.empty: kurven["🌍 Kassandra"] = k
        elif kass_ticker_kauf:
            k = lade_strategie_kurve(kass_ticker_kauf, "Kassandra", kass_start)
            if not k.empty: kurven["🌍 Kassandra"] = k

        if etf_ticker_kauf:
            k = lade_strategie_kurve(etf_ticker_kauf, "ETF", etf_start)
            if not k.empty: kurven["📊 ETF Aktien"] = k

        if ivy_ticker_kauf:
            k = lade_strategie_kurve(ivy_ticker_kauf, "IVY", ivy_start)
            if not k.empty: kurven["🏛 IVY/RAA"] = k

        if sp100_tk_kauf and any(v > 0 for _, v in sp100_tk_kauf):
            k = lade_strategie_kurve(sp100_tk_kauf, "SP100", sp100_start)
            if not k.empty: kurven["📈 S&P 100"] = k

        # Frühestes Datum über alle Strategien
        alle_starts = []
        if kass_ticker_kauf:  alle_starts.append(kass_start)
        if etf_ticker_kauf:   alle_starts.append(etf_start)
        if ivy_ticker_kauf:   alle_starts.append(ivy_start)
        if sp100_tk_kauf and any(v > 0 for _, v in sp100_tk_kauf):
            alle_starts.append(sp100_start)
        gesamt_start = min(alle_starts) if alle_starts else "2026-01-01"

        spy_kurve = lade_spy(gesamt_start)
        if not spy_kurve.empty:
            kurven["📊 SPY (Benchmark)"] = spy_kurve

    if kurven:
        farben = {
            "🌍 Kassandra":       "#00c853",
            "📊 ETF Aktien":     "#00b0ff",
            "🏛 IVY/RAA":        "#ffd600",
            "📈 S&P 100":        "#ff6d00",
            "🇪🇺 Small Cap":     "#e040fb",
            "📊 SPY (Benchmark)": "#aaaaaa",
        }

        fig = go.Figure()
        for name, serie in kurven.items():
            farbe  = farben.get(name, "#aaaaaa")
            is_spy = "SPY" in name
            dash   = "dash" if is_spy else "solid"
            breite = 2 if is_spy else 3.5

            # Letzter Wert für Endlabel
            letzter_y   = float(serie.iloc[-1])
            performance = letzter_y - 100
            label_text  = f"{name}  {performance:+.1f}%"

            fig.add_trace(go.Scatter(
                x=serie.index, y=serie.values,
                mode="lines",
                name=name,
                line=dict(color=farbe, width=breite, dash=dash),
                hovertemplate=(
                    "<b>%{fullData.name}</b><br>"
                    "%{x|%d.%m.%Y}<br>"
                    "Performance: <b>%{customdata:+.1f}%</b>"
                    "<extra></extra>"
                ),
                customdata=serie.values - 100,
            ))

            # Endlabel direkt an der Linie
            fig.add_annotation(
                x=serie.index[-1],
                y=letzter_y,
                text=f"<b>{performance:+.1f}%</b>",
                showarrow=False,
                xanchor="left",
                xshift=8,
                font=dict(color=farbe, size=12, family="monospace"),
            )

        fig.add_hline(y=100, line_color="#444444", line_dash="dot",
                      line_width=1, annotation_text="Kaufpreis",
                      annotation_font_color="#666")

        fig.update_layout(
            height=500,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(15,15,25,0.9)",
            legend=dict(
                bgcolor="rgba(20,20,40,0.85)",
                bordercolor="#444",
                borderwidth=1,
                font=dict(color="white", size=14),
                yanchor="top", y=0.99,
                xanchor="left", x=0.01,
                itemsizing="constant",
                tracegroupgap=8,
            ),
            xaxis=dict(
                gridcolor="#2a2a3a",
                tickfont=dict(color="#aaa", size=11),
                showgrid=True,
                tickformat="%d.%m.%Y",
            ),
            yaxis=dict(
                gridcolor="#2a2a3a",
                tickfont=dict(color="#aaa", size=11),
                title=dict(text="Performance (Kauf = 100)", font=dict(color="#aaa")),
                tickformat=".0f",
                ticksuffix="",
            ),
            margin=dict(l=10, r=100, t=20, b=10),
            hovermode="x unified",
            hoverlabel=dict(
                bgcolor="rgba(20,20,40,0.9)",
                bordercolor="#555",
                font=dict(color="white", size=13),
            ),
        )

        st.plotly_chart(fig, use_container_width=True)

        # Legende erklären
        st.caption("Alle Strategien normalisiert auf 100 beim jeweiligen Kaufdatum. SPY = S&P 500 ETF als Benchmark ab ältestem Kaufdatum.")
    else:
        st.info("Keine Kursdaten verfügbar für Chart")

    st.divider()

    # Balkendiagramm beste/schlechteste
    if rows:
        st.subheader("🏆 Top & Flop — 1 Monat")
        df_chart = df[df["_1M"].notna()].sort_values("_1M", ascending=False)
        if not df_chart.empty:
            fig = go.Figure(go.Bar(
                x=df_chart["_1M"],
                y=df_chart["Ticker"],
                orientation="h",
                marker_color=["#00c853" if v >= 0 else "#ff1744"
                              for v in df_chart["_1M"]],
                text=[f"{v:+.1f}%" for v in df_chart["_1M"]],
                textposition="outside",
            ))
            fig.update_layout(
                height=max(300, len(df_chart)*28),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#333", tickfont=dict(color="#aaa"),
                           title="Performance 1 Monat %"),
                yaxis=dict(tickfont=dict(color="white")),
                margin=dict(l=0, r=70, t=10, b=0),
            )
            st.plotly_chart(fig, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: SMALL CAP EU
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "🇪🇺 Small Cap EU":
    st.title("🇪🇺 Small Cap Europe")
    ci = check_info("smallcap")

    col1, col2, col3 = st.columns(3)
    handel_str = ci["handel_uhrzeit"]
    if ci.get("handel_uhrzeit2"):
        handel_str += " EU / " + ci["handel_uhrzeit2"] + " US"
    col1.metric("📊 EOD Daten", format_datum(ci["daten_tag"]) + " " + ci["daten_uhrzeit"])
    col2.metric("🛒 Handeln", format_datum(ci["naechster"]) + " " + handel_str,
                delta=f"in {ci['tage_bis']} Tagen")
    col3.metric("Letzter Handel", format_datum(ci["letzter"]))
    st.caption(ci["markt_info"])

    st.info("Stop: 15% Trailing Stop | Rebalancing: Freitag")
    st.divider()

    if not SMALLCAP_POS:
        st.warning("Noch keine Positionen — add_position() im Script verwenden")
        st.code('add_position("ISIN", "2026-05-24", kaufkurs, stueckzahl)')
        st.stop()

    TS       = 0.15
    pos_data = []
    with st.spinner("Lade Kurse..."):
        for isin, p in SMALLCAP_POS.items():
            tk   = p.get("ticker", isin[:10])
            kd   = p.get("buy_date", "-")
            kauf = p.get("buy_price", 0)
            if not kauf: continue
            name = eodhd_name(tk)
            kurs = eodhd_kurs(tk)
            time.sleep(0.05)
            if kurs:
                stop   = round(kauf*(1-TS), 2)
                pnl    = round((kurs/kauf-1)*100, 1)
                puffer = round((kurs/stop - 1)*100, 1)
                pos_data.append({
                    "Ticker": tk, "Name": name, "ISIN": isin,
                    "Kaufdatum": kd, "Kaufkurs": kauf,
                    "Jetzt": round(kurs,2), "Stop": stop,
                    "PnL %": pnl, "Puffer zum Stop": balken(puffer),
                    "Status": status_icon(puffer),
                })

    if pos_data:
        df   = pd.DataFrame(pos_data)
        disp = ["Ticker","Name","ISIN","Kaufdatum","Kaufkurs","Jetzt","Stop","PnL %","Puffer zum Stop","Status"]
        st.dataframe(
            df[disp].style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ),
            use_container_width=True, hide_index=True,
        )
