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
ETF       = lade_json("etf_eingabe.json")
SMALLCAP  = lade_json("smallcap_positionen.json")

# ── Stop-Checks ──────────────────────────────────────────────────

alerts  = []   # Stops ausgelöst
warnungen = [] # Puffer < 5%
alle    = []   # Alle Positionen für Übersicht

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

# S&P 100 (RSL-Trail — nur Kurs anzeigen, kein Stop-Level)
sp100_detail = SP100.get("positionen", {})
for ticker in SP100.get("tickers", []):
    kurs = eodhd_kurs(ticker + ".US")
    pos  = sp100_detail.get(ticker, {})
    kauf = pos.get("kauf_kurs", 0)
    if kauf and kurs:
        pnl = round((kurs/kauf - 1) * 100, 1)
        alle.append({"strategie": "📈 S&P 100", "ticker": ticker,
                     "kurs": kurs, "stop": "RSL-Trail", "puffer": None,
                     "pnl": pnl})

# IVY (15% Stop)
for tk, p in IVY.items():
    ep_str = p.get("entry_price", "")
    kauf   = float(ep_str) if ep_str else None
    if not kauf: continue
    eodhd_tk = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
    kurs = eodhd_kurs(eodhd_tk)
    if not kurs: continue
    stop   = round(kauf * 0.85, 2)
    puffer = round((kurs / stop - 1) * 100, 1)
    eintrag = {"strategie": "🏛 IVY/RAA", "ticker": tk,
                "kurs": kurs, "stop": stop, "puffer": puffer}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 5:
        warnungen.append(eintrag)

# ETF Aktien (10% Stop)
for ticker, pos in ETF.items():
    kauf = pos.get("kauf_kurs", 0)
    if not kauf: continue
    kurs = eodhd_kurs(ticker)
    if not kurs: continue
    stop   = round(kauf * 0.90, 2)
    puffer = round((kurs / stop - 1) * 100, 1)
    eintrag = {"strategie": "📊 ETF Aktien",
                "ticker": ticker.replace(".US","").replace(".TO",""),
                "kurs": kurs, "stop": stop, "puffer": puffer}
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
    "ST": ("SEK", 1.0),    # Schweden
    "CO": ("DKK", 1.0),    # Dänemark
    "SW": ("CHF", 1.0),    # Schweiz
    "LSE": ("GBp", 0.01),  # London (Pence → Pfund)
    "US": ("USD", 1.0),
    "TO": ("CAD", 1.0),
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

    # Aktueller Kurs in Lokalwährung
    kurs_lokal = eodhd_kurs(tk)
    if not kurs_lokal: continue

    # Hoch seit Kauf in Lokalwährung
    hoch_lokal = eodhd_hoch(tk, kauf_datum)

    # Wechselkurs Lokalwährung → EUR
    exchange = exchange_von_ticker(tk)
    ccy, unit = EXCHANGE_CCY.get(exchange, ("EUR", 1.0))
    fx = eodhd_fx(ccy) if ccy != "EUR" else 1.0

    # Alles in EUR umrechnen
    kurs = round(kurs_lokal * unit * fx, 4)
    if hoch_lokal:
        hoch = round(hoch_lokal * unit * fx, 4)
    else:
        hoch = kauf_eur   # Fallback auf Kaufkurs

    # Sicherstellung: Hoch >= Kaufkurs
    if hoch < kauf_eur:
        hoch = kauf_eur

    stop   = round(hoch * 0.85, 2)
    puffer = round((kurs / stop - 1) * 100, 1)

    # NEU (kein Stop-Alert mehr für Small Cap):
    eintrag = {"strategie": "🇪🇺 Small Cap", "ticker": tk,
               "kurs": kurs, "stop": "EMA100", "puffer": None,
               "hoch": hoch}
    alle.append(eintrag)
    # Kein Stop-Alert – Trailing Stop deaktiviert

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

# Email-Betreff
if alerts:
    betreff = f"🔴 STOP AUSGELÖST — {len(alerts)} Position(en) — {now}"
elif warnungen:
    betreff = f"🟡 Vorsicht — {len(warnungen)} Position(en) nahe Stop — {now}"
else:
    betreff = f"✅ Alle Stops OK — Trading Dashboard — {now}"

# HTML Email
def zeile(pos):
    p = pos.get("puffer")
    fb = farbe(p)
    puf_str = f"{p:+.1f}%" if p is not None else "RSL-Trail"
    pnl_str = f"{pos.get('pnl', 0):+.1f}%" if pos.get('pnl') is not None else ""
    return f"""
    <tr>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a">{pos['strategie']}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;font-weight:bold">{pos['ticker']}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{pos['kurs']:.2f}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{pos['stop'] if isinstance(pos['stop'], str) else f"{pos['stop']:.2f}"}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right;color:{fb};font-weight:bold">{puf_str}</td>
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
            </tr>
            {"".join(zeile(a) for a in alerts)}
        </table>
    </div>"""
        <table style="width:100%;border-collapse:collapse">
            <tr style="color:#aaa;font-size:12px">
                <th style="text-align:left;padding:6px">Strategie</th>
                <th style="text-align:left;padding:6px">Ticker</th>
                <th style="text-align:right;padding:6px">Kurs</th>
                <th style="text-align:right;padding:6px">Stop</th>
                <th style="text-align:right;padding:6px">Puffer</th>
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
    # googlemail.com und gmail.com sind identisch bei Google
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
