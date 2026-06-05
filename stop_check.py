# ═══════════════════════════════════════════════════════════════
#  TRADING STOP-CHECK — GitHub Actions
#  Läuft täglich 08:00 + 14:30 Uhr
#  Sendet Email wenn Stop ausgelöst oder Puffer < 5%
# ═══════════════════════════════════════════════════════════════

import os, json, requests, smtplib
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

API_KEY  = os.environ["EODHD_API_KEY"]
EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO   = os.environ["EMAIL_TO"]
EMAIL_PWD  = os.environ["EMAIL_PASSWORD"]

# ── Hilfsfunktionen ──────────────────────────────────────────────

def eodhd_kurs(ticker):
    try:
        r = requests.get(
            f"https://eodhd.com/api/real-time/{ticker}",
            params={"api_token": API_KEY, "fmt": "json"}, timeout=10)
        d = r.json()
        k = float(d.get("close") or d.get("previousClose") or 0)
        return k if k > 0 else None
    except:
        return None

def lade_json(pfad):
    p = Path(pfad)
    return json.loads(p.read_text()) if p.exists() else {}

def ticker_fix(ticker):
    """Konvertiert .L → .LSE für EODHD."""
    if ticker.endswith(".L"):
        return ticker[:-2] + ".LSE"
    if "." not in ticker:
        return ticker + ".US"
    return ticker

TICKER_MAP_IVY = {
    "LYTR.XETRA": "LYTR.XETRA", "IFX.DE": "IFX.XETRA",
    "ASM.AS": "ASM.AS",         "RWE.DE": "RWE.XETRA",
    "ABBN.SW": "ABBN.SW",       "TSEM.US": "TSEM.US",
    "FN.US": "FN.US",           "CVE.TO": "CVE.TO",
    "FLEX.US": "FLEX.US",       "LRCX": "LRCX.US",
    "CIEN": "CIEN.US",
}

# ── Positionen laden ─────────────────────────────────────────────

KASSANDRA = lade_json("kassandra_positionen.json")
SP100     = lade_json("sp100_positionen.json")
IVY       = lade_json("ivy_portfolio.json")
SMALLCAP  = lade_json("smallcap_positionen.json")

# etf_eingabe.json hat Struktur {"positionen": [...], "kapital": ..., "trailing_pct": ...}
# → in ticker-keyetes Dict umwandeln
_etf_raw      = lade_json("etf_eingabe.json")
_etf_pos_list = _etf_raw.get("positionen", []) if isinstance(_etf_raw, dict) else []
ETF           = {p["ticker"]: p for p in _etf_pos_list if isinstance(p, dict) and p.get("ticker")}

# ETF State (portfolio_state.json) für stop_level (native Währung)
_etf_state_raw = lade_json("portfolio_state.json")
ETF_STATE_POS  = _etf_state_raw.get("positionen", {}) if isinstance(_etf_state_raw, dict) else {}

# ── Stop-Checks ──────────────────────────────────────────────────

alerts    = []   # Stops ausgelöst
warnungen = []   # Puffer < 5%
alle      = []   # Alle Positionen für Übersicht

now = datetime.now().strftime("%d.%m.%Y %H:%M")

# Kassandra (20% Trailing Stop)
for ticker, p in KASSANDRA.items():
    kauf = p.get("einstieg", 0)
    hoch = p.get("hoch", kauf)
    if not kauf: continue
    eodhd_tk = ticker_fix(ticker)
    kurs = eodhd_kurs(eodhd_tk)
    if not kurs: continue
    stop   = round(hoch * 0.80, 2)
    puffer = round((kurs / stop - 1) * 100, 1)
    eintrag = {"strategie": "🌍 Kassandra", "ticker": ticker,
                "kurs": kurs, "stop": stop, "puffer": puffer}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 5:
        warnungen.append(eintrag)

# S&P 100 (RSL-Peak-Trail 35% — aus rsl_data Export)
for ticker, info in SP100.get("rsl_data", {}).items():
    trail = info.get("trail")
    rsl_now = info.get("rsl", 0)
    puffer = info.get("puffer")
    if trail is None or puffer is None:
        continue
    eintrag = {"strategie": "📈 S&P 100", "ticker": ticker,
               "kurs": rsl_now, "stop": trail, "puffer": puffer}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 10:
        warnungen.append(eintrag)

# IVY (15% Trailing unter Peak — wie Ivy_2.1.ipynb)
for tk, p in IVY.items():
    if tk == "FIX":
        continue
    ep_str = p.get("entry_price", "")
    kauf   = float(ep_str) if ep_str else None
    if not kauf: continue
    peak_str = p.get("peak_price", "")
    peak = float(peak_str) if peak_str else kauf
    eodhd_tk = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
    kurs = eodhd_kurs(eodhd_tk)
    if not kurs: continue
    stop   = round(peak * 0.85, 2)
    puffer = round((kurs / stop - 1) * 100, 1)
    eintrag = {"strategie": "🏛 IVY/RAA", "ticker": tk,
                "kurs": kurs, "stop": stop, "puffer": puffer}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 5:
        warnungen.append(eintrag)

# ETF Aktien (10% Trailing Stop — stop_level aus portfolio_state, nativ vs nativ)
TS_ETF = _etf_raw.get("trailing_pct", 0.10) if isinstance(_etf_raw, dict) else 0.10
for ticker, pos in ETF.items():
    kauf_eur = pos.get("kauf_kurs", 0)   # EUR (Nutzereingabe)
    if kauf_eur and kauf_eur < 0.01: kauf_eur = 0   # Pence-Bug-Schutz
    if not kauf_eur: continue
    kurs = eodhd_kurs(ticker)            # nativ (USD/GBP/CAD)
    if not kurs: continue

    state      = ETF_STATE_POS.get(ticker, {})
    hoch_nativ = state.get("hoch_kurs") or pos.get("hoch_kurs") or kurs
    stop_nativ = state.get("stop_level") or pos.get("stop_nativ") or round(hoch_nativ * (1 - TS_ETF), 2)
    puffer     = round((kurs / stop_nativ - 1) * 100, 1) if stop_nativ else 0

    # P&L: EUR-basiert aus etf_eingabe.json (korrekt berechnet vom Notebook)
    pnl_pct = pos.get("pnl_pct")
    pnl_s   = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—"

    eintrag = {"strategie": "📊 ETF Aktien",
               "ticker":   ticker.replace(".US","").replace(".TO",""),
               "kurs":     kurs, "stop": round(stop_nativ, 2), "puffer": puffer,
               "pnl_s":   pnl_s}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 3:
        warnungen.append(eintrag)

# Small Cap (15% Trailing Stop — Hoch via EODHD laden)
def eodhd_hoch(ticker, kauf_datum):
    """Holt das Hoch seit Kaufdatum von EODHD (in Lokalwährung)."""
    try:
        r = requests.get(
            f"https://eodhd.com/api/eod/{ticker}",
            params={"api_token": API_KEY, "from": kauf_datum,
                    "to": datetime.now().strftime("%Y-%m-%d"),
                    "fmt": "json", "period": "d"}, timeout=15)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            return max(float(d["high"]) for d in data)
    except:
        pass
    return None

def eodhd_fx(von, nach="EUR"):
    """Holt aktuellen Wechselkurs (z.B. SEK→EUR = 1/EURSEK)."""
    if von == nach or von == "EUR":
        return 1.0
    try:
        ticker = f"EUR{von}.FOREX"
        r = requests.get(
            f"https://eodhd.com/api/real-time/{ticker}",
            params={"api_token": API_KEY, "fmt": "json"}, timeout=10)
        kurs = float(r.json().get("close") or r.json().get("previousClose") or 0)
        if kurs > 0:
            return round(1.0 / kurs, 8)   # 1 FCY = ? EUR
    except:
        pass
    return 1.0

# Währungs-Mapping für EODHD-Exchanges
EXCHANGE_CCY = {
    "ST": ("SEK", 1.0),
    "CO": ("DKK", 1.0),
    "SW": ("CHF", 1.0),
    "LSE": ("GBp", 0.01),
    "US": ("USD", 1.0),
    "TO": ("CAD", 1.0),
    "XETRA": ("EUR", 1.0),
    "PA": ("EUR", 1.0),
    "AS": ("EUR", 1.0),
    "MC": ("EUR", 1.0),
    "DE": ("EUR", 1.0),
    "F": ("EUR", 1.0),
}

def exchange_von_ticker(ticker):
    """Gibt Exchange-Suffix zurück (z.B. 'ST' für SIVE.ST)."""
    if "." in ticker:
        return ticker.split(".")[-1].upper()
    return "US"

for isin, p in SMALLCAP.items():
    tk         = p.get("ticker", isin[:10])
    kauf_eur   = p.get("buy_price", 0)      # immer EUR im JSON
    kauf_datum = p.get("buy_date", "2026-01-01")
    if not kauf_eur: continue

    kurs_lokal = eodhd_kurs(tk)
    if not kurs_lokal: continue

    hoch_lokal = eodhd_hoch(tk, kauf_datum)

    exchange = exchange_von_ticker(tk)
    ccy, unit = EXCHANGE_CCY.get(exchange, ("EUR", 1.0))
    fx = eodhd_fx(ccy) if ccy != "EUR" else 1.0

    kurs = round(kurs_lokal * unit * fx, 4)
    hw_json = p.get("high_water", kauf_eur)
    if hoch_lokal:
        hoch = round(max(hoch_lokal * unit * fx, hw_json, kauf_eur), 4)
    else:
        hoch = max(hw_json, kauf_eur, kurs)

    stop   = round(hoch * 0.85, 2)
    puffer = round((kurs / stop - 1) * 100, 1)

    eintrag = {"strategie": "🇪🇺 Small Cap", "ticker": tk,
               "kurs": kurs, "stop": stop, "puffer": puffer}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 5:
        warnungen.append(eintrag)

# ── Email erstellen ───────────────────────────────────────────────

def puffer_balken(puffer, breite=10):
    if puffer is None: return "—"
    p = max(0, min(breite, round(puffer / 25 * breite)))
    return "█" * p + "░" * (breite - p) + f"  {puffer:+.1f}%"

def farbe(puffer):
    if puffer is None: return "#888888"
    if puffer <= 0:    return "#ff1744"
    elif puffer < 5:   return "#ffd600"
    return "#00c853"

if alerts:
    betreff = f"🔴 STOP AUSGELÖST — {len(alerts)} Position(en) — {now}"
elif warnungen:
    betreff = f"🟡 Vorsicht — {len(warnungen)} Position(en) nahe Stop — {now}"
else:
    betreff = f"✅ Alle Stops OK — Trading Dashboard — {now}"

def zeile(pos):
    p = pos.get("puffer")
    fb = farbe(p)
    puf_str = f"{p:+.1f}%" if p is not None else "RSL-Trail"
    pnl_s   = pos.get("pnl_s", "")
    return f"""
    <tr>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a">{pos['strategie']}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;font-weight:bold">{pos['ticker']}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{pos['kurs']:.2f}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{pos['stop'] if isinstance(pos['stop'], str) else f"{pos['stop']:.2f}"}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right;color:{fb};font-weight:bold">{puf_str}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right;color:#aaa">{pnl_s}</td>
    </tr>"""

alert_html = ""
if alerts:
    alert_html = f"""
    <div style="background:#3a0000;border:2px solid #ff1744;border-radius:8px;padding:15px;margin:15px 0">
        <h2 style="color:#ff1744;margin:0 0 10px 0">🔴 STOP AUSGELÖST — Sofort handeln!</h2>
        <table style="width:100%;border-collapse:collapse">
            <tr style="color:#aaa;font-size:12px">
                <th style="text-align:left;padding:6px">Strategie</th>
                <th style="text-align:left;padding:6px">Ticker</th>
                <th style="text-align:right;padding:6px">Kurs</th>
                <th style="text-align:right;padding:6px">Stop</th>
                <th style="text-align:right;padding:6px">Puffer</th>
                <th style="text-align:right;padding:6px">P&L €</th>
            </tr>
            {"".join(zeile(a) for a in alerts)}
        </table>
    </div>"""

warn_html = ""
if warnungen:
    warn_html = f"""
    <div style="background:#3a3000;border:2px solid #ffd600;border-radius:8px;padding:15px;margin:15px 0">
        <h2 style="color:#ffd600;margin:0 0 10px 0">🟡 Vorsicht — Puffer unter 5%</h2>
        <table style="width:100%;border-collapse:collapse">
            <tr style="color:#aaa;font-size:12px">
                <th style="text-align:left;padding:6px">Strategie</th>
                <th style="text-align:left;padding:6px">Ticker</th>
                <th style="text-align:right;padding:6px">Kurs</th>
                <th style="text-align:right;padding:6px">Stop</th>
                <th style="text-align:right;padding:6px">Puffer</th>
                <th style="text-align:right;padding:6px">P&L €</th>
            </tr>
            {"".join(zeile(w) for w in warnungen)}
        </table>
    </div>"""

uebersicht_html = f"""
    <div style="margin:15px 0">
        <h2 style="color:#aaa;margin:0 0 10px 0">📋 Alle Positionen</h2>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="color:#aaa;font-size:12px;background:#1a1a2e">
                <th style="text-align:left;padding:8px">Strategie</th>
                <th style="text-align:left;padding:8px">Ticker</th>
                <th style="text-align:right;padding:8px">Kurs</th>
                <th style="text-align:right;padding:8px">Stop</th>
                <th style="text-align:right;padding:8px">Puffer</th>
                <th style="text-align:right;padding:8px">P&L €</th>
            </tr>
            {"".join(zeile(a) for a in alle)}
        </table>
    </div>"""

html = f"""
<html><body style="background:#0f0f1a;color:white;font-family:Arial,sans-serif;padding:20px">
    <div style="max-width:700px;margin:0 auto">
        <h1 style="color:#00c853;border-bottom:2px solid #00c853;padding-bottom:10px">
            📈 Trading Dashboard — Stop-Check
        </h1>
        <p style="color:#aaa">Stand: {now} | Automatischer Check via GitHub Actions</p>
        {alert_html}
        {warn_html}
        {uebersicht_html}
        <div style="margin-top:20px;padding:10px;background:#1a1a2e;border-radius:8px">
            <p style="color:#aaa;font-size:12px;margin:0">
                🔗 Dashboard: <a href="https://lazar-trading-dashboard.streamlit.app" style="color:#00c853">
                lazar-trading-dashboard.streamlit.app</a><br>
                ⏰ Nächster Check: täglich 08:00 + 14:30 Uhr
            </p>
        </div>
    </div>
</body></html>"""

# ── Email senden ─────────────────────────────────────────────────

msg = MIMEMultipart("alternative")
msg["Subject"] = betreff
msg["From"]    = EMAIL_FROM
msg["To"]      = EMAIL_TO
msg.attach(MIMEText(html, "html"))

try:
    smtp_server = "smtp.gmail.com"
    with smtplib.SMTP_SSL(smtp_server, 465) as server:
        server.ehlo()
        server.login(EMAIL_FROM.replace("googlemail.com", "gmail.com"), EMAIL_PWD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"✅ Email gesendet: {betreff}")
    if alerts:
        print(f"🔴 {len(alerts)} Stop(s) ausgelöst!")
    elif warnungen:
        print(f"🟡 {len(warnungen)} Warnung(en)")
    else:
        print("✅ Alle Stops OK")
except Exception as e:
    print(f"❌ Email-Fehler: {e}")
    raise
