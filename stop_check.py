# ═══════════════════════════════════════════════════════════════
#  TRADING STOP-CHECK — GitHub Actions
#  Läuft täglich 08:00 + 14:30 Uhr
#  Sendet Email wenn Stop ausgelöst oder Puffer < 5%
# ═══════════════════════════════════════════════════════════════

import os, json, math, requests, smtplib
from pathlib import Path
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from name_lookup import lookup_name, ticker_label as _ticker_label

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

_name_cache = {}

def position_name(ticker, pos=None):
    return lookup_name(ticker, pos, API_KEY, _name_cache)

def ticker_label(ticker, pos=None):
    return _ticker_label(ticker, pos, API_KEY, _name_cache)

def lade_json(pfad):
    p = Path(pfad)
    return json.loads(p.read_text()) if p.exists() else {}

def safe_float(x):
    if x is None:
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v) or v <= 0:
            return None
        return v
    except (TypeError, ValueError):
        return None

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
    "CIEN": "CIEN.US",          "FIX": "FIX.US",
    "WDC": "WDC.F",             "TECK-B.TO": "TGB.F",
    "STMPA.PA": "STMPA.PA",     "ESLT.US": "E4L.F",
}

IVY_TS_EXCLUDE = {"LYTR.XETRA", "VTI", "VEU", "BND", "VNQ"}
IVY_WARMUP_DAYS = 10


def ivy_ffm_ticker(pos):
    ffm = (pos.get("ffm_ticker") or "").strip().upper()
    if not ffm:
        return None
    return ffm if ffm.endswith(".F") else ffm + ".F"


def _ivy_kurs_plausibel(kurs, peak):
    if not peak or not kurs:
        return True
    ratio = kurs / peak
    return 0.55 <= ratio <= 1.15


def ivy_eur_kurs(tk, pos, peak_hint=None):
    ffm = ivy_ffm_ticker(pos)
    if ffm:
        k = safe_float(eodhd_kurs(ffm))
        if k and _ivy_kurs_plausibel(k, peak_hint):
            return k
    eodhd_tk = TICKER_MAP_IVY.get(tk, tk + ".US" if "." not in tk else tk)
    k = safe_float(eodhd_kurs(eodhd_tk))
    if k and _ivy_kurs_plausibel(k, peak_hint):
        return k
    return None


def ivy_peak(pos):
    return safe_float(pos.get("peak_price")) or safe_float(pos.get("entry_price"))


def ivy_handelstage_seit_kauf(entry_date_str):
    if not entry_date_str:
        return None
    try:
        start = date.fromisoformat(str(entry_date_str).strip()[:10])
    except ValueError:
        return None
    heute = date.today()
    if heute < start:
        return 0
    n = 0
    d = start
    while d <= heute:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def ivy_in_warmup(pos):
    ht = ivy_handelstage_seit_kauf(pos.get("entry_date"))
    return ht is not None and ht < IVY_WARMUP_DAYS


def portfolio_ohne_meta(data):
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if not str(k).startswith("_") and isinstance(v, dict)
    }

# ── Positionen laden ─────────────────────────────────────────────

KASSANDRA = portfolio_ohne_meta(lade_json("kassandra_positionen.json"))
SP100     = lade_json("sp100_positionen.json")
IVY       = portfolio_ohne_meta(lade_json("ivy_portfolio.json"))
SMALLCAP  = portfolio_ohne_meta(lade_json("smallcap_positionen.json"))

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
    eintrag = {"strategie": "🌍 Kassandra", "ticker": ticker_label(ticker, p),
                "kurs": kurs, "stop": stop, "puffer": puffer}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 5:
        warnungen.append(eintrag)

# S&P 100 (RSL-Peak-Trail 35% — RSL-Werte aus rsl_data Export)
_sp100_allowed = None
if "meine_aktien" in SP100:
    _sp100_allowed = set(SP100.get("meine_aktien") or []) | set(SP100.get("tickers") or [])
for ticker, info in SP100.get("rsl_data", {}).items():
    if _sp100_allowed is not None and ticker not in _sp100_allowed:
        continue
    trail = info.get("trail")
    rsl_now = info.get("rsl", 0)
    puffer = info.get("puffer")
    if trail is None or puffer is None:
        continue
    abst = info.get("abst_hoch_pct")
    abst_s = f"  ({abst:+.1f}% Kurs-Hoch)" if abst is not None else ""
    name = position_name(ticker, info)
    ticker_s = ticker_label(ticker, info)
    eintrag = {"strategie": "📈 S&P 100", "ticker": ticker_s,
               "kurs": f"RSL {rsl_now:.3f}{abst_s}",
               "stop": f"RSL {trail:.3f}", "puffer": puffer,
               "pnl_s": ""}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 10:
        warnungen.append(eintrag)

# IVY (15% Trailing unter Peak in EUR — wie Ivy_2.1.ipynb)
for tk, p in IVY.items():
    if tk in IVY_TS_EXCLUDE or not p.get("entry_price"):
        continue
    peak = ivy_peak(p)
    if not peak:
        continue
    kurs = ivy_eur_kurs(tk, p, peak)
    if not kurs:
        continue
    stop   = round(peak * 0.85, 2)
    puffer = round((kurs / stop - 1) * 100, 1)
    warmup = ivy_in_warmup(p)
    eintrag = {"strategie": "🏛 IVY/RAA", "ticker": ticker_label(tk, p),
                "kurs": kurs, "stop": stop, "puffer": puffer,
                "warmup": warmup}
    alle.append(eintrag)
    if warmup:
        continue
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 5:
        warnungen.append(eintrag)

# ETF Aktien (10% Trailing Stop — stop_level aus portfolio_state, nativ vs nativ)
TS_ETF = _etf_raw.get("trailing_pct", 0.10) if isinstance(_etf_raw, dict) else 0.10
for ticker, pos in ETF.items():
    state_pos = ETF_STATE_POS
    if state_pos and ticker not in state_pos:
        continue
    kauf_eur = pos.get("kauf_kurs", 0)   # EUR (Nutzereingabe)
    if kauf_eur and kauf_eur < 0.01: kauf_eur = 0   # Pence-Bug-Schutz
    if not kauf_eur: continue
    kurs = safe_float(eodhd_kurs(ticker))  # nativ (USD/GBP/CAD)
    if not kurs: continue

    state      = ETF_STATE_POS.get(ticker, {})
    hoch_nativ = safe_float(state.get("hoch_kurs")) or safe_float(pos.get("hoch_kurs")) or kurs
    stop_nativ = safe_float(state.get("stop_level")) or safe_float(pos.get("stop_nativ"))
    if stop_nativ is None and hoch_nativ:
        stop_nativ = round(hoch_nativ * (1 - TS_ETF), 2)
    puffer     = round((kurs / stop_nativ - 1) * 100, 1) if stop_nativ else 0

    # P&L: EUR-basiert aus etf_eingabe.json (korrekt berechnet vom Notebook)
    pnl_pct = pos.get("pnl_pct")
    pnl_s   = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—"

    eintrag = {"strategie": "📊 ETF Aktien",
               "ticker":   ticker_label(ticker, pos),
               "kurs":     kurs, "stop": round(stop_nativ, 2), "puffer": puffer,
               "pnl_s":   pnl_s}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 3:
        warnungen.append(eintrag)

# Small Cap EU: kein Trailing Stop im Live-Betrieb
# (Exit: EMA100 −5%, Kassandra ROT, wöchentliches Rebalancing — wie Small Cap Europe.ipynb)

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
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{pos['kurs'] if isinstance(pos['kurs'], str) else f"{pos['kurs']:.2f}"}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{pos['stop'] if isinstance(pos['stop'], str) else f"{pos['stop']:.2f}"}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right;color:{fb};font-weight:bold">{puf_str}{' (RSL)' if pos['strategie'] == '📈 S&P 100' else ''}</td>
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
