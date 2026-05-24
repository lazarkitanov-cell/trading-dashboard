# ═══════════════════════════════════════════════════════════════════════════
#  TRADING DASHBOARD — Streamlit Cloud
#  Alle 5 Strategien | Live-Kurse via EODHD | Stop-Status | Charts
# ═══════════════════════════════════════════════════════════════════════════

import streamlit as st
import requests
import json
import time
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
from pathlib import Path

# ── Seitenkonfiguration ──────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .stMetric { background: #1e1e2e; padding: 10px; border-radius: 8px; }
    .stop-ok   { color: #00c853; font-weight: bold; }
    .stop-warn { color: #ffd600; font-weight: bold; }
    .stop-red  { color: #ff1744; font-weight: bold; }
    .card { background: #1e1e2e; padding: 15px; border-radius: 10px; margin: 5px 0; }
    div[data-testid="stMetricValue"] { font-size: 1.4rem; }
</style>
""", unsafe_allow_html=True)

# ── EODHD API Key ─────────────────────────────────────────────────────────────
try:
    API_KEY = st.secrets["EODHD_API_KEY"]
except Exception:
    API_KEY = "69c0f8ad5ac198.37699109"

# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

@st.cache_data(ttl=300)  # 5 Minuten Cache
def eodhd_kurs(ticker):
    """Holt aktuellen Kurs von EODHD."""
    try:
        r = requests.get(
            "https://eodhd.com/api/real-time/" + ticker,
            params={"api_token": API_KEY, "fmt": "json"},
            timeout=10
        )
        data = r.json()
        kurs = float(data.get("close") or data.get("previousClose") or 0)
        return kurs if kurs > 0 else None
    except Exception:
        return None

@st.cache_data(ttl=3600)  # 1 Stunde Cache
def eodhd_history(ticker, tage=90):
    """Holt Kursverlauf von EODHD."""
    try:
        von = (datetime.today() - timedelta(days=tage)).strftime("%Y-%m-%d")
        bis = datetime.today().strftime("%Y-%m-%d")
        r   = requests.get(
            "https://eodhd.com/api/eod/" + ticker,
            params={"api_token": API_KEY, "from": von, "to": bis,
                    "fmt": "json", "period": "d"},
            timeout=15
        )
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")
    except Exception:
        pass
    return pd.DataFrame()

def lade_json(pfad):
    """Lädt JSON aus dem Repo."""
    p = Path(pfad)
    if p.exists():
        return json.loads(p.read_text())
    return None

def puffer_farbe(puffer):
    if puffer <= 0:   return "🔴"
    elif puffer < 5:  return "🟡"
    else:             return "🟢"

def puffer_balken(puffer, breite=10):
    gefuellt = max(0, min(breite, round(puffer / 25 * breite)))
    return "█" * gefuellt + "░" * (breite - gefuellt)

def mini_chart(ticker, kauf_kurs=None, stop_kurs=None, tage=60):
    """Erstellt kleinen Kurschart mit Stop-Linie."""
    df = eodhd_history(ticker, tage)
    if df.empty:
        return None

    fig = go.Figure()

    # Kursverlauf
    fig.add_trace(go.Scatter(
        x=df.index, y=df["close"],
        mode="lines",
        line=dict(color="#00c853", width=2),
        name="Kurs"
    ))

    # Kaufpreis
    if kauf_kurs:
        fig.add_hline(y=kauf_kurs, line_dash="dot",
                      line_color="#ffd600", annotation_text="Kauf")

    # Stop-Level
    if stop_kurs:
        fig.add_hline(y=stop_kurs, line_dash="dash",
                      line_color="#ff1744", annotation_text="Stop")

    fig.update_layout(
        height=150,
        margin=dict(l=0, r=0, t=5, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        xaxis=dict(showgrid=False, showticklabels=False),
        yaxis=dict(showgrid=True, gridcolor="#333", showticklabels=True,
                   tickfont=dict(size=9, color="#aaa")),
    )
    return fig

def portfolio_chart(positionen_list):
    """Tortendiagramm der Portfolio-Verteilung."""
    if not positionen_list:
        return None
    labels  = [p["ticker"] for p in positionen_list]
    values  = [abs(p.get("wert", 1)) for p in positionen_list]
    colors  = ["#00c853" if p.get("pnl", 0) >= 0 else "#ff1744"
               for p in positionen_list]

    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.4,
        marker=dict(colors=colors),
        textinfo="label+percent",
        textfont=dict(size=11),
    ))
    fig.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        font=dict(color="white"),
    )
    return fig


# ── Daten laden ───────────────────────────────────────────────────────────────
KASSANDRA_POS    = lade_json("kassandra_positionen.json") or {}
KASSANDRA_TICKER = lade_json("kassandra_meine_ticker.json") or {}
SP100_POS        = lade_json("sp100_positionen.json") or {}
IVY_POS          = lade_json("ivy_portfolio.json") or {}
ETF_POS          = lade_json("etf_eingabe.json") or {}
SMALLCAP_POS     = lade_json("smallcap_positionen.json") or {}

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

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📈 Trading Dashboard")
    st.caption(f"Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    st.divider()

    seite = st.radio("Strategie wählen:", [
        "🏠 Übersicht",
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
#  SEITE: ÜBERSICHT
# ══════════════════════════════════════════════════════════════════════════════
if seite == "🏠 Übersicht":
    st.title("🏠 Portfolio Übersicht")
    st.caption(f"Alle 5 Strategien — {datetime.now().strftime('%d.%m.%Y %H:%M')}")

    # ── Kennzahlen oben ───────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        n = len(KASSANDRA_POS)
        alerts = sum(1 for p in KASSANDRA_POS.values()
                     if p.get("hoch") and p.get("einstieg") and
                     p["einstieg"] <= p["hoch"] * 0.80)
        st.metric("🌍 Kassandra", f"{n} Pos.",
                  delta="⚠ STOP!" if alerts else "✅ OK",
                  delta_color="inverse" if alerts else "normal")

    with col2:
        n = len(SP100_POS.get("tickers", []))
        st.metric("📈 S&P 100", f"{n} Pos.", delta="RSL-Trail 35%")

    with col3:
        n = len(IVY_POS)
        st.metric("🏛 IVY/RAA", f"{n} Pos.", delta="Stop 15% → SHY")

    with col4:
        n = len(ETF_POS)
        st.metric("📊 ETF Aktien", f"{n} Pos.", delta="Stop 10%")

    with col5:
        n = len(SMALLCAP_POS)
        st.metric("🇪🇺 Small Cap", f"{n} Pos.", delta="Stop 15%")

    st.divider()

    # ── Stop-Status Tabelle ───────────────────────────────────────────────────
    st.subheader("🛡 Stop-Status Alle Strategien")

    rows = []

    # Kassandra
    for ticker, p in KASSANDRA_POS.items():
        kauf = p.get("einstieg", 0)
        hoch = p.get("hoch", kauf)
        if not kauf: continue
        puffer = round(20 - (1 - kauf/hoch)*100, 1)
        rows.append({
            "Strategie": "Kassandra",
            "Ticker":    ticker,
            "Kauf":      kauf,
            "Stop":      round(hoch*0.80, 2),
            "Puffer %":  puffer,
            "Status":    "🔴 STOP" if puffer <= 0 else ("🟡 Vorsicht" if puffer < 5 else "🟢 OK")
        })

    # ETF Aktien (mit Live-Kurs)
    for ticker, pos in ETF_POS.items():
        kauf = pos.get("kauf_kurs", 0)
        if not kauf: continue
        kurs = eodhd_kurs(ticker)
        if kurs:
            puffer = round((kurs/kauf - 1 + 0.10)*100, 1)
            rows.append({
                "Strategie": "ETF Aktien",
                "Ticker":    ticker.replace(".US",""),
                "Kauf":      kauf,
                "Stop":      round(kauf*0.90, 2),
                "Puffer %":  puffer,
                "Status":    "🔴 STOP" if puffer <= 0 else ("🟡 Vorsicht" if puffer < 3 else "🟢 OK")
            })

    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(
            df.style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Keine Positionsdaten vorhanden — data/ Ordner prüfen")


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: KASSANDRA
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "🌍 Kassandra":
    st.title("🌍 Kassandra — Länder ETF")

    if not KASSANDRA_POS:
        st.warning("Keine Positionsdaten — data/kassandra_positionen.json fehlt")
        st.stop()

    # Handelsanweisungen
    ticker_soll = KASSANDRA_TICKER.get("ticker", []) if isinstance(KASSANDRA_TICKER, dict) else []
    pos_tickers = list(KASSANDRA_POS.keys())

    if ticker_soll:
        col1, col2, col3 = st.columns(3)
        verkaufen = [t for t in pos_tickers if t not in ticker_soll]
        kaufen    = [t for t in ticker_soll if t not in pos_tickers]
        halten    = [t for t in pos_tickers if t in ticker_soll]

        with col1:
            st.error("🔴 VERKAUFEN\n\n" + "\n".join(verkaufen) if verkaufen else "")
            if not verkaufen: st.success("Kein Verkauf nötig")
        with col2:
            st.success("🟢 KAUFEN\n\n" + "\n".join(kaufen) if kaufen else "")
            if not kaufen: st.info("Kein Kauf nötig")
        with col3:
            st.info("🔵 HALTEN\n\n" + "\n".join(halten) if halten else "")

    st.divider()
    st.subheader("🛡 Trailing Stop (20% unter Hoch)")

    pos_list = []
    for ticker, p in KASSANDRA_POS.items():
        kauf  = p.get("einstieg", 0)
        hoch  = p.get("hoch", kauf)
        datum = p.get("kaufdatum", "-")
        if not kauf: continue
        stop   = round(hoch * 0.80, 2)
        puffer = round(20 - (1 - kauf/hoch)*100, 1)
        pos_list.append({
            "ticker": ticker, "kauf": kauf, "hoch": hoch,
            "stop": stop, "puffer": puffer, "datum": datum,
            "wert": kauf, "pnl": 0
        })

    for pos in pos_list:
        col1, col2 = st.columns([2, 3])
        with col1:
            icon = puffer_farbe(pos["puffer"])
            st.markdown(f"### {icon} {pos['ticker']}")
            m1, m2, m3 = st.columns(3)
            m1.metric("Kaufkurs", f"{pos['kauf']:.2f}")
            m2.metric("Stop", f"{pos['stop']:.2f}")
            m3.metric("Puffer", f"{pos['puffer']:+.1f}%",
                      delta_color="inverse" if pos["puffer"] < 5 else "normal")
            st.progress(min(1.0, max(0.0, pos["puffer"]/25)),
                        text=f"Abstand zum Stop: {pos['puffer']:+.1f}%")
            st.caption(f"Kauf: {pos['datum']}  |  Hoch: {pos['hoch']:.2f}")
        with col2:
            fig = mini_chart(pos["ticker"], pos["kauf"], pos["stop"])
            if fig:
                st.plotly_chart(fig, use_container_width=True, key=pos["ticker"]+"_k")
        st.divider()


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: S&P 100
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "📈 S&P 100":
    st.title("📈 S&P 100 Momentum")

    tickers = SP100_POS.get("tickers", [])
    if not tickers:
        st.warning("Keine Positionsdaten — data/sp100_positionen.json fehlt")
        st.stop()

    st.info(f"**{len(tickers)} Positionen aktiv** | Stop: RSL-Peak-Trail 35% | Rebalancing: Mittwoch")
    st.divider()

    cols = st.columns(3)
    for i, ticker in enumerate(tickers):
        with cols[i % 3]:
            kurs = eodhd_kurs(ticker + ".US")
            if kurs:
                st.metric(f"🔵 {ticker}", f"${kurs:.2f}")
            else:
                st.metric(f"🔵 {ticker}", "kein Kurs")

    st.divider()
    st.subheader("📊 Kursverlauf (60 Tage)")
    ticker_sel = st.selectbox("Ticker wählen:", tickers)
    if ticker_sel:
        df = eodhd_history(ticker_sel + ".US", 60)
        if not df.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df.index, y=df["close"],
                mode="lines", line=dict(color="#00c853", width=2),
                fill="tozeroy", fillcolor="rgba(0,200,83,0.1)"
            ))
            fig.update_layout(
                height=300, paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#333"),
                yaxis=dict(gridcolor="#333", tickfont=dict(color="#aaa")),
                margin=dict(l=0, r=0, t=10, b=0),
            )
            st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: IVY / RAA
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "🏛 IVY / RAA":
    st.title("🏛 IVY / Hybrid-RAA")
    st.info("Stop: 15% unter Kaufkurs → wechseln zu SHY | Rebalancing: Monatsende")

    if not IVY_POS:
        st.warning("Keine Positionsdaten — data/ivy_portfolio.json fehlt")
        st.stop()

    TS = 0.15
    st.subheader(f"🛡 Trailing Stop ({TS:.0%})")

    pos_data = []
    with st.spinner("Lade Live-Kurse..."):
        for tk, p in IVY_POS.items():
            ep_str    = p.get("entry_price", "")
            kauf_kurs = float(ep_str) if ep_str else None
            ed        = str(p.get("entry_date", "-"))
            eodhd_tk  = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
            kurs      = eodhd_kurs(eodhd_tk)
            time.sleep(0.1)

            if kurs and kauf_kurs:
                stop   = round(kauf_kurs * (1-TS), 2)
                pnl    = round((kurs/kauf_kurs-1)*100, 1)
                puffer = round((kurs/kauf_kurs-1+TS)*100, 1)
                pos_data.append({
                    "Ticker": tk, "Kauf": ed, "Kaufkurs": kauf_kurs,
                    "Jetzt": round(kurs,2), "Stop": stop,
                    "PnL %": pnl, "Puffer %": puffer,
                    "Status": "🔴 STOP" if puffer<=0 else ("🟡 Vorsicht" if puffer<5 else "🟢 OK"),
                    "eodhd_tk": eodhd_tk, "wert": kauf_kurs, "pnl": pnl
                })
            else:
                pos_data.append({
                    "Ticker": tk, "Kauf": ed, "Kaufkurs": kauf_kurs or 0,
                    "Jetzt": None, "Stop": None,
                    "PnL %": None, "Puffer %": None,
                    "Status": "❓ kein Kurs",
                    "eodhd_tk": eodhd_tk, "wert": 0, "pnl": 0
                })

    if pos_data:
        df = pd.DataFrame(pos_data)

        # Zusammenfassung
        col1, col2, col3 = st.columns(3)
        ok      = sum(1 for p in pos_data if p["Puffer %"] and p["Puffer %"] > 5)
        vorsicht = sum(1 for p in pos_data if p["Puffer %"] and 0 < p["Puffer %"] <= 5)
        stops   = sum(1 for p in pos_data if p["Puffer %"] and p["Puffer %"] <= 0)
        col1.metric("🟢 OK",       ok)
        col2.metric("🟡 Vorsicht", vorsicht)
        col3.metric("🔴 Stop",     stops)

        st.divider()

        # Tabelle
        disp_cols = ["Ticker","Kauf","Kaufkurs","Jetzt","PnL %","Puffer %","Status"]
        st.dataframe(
            df[disp_cols].style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ).format({"Kaufkurs": "{:.2f}", "Jetzt": "{:.2f}",
                      "PnL %": "{:+.1f}", "Puffer %": "{:+.1f}"}),
            use_container_width=True,
            hide_index=True,
        )

        # Portfolio Chart
        st.divider()
        st.subheader("📊 Portfolio Verteilung")
        fig = portfolio_chart([p for p in pos_data if p["wert"] > 0])
        if fig:
            st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: ETF AKTIEN
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "📊 ETF Aktien":
    st.title("📊 ETF Aktien Momentum")
    st.info("Stop: 10% Trailing Stop | Rebalancing: Monatsende")

    if not ETF_POS:
        st.warning("Keine Positionsdaten — data/etf_eingabe.json fehlt")
        st.stop()

    TS = 0.10
    st.subheader(f"🛡 Trailing Stop ({TS:.0%})")

    pos_data = []
    with st.spinner("Lade Live-Kurse..."):
        for ticker, pos in ETF_POS.items():
            kauf_kurs = pos.get("kauf_kurs", 0)
            waehr     = pos.get("waehrung", "USD")
            if not kauf_kurs: continue
            kurs = eodhd_kurs(ticker)
            time.sleep(0.1)
            if kurs:
                stop   = round(kauf_kurs * (1-TS), 2)
                pnl    = round((kurs/kauf_kurs-1)*100, 1)
                puffer = round((kurs/kauf_kurs-1+TS)*100, 1)
                pos_data.append({
                    "Ticker":   ticker.replace(".US","").replace(".TO",""),
                    "Währung":  waehr,
                    "Kaufkurs": kauf_kurs,
                    "Jetzt":    round(kurs,2),
                    "Stop":     stop,
                    "PnL %":    pnl,
                    "Puffer %": puffer,
                    "Status":   "🔴 STOP" if puffer<=0 else ("🟡 Vorsicht" if puffer<3 else "🟢 OK"),
                    "ticker_raw": ticker,
                    "wert": kauf_kurs, "pnl": pnl
                })

    if pos_data:
        # Kennzahlen
        col1, col2, col3, col4 = st.columns(4)
        ok      = sum(1 for p in pos_data if p["Puffer %"] > 5)
        vorsicht = sum(1 for p in pos_data if 0 < p["Puffer %"] <= 5)
        stops   = sum(1 for p in pos_data if p["Puffer %"] <= 0)
        avg_pnl = sum(p["PnL %"] for p in pos_data) / len(pos_data)
        col1.metric("🟢 OK", ok)
        col2.metric("🟡 Vorsicht", vorsicht)
        col3.metric("🔴 Stop", stops)
        col4.metric("Ø PnL", f"{avg_pnl:+.1f}%")

        st.divider()

        # Tabelle
        df = pd.DataFrame(pos_data)
        disp = ["Ticker","Währung","Kaufkurs","Jetzt","Stop","PnL %","Puffer %","Status"]
        st.dataframe(
            df[disp].style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ).format({"Kaufkurs": "{:.2f}", "Jetzt": "{:.2f}", "Stop": "{:.2f}",
                      "PnL %": "{:+.1f}", "Puffer %": "{:+.1f}"}),
            use_container_width=True,
            hide_index=True,
        )

        # PnL Chart
        st.divider()
        st.subheader("📊 PnL je Position")
        df_chart = df.sort_values("PnL %")
        fig = go.Figure(go.Bar(
            x=df_chart["PnL %"],
            y=df_chart["Ticker"],
            orientation="h",
            marker_color=["#ff1744" if v < 0 else "#00c853" for v in df_chart["PnL %"]],
            text=[f"{v:+.1f}%" for v in df_chart["PnL %"]],
            textposition="outside",
        ))
        fig.update_layout(
            height=350,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(gridcolor="#333", tickfont=dict(color="#aaa")),
            yaxis=dict(tickfont=dict(color="white")),
            margin=dict(l=0, r=60, t=10, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)

        # Einzel-Charts
        st.divider()
        st.subheader("📈 Kursverlauf")
        ticker_sel = st.selectbox("Ticker:", [p["Ticker"] for p in pos_data])
        sel_pos    = next(p for p in pos_data if p["Ticker"] == ticker_sel)
        df_hist    = eodhd_history(sel_pos["ticker_raw"], 60)

        if not df_hist.empty:
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=df_hist.index, y=df_hist["close"],
                mode="lines", line=dict(color="#00c853", width=2),
                fill="tozeroy", fillcolor="rgba(0,200,83,0.1)", name="Kurs"
            ))
            fig2.add_hline(y=sel_pos["Kaufkurs"], line_dash="dot",
                           line_color="#ffd600", annotation_text="Kauf")
            fig2.add_hline(y=sel_pos["Stop"], line_dash="dash",
                           line_color="#ff1744", annotation_text="Stop")
            fig2.update_layout(
                height=300, paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="#333"),
                yaxis=dict(gridcolor="#333", tickfont=dict(color="#aaa")),
                margin=dict(l=0, r=0, t=10, b=0),
            )
            st.plotly_chart(fig2, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SEITE: SMALL CAP EU
# ══════════════════════════════════════════════════════════════════════════════
elif seite == "🇪🇺 Small Cap EU":
    st.title("🇪🇺 Small Cap Europe")
    st.info("Stop: 15% Trailing Stop | Rebalancing: Freitag")

    if not SMALLCAP_POS:
        st.warning("Noch keine Positionen — add_position() im Script verwenden")
        st.divider()
        st.subheader("So Positionen eintragen:")
        st.code("""
# Im Small Cap Script in Colab:
add_position("ISIN", "2026-05-24", kaufkurs, stueckzahl)

# Dann sync_to_github() ausführen um Dashboard zu aktualisieren
        """)
        st.stop()

    TS = 0.15
    pos_data = []
    with st.spinner("Lade Kurse..."):
        for isin, p in SMALLCAP_POS.items():
            tk   = p.get("ticker", isin[:10])
            kd   = p.get("buy_date", "-")
            kauf = p.get("buy_price", 0)
            if not kauf: continue
            kurs = eodhd_kurs(tk)
            time.sleep(0.1)
            if kurs:
                stop   = round(kauf * (1-TS), 2)
                pnl    = round((kurs/kauf-1)*100, 1)
                puffer = round((kurs/kauf-1+TS)*100, 1)
                pos_data.append({
                    "Ticker": tk, "Kauf": kd, "Kaufkurs": kauf,
                    "Jetzt": round(kurs,2), "Stop": stop,
                    "PnL %": pnl, "Puffer %": puffer,
                    "Status": "🔴 STOP" if puffer<=0 else ("🟡 Vorsicht" if puffer<5 else "🟢 OK"),
                    "wert": kauf, "pnl": pnl
                })

    if pos_data:
        df = pd.DataFrame(pos_data)
        disp = ["Ticker","Kauf","Kaufkurs","Jetzt","Stop","PnL %","Puffer %","Status"]
        st.dataframe(
            df[disp].style.map(
                lambda v: "color: #ff1744" if "STOP" in str(v)
                     else ("color: #ffd600" if "Vorsicht" in str(v)
                     else "color: #00c853" if "OK" in str(v) else ""),
                subset=["Status"]
            ),
            use_container_width=True,
            hide_index=True,
        )
