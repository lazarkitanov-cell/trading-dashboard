# ═══════════════════════════════════════════════════════════════
#  TRADING STOP-CHECK — GitHub Actions
#  Läuft täglich 08:00 + 14:30 Uhr
#  v3.16 — Namens-Auflösung Breakout Meta + ETF Yahoo
# ═══════════════════════════════════════════════════════════════

import os, json, math, requests, smtplib, sys
from pathlib import Path
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    from name_lookup import resolve_smallcap_name, resolve_stock_name
except ImportError:
    def resolve_smallcap_name(ticker=None, pos=None, isin=None, api_key=None, cache=None):
        if isinstance(pos, dict) and pos.get("name"):
            return pos["name"]
        return (ticker or isin or "").split(".")[0]

    def resolve_stock_name(ticker=None, pos=None, signals=None, api_key=None, cache=None):
        if isinstance(pos, dict) and pos.get("name"):
            return pos["name"]
        return (ticker or "").split(".")[0]

try:
    from kassandra_regime_display import regime_email_html
except ImportError:
    def regime_email_html(data):
        return ""

try:
    from sp100_rsl import sp100_rsl_live
except ImportError:
    def sp100_rsl_live(ticker, rsl_peak_stored=None, api_key=None, prices=None):
        return None

try:
    from daily_stops import (
        fetch_quote,
        collect_json_sofort_exits,
        collect_sofort_orders_all,
        merge_stop_alerts,
        sofort_orders_to_alerts,
        json_kurs_hints,
        smallcap_stop_row,
        JSON_STRATEGIES,
    )
    _DAILY_STOPS_OK = True
except ImportError as _ds_err:
    _DAILY_STOPS_OK = False
    print(f"⚠️ daily_stops.py nicht importierbar: {_ds_err}")
    def fetch_quote(api_key, ticker, fallback_price=None, timeout=10):
        k = eodhd_kurs(ticker_fix(ticker))
        return {"close": k, "source": "RT"} if k else (
            {"close": fallback_price, "source": "JSON"} if fallback_price else None
        )

    def json_kurs_hints(raw):
        return {}

    def smallcap_stop_row(isin, pos, trailing_pct, api_key, kurs_hints=None):
        return None

    def collect_json_sofort_exits(raw, strategie_label):
        return []

    def merge_stop_alerts(live_alerts, json_alerts):
        return live_alerts

    def collect_sofort_orders_all(pairs):
        return []

    def sofort_orders_to_alerts(orders):
        return []

    JSON_STRATEGIES = ()

if not _DAILY_STOPS_OK:
    print("⚠️ Fallback-Modus — Small-Cap/JSON-Sofort in E-Mail eingeschränkt!")
    print("   → daily_stops.py muss im Repo neben stop_check.py liegen.")

def _inline_sofort_from_json(raw, label):
    """Sofort-VERKAUFEN aus handelsanweisungen — funktioniert ohne daily_stops."""
    rows = []
    if not isinstance(raw, dict):
        return rows
    for rec in raw.get("handelsanweisungen") or []:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("prioritaet") or "").strip().lower() != "sofort":
            continue
        act = str(rec.get("aktion") or rec.get("action") or "").upper()
        if "VERKAUF" not in act and "ALLE VERKAUF" not in act:
            continue
        rows.append({
            "strategie": label,
            "aktion": rec.get("aktion") or rec.get("action") or "🔴 VERKAUFEN",
            "ticker": rec.get("ticker") or rec.get("isin") or "—",
            "name": rec.get("name") or "",
            "grund": rec.get("grund") or "",
            "kurs_eur": rec.get("kurs_eur") or rec.get("kurs"),
            "pnl_pct": rec.get("pnl_pct"),
        })
    return rows


def _inline_sofort_to_alerts(orders):
    out = []
    for o in orders:
        ticker = o.get("ticker") or "—"
        name = o.get("name") or ""
        ticker_s = f"{ticker} — {name}" if name else str(ticker)
        kurs = o.get("kurs_eur")
        try:
            kurs_f = float(kurs) if kurs is not None else None
        except (TypeError, ValueError):
            kurs_f = None
        pnl = o.get("pnl_pct")
        out.append({
            "strategie": o.get("strategie", "?"),
            "ticker": ticker_s,
            "ticker_key": str(ticker).upper(),
            "kurs": kurs_f if kurs_f else "—",
            "stop": "—",
            "puffer": 0.0,
            "pnl_s": f"{pnl:+.1f}%" if pnl is not None else "",
            "grund": f"{o.get('aktion', '')} · {o.get('grund') or 'Sofort'}".strip(" ·"),
            "json_sofort": True,
            "dashboard_sofort": True,
        })
    return out

_NAME_CACHE = {}


def _require_env(name: str) -> str:
    val = (os.environ.get(name) or "").strip()
    if not val:
        print(f"❌ GitHub Secret fehlt: {name}")
        print("   → Repo Settings → Secrets and variables → Actions")
        sys.exit(1)
    return val


API_KEY    = _require_env("EODHD_API_KEY")
EMAIL_FROM = _require_env("EMAIL_FROM")
EMAIL_TO   = _require_env("EMAIL_TO")
EMAIL_PWD  = _require_env("EMAIL_PASSWORD")

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
    "score_details", "naechster_check", "naechster_handel", "etf_check_heute", "depot",
    "rebal_freq", "crash_exit_day",
    "handel_am", "ampel", "datum", "datum_heute", "sync_ts", "stand",
    "last_update", "tickers", "meine_aktien", "rsl_data", "kassandra",
    "_kassandra_meta", "rebalancing", "kapital", "positionen", "trailing_pct",
    "ampel_source", "invest_pct", "quoten", "regime_datum",
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
REGIME_JSON = lade_json("kassandra_regime_live.json")

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
_dashboard_sofort = []   # wie „Anstehende Transaktionen“ (Sofort)
_dash_sofort_keys = set()

now = datetime.now().strftime("%d.%m.%Y %H:%M")


def _track_dashboard_sofort(strategie, aktion, ticker, name="", grund="",
                            kurs_eur=None, pnl_pct=None):
    """Sofort-Trade für E-Mail (dedupliziert)."""
    tk = str(ticker or "—").upper()
    key = (strategie, tk)
    if key in _dash_sofort_keys:
        return
    _dash_sofort_keys.add(key)
    _dashboard_sofort.append({
        "strategie": strategie,
        "aktion": aktion or "🔴 VERKAUFEN",
        "ticker": ticker,
        "name": name or "",
        "grund": grund or "",
        "kurs_eur": kurs_eur,
        "pnl_pct": pnl_pct,
    })

# Kassandra (20% Trailing + Crash Exit)
for ticker, p in KASSANDRA.items():
    kauf = p.get("einstieg", 0)
    hoch = p.get("hoch", kauf)
    if not kauf:
        continue
    q = fetch_quote(API_KEY, ticker, fallback_price=kauf)
    if not q:
        continue
    kurs = q["close"]
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
        _track_dashboard_sofort(
            "🌍 Kassandra", "🔴 VERKAUFEN", ticker, name,
            f"Crash Exit {tages_ret:+.1f}%",
        )
    elif puffer <= 0:
        alerts.append(eintrag)
        _track_dashboard_sofort(
            "🌍 Kassandra", "🔴 VERKAUFEN", ticker, name,
            f"Trailing Stop ({puffer:+.1f}% zum Stop)",
        )
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
        _track_dashboard_sofort(
            "📈 S&P 100", "🔴 VERKAUFEN", ticker, name,
            f"RSL-Peak-Trail ausgelöst ({puffer:+.1f}% Puffer, live)",
        )
    elif puffer < 10:
        warnungen.append(eintrag)

# IVY — kein Trailing Stop (Ivy 2.4: QM-Exit + TAA-Ampel; Verkäufe nur aus JSON/Ampel ROT)

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
    etf_name = resolve_stock_name(ticker, pos=pos, api_key=API_KEY, cache=_NAME_CACHE)
    tk_short = ticker.replace(".US", "").replace(".TO", "")
    ticker_s = f"{tk_short} — {etf_name}" if etf_name and etf_name.upper() != tk_short.upper() else tk_short

    eintrag = {"strategie": "📊 ETF Yahoo Top10",
               "ticker":   ticker_s,
               "name":     etf_name or "",
               "kurs":     kurs, "stop": round(stop_nativ, 2), "puffer": puffer,
               "pnl_s":   pnl_s}
    alle.append(eintrag)
    if puffer <= 0:
        alerts.append(eintrag)
    elif puffer < 3:
        warnungen.append(eintrag)

# Small Cap EU — Trailing Stop (Live + JSON-Fallback)
_SC_TS = float(SMALLCAP_RAW.get("trailing_pct", 0.25)) if isinstance(SMALLCAP_RAW, dict) else 0.25
_sc_kurs_hints = json_kurs_hints(SMALLCAP_RAW)
for isin, p in SMALLCAP.items():
    sc_row = smallcap_stop_row(isin, p, _SC_TS, API_KEY, _sc_kurs_hints)
    if not sc_row:
        continue
    ticker = sc_row["ticker"]
    kurs = sc_row["kurs"]
    stop = sc_row["stop"]
    puffer = sc_row["puffer"]
    pnl_pct = p.get("pnl_pct")
    pnl_s = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—"
    name = resolve_smallcap_name(
        ticker=ticker, pos=p, isin=isin, api_key=API_KEY, cache=_NAME_CACHE,
    )
    ticker_s = f"{ticker} — {name}" if name and name.upper() != ticker.split(".")[0].upper() else ticker
    src_note = f" [{sc_row.get('quote_source')}]" if sc_row.get("quote_source") == "JSON" else ""
    eintrag = {
        "strategie": "🇪🇺 Small Cap EU", "ticker": ticker_s,
        "ticker_key": str(ticker).upper(),
        "kurs": kurs, "stop": stop, "puffer": puffer, "pnl_s": pnl_s,
        "peak": sc_row.get("hw"),
        "grund": f"Trailing Stop {int(_SC_TS * 100)}%{src_note}",
    }
    alle.append(eintrag)
    if sc_row["triggered"]:
        alerts.append(eintrag)
        _track_dashboard_sofort(
            "🇪🇺 Small Cap EU", "🔴 VERKAUFEN", ticker, name,
            eintrag.get("grund", "Trailing Stop"),
            kurs_eur=kurs, pnl_pct=p.get("pnl_pct"),
        )
    elif puffer < 5:
        warnungen.append(eintrag)

# Breakout Meta — Stop −5% · Ziel +10% · max. 20 Handelstage (USD)
_BM_PROFIT = 0.10
_BM_STOP = 0.05
_BM_HOLD = 20
_BM_RAW = lade_json("breakout_meta_signals.json")
_BM_PORT = lade_json("breakout_meta_portfolio.json") or {}
if isinstance(_BM_RAW, dict) and isinstance(_BM_RAW.get("portfolio"), dict):
    _BM_PORT = {**_BM_PORT, **_BM_RAW["portfolio"]}


def _bm_parse_date(val):
    if val is None:
        return None
    try:
        return datetime.fromisoformat(str(val)[:10]).date()
    except ValueError:
        try:
            return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _bm_handelstage(start, end):
    d, n = start, 0
    while d < end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


for ticker, pos in (_BM_PORT or {}).items():
    if not isinstance(pos, dict):
        continue
    ep = float(pos.get("entry_price") or 0)
    if ep <= 0:
        continue
    edate = _bm_parse_date(pos.get("entry_date"))
    stop = round(ep * (1 - _BM_STOP), 2)
    target = round(ep * (1 + _BM_PROFIT), 2)
    q = fetch_quote(API_KEY, ticker_fix(f"{ticker}.US"), fallback_price=ep)
    if not q:
        q = fetch_quote(API_KEY, ticker_fix(ticker), fallback_price=ep)
    kurs = q["close"] if q else None
    days = _bm_handelstage(edate, date.today()) if edate else None
    pnl_pct = round((kurs / ep - 1) * 100, 1) if kurs else None
    pnl_s = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—"
    puffer = round((kurs / stop - 1) * 100, 1) if kurs and stop else None
    days_s = f"{days}/{_BM_HOLD}" if days is not None else "—"
    bm_sigs = _BM_RAW.get("signals") if isinstance(_BM_RAW, dict) else None
    name = resolve_stock_name(
        ticker, pos=pos, signals=bm_sigs, api_key=API_KEY, cache=_NAME_CACHE,
    )
    ticker_s = f"{ticker} — {name}" if name and name.upper() != str(ticker).upper() else str(ticker)
    eintrag = {
        "strategie": "💥 Breakout Meta",
        "ticker": ticker_s,
        "ticker_key": str(ticker).upper(),
        "name": name or "",
        "kurs": kurs if kurs is not None else "—",
        "peak": f"${target:.2f} (T/P)",
        "stop": f"${stop:.2f}",
        "puffer": puffer if puffer is not None else 0,
        "pnl_s": pnl_s,
        "days_s": days_s,
    }
    alle.append(eintrag)
    if kurs is not None and kurs <= stop:
        grund = f"🛑 Stop −5% (${kurs:.2f})"
        alerts.append({**eintrag, "grund": grund})
        _track_dashboard_sofort(
            "💥 Breakout Meta", "🔴 VERKAUFEN", ticker, name,
            grund, pnl_pct=pnl_pct,
        )
    elif kurs is not None and kurs >= target:
        grund = f"🎯 Ziel +10% (${kurs:.2f})"
        alerts.append({**eintrag, "grund": grund})
        _track_dashboard_sofort(
            "💥 Breakout Meta", "🔴 VERKAUFEN", ticker, name,
            grund, pnl_pct=pnl_pct,
        )
    elif days is not None and days >= _BM_HOLD:
        grund = f"⏱ Zeitlimit {days}/{_BM_HOLD} Handelstage — VERKAUFEN"
        alerts.append({**eintrag, "grund": grund, "puffer": 0})
        _track_dashboard_sofort(
            "💥 Breakout Meta", "🔴 VERKAUFEN", ticker, name,
            grund, pnl_pct=pnl_pct,
        )
    elif days is not None and days >= _BM_HOLD - 3:
        warnungen.append({**eintrag, "grund": f"⏱ {days}/{_BM_HOLD} Tage"})

# Colab-JSON: Sofort-Exits ergänzen (wenn Live-Check fehlte oder veraltet)
_json_sofort = []
_json_sofort.extend(collect_json_sofort_exits(SMALLCAP_RAW, "🇪🇺 Small Cap EU", pos=SMALLCAP))
_json_sofort.extend(collect_json_sofort_exits(KASSANDRA_RAW, "🌍 Kassandra"))
_json_sofort.extend(collect_json_sofort_exits(SP100, "📈 S&P 100"))
_json_sofort.extend(collect_json_sofort_exits(lade_json("ivy_portfolio.json"), "🏛 IVY/RAA"))
_json_sofort.extend(collect_json_sofort_exits(_etf_raw, "📊 ETF Yahoo Top10"))
_json_sofort.extend(collect_json_sofort_exits(lade_json("regime_momentum_positionen.json"), "🚀 Regime Momentum"))
alerts = merge_stop_alerts(alerts, _json_sofort)
_alle_keys = {
    (a.get("strategie"), a.get("ticker_key") or str(a.get("ticker", "")).upper())
    for a in alle
}
for ja in _json_sofort:
    key = (ja.get("strategie"), ja.get("ticker_key") or str(ja.get("ticker", "")).upper())
    if key not in _alle_keys:
        alle.append(ja)
        _alle_keys.add(key)

_sofort_orders = collect_sofort_orders_all([
    (SMALLCAP_RAW, "🇪🇺 Small Cap EU"),
    (KASSANDRA_RAW, "🌍 Kassandra"),
    (SP100, "📈 S&P 100"),
    (lade_json("ivy_portfolio.json"), "🏛 IVY/RAA"),
    (_etf_raw, "📊 ETF Yahoo Top10"),
    (lade_json("regime_momentum_positionen.json"), "🚀 Regime Momentum"),
])
# Inline-Fallback + Dashboard-Parität (handelsanweisungen aus JSON)
for _raw, _lbl in (
    (SMALLCAP_RAW, "🇪🇺 Small Cap EU"),
    (KASSANDRA_RAW, "🌍 Kassandra"),
    (SP100, "📈 S&P 100"),
    (lade_json("ivy_portfolio.json"), "🏛 IVY/RAA"),
    (_etf_raw, "📊 ETF Yahoo Top10"),
    (lade_json("regime_momentum_positionen.json"), "🚀 Regime Momentum"),
):
    for _o in _inline_sofort_from_json(_raw, _lbl):
        _track_dashboard_sofort(
            _o["strategie"], _o["aktion"], _o["ticker"], _o.get("name"),
            _o.get("grund"), _o.get("kurs_eur"), _o.get("pnl_pct"),
        )
_sofort_orders = _dashboard_sofort

_to_alert_fn = sofort_orders_to_alerts if _DAILY_STOPS_OK else _inline_sofort_to_alerts
alerts = merge_stop_alerts(alerts, _to_alert_fn(_sofort_orders))

print(
    f"daily_stops: {'OK' if _DAILY_STOPS_OK else 'FEHLT'} · "
    f"Small Cap: {len(SMALLCAP)} Pos · ha={len(SMALLCAP_RAW.get('handelsanweisungen') or [])} · "
    f"JSON-Sofort: {len(_json_sofort)} · Alerts: {len(alerts)} · "
    f"Sofort-Trades: {len(_sofort_orders)}"
)
for _o in _sofort_orders:
    print(f"  → {_o['strategie']} | {_o.get('aktion')} | {_o.get('ticker')} | {_o.get('grund')}")

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
    n_st = len(_sofort_orders)
    extra = f" · {n_st} Sofort-Trade(s)" if n_st else ""
    betreff = f"🔴 {len(alerts)} Signal(e){extra} — {now}"
elif _sofort_orders:
    betreff = f"📋 {len(_sofort_orders)} Sofort-Order(s) — Colab JSON — {now}"
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
    else:
        puf_str = "RSL-Trail"
    pnl_s = pos.get("pnl_s", "")
    grund_s = pos.get("grund", "")
    kurs_s = pos["kurs"] if isinstance(pos["kurs"], str) else f"{pos['kurs']:.2f}"
    peak_v = pos.get("peak")
    peak_s = (
        f"{peak_v:.2f}" if isinstance(peak_v, (int, float)) else str(peak_v or "")
    )
    stop_s = pos["stop"] if isinstance(pos["stop"], str) else f"{pos['stop']:.2f}"
    if grund_s and pos.get("json_sofort"):
        puf_str = f"JSON · {grund_s[:40]}"
        fb = "#ff1744"
    elif grund_s and pos.get("strategie") == "💥 Breakout Meta":
        puf_str = grund_s[:55]
        fb = "#ff1744" if any(x in grund_s for x in ("Stop", "Ziel", "Zeitlimit")) else "#ffd600"
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

orders_html = ""
if _sofort_orders:
    def _order_row(o):
        kurs = o.get("kurs_eur")
        kurs_s = f"{float(kurs):.2f} €" if kurs is not None else "—"
        pnl = o.get("pnl_pct")
        pnl_s = f"{pnl:+.1f}%" if pnl is not None else ""
        name = o.get("name") or ""
        tick = o.get("ticker") or "—"
        lbl = f"{tick} — {name}" if name else tick
        return f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #2a2a3a">{o['strategie']}</td>
            <td style="padding:8px;border-bottom:1px solid #2a2a3a;font-weight:bold">{o.get('aktion','—')}</td>
            <td style="padding:8px;border-bottom:1px solid #2a2a3a">{lbl}</td>
            <td style="padding:8px;border-bottom:1px solid #2a2a3a">{o.get('grund') or '—'}</td>
            <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right">{kurs_s}</td>
            <td style="padding:8px;border-bottom:1px solid #2a2a3a;text-align:right;color:#aaa">{pnl_s}</td>
        </tr>"""

    orders_html = f"""
    <div style="background:#1a1a2e;border:2px solid #00c853;border-radius:8px;padding:15px;margin:15px 0">
        <h2 style="color:#00c853;margin:0 0 10px 0">📋 Sofort handeln (wie Dashboard)</h2>
        <p style="color:#aaa;font-size:13px;margin:0 0 10px 0">
            Entspricht „Anstehende Transaktionen“ mit Priorität <strong>Sofort</strong>
        </p>
        <table style="width:100%;border-collapse:collapse">
            <tr style="color:#aaa;font-size:12px">
                <th style="text-align:left;padding:6px">Strategie</th>
                <th style="text-align:left;padding:6px">Aktion</th>
                <th style="text-align:left;padding:6px">Ticker</th>
                <th style="text-align:left;padding:6px">Grund</th>
                <th style="text-align:right;padding:6px">Kurs €</th>
                <th style="text-align:right;padding:6px">G/V</th>
            </tr>
            {"".join(_order_row(o) for o in _sofort_orders)}
        </table>
    </div>"""

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

_depot_counts = (
    f"Kassandra {len(KASSANDRA)} · S&P100 {len(SP100.get('rsl_data') or {})} · "
    f"IVY {len(IVY)} · ETF {len(ETF)} · Small Cap {len(SMALLCAP)}"
)
if alle:
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
else:
    uebersicht_html = f"""
    <div style="background:#3a3000;border:2px solid #ffd600;border-radius:8px;padding:15px;margin:15px 0">
        <h2 style="color:#ffd600;margin:0 0 8px 0">⚠️ Keine Live-Kurse in der Übersicht</h2>
        <p style="margin:0;color:#ccc;font-size:14px">
            JSON-Depots geladen ({_depot_counts}) — aber EODHD lieferte für keine Position einen Kurs.<br>
            Prüfe <strong>EODHD_API_KEY</strong> in GitHub Secrets oder Ticker-Symbole in den JSON-Dateien.
        </p>
    </div>"""

regime_html = regime_email_html(REGIME_JSON if REGIME_JSON else None)

_sc_meta = SMALLCAP_RAW.get("_kassandra_meta", {}) if isinstance(SMALLCAP_RAW, dict) else {}
sc_quota_html = ""
if _sc_meta.get("ampel_source") == "kassandra_regime" and _sc_meta.get("invest_pct") is not None:
    _sig = _sc_meta.get("signal", "—")
    _pct = int(round(float(_sc_meta["invest_pct"]) * 100))
    sc_quota_html = f"""
    <div style="background:#1a1a2e;border-left:4px solid #00c853;padding:10px 15px;margin:0 0 15px 0">
        <p style="margin:0;color:#ccc;font-size:14px">
            🇪🇺 <strong>Small Cap EU</strong> — Investitionsquote via Kassandra Regime:
            <strong style="color:#00c853">{_sig} ({_pct}%)</strong>
            · Exit-only · kein Ampel-Verkauf
        </p>
    </div>"""

_k_meta = KASSANDRA_RAW if isinstance(KASSANDRA_RAW, dict) else {}
kass_regime_html = ""
if _k_meta.get("ampel_source") == "kassandra_regime" and _k_meta.get("invest_pct") is not None:
    _kpct = int(round(float(_k_meta["invest_pct"]) * 100))
    _ksig = _k_meta.get("score", _kpct)
    kass_regime_html = f"""
    <div style="background:#1a1a2e;border-left:4px solid #00c853;padding:10px 15px;margin:0 0 15px 0">
        <p style="margin:0;color:#ccc;font-size:14px">
            🌍 <strong>Länder-ETF Kassandra</strong> — Slots via Kassandra Regime:
            <strong style="color:#00c853">{_kpct}% Exposure</strong>
            (Score {_ksig}/100)
        </p>
    </div>"""

html = f"""
<html><body style="background:#0f0f1a;color:white;font-family:Arial,sans-serif;padding:20px">
    <div style="max-width:700px;margin:0 auto">
        <h1 style="color:#00c853;border-bottom:2px solid #00c853;padding-bottom:10px">
            📈 Trading Dashboard — Stop-Check
        </h1>
        <p style="color:#aaa">Stand: {now} | Automatischer Check via GitHub Actions</p>
        {regime_html}
        {kass_regime_html}
        {sc_quota_html}
        {orders_html}
        {alert_html}
        {warn_html}
        {uebersicht_html}
        <div style="margin-top:20px;padding:10px;background:#1a1a2e;border-radius:8px">
            <p style="color:#aaa;font-size:12px;margin:0">
                🔗 Dashboard: <a href="https://lazar-trading-dashboard.streamlit.app" style="color:#00c853">
                lazar-trading-dashboard.streamlit.app</a><br>
                ⏰ Nächster Check: täglich 08:00 + 14:30 Uhr<br>
                🏛 IVY: kein Trailing Stop — Verkäufe via QM-Exit (Colab) oder Ampel ROT
            </p>
        </div>
    </div>
</body></html>"""

# ── Email senden ─────────────────────────────────────────────────

plain_lines = [betreff, f"Stand: {now}", ""]
if REGIME_JSON.get("signal"):
    plain_lines.append(
        f"Regime: {REGIME_JSON.get('signal')} · "
        f"{int(float(REGIME_JSON.get('invest_pct', 0)) * 100)}%"
    )
plain_lines.append(f"Depots in JSON: {_depot_counts}")
if _sofort_orders:
    plain_lines.append("")
    plain_lines.append(f"📋 {len(_sofort_orders)} Sofort-Order(s) aus Colab JSON:")
    for o in _sofort_orders:
        pnl = o.get("pnl_pct")
        pnl_s = f" G/V {pnl:+.1f}%" if pnl is not None else ""
        plain_lines.append(
            f"  {o['strategie']} | {o.get('aktion','—')} | {o.get('ticker')} | "
            f"{o.get('grund') or '—'}{pnl_s}"
        )
if alerts:
    plain_lines.append("")
    plain_lines.append(f"🔴 {len(alerts)} Stop(s) ausgelöst:")
    for a in alerts:
        puf = a.get("puffer")
        puf_s = f"{puf:+.1f}%" if isinstance(puf, (int, float)) else str(puf or "—")
        grund = a.get("grund") or ""
        line = f"  {a['strategie']} | {a['ticker']} | Puffer {puf_s}"
        if grund:
            line += f" | {grund}"
        plain_lines.append(line)
if alle:
    plain_lines.append("")
    plain_lines.append("Alle Positionen:")
    for a in alle:
        puf = a.get("puffer")
        puf_s = f"{puf:+.1f}%" if isinstance(puf, (int, float)) else str(puf or "—")
        grund = a.get("grund") or ""
        line = f"  {a['strategie']} | {a['ticker']} | Puffer {puf_s}"
        if grund and a.get("json_sofort"):
            line += f" | {grund}"
        plain_lines.append(line)
else:
    plain_lines.append("")
    plain_lines.append("Keine Live-Kurse — EODHD oder Ticker prüfen.")
plain_lines.append("")
plain_lines.append("Dashboard: https://lazar-trading-dashboard.streamlit.app")

msg = MIMEMultipart("alternative")
msg["Subject"] = betreff
msg["From"]    = EMAIL_FROM
msg["To"]      = EMAIL_TO
msg.attach(MIMEText("\n".join(plain_lines), "plain", "utf-8"))
msg.attach(MIMEText(html, "html", "utf-8"))

try:
    smtp_server = "smtp.gmail.com"
    with smtplib.SMTP_SSL(smtp_server, 465) as server:
        server.ehlo()
        server.login(EMAIL_FROM.replace("googlemail.com", "gmail.com"), EMAIL_PWD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"✅ Email gesendet: {betreff}")
    if alerts:
        print(f"🔴 {len(alerts)} Stop(s) ausgelöst!")
    elif _sofort_orders:
        print(f"📋 {len(_sofort_orders)} Sofort-Order(s) aus JSON")
    elif warnungen:
        print(f"🟡 {len(warnungen)} Warnung(en)")
    else:
        print("✅ Alle Stops OK")
except Exception as e:
    print(f"❌ Email-Fehler: {e}")
    print("   → Gmail: App-Passwort (16 Zeichen) in Secret EMAIL_PASSWORD")
    print("   → EMAIL_FROM = dieselbe Gmail-Adresse wie beim App-Passwort")
    sys.exit(1)
