# ═══════════════════════════════════════════════════════════════
#  TRADING STOP-CHECK — GitHub Actions
#  Läuft täglich 08:00 + 14:30 Uhr
#  v3.8 — Dauerläufer entfernt (archiviert)
# ═══════════════════════════════════════════════════════════════

import os, json, math, requests, smtplib
from pathlib import Path
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from name_lookup import resolve_smallcap_name
from sp100_rsl import sp100_rsl_live

_NAME_CACHE = {}

API_KEY  = os.environ["EODHD_API_KEY"]
EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO   = os.environ["EMAIL_TO"]
EMAIL_PWD  = os.environ["EMAIL_PASSWORD"]

# ── Hilfsfunktionen ──────────────────────────────────────────────

def eodhd_realtime(ticker):
    try:
        r = requests.get(
            f"https://eodhd.com/api/real-time/{ticker}",
            params={"api_token": API_KEY, "fmt": "json"}, timeout=10)
        d = r.json()
        close = float(d.get("close") or d.get("previousClose") or 0)
        prev = float(d.get("previousClose") or 0)
        if close <= 0:
            return None
        return {"close": close, "previousClose": prev if prev > 0 else None}
    except Exception:
        return None

def eodhd_kurs(ticker):
    rt = eodhd_realtime(ticker)
    return rt["close"] if rt else None

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
KASSANDRA_CRASH_EXIT_DEFAULT = 0.08

IVY_EXCHANGE_CCY = {
    "US": "USD", "": "USD",
    "DE": "EUR", "PA": "EUR", "AS": "EUR", "MI": "EUR", "MC": "EUR",
    "LS": "EUR", "BR": "EUR", "HE": "EUR", "VI": "EUR", "XETRA": "EUR",
    "F": "EUR",
    "L": "GBP", "SW": "CHF", "TO": "CAD", "V": "CAD",
}
IVY_FX_PAIRS = {
    "GBP": ("GBPUSD.FOREX", False),
    "CHF": ("USDCHF.FOREX", True),
    "CAD": ("USDCAD.FOREX", True),
}


def ivy_ticker_currency(ticker):
    sfx = ticker.rsplit(".", 1)[1] if "." in ticker else "US"
    return IVY_EXCHANGE_CCY.get(sfx, "USD")


def eurusd_rate():
    rt = eodhd_realtime("EURUSD.FOREX")
    return rt["close"] if rt else None


def _fx_usd_per_local(ccy):
    if ccy == "USD":
        return 1.0
    spec = IVY_FX_PAIRS.get(ccy)
    if not spec:
        return None
    pair, invert = spec
    rt = eodhd_realtime(pair)
    if not rt or not rt.get("close"):
        return None
    v = rt["close"]
    return (1.0 / v) if invert else v


def ivy_to_eur(price, ticker):
    ccy = ivy_ticker_currency(ticker)
    if ccy == "EUR":
        return price
    eur_usd = eurusd_rate()
    if not eur_usd:
        return None
    if ccy == "USD":
        return price / eur_usd
    local_usd = _fx_usd_per_local(ccy)
    if not local_usd:
        return None
    return price * local_usd / eur_usd


def ivy_native_ticker(tk):
    if tk in TICKER_MAP_IVY:
        return TICKER_MAP_IVY[tk]
    if "." in tk:
        return tk
    return tk + ".US"


def fmt_eur(val):
    if val is None:
        return "—"
    return f"{float(val):.2f} €"


def ivy_handelstage_seit_kauf(entry_date_str):
    if not entry_date_str:
        return None
    try:
        start = date.fromisoformat(str(entry_date_str).strip()[:10])
    except ValueError:
        return None
    heute = date.today()
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


def kassandra_crash_exit_pct(kass_meta=None):
    meta = kass_meta if isinstance(kass_meta, dict) else {}
    raw = meta.get("crash_exit_day")
    if raw is None:
        return KASSANDRA_CRASH_EXIT_DEFAULT
    try:
        v = float(raw)
        return v if v > 0 else 0.0
    except (TypeError, ValueError):
        return KASSANDRA_CRASH_EXIT_DEFAULT


def kass_tages_return_pct(ticker):
    rt = eodhd_realtime(ticker_fix(ticker))
    if not rt or not rt.get("previousClose"):
        return None
    return round((rt["close"] / rt["previousClose"] - 1) * 100, 2)


def _ivy_kurs_plausibel(kurs, peak):
    if not peak or not kurs:
        return True
    return 0.55 <= (kurs / peak) <= 1.15


def ivy_ffm_ticker(pos):
    ffm = (pos.get("ffm_ticker") or "").strip().upper()
    if not ffm:
        return None
    return ffm if ffm.endswith(".F") else ffm + ".F"


def ivy_eur_kurs(tk, pos, peak_hint=None):
    """EUR-Kurs wie app.py — FFM (.F) oder FX-Umrechnung, kein USD-Fallback."""
    ffm = ivy_ffm_ticker(pos)
    if ffm:
        k = safe_float(eodhd_kurs(ffm))
        if k and _ivy_kurs_plausibel(k, peak_hint):
            return k, "FFM"
        return None, None
    native = ivy_native_ticker(tk)
    k = safe_float(eodhd_kurs(native))
    if not k:
        return None, None
    k_eur = ivy_to_eur(k, native)
    if k_eur and _ivy_kurs_plausibel(k_eur, peak_hint):
        return k_eur, "FX"
    return None, None


def ivy_peak(pos):
    return safe_float(pos.get("peak_price")) or safe_float(pos.get("entry_price"))


JSON_TOP_META_KEYS = frozenset({
    "handelsanweisungen", "orders", "verkaufen", "kaufen",
    "kassandra_ampel", "score", "score_smooth", "score_raw", "score_heute",
    "score_details", "naechster_check", "rebal_freq", "crash_exit_day",
    "handel_am", "ampel", "datum", "datum_heute", "sync_ts", "stand",
    "last_update", "tickers", "meine_aktien", "rsl_data", "kassandra",
    "_kassandra_meta", "rebalancing", "kapital", "positionen", "trailing_pct",
    "empfehlung", "metadata", "meta",
})

POSITION_FIELD_MARKERS = (
    "einstieg", "entry_price", "buy_price", "kauf_kurs", "kaufdatum",
    "buy_date", "shares", "hoch", "high_water", "peak_price",
)


def portfolio_ohne_meta(data):
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if not str(k).startswith("_")
        and k not in JSON_TOP_META_KEYS
        and isinstance(v, dict)
        and any(f in v for f in POSITION_FIELD_MARKERS)
    }

# ── Positionen laden ─────────────────────────────────────────────

KASSANDRA_RAW = lade_json("kassandra_positionen.json")
KASSANDRA = portfolio_ohne_meta(KASSANDRA_RAW)
KASS_CRASH_PCT = kassandra_crash_exit_pct(KASSANDRA_RAW)
SP100     = lade_json("sp100_positionen.json")
IVY       = portfolio_ohne_meta(lade_json("ivy_portfolio.json"))
SMALLCAP_RAW = lade_json("smallcap_positionen.json")
SMALLCAP  = portfolio_ohne_meta(SMALLCAP_RAW)

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

# Kassandra (20% Trailing + Crash Exit)
for ticker, p in KASSANDRA.items():
    kauf = p.get("einstieg", 0)
    hoch = p.get("hoch", kauf)
    if not kauf: continue
    eodhd_tk = ticker_fix(ticker)
    kurs = eodhd_kurs(eodhd_tk)
    if not kurs: continue
    stop   = round(hoch * 0.80, 2)
    puffer = round((kurs / stop - 1) * 100, 1)
    tages_ret = kass_tages_return_pct(ticker)
    crash = KASS_CRASH_PCT and tages_ret is not None and tages_ret <= -KASS_CRASH_PCT * 100
    name = p.get("name") or ""
    ticker_s = f"{ticker} — {name}" if name else ticker
    eintrag = {"strategie": "🌍 Kassandra", "ticker": ticker_s,
                "kurs": kurs, "stop": stop, "puffer": puffer, "crash": crash,
                "tages_ret": tages_ret}
    alle.append(eintrag)
    if crash:
        alerts.append({**eintrag, "grund": f"Crash Exit {tages_ret:+.1f}%"})
    elif puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 5:
        warnungen.append(eintrag)

# S&P 100 (RSL-Peak-Trail 35% — live RSL täglich, Peak aus JSON)
_sp100_depot = set(SP100.get("meine_aktien") or []) if "meine_aktien" in SP100 else None
for ticker, info in SP100.get("rsl_data", {}).items():
    if _sp100_depot is not None and ticker not in _sp100_depot:
        continue
    live = sp100_rsl_live(
        ticker,
        info.get("rsl_peak") or info.get("rsl_hoch") or info.get("peak"),
        api_key=API_KEY,
    )
    if not live:
        trail = info.get("trail")
        rsl_now = info.get("rsl", 0)
        puffer = info.get("puffer")
    else:
        trail = live.get("trail")
        rsl_now = live.get("rsl", 0)
        puffer = live.get("puffer")
    if trail is None or puffer is None:
        continue
    abst = info.get("abst_hoch_pct")
    abst_s = f"  ({abst:+.1f}% Kurs-Hoch)" if abst is not None else ""
    name = info.get("name") or ""
    ticker_s = f"{ticker} — {name}" if name else ticker
    eintrag = {"strategie": "📈 S&P 100", "ticker": ticker_s,
               "kurs": f"RSL {rsl_now:.3f}{abst_s}",
               "stop": f"RSL {trail:.3f}", "puffer": puffer,
               "pnl_s": ""}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 10:
        warnungen.append(eintrag)

# IVY (15% Trailing unter Peak in EUR — wie app.py / Ivy_2.1.ipynb)
for tk, p in IVY.items():
    if tk in IVY_TS_EXCLUDE or not p.get("entry_price"):
        continue
    peak = ivy_peak(p)
    if not peak:
        continue
    kurs, ksrc = ivy_eur_kurs(tk, p, peak)
    if not kurs:
        continue
    stop = round(peak * 0.85, 2)
    puffer = round((kurs / stop - 1) * 100, 1)
    peak_abst = round((kurs / peak - 1) * 100, 1)
    warmup = ivy_in_warmup(p)
    name = p.get("name") or ""
    ticker_s = f"{tk} — {name}" if name else tk
    eintrag = {
        "strategie": "🏛 IVY/RAA", "ticker": ticker_s,
        "kurs": fmt_eur(kurs), "peak": fmt_eur(peak), "stop": fmt_eur(stop),
        "puffer": puffer, "peak_abst": peak_abst, "warmup": warmup,
        "ksrc": ksrc or "?",
    }
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
               "ticker":   ticker.replace(".US","").replace(".TO",""),
               "kurs":     kurs, "stop": round(stop_nativ, 2), "puffer": puffer,
               "pnl_s":   pnl_s}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 3:
        warnungen.append(eintrag)

# Small Cap EU — 25% Trailing Stop
for isin, p in SMALLCAP.items():
    kauf = p.get("buy_price") or p.get("einstieg") or 0
    hw = p.get("high_water") or p.get("hoch") or kauf
    if not kauf:
        continue
    ticker = p.get("ticker") or isin
    eodhd_tk = ticker_fix(ticker)
    kurs = eodhd_kurs(eodhd_tk)
    if not kurs:
        continue
    hw = max(hw, kurs)
    stop = round(hw * 0.75, 2)
    puffer = round((kurs / stop - 1) * 100, 1)
    pnl_pct = p.get("pnl_pct")
    pnl_s = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—"
    name = resolve_smallcap_name(
        ticker=ticker, pos=p, isin=isin, api_key=API_KEY, cache=_NAME_CACHE,
    )
    ticker_s = f"{ticker} — {name}" if name and name.upper() != ticker.split(".")[0].upper() else ticker
    in_top = isin in _sc_top
    eintrag = {"strategie": "🇪🇺 Small Cap EU", "ticker": ticker_s,
               "kurs": kurs, "stop": stop, "puffer": puffer, "pnl_s": pnl_s}
    alle.append(eintrag)
    if kurs <= stop:
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
    if pos.get("crash"):
        puf_str = f"CRASH {pos.get('tages_ret', 0):+.1f}%"
        fb = "#ff1744"
    elif pos.get("warmup"):
        puf_str = f"{p:+.1f}% ⏳"
        fb = "#29b6f6"
    elif p is not None:
        puf_str = f"{p:+.1f}%"
        if pos["strategie"] == "🏛 IVY/RAA" and pos.get("peak_abst") is not None:
            puf_str += f" <span style='color:#888;font-size:11px'>(Peak {pos['peak_abst']:+.1f}%)</span>"
    else:
        puf_str = "RSL-Trail"
    pnl_s = pos.get("pnl_s", "")
    kurs_s = pos["kurs"] if isinstance(pos["kurs"], str) else f"{pos['kurs']:.2f}"
    peak_s = pos.get("peak", "")
    stop_s = pos["stop"] if isinstance(pos["stop"], str) else f"{pos['stop']:.2f}"
    return f"""
    <tr>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a">{pos['strategie']}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;font-weight:bold">{pos['ticker']}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{kurs_s}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right;color:#aaa">{peak_s}</td>
        <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{stop_s}</td>
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
                <th style="text-align:right;padding:6px">Peak</th>
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
                <th style="text-align:right;padding:6px">Peak</th>
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
                <th style="text-align:right;padding:8px">Peak</th>
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
                ⏰ Nächster Check: täglich 08:00 + 14:30 Uhr<br>
                🏛 IVY: Kurse in EUR (FFM/FX) · Puffer = % zum Stop · Peak in Klammern = % vom Peak
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
