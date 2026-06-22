# ═══════════════════════════════════════════════════════════════════════════
#  TRADING DASHBOARD v5.1.0 — Live-Sync von GitHub
#  Nächster Check + Trailing-Stop (6 Strategien, JSON von GitHub / Colab)
# ═══════════════════════════════════════════════════════════════════════════

APP_VERSION = "5.2.9"
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

from kassandra_regime_display import format_regime_banner
from name_lookup import resolve_smallcap_name
from sp100_rsl import compute_rsl_from_series

st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📈",
    layout="wide",
)

if "json_refresh" not in st.session_state:
    st.session_state.json_refresh = 0

if "name_cache" not in st.session_state:
    st.session_state.name_cache = {}


def _sc_name(ticker=None, pos=None, isin=None):
    return resolve_smallcap_name(
        ticker=ticker, pos=pos, isin=isin,
        api_key=API_KEY, cache=st.session_state.name_cache,
    )


def _sp100_live_rsl(ticker, info):
    """Täglich: RSL + Puffer aus EODHD; RSL-Peak aus JSON (wächst nur nach oben)."""
    if not isinstance(info, dict):
        info = {}
    peak = info.get("rsl_peak") or info.get("rsl_hoch") or info.get("peak")
    prices = eodhd_eod_series(ticker_fix(ticker), days=45)
    live = compute_rsl_from_series(prices, peak)
    if live:
        return live
    return {
        "rsl": info.get("rsl"),
        "rsl_peak": peak,
        "trail": info.get("trail"),
        "puffer": info.get("puffer"),
        "status": info.get("status"),
    }

try:
    API_KEY = st.secrets["EODHD_API_KEY"]
except Exception:
    API_KEY = "69c0f8ad5ac198.37699109"

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

KURS_STALE_TAGE = 1  # ⚠️ wenn Kursdatum älter als so viele Kalendertage


@st.cache_data(ttl=300)
def eodhd_realtime(ticker):
    try:
        r = requests.get(
            f"https://eodhd.com/api/real-time/{ticker}",
            params={"api_token": API_KEY, "fmt": "json"},
            timeout=10,
        )
        data = r.json()
        close = float(data.get("close") or data.get("previousClose") or 0)
        prev = float(data.get("previousClose") or 0)
        if close <= 0:
            return None
        quote_date = _eodhd_ts_to_date(data.get("timestamp"))
        return {
            "close": close,
            "previousClose": prev if prev > 0 else None,
            "quote_date": quote_date,
            "source": "RT",
        }
    except Exception:
        return None


def _eodhd_ts_to_date(ts):
    if ts in (None, "", 0, "0"):
        return None
    try:
        return datetime.utcfromtimestamp(int(ts)).date()
    except (TypeError, ValueError, OSError):
        return None


def eodhd_kurs(ticker):
    q = eodhd_quote(ticker)
    return q["close"] if q else None


@st.cache_data(ttl=300)
def eodhd_eod_last_quote(ticker, days=14):
    s = eodhd_eod_series(ticker, days)
    if s is None or len(s) == 0:
        return None
    return {
        "close": float(s.iloc[-1]),
        "quote_date": s.index[-1].date(),
        "source": "EOD",
    }


@st.cache_data(ttl=300)
def eodhd_quote(ticker):
    """Bester verfügbarer Kurs inkl. Datum — bevorzugt neuere Quelle (RT vs. EOD)."""
    tk = ticker_fix(ticker)
    rt = eodhd_realtime(tk)
    eod = eodhd_eod_last_quote(tk)
    candidates = []
    if rt and rt.get("close"):
        candidates.append(rt)
    if eod and eod.get("close"):
        candidates.append(eod)
    if not candidates:
        return None

    def _rank(c):
        qd = c.get("quote_date")
        src = 1 if c.get("source") == "RT" else 0
        return (qd or date.min, src)

    best = max(candidates, key=_rank)
    qd = best.get("quote_date")
    stale = (date.today() - qd).days > KURS_STALE_TAGE if qd else True
    return {**best, "stale": stale}


@st.cache_data(ttl=3600)
def eodhd_eod_series(ticker, days=500):
    try:
        start = (date.today() - timedelta(days=days)).isoformat()
        r = requests.get(
            f"https://eodhd.com/api/eod/{ticker}",
            params={"api_token": API_KEY, "fmt": "json", "period": "d", "from": start},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        if not rows:
            return None
        idx, vals = [], []
        for row in rows:
            d = row.get("date")
            c = row.get("adjusted_close") or row.get("close")
            if d and c:
                idx.append(pd.Timestamp(d))
                vals.append(float(c))
        return pd.Series(vals, index=idx).sort_index() if vals else None
    except Exception:
        return None


def lade_json(pfad):
    p = Path(pfad)
    return json.loads(p.read_text()) if p.exists() else None


def _lade_json_github_api(dateiname):
    """GitHub Contents API — zuverlässiger als CDN-Cache von raw.githubusercontent.com."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{dateiname}"
        r = requests.get(
            url,
            timeout=15,
            headers={
                "Accept": "application/vnd.github.raw",
                "Cache-Control": "no-cache",
            },
        )
        if r.status_code == 200 and r.text.strip():
            return json.loads(r.text)
    except Exception:
        pass
    return None


@st.cache_data(ttl=120, show_spinner=False)
def lade_json_github(dateiname, _refresh=0):
    """JSON live von GitHub (Colab-Upload) — Fallback auf Repo-Datei."""
    bust = f"?_={_refresh}" if _refresh else ""
    for url in (GITHUB_RAW + dateiname + bust, GITHUB_RAW + dateiname):
        try:
            r = requests.get(
                url,
                timeout=15,
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            )
            if r.status_code == 200 and r.text.strip():
                return json.loads(r.text)
        except Exception:
            continue
    data = _lade_json_github_api(dateiname)
    if data:
        return data
    return lade_json(dateiname)


def sp100_depot_ticker(sp100_pos):
    """Nur echte Depot-Positionen (meine_aktien) — keine Kauf-Signale aus tickers."""
    if not sp100_pos:
        return None
    meine = sp100_pos.get("meine_aktien")
    if meine is None:
        return None
    return set(meine)


def json_meta_ts(data):
    if not isinstance(data, dict):
        return None
    return (
        data.get("sync_ts")
        or data.get("_sync_ts")
        or data.get("stand")
        or data.get("datum")
        or data.get("datum_heute")
        or data.get("last_update")
    )


def _parse_json_ts(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        if v > 1e12:
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v)
        except (OSError, ValueError):
            return None
    s = str(raw).strip()
    if not s or s == "—":
        return None
    for part in (s, s[:19], s[:16], s[:10]):
        try:
            return datetime.fromisoformat(part.replace("Z", ""))
        except ValueError:
            pass
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        for n in (len(s), 16, 10):
            if n > len(s):
                continue
            try:
                return datetime.strptime(s[:n], fmt)
            except ValueError:
                pass
    return None


def format_letztes_json(data):
    """Letzter Colab-/GitHub-Stand aus JSON-Metadaten."""
    raw = json_meta_ts(data)
    dt = _parse_json_ts(raw)
    if dt:
        return dt.strftime("%d.%m.%Y %H:%M")
    if raw:
        return str(raw)
    return "—"


def json_sync_hinweis(label, data):
    return f"{label}: {format_letztes_json(data)}"


def _ivy_ticker_norm(ticker):
    return str(ticker or "").strip().upper().replace(".US", "").replace(".TO", "")


def _ivy_depot_ticker_set(data):
    """Normierte Ticker aus dem gespeicherten IVY-Depot (ohne Meta-Keys)."""
    if not isinstance(data, dict):
        return set()
    return {_ivy_ticker_norm(k) for k in portfolio_ohne_meta(data)}


def _ivy_order_plausibel(order, depot_norm):
    """
    Filtert Backtest-Allokations-Orders, die nicht zum Live-Depot passen.
    KAUFEN obwohl schon im Depot · VERKAUFEN obwohl Ticker nie gehalten.
    """
    if not isinstance(order, dict):
        return False
    tk = _ivy_ticker_norm(order.get("ticker"))
    act = (order.get("action") or order.get("aktion") or "").upper()
    if "KAUF" in act and tk in depot_norm:
        return False
    if "VERKAUF" in act and tk not in depot_norm:
        return False
    return bool(act and act != "HALTEN" and "HALTEN" not in act)


def _ivy_orders_aus_json(data):
    """Gefilterte IVY-Orders für Transaktionstabelle (ohne stale Backtest-Signale)."""
    if not isinstance(data, dict):
        return []
    roh = data.get("handelsanweisungen") or data.get("orders") or []
    depot = _ivy_depot_ticker_set(data)
    if not depot:
        return roh
    return [o for o in roh if _ivy_order_plausibel(o, depot)]


def _ivy_orders_roh(data):
    """Ungefilterte Orders — nur für Stale-Warnung."""
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or data.get("orders") or []


def _ivy_orders_stale_hinweis(data):
    """True wenn JSON-Orders offensichtlich nicht zum Depot passen."""
    roh = _ivy_orders_roh(data)
    if not roh:
        return False
    gef = _ivy_orders_aus_json(data)
    offen_roh = sum(
        1 for o in roh
        if isinstance(o, dict)
        and (o.get("action") or o.get("aktion") or "").upper() not in ("", "HALTEN")
        and "HALTEN" not in str(o.get("action") or o.get("aktion") or "").upper()
    )
    offen_gef = sum(
        1 for o in gef
        if isinstance(o, dict)
        and (o.get("action") or o.get("aktion") or "").upper() not in ("", "HALTEN")
        and "HALTEN" not in str(o.get("action") or o.get("aktion") or "").upper()
    )
    return offen_roh > offen_gef


def _handels_aktionen(data, quelle="ivy"):
    if not isinstance(data, dict):
        return []
    if quelle == "ivy":
        roh = _ivy_orders_aus_json(data)
        # Nur offene Trades (ohne HALTEN)
        roh = [
            o for o in roh
            if isinstance(o, dict)
            and (o.get("action") or o.get("aktion") or "").upper() not in ("", "HALTEN")
            and "HALTEN" not in str(o.get("action") or o.get("aktion") or "").upper()
        ]
    else:
        roh = data.get("handelsanweisungen") or []
    out = []
    for o in roh:
        if not isinstance(o, dict):
            continue
        act = (o.get("action") or o.get("aktion") or "").upper()
        if act and act != "HALTEN" and "HALTEN" not in act:
            if quelle == "smallcap" and _aktion_typ(act) not in ("kauf", "verkauf", "aufstock", "reduz"):
                continue
            out.append(o)
    return out


def _aktion_typ(act):
    """KAUFEN vs VERKAUFEN — VERKAUFEN darf nicht als KAUF zählen."""
    a = (act or "").upper()
    if "VERKAUF" in a or "ALLE VERKAUFEN" in a:
        return "verkauf"
    if "KAUF" in a:
        return "kauf"
    if "AUFSTOCK" in a:
        return "aufstock"
    if "REDUZ" in a:
        return "reduz"
    return "other"


def json_trade_hinweis(label, data, quelle="ivy"):
    ha = _handels_aktionen(data, quelle)
    if ha:
        k = sum(1 for o in ha if _aktion_typ(o.get("action") or o.get("aktion")) in ("kauf", "aufstock"))
        v = sum(1 for o in ha if _aktion_typ(o.get("action") or o.get("aktion")) == "verkauf")
        return f"{label}: {len(ha)} Trades ({k} Kaufen · {v} Verkaufen)"
    if quelle in ("etf", "etf_eodhd") and isinstance(data, dict) and data.get("empfehlung"):
        n = len(data.get("empfehlung") or [])
        return f"{label}: keine Handelsanweisungen — {n} Kandidaten (empfehlung)"
    return f"{label}: keine Handelsanweisungen in JSON"


JSON_TOP_META_KEYS = frozenset({
    "handelsanweisungen", "orders", "verkaufen", "kaufen",
    "kassandra_ampel", "score", "score_smooth", "score_raw", "score_heute",
    "score_details", "naechster_check", "rebal_freq", "crash_exit_day",
    "handel_am", "ampel", "datum", "datum_heute", "sync_ts", "stand",
    "last_update", "tickers", "meine_aktien", "rsl_data", "kassandra",
    "ampel_source", "invest_pct", "quoten", "regime_datum",
    "empfehlung", "metadata", "meta",
    "stock_data", "ziel_aktien", "kassandra_score", "use_kassandra", "depot_quelle",
    # HAA-Balanced Meta
    "strategie", "signal_monat", "regime", "regime_label", "tip_momentum", "crash",
    "cash_fallback", "ziel", "ziel_ticker", "ziel_gewichte", "rankings_offensive",
    "selection_erklaerung", "vergleich_offensiv", "vergleich_defensiv", "canary_detail",
    "regel_text", "momentum_methode", "hinweis", "kapital_eur",
    "screening_detail", "vergleich_kandidaten", "vergleich",
})

POSITION_FIELD_MARKERS = (
    "einstieg", "entry_price", "buy_price", "kauf_kurs", "kaufdatum",
    "buy_date", "shares", "hoch", "high_water", "peak_price",
)


def portfolio_ohne_meta(data):
    """Entfernt Meta-Keys aus Positions-JSON (Ticker- und ISIN-Schlüssel)."""
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if not str(k).startswith("_")
        and k not in JSON_TOP_META_KEYS
        and isinstance(v, dict)
        and any(f in v for f in POSITION_FIELD_MARKERS)
    }


def position_entry(p):
    """Einstiegspreis — unterstützt einstieg / entry_price / buy_price."""
    if not isinstance(p, dict):
        return 0
    for k in ("einstieg", "entry_price", "buy_price", "kauf_kurs"):
        v = safe_float(p.get(k))
        if v and v > 0:
            return v
    return 0


def position_high(p, entry=0):
    if not isinstance(p, dict):
        return entry or 0
    for k in ("hoch", "high_water", "peak_price"):
        v = safe_float(p.get(k))
        if v and v > 0:
            return v
    return entry or 0


_APP_DIR = Path(__file__).resolve().parent
try:
    _KASS_BEREICH_MAP = json.loads(
        (_APP_DIR / "kass_etf_bereich.json").read_text(encoding="utf-8"),
    )
except Exception:
    _KASS_BEREICH_MAP = {}

_KASS_BEREICH_KAPITAL = {
    "all_etf": 0.20,
    "themen": 0.20,
    "laender": 0.40,
    "krypto": 0.20,
}
_KASS_BEREICH_LABEL = {
    "all_etf": "ALL ETF",
    "themen": "Themen",
    "laender": "Länder",
    "krypto": "Krypto",
}


def _kass_bereich_for(ticker, pos=None):
    if isinstance(pos, dict):
        b = pos.get("bereich") or pos.get("bucket")
        if b:
            return str(b)
    return _KASS_BEREICH_MAP.get(ticker) or _KASS_BEREICH_MAP.get(str(ticker).upper()) or ""


def _kass_infer_gewichte(positions):
    """Fallback: Bucket-Gewichte aus Bereich + Anzahl Positionen (wie Colab)."""
    by_b = {}
    for t, p in positions.items():
        b = _kass_bereich_for(t, p)
        if b:
            by_b.setdefault(b, []).append(t)
    if not by_b:
        return {}
    kap = dict(_KASS_BEREICH_KAPITAL)
    if "krypto" not in by_b:
        kap["krypto"] = 0.0
    active = {b: ts for b, ts in by_b.items() if kap.get(b, 0) > 0}
    gesamt = sum(kap[b] for b in active)
    if gesamt <= 0:
        return {}
    out = {}
    for b, tickers in active.items():
        pro = kap[b] / gesamt / len(tickers)
        for t in tickers:
            out[t] = pro
    return out


def _kass_live_peak(ticker, stored_peak, einstieg):
    peak = stored_peak or einstieg or 0
    live = safe_float(eodhd_kurs(ticker))
    if live and live > peak:
        peak = live
    return peak


def positions_merged(data, list_key="positionen"):
    """Top-Level-Ticker + optionale Liste positionen[] (wie etf_eingabe.json)."""
    pos = dict(portfolio_ohne_meta(data))
    if isinstance(data, dict):
        for item in data.get(list_key) or []:
            if not isinstance(item, dict):
                continue
            tk = item.get("ticker") or item.get("isin")
            if tk:
                key = str(tk)
                pos[key] = {**pos.get(key, {}), **item}
    return pos


def parse_etf_portfolio(raw):
    """positionen[] aus etf_eingabe.json → Ticker-Dict + Trailing-Pct."""
    if isinstance(raw, dict) and "positionen" in raw:
        pos = {
            p["ticker"]: p
            for p in raw.get("positionen", [])
            if isinstance(p, dict) and p.get("ticker")
        }
        return pos, raw.get("trailing_pct", 0.10)
    return (raw if isinstance(raw, dict) else {}), 0.10


def ticker_fix(ticker):
    if ticker.endswith(".L"):
        return ticker[:-2] + ".LSE"
    if "." not in ticker:
        return ticker + ".US"
    return ticker


# Börsen-Suffix → ISO-Währung (wie Ivy_2.1 / Kassandra EODHD)
EXCHANGE_CURRENCY = {
    "US": "USD", "": "USD",
    "DE": "EUR", "PA": "EUR", "AS": "EUR", "MI": "EUR", "MC": "EUR",
    "LS": "EUR", "BR": "EUR", "HE": "EUR", "VI": "EUR", "XETRA": "EUR",
    "F": "EUR",
    "L": "GBP", "LSE": "GBP",
    "SW": "CHF",
    "TO": "CAD", "V": "CAD",
    "SA": "BRL",
    "AU": "AUD",
    "HK": "HKD",
    "ST": "SEK", "CO": "DKK", "OL": "NOK",
    "SI": "SGD", "NZ": "NZD", "TW": "TWD", "KO": "KRW",
}

# Anzeige: (Symbol, "before" | "after")
CURRENCY_FMT = {
    "USD": ("$", "before"),
    "EUR": ("€", "after"),
    "GBP": ("£", "before"),
    "CHF": ("CHF ", "before"),
    "CAD": ("C$", "before"),
    "AUD": ("A$", "before"),
    "JPY": ("¥", "before"),
    "SEK": ("SEK ", "after"),
    "NOK": ("NOK ", "after"),
    "DKK": ("DKK ", "after"),
    "HKD": ("HK$", "before"),
    "SGD": ("S$", "before"),
    "BRL": ("R$", "before"),
}


def ticker_currency(ticker):
    """Handelswährung aus Ticker-Suffix (Original-Ticker, vor ticker_fix)."""
    t = (ticker or "").strip().upper()
    if not t:
        return "USD"
    if t.endswith(".LSE") or t.endswith(".L"):
        return "GBP"
    if "." not in t:
        return "USD"
    suffix = t.rsplit(".", 1)[1]
    if suffix in ("TO",) or t.endswith(".TO"):
        return "CAD"
    if suffix == "T":  # z.B. 7203.T (Tokio)
        return "JPY"
    return EXCHANGE_CURRENCY.get(suffix, "USD")


def format_kurs(value, ticker):
    """Kurs mit passendem Währungssymbol — nur Anzeige, Stop-Logik unverändert."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    ccy = ticker_currency(ticker)
    sym, pos = CURRENCY_FMT.get(ccy, (f"{ccy} ", "before"))
    if pos == "after":
        return f"{v:.2f} {sym.strip()}"
    return f"{sym}{v:.2f}"


def format_akt_kurs(value, ticker, quote=None, fallback_label=None, extra=None, currency=None):
    """Akt. Kurs mit Datum; ⚠️ wenn älter als KURS_STALE_TAGE."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if currency == "EUR":
        base = f"{v:.2f} €"
    else:
        base = format_kurs(v, ticker)
    if extra:
        base = f"{base} ({extra})"
    if fallback_label:
        return f"{base} · {fallback_label}"
    if not quote:
        return base
    qd = quote.get("quote_date")
    dat_str = qd.strftime("%d.%m.%Y") if qd else "Datum ?"
    prefix = "⚠️ " if quote.get("stale") else ""
    return f"{prefix}{base} · {dat_str}"


def naechster_wochentag(weekday):
    """Nächster Wochentag — ohne heute (für Rückwärts-Lookups)."""
    heute = date.today()
    tage = (weekday - heute.weekday()) % 7
    if tage == 0:
        tage = 7
    return heute + timedelta(days=tage)


def naechster_check_tag(weekday):
    """Nächster Signal-Check — heute zählt mit, wenn heute Check-Tag ist."""
    heute = date.today()
    tage = (weekday - heute.weekday()) % 7
    return heute + timedelta(days=tage)


def handel_nach_check(check_datum, handel_wd):
    """Erster Handelstag nach dem Signal-Check."""
    d = check_datum + timedelta(days=1)
    for _ in range(8):
        if d.weekday() == handel_wd:
            return d
        d += timedelta(days=1)
    return d


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
    "regime_momentum": {
        "label": "🚀 Regime Momentum",
        "frequenz": "wöchentlich",
        "check_tag": 3,       # Do EOD
        "handel_tag": 4,      # Fr 15:30 US
        "handel_uhrzeit": "15:30",
        "hinweis": "Do EOD → Fr 15:30 US · Kassandra Regime + S&P 500 Momentum",
    },
    "smallcap": {
        "label": "🇪🇺 Small Cap EU",
        "frequenz": "wöchentlich",
        "check_tag": 1,
        "handel_tag": 2,
        "handel_uhrzeit": "09:00",
        "hinweis": "Di EOD → Mi 09:00 · Exit-only · TS 25% · EMA −5%",
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
        "label": "📊 ETF Yahoo Top10",
        "frequenz": "monatlich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "15:30",
        "hinweis": "Monatsende → 1. Handelstag 15:30 US · yfinance Top10",
    },
    "etf_eodhd": {
        "label": "📊 ETF EODHD Voll",
        "frequenz": "monatlich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "15:30",
        "hinweis": "Monatsende → 1. Handelstag 15:30 US · EODHD Voll-Holdings",
    },
    "haa": {
        "label": "⚖️ HAA-Balanced",
        "frequenz": "monatlich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "15:30",
        "hinweis": "Monatsende → 1. Handelstag 15:30 US (4 ETFs + TIP-Canary)",
    },
}

STOP_CFG = {
    "kassandra": {
        "pct": 0.20, "typ": "Trailing", "basis": "hoch", "active": True,
        "regel": "20% Trailing Stop (vom Hoch) + Crash Exit (≥8% Tagesverlust)",
    },
    "sp100": {
        "pct": 0.35, "typ": "RSL-Trail", "basis": "rsl_peak", "active": True,
        "regel": (
            "35% RSL-Peak-Trail — Verkauf wenn RSL 35% unter dem "
            "eigenen RSL-Hoch fällt (kein Kurs-Trailing-Stop)"
        ),
    },
    "regime_momentum": {
        "pct": None, "typ": None, "basis": None, "active": False,
        "regel": (
            "Ranking-Exit (Rank > exit_rank oder Close < SMA100) · "
            "kein Trailing Stop · Kassandra steuert Brutto-Quote"
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
    "etf_eodhd": {
        "pct": 0.10, "typ": "Trailing", "basis": "hoch", "active": True,
        "regel": "10% Trailing Stop (vom Hoch, native Währung)",
    },
    "smallcap": {
        "pct": 0.25, "typ": "Trailing", "basis": "high_water", "active": True,
        "regel": "25% TS · EMA100 −5% · Ampel · Exit-only (kein Ranking-Verkauf)",
    },
    "haa": {
        "pct": None, "typ": None, "basis": None, "active": False,
        "regel": "Kein Trailing Stop (monatliches TAA-Rebalancing · TIP-Canary)",
    },
}


def stop_regel(key):
    """Ausführliche Stop-Regel (Hinweise / Info-Box)."""
    if key == "etf":
        return f"{int(ETF_TS * 100)}% Trailing Stop (vom Hoch, native Währung)"
    if key == "etf_eodhd":
        return f"{int(ETF_EODHD_TS * 100)}% Trailing Stop (vom Hoch, native Währung)"
    return STOP_CFG[key]["regel"]


def stop_pct_anzeige(key):
    """Kompakter Trailing-Stop-Wert je Strategie (nur %)."""
    if not STOP_CFG[key].get("active"):
        return "—"
    if key == "etf":
        pct = ETF_TS
    elif key == "etf_eodhd":
        pct = ETF_EODHD_TS
    else:
        pct = STOP_CFG[key]["pct"]
    if key == "sp100":
        return f"{int(round(pct * 100))}% RSL"
    return f"{int(round(pct * 100))}%"


def format_naechster_check(key, ci):
    """Geplanter nächster Signal-Check (Rhythmus der Strategie)."""
    base = format_datum(ci["check_datum"])
    cfg = CHECK_ZEITEN[key]
    if cfg["frequenz"] == "monatlich":
        return f"{base} · Monatsende"
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][cfg["check_tag"]]
    if cfg["frequenz"] == "2-wöchentlich":
        return f"{base} · {wd} EOD (2-wöchentlich)"
    if cfg["frequenz"] == "4-wöchentlich":
        return f"{base} · {wd} EOD (4-wöchentlich)"
    return f"{base} · {wd} EOD"


def signal_spalten(key, ci, json_data):
    """Nächster Check + letzter JSON-Upload für Tabellenzeilen."""
    return {
        "Nächster Check": format_naechster_check(key, ci),
        "Letztes JSON": format_letztes_json(json_data),
    }


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
        daten = naechster_check_tag(check_wd)
        handel = handel_nach_check(daten, handel_wd)
    return {
        "label": cfg["label"],
        "frequenz": cfg["frequenz"],
        "check_datum": daten,
        "handel_datum": handel,
        "handel_uhrzeit": cfg["handel_uhrzeit"],
        "tage_bis_check": tage_bis(daten),
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
    "WDC": "WDC.F",
    "TECK-B.TO": "TGB.F",
    "STMPA.PA": "STMPA.PA",
    "ESLT.US": "E4L.F",
}

IVY_TS_EXCLUDE = {"LYTR.XETRA", "VTI", "VEU", "BND", "VNQ"}
IVY_WARMUP_DAYS = 10

IVY_TREND_MONTHS = 10
IVY_VIX_THRESHOLD = 30.0
IVY_YELLOW_SAFE_W = 0.50
IVY_SPY_TICKER = "SPY.US"
IVY_VIX_TICKERS = ("VIX.INDX", "VIX.US", "^VIX")
IVY_SAFE_ASSET = "SHY"

KASSANDRA_CRASH_EXIT_DEFAULT = 0.08

IVY_EXCHANGE_CCY = {
    "US": "USD", "": "USD",
    "DE": "EUR", "PA": "EUR", "AS": "EUR", "MI": "EUR", "MC": "EUR",
    "LS": "EUR", "LSE": "GBP", "BR": "EUR", "HE": "EUR", "VI": "EUR",
    "XETRA": "EUR", "F": "EUR",
    "L": "GBP", "SW": "CHF", "TO": "CAD", "V": "CAD",
}
IVY_FX_PAIRS = {
    "GBP": ("GBPUSD.FOREX", False),
    "CHF": ("USDCHF.FOREX", True),
    "CAD": ("USDCAD.FOREX", True),
}


def ivy_ffm_ticker(pos):
    ffm = (pos.get("ffm_ticker") or "").strip().upper()
    if not ffm:
        return None
    return ffm if ffm.endswith(".F") else ffm + ".F"


def ivy_ticker_currency(ticker):
    sfx = ticker.rsplit(".", 1)[1] if "." in ticker else "US"
    return IVY_EXCHANGE_CCY.get(sfx, "USD")


@st.cache_data(ttl=300)
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


@st.cache_data(ttl=300)
def eodhd_eod_last(ticker, days=14):
    s = eodhd_eod_series(ticker, days)
    return float(s.iloc[-1]) if s is not None and len(s) else None


def _ivy_kurs_plausibel(kurs, peak):
    if not peak or not kurs:
        return True
    ratio = kurs / peak
    return 0.55 <= ratio <= 1.15


def ivy_eur_kurs(tk, pos, peak_hint=None):
    """EUR-Kurs: FFM (.F) oder FX — kein USD-Fallback bei gesetztem ffm_ticker."""
    ffm = ivy_ffm_ticker(pos)
    if ffm:
        q = eodhd_quote(ffm)
        if q and q.get("close") and _ivy_kurs_plausibel(q["close"], peak_hint):
            return q["close"], "FFM", q
        return None, None, None
    native = ivy_native_ticker(tk)
    q = eodhd_quote(native)
    if not q or not q.get("close"):
        return None, None, None
    k = q["close"]
    k_eur = ivy_to_eur(k, native)
    if k_eur and _ivy_kurs_plausibel(k_eur, peak_hint):
        return k_eur, "FX", q
    return None, None, None


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


def ivy_status(puffer, pos):
    ht = ivy_handelstage_seit_kauf(pos.get("entry_date"))
    if ht is not None and ht < IVY_WARMUP_DAYS:
        return f"⏳ Warmup ({ht}/{IVY_WARMUP_DAYS}d)"
    return status_icon(puffer)


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
    """Tagesrendite — Real-Time, Fallback: letzte 2 EOD-Schlusskurse."""
    tk = ticker_fix(ticker)
    rt = eodhd_realtime(tk)
    if rt and rt.get("previousClose"):
        return round((rt["close"] / rt["previousClose"] - 1) * 100, 2)
    s = eodhd_eod_series(tk, days=10)
    if s is not None and len(s) >= 2:
        return round((float(s.iloc[-1]) / float(s.iloc[-2]) - 1) * 100, 2)
    return None


def kassandra_status(puffer, tages_ret, crash_pct):
    if crash_pct and tages_ret is not None and tages_ret <= -crash_pct * 100:
        return "🔴 CRASH"
    return status_icon(puffer)


@st.cache_data(ttl=3600)
def ivy_markt_ampel():
    monthly = eodhd_eod_series(IVY_SPY_TICKER)
    spy_rt = eodhd_kurs(IVY_SPY_TICKER)
    vix = None
    for vt in IVY_VIX_TICKERS:
        vix = eodhd_kurs(vt)
        if vix:
            break
    if monthly is None or monthly.empty:
        return {"ampel": "red", "label": "🔴 ROT", "aktion": "100% SHY — Daten unvollständig",
                "spy": spy_rt, "sma": None, "vix": vix, "spy_vs_sma_pct": None}
    monthly = monthly.resample("ME").last().dropna()
    if spy_rt and len(monthly) > 0:
        monthly.iloc[-1] = spy_rt
    spy_now = float(monthly.iloc[-1]) if len(monthly) else spy_rt
    sma_now = None
    ampel = "red"
    if len(monthly) >= IVY_TREND_MONTHS:
        sma_now = float(monthly.rolling(IVY_TREND_MONTHS).mean().iloc[-1])
        if spy_now >= sma_now:
            ampel = "yellow" if (vix and vix > IVY_VIX_THRESHOLD) else "green"
    meta = {
        "green": ("🟢 GRÜN", "Voll investiert"),
        "yellow": ("🟡 GELB", f"Defensiv — {int(IVY_YELLOW_SAFE_W * 100)}% {IVY_SAFE_ASSET}"),
        "red": ("🔴 ROT", f"100% {IVY_SAFE_ASSET} — alle Aktien verkaufen!"),
    }
    label, aktion = meta[ampel]
    spy_vs = round((spy_now / sma_now - 1) * 100, 2) if sma_now else None
    return {"ampel": ampel, "label": label, "aktion": aktion,
            "spy": spy_now, "sma": sma_now, "vix": vix, "spy_vs_sma_pct": spy_vs}


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


def fmt_pct(val):
    """Prozent-Anzeige; None/NaN → — (nicht 'None' in Streamlit)."""
    if val is None:
        return "—"
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return "—"
        return f"{v:+.1f}%"
    except (TypeError, ValueError):
        return "—"


# ── JSON laden ────────────────────────────────────────────────────────────────

_JSON_REFRESH = st.session_state.json_refresh

_kass_raw = lade_json_github("kassandra_positionen.json", _JSON_REFRESH) or {}
KASSANDRA_POS = positions_merged(_kass_raw)
KASS_CRASH_PCT = kassandra_crash_exit_pct(_kass_raw)
SP100_POS = lade_json_github("sp100_positionen.json", _JSON_REFRESH) or {}
_ivy_raw = lade_json_github("ivy_portfolio.json", _JSON_REFRESH) or {}
IVY_POS = portfolio_ohne_meta(_ivy_raw)
_etf_raw = lade_json_github("etf_eingabe.json", _JSON_REFRESH) or {}
ETF_POS, ETF_TS = parse_etf_portfolio(_etf_raw)
ETF_STATE = lade_json_github("portfolio_state.json", _JSON_REFRESH) or {}
_etf_eodhd_raw = lade_json_github("etf_eodhd_eingabe.json", _JSON_REFRESH) or {}
ETF_EODHD_POS, ETF_EODHD_TS = parse_etf_portfolio(_etf_eodhd_raw)
ETF_EODHD_STATE = lade_json_github("portfolio_state_eodhd.json", _JSON_REFRESH) or {}
_sc_raw = lade_json_github("smallcap_positionen.json", _JSON_REFRESH) or {}
SMALLCAP_POS = portfolio_ohne_meta(_sc_raw)
_haa_raw = lade_json_github("haa_balanced_positionen.json", _JSON_REFRESH) or {}
_RM_RAW = lade_json_github("regime_momentum_positionen.json", _JSON_REFRESH) or {}
_REGIME_RAW = lade_json_github("kassandra_regime_live.json", _JSON_REFRESH) or {}
SP100_DEPOT = sp100_depot_ticker(SP100_POS)

# ── Trailing-Stop Zeilen ──────────────────────────────────────────────────────

def build_stop_rows():
    rows = []

    # Kassandra — 20% Trailing + Crash Exit (≥8% Tagesverlust)
    ci = check_info("kassandra")
    for ticker, p in KASSANDRA_POS.items():
        kauf = position_entry(p)
        hoch = position_high(p, kauf)
        if not kauf:
            continue
        tk = ticker_fix(ticker)
        q = eodhd_quote(tk)
        kurs = q["close"] if q else None
        if not kurs:
            kurs = kauf
            q = None
        stop = round(hoch * (1 - STOP_CFG["kassandra"]["pct"]), 2)
        puf = puffer_pct(kurs, stop)
        tages_ret = kass_tages_return_pct(ticker)
        akt_label = "Einstieg" if not q else None
        row = {
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige("kassandra"),
            **signal_spalten("kassandra", ci, _kass_raw),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker,
            "Name": p.get("name") or "—",
            "Akt. Kurs": format_akt_kurs(
                kurs, ticker, q, fallback_label=akt_label,
            ),
            "Peak/Hoch": format_kurs(hoch, ticker),
            "Stop-Kurs": format_kurs(stop, ticker),
            "% zum Stop": fmt_pct(puf),
            "Status": kassandra_status(puf, tages_ret, KASS_CRASH_PCT),
        }
        if KASS_CRASH_PCT:
            row["Tages %"] = fmt_pct(tages_ret)
        rows.append(row)

    # S&P 100 — RSL-Peak-Trail 35% (RSL-Werte, nicht EUR/USD-Kurs!)
    ci = check_info("sp100")
    rsl_data = SP100_POS.get("rsl_data", {})
    for ticker, info in rsl_data.items():
        if SP100_DEPOT is not None and ticker not in SP100_DEPOT:
            continue
        live = _sp100_live_rsl(ticker, info)
        trail = live.get("trail")
        rsl_now = live.get("rsl", 0)
        puf = live.get("puffer")
        if trail is None:
            continue
        kurs_live = safe_float(eodhd_kurs(ticker_fix(ticker)))
        q_usd = eodhd_quote(ticker_fix(ticker))
        abst_hoch = info.get("abst_hoch_pct")
        if abst_hoch is None and kurs_live:
            kurs_hoch = safe_float(info.get("kurs_hoch_usd"))
            if kurs_hoch:
                abst_hoch = round((kurs_live / kurs_hoch - 1) * 100, 1)
        kurs_anzeige = f"RSL {rsl_now:.3f}"
        if kurs_live:
            usd_teil = format_akt_kurs(kurs_live, ticker, q_usd)
            kurs_anzeige += f"  |  {usd_teil}"
        if abst_hoch is not None:
            kurs_anzeige += f"  ({abst_hoch:+.1f}% Hoch)"
        name = info.get("name") or ""
        ticker_anzeige = f"{ticker} · {name}" if name else ticker
        kurs_hoch = safe_float(info.get("kurs_hoch_usd"))
        peak_anzeige = f"${kurs_hoch:.2f}" if kurs_hoch else "—"
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige("sp100"),
            **signal_spalten("sp100", ci, SP100_POS),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker_anzeige,
            "Name": name or "—",
            "Akt. Kurs": kurs_anzeige,
            "Peak/Hoch": peak_anzeige,
            "Stop-Kurs": f"RSL {trail:.3f}",
            "% zum Stop": f"{puf:+.1f}% (RSL)" if puf is not None else "—",
            "Status": live.get("status") or status_icon(puf, 10),
        })

    # IVY — 15% Trailing unter Peak in EUR (wie Ivy_2.1.ipynb)
    ci = check_info("ivy")
    for tk, p in IVY_POS.items():
        if tk in IVY_TS_EXCLUDE or not p.get("entry_price"):
            continue
        peak = ivy_peak(p)
        if not peak:
            continue
        kurs, ksrc, q = ivy_eur_kurs(tk, p, peak)
        if kurs is None:
            kurs, ksrc, q = peak, "?", None
        stop = round(peak * (1 - STOP_CFG["ivy"]["pct"]), 2)
        puf = puffer_pct(kurs, stop)
        peak_abst = puffer_pct(kurs, peak)  # Abstand zum Peak in %
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige("ivy"),
            **signal_spalten("ivy", ci, _ivy_raw),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": tk,
            "Name": p.get("name") or "—",
            "Akt. Kurs": format_akt_kurs(
                kurs, tk, q, extra=ksrc if ksrc else None, currency="EUR",
            ),
            "Peak/Hoch": f"{peak:.2f} €",
            "Stop-Kurs": f"{stop:.2f} €",
            "% vom Peak": fmt_pct(peak_abst),
            "% zum Stop": fmt_pct(puf),
            "Status": ivy_status(puf, p),
        })

    # ETF Aktien — 10% Trailing (native Währung, wie ETF Ampel_2)
    _append_etf_stop_rows(rows, ETF_POS, ETF_STATE, ETF_TS, "etf", _etf_raw)
    _append_etf_stop_rows(
        rows, ETF_EODHD_POS, ETF_EODHD_STATE, ETF_EODHD_TS, "etf_eodhd", _etf_eodhd_raw,
    )

    # Small Cap EU — 25% Trailing Stop (vom High-Water)
    ci = check_info("smallcap")
    sc_ts = STOP_CFG["smallcap"]["pct"]
    for isin, p in SMALLCAP_POS.items():
        kauf = safe_float(p.get("buy_price") or p.get("einstieg"))
        hw = safe_float(p.get("high_water") or p.get("hoch") or kauf)
        if not kauf:
            continue
        ticker = p.get("ticker") or isin
        tk = ticker_fix(ticker)
        q = eodhd_quote(tk)
        kurs = q["close"] if q else None
        if not kurs:
            kurs = kauf
            q = None
        hw = max(hw, kurs)
        stop = round(hw * (1 - sc_ts), 2)
        puf = puffer_pct(kurs, stop)
        status = status_icon(puf)
        sc_name = _sc_name(ticker=ticker, pos=p, isin=isin)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige("smallcap"),
            **signal_spalten("smallcap", ci, _sc_raw),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker,
            "Name": sc_name or "—",
            "Akt. Kurs": format_akt_kurs(kurs, ticker, q, fallback_label=None if q else "Einstieg"),
            "Peak/Hoch": format_kurs(hw, ticker),
            "Stop-Kurs": format_kurs(stop, ticker),
            "% zum Stop": fmt_pct(puf),
            "Status": status,
        })

    return rows


_JSON_BY_STRATEGY = {
    "kassandra": lambda: _kass_raw,
    "sp100": lambda: SP100_POS,
    "ivy": lambda: _ivy_raw,
    "etf": lambda: _etf_raw,
    "etf_eodhd": lambda: _etf_eodhd_raw,
    "smallcap": lambda: _sc_raw,
    "haa": lambda: _haa_raw,
}

_TXN_PRIO = {"Sofort": 0, "Hoch": 1, "Normal": 2, "Plan": 3}


def _txn_row(key, aktion, ticker, name, grund, prioritaet="Normal"):
    ci = check_info(key)
    return {
        "Strategie": ci["label"],
        "Priorität": prioritaet,
        "Aktion": aktion,
        "Ticker": ticker or "—",
        "Name": name or "—",
        "Grund / Details": grund,
        **signal_spalten(key, ci, _JSON_BY_STRATEGY[key]()),
        "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
        "_sort": _TXN_PRIO.get(prioritaet, 9),
    }


def _etf_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or []


def _append_etf_stop_rows(rows, pos, state, ts, key, raw):
    """Trailing-Stop-Zeilen für ETF Yahoo oder EODHD."""
    ci = check_info(key)
    state_pos = state.get("positionen", {}) if isinstance(state, dict) else {}
    for ticker, pos_item in pos.items():
        if not isinstance(pos_item, dict):
            continue
        if state_pos and ticker not in state_pos:
            continue
        kauf_eur = pos_item.get("kauf_kurs", 0)
        if not kauf_eur or kauf_eur < 0.01:
            continue
        q = eodhd_quote(ticker)
        kurs = safe_float(q["close"]) if q else None
        st = state_pos.get(ticker, {})
        hoch = (
            safe_float(st.get("hoch_kurs"))
            or safe_float(pos_item.get("hoch_kurs"))
            or kurs
        )
        stop = safe_float(st.get("stop_level")) or safe_float(pos_item.get("stop_nativ"))
        if stop is None and hoch:
            stop = round(hoch * (1 - ts), 2)
        kurs_f = kurs or hoch
        puf = puffer_pct(kurs_f, stop)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige(key),
            **signal_spalten(key, ci, raw),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker.replace(".US", "").replace(".TO", ""),
            "Name": pos_item.get("name") or "—",
            "Akt. Kurs": format_akt_kurs(kurs_f, ticker, q),
            "Peak/Hoch": format_kurs(hoch, ticker),
            "Stop-Kurs": format_kurs(stop, ticker),
            "% zum Stop": fmt_pct(puf),
            "Status": status_icon(puf, 3),
        })


def _append_etf_transaction_rows(add, etf_raw, etf_state, etf_pos, etf_ts, key):
    """Transaktionszeilen für ETF Yahoo oder EODHD."""
    state_pos = etf_state.get("positionen", {}) if isinstance(etf_state, dict) else {}
    active = set(state_pos.keys()) if state_pos else set(etf_pos.keys())
    for ticker, pos in etf_pos.items():
        if not isinstance(pos, dict):
            continue
        if state_pos and ticker not in state_pos:
            continue
        if not pos.get("kauf_kurs"):
            continue
        q = eodhd_quote(ticker)
        kurs = safe_float(q["close"]) if q else None
        st = state_pos.get(ticker, {})
        hoch = (
            safe_float(st.get("hoch_kurs"))
            or safe_float(pos.get("hoch_kurs"))
            or kurs
        )
        stop = safe_float(st.get("stop_level")) or safe_float(pos.get("stop_nativ"))
        if stop is None and hoch:
            stop = round(hoch * (1 - etf_ts), 2)
        kurs_f = kurs or hoch
        puf = puffer_pct(kurs_f, stop)
        if puf is not None and puf <= 0:
            add(
                key, "🔴 VERKAUFEN",
                ticker.replace(".US", "").replace(".TO", ""),
                pos.get("name") or "",
                f"10% Trailing Stop ({fmt_pct(puf)} zum Stop)",
                "Sofort",
            )
    etf_ha = _etf_handels_aus_json(etf_raw)
    if etf_ha:
        for rec in etf_ha:
            if not isinstance(rec, dict):
                continue
            aktion = str(rec.get("aktion") or "")
            if "HALTEN" in aktion:
                continue
            ticker = rec.get("ticker") or ""
            delta = rec.get("delta_eur")
            parts = ["Monats-Rebalancing"]
            if delta is not None:
                parts.append(f"Δ {delta:+,.0f} €")
            if rec.get("ziel_eur") is not None:
                parts.append(f"Ziel {rec['ziel_eur']:,.0f} €")
            if rec.get("aktuell_eur") is not None:
                parts.append(f"ist {rec['aktuell_eur']:,.0f} €")
            add(
                key, aktion or "—",
                ticker.replace(".US", "").replace(".TO", ""),
                rec.get("name") or "",
                " · ".join(parts),
                "Plan",
            )
    else:
        for rec in (etf_raw.get("empfehlung") or [] if isinstance(etf_raw, dict) else []):
            if not isinstance(rec, dict):
                continue
            ticker = rec.get("ticker")
            if not ticker or ticker in active:
                continue
            score = rec.get("score")
            score_s = f"Score {score:.2f}" if score is not None else "Screening-Kandidat"
            add(
                key, "🟢 KAUFEN", ticker.replace(".US", "").replace(".TO", ""),
                rec.get("name") or "",
                f"Monats-Rebalancing · {score_s} (noch nicht im Portfolio)",
                "Plan",
            )


def _smallcap_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or []


def _kass_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or []


def _haa_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or []


_WARUM_COLS = (
    "rang", "ticker", "name", "bereich", "score", "momentum_pct", "momentum",
    "ziel_gewicht", "gewicht", "status", "begruendung", "etf", "quelle_etf",
    "aktie_code", "aktie_name", "rsl", "rsl_hoch", "trail_stop", "puffer_pct",
    "aktion", "komponente", "wert", "abst_hoch_pct", "einstieg_eur", "peak_eur",
)

_KASS_DEPOT_COLS = (
    "rang", "ticker", "name", "bereich", "gewicht",
    "einstieg_eur", "peak_eur", "status", "begruendung",
)

_WARUM_EXPANDER_TITEL = {
    "haa": "Warum diese ETFs?",
    "etf": "Warum diese Aktien?",
    "etf_eodhd": "Warum diese Aktien?",
    "smallcap": "Warum diese Auswahl?",
    "regime_momentum": "Ranking & Ziel-Portfolio",
}

_RM_RANK_COLS = (
    "rang", "ticker", "name", "score", "im_portfolio", "top_n", "exit_zone",
)


def _warum_caption(raw):
    parts = []
    for k in ("regel_text", "regime_label", "hinweis", "momentum_methode"):
        v = raw.get(k)
        if v:
            parts.append(str(v))
    return "\n\n".join(parts)


def _warum_df(records, preferred_cols=None):
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    pref = preferred_cols or _WARUM_COLS
    cols = [c for c in pref if c in df.columns]
    if not cols:
        cols = list(df.columns)
    return df[cols]


def _etf_empfehlung_table(raw):
    emp = raw.get("empfehlung") or []
    if not emp:
        return []
    ziel = {
        p.get("ticker") for p in (raw.get("positionen") or [])
        if isinstance(p, dict) and p.get("ticker")
    }
    ziel_norm = {str(t).replace(".US", "").replace(".TO", "") for t in ziel}
    ha_tk = {
        str(h.get("ticker", "")).replace(".US", "").replace(".TO", "")
        for h in (raw.get("handelsanweisungen") or [])
        if isinstance(h, dict) and "KAUF" in str(h.get("aktion", "")).upper()
    }
    rows = []
    for i, rec in enumerate(
        sorted(emp, key=lambda x: -(safe_float(x.get("score")) or -999)), 1,
    ):
        tk = rec.get("ticker") or ""
        tk_short = tk.replace(".US", "").replace(".TO", "")
        sc = rec.get("score")
        if tk in ziel or tk_short in ziel_norm:
            status = "IM DEPOT"
        elif tk_short in ha_tk:
            status = "KAUF-SIGNAL"
        else:
            status = "KANDIDAT"
        begr = rec.get("begruendung")
        if not begr and sc is not None:
            quelle = rec.get("etf") or "?"
            begr = f"Score {sc:+.4f} · Top aus {quelle}"
        rows.append({
            "rang": i,
            "ticker": tk_short,
            "name": rec.get("name") or "",
            "score": sc,
            "quelle_etf": rec.get("etf") or "—",
            "status": status,
            "begruendung": begr or "Screening-Kandidat",
        })
    return rows


def _sp100_rsl_table(raw):
    rsl_data = raw.get("rsl_data") or {}
    if not rsl_data:
        return []
    depot = sp100_depot_ticker(raw)
    items = [
        (tk, info) for tk, info in rsl_data.items()
        if isinstance(info, dict) and (depot is None or tk in depot)
    ]
    items.sort(key=lambda x: -(safe_float(_sp100_live_rsl(x[0], x[1]).get("rsl")) or 0))
    rows = []
    for i, (tk, info) in enumerate(items, 1):
        live = _sp100_live_rsl(tk, info)
        trail = live.get("trail")
        puf = live.get("puffer")
        rsl = live.get("rsl")
        if trail is not None and puf is not None:
            begr = f"RSL-Peak-Trail 35% · Stop RSL {trail:.3f} · Puffer {puf:+.1f}% (live)"
        else:
            begr = "RSL-Werte aus Colab (35% Peak-Trail)"
        rows.append({
            "rang": i,
            "ticker": tk,
            "name": info.get("name") or "",
            "rsl": rsl,
            "rsl_hoch": live.get("rsl_peak") or info.get("rsl_peak") or info.get("rsl_hoch"),
            "trail_stop": round(trail, 3) if trail is not None else None,
            "puffer_pct": puf,
            "abst_hoch_pct": info.get("abst_hoch_pct"),
            "status": "DEPOT",
            "begruendung": begr,
        })
    return rows


def _kassandra_depot_table(raw):
    """Live-Depot aus kassandra_positionen.json (Ticker als Top-Level-Keys)."""
    positions = portfolio_ohne_meta(raw)
    inferred_gew = _kass_infer_gewichte(positions)
    rows = []
    for i, (ticker, p) in enumerate(sorted(positions.items()), 1):
        if not isinstance(p, dict):
            continue
        einstieg = position_entry(p)
        stored_peak = position_high(p, einstieg)
        peak = _kass_live_peak(ticker, stored_peak, einstieg)
        kdat = p.get("kaufdatum") or p.get("datum") or "—"
        bereich_key = _kass_bereich_for(ticker, p)
        bereich = _KASS_BEREICH_LABEL.get(bereich_key, bereich_key) if bereich_key else "—"
        gew = safe_float(p.get("gewicht") or p.get("gew") or p.get("weight"))
        if not gew:
            gew = inferred_gew.get(ticker)
        gew_s = f"{gew * 100:.1f}%" if gew else "—"
        begr = f"Kauf {kdat}"
        if einstieg:
            begr += f" · Einstieg {format_kurs(einstieg, ticker)}"
        if bereich and bereich != "—":
            begr += f" · {bereich}"
        rows.append({
            "rang": i,
            "ticker": ticker,
            "name": p.get("name") or "",
            "bereich": bereich,
            "gewicht": gew_s,
            "einstieg_eur": format_kurs(einstieg, ticker) if einstieg else "—",
            "peak_eur": format_kurs(peak, ticker) if peak else "—",
            "status": "DEPOT",
            "begruendung": begr,
        })
    return rows


def _kassandra_score_table(raw):
    details = raw.get("score_details")
    if isinstance(details, list) and details:
        return details
    if isinstance(details, dict):
        return [
            {"komponente": k, "wert": v, "begruendung": "Score-Komponente"}
            for k, v in details.items()
        ]
    rows = []
    score = raw.get("score_smooth") or raw.get("score") or raw.get("score_heute")
    ampel = raw.get("kassandra_ampel")
    if score is not None:
        rows.append({
            "komponente": "Kassandra-Score",
            "wert": score,
            "begruendung": f"Ampel: {ampel}" if ampel else "Gesamt-Score",
        })
    for rec in raw.get("handelsanweisungen") or []:
        if not isinstance(rec, dict):
            continue
        rows.append({
            "ticker": rec.get("ticker") or rec.get("isin") or "",
            "name": rec.get("name") or "",
            "aktion": rec.get("aktion") or "",
            "begruendung": rec.get("grund") or "Handelsanweisung",
        })
    return rows


def _handels_grund_table(orders):
    rows = []
    for i, o in enumerate(orders, 1):
        if not isinstance(o, dict):
            continue
        act = str(o.get("action") or o.get("aktion") or "")
        if "HALTEN" in act.upper():
            continue
        grund = o.get("grund") or ""
        if not grund and o.get("prev") is not None:
            grund = f"Gewicht {o.get('prev', 0):.1%} → {o.get('new', 0):.1%}"
        rows.append({
            "rang": i,
            "ticker": o.get("ticker") or "",
            "name": o.get("name") or "",
            "aktion": act.replace("🟢 ", "").replace("🔴 ", "").strip() or act,
            "begruendung": grund or "Rebalancing",
        })
    return rows


def _smallcap_depot_table(raw):
    """Live-Depot aus smallcap_positionen.json."""
    rows = []
    for i, (isin, p) in enumerate(sorted(portfolio_ohne_meta(raw).items()), 1):
        if not isinstance(p, dict):
            continue
        ticker = p.get("ticker") or isin
        name = _sc_name(ticker=ticker, pos=p, isin=isin) or p.get("name") or ""
        kauf = p.get("buy_price") or p.get("einstieg")
        hw = p.get("high_water") or p.get("hoch")
        kdat = p.get("buy_date") or p.get("kaufdatum") or "—"
        rows.append({
            "rang": i,
            "ticker": ticker,
            "name": name,
            "einstieg_eur": kauf,
            "peak_eur": hw,
            "status": "DEPOT",
            "begruendung": f"Kauf {kdat}" + (f" · {kauf} €" if kauf else ""),
        })
    return rows


def _ivy_depot_table(raw):
    """Live-Depot aus ivy_portfolio.json für Expander."""
    rows = []
    for i, (tk, p) in enumerate(sorted(portfolio_ohne_meta(raw).items()), 1):
        if not isinstance(p, dict):
            continue
        einstieg = p.get("entry_price") or p.get("einstieg") or p.get("kauf_kurs")
        peak = p.get("peak_price") or p.get("hoch")
        kdat = p.get("entry_date") or p.get("kauf_datum") or "—"
        rows.append({
            "rang": i,
            "ticker": tk,
            "name": p.get("name") or "",
            "einstieg_eur": einstieg,
            "peak_eur": peak,
            "status": "DEPOT",
            "begruendung": f"Kauf {kdat}" + (f" · {einstieg} €" if einstieg else ""),
        })
    return rows


def _warum_sections(raw, key):
    """Expander-Inhalte je Strategie — nutzt Colab-JSON (HAA-Stil oder Fallbacks)."""
    if not isinstance(raw, dict):
        return []
    sections = []
    caption = _warum_caption(raw)

    for field, title in (
        ("vergleich_offensiv", "Offensive"),
        ("vergleich_defensiv", "Defensive"),
        ("vergleich_kandidaten", "Kandidaten"),
        ("vergleich", "Auswahl"),
        ("screening_detail", "Screening"),
    ):
        rows = raw.get(field)
        if isinstance(rows, list) and rows:
            cap = caption if not sections else ""
            sections.append((title, cap, rows, _WARUM_COLS))
            caption = ""

    if key in ("etf", "etf_eodhd"):
        emp_rows = _etf_empfehlung_table(raw)
        if emp_rows and not any(s[0] == "Screening" for s in sections):
            cap = caption if not sections else ""
            sections.append(("Ziel-Portfolio (Screening)", cap, emp_rows, _WARUM_COLS))
            caption = ""

    if key == "sp100" and not sections:
        rsl_rows = _sp100_rsl_table(raw)
        if rsl_rows:
            regel = (
                "Regel: RSL-Peak-Trail 35% — Verkauf wenn RSL 35% unter "
                "eigenem RSL-Hoch fällt (nicht Kurs-Trailing)."
            )
            cap = f"{regel}\n\n{caption}" if caption else regel
            sections.append(("Depot · RSL-Stand", cap, rsl_rows, _WARUM_COLS))

    if key == "kassandra":
        src = raw.get("ampel_source", "")
        src_s = " · Kassandra Regime" if src == "kassandra_regime" else ""
        pct = raw.get("invest_pct")
        pct_s = f" · Quote {int(round(float(pct) * 100))}%" if pct is not None else ""
        regel = (
            f"Regel: Kassandra-Ampel (Score) wählt Slots{src_s}{pct_s} · "
            "20% Trailing Stop + optional Crash Exit."
        )
        depot_rows = _kassandra_depot_table(raw)
        if depot_rows:
            cap = f"{regel}\n\n{caption}" if caption else regel
            sections.append(("Mein Depot", cap, depot_rows, _KASS_DEPOT_COLS))
        k_rows = _kassandra_score_table(raw)
        if k_rows:
            cap = "" if depot_rows else (f"{regel}\n\n{caption}" if caption else regel)
            sections.append(("Ampel & Handelsplan", cap, k_rows, _WARUM_COLS))

    if key == "ivy":
        regel = (
            "Regel: TAA-Ampel (SPY/VIX) · Quality-Momentum je Region "
            "(US/EU/APAC) · 15% Trailing nach 10d Warmup."
        )
        cap = regel
        if _ivy_orders_stale_hinweis(raw):
            cap += (
                "\n\n⚠️ JSON-Handelsanweisungen = Backtest-Allokation (Vormonat→aktuell), "
                "nicht Portfolio-Tracker. Maßgeblich: Colab „UMSCHICHTUNGS-ANALYSE“."
            )
        depot_rows = _ivy_depot_table(raw)
        if depot_rows:
            sections.append(("Mein Depot", cap if not sections else "", depot_rows, _WARUM_COLS))
        plaus = _handels_grund_table(_ivy_orders_aus_json(raw))
        if plaus:
            sections.append((
                "Plausible Trades (JSON, gefiltert)",
                "" if sections else cap,
                plaus,
                _WARUM_COLS,
            ))
        elif _ivy_orders_roh(raw) and not plaus:
            sections.append((
                "Handelsplan",
                ("" if sections else cap)
                + "\n\nℹ️ Keine plausiblen Trades im JSON — aktuell nur in Colab sichtbar "
                "(z. B. FLEX.US verkaufen).",
                [],
                _WARUM_COLS,
            ))

    if key == "smallcap":
        regel = (
            "Regel: Exit-only · TS 25% · EMA100 −5% · Ampel nur Quote (kein Ranking-Verkauf)."
        )
        depot_rows = _smallcap_depot_table(raw)
        if depot_rows:
            sections.append(("Mein Depot", regel if not sections else "", depot_rows, _WARUM_COLS))
        sc_orders = _smallcap_handels_aus_json(raw)
        sc_rows = _handels_grund_table(sc_orders)
        isin_by_ticker = {
            str(o.get("ticker")): o.get("isin")
            for o in sc_orders if isinstance(o, dict) and o.get("ticker")
        }
        for row in sc_rows:
            if row.get("name"):
                continue
            tk = row.get("ticker")
            row["name"] = _sc_name(ticker=tk, isin=isin_by_ticker.get(tk)) or "—"
        if sc_rows:
            sections.append(("Rebalancing-Plan", "" if sections else regel, sc_rows, _WARUM_COLS))

    if key == "regime_momentum":
        regel = raw.get("regel_text") or ""
        gross = raw.get("gross_exposure")
        gross_s = f" · Brutto {gross:.0%}" if gross is not None else ""
        pct = raw.get("invest_pct")
        pct_s = f" · Regime-Quote {int(round(float(pct) * 100))}%" if pct is not None else ""
        regel_full = f"Regel: {regel}{pct_s}{gross_s}" if regel else ""
        cap = _warum_caption(raw)
        ziel_rows = [
            {
                "rang": i,
                "ticker": z.get("ticker"),
                "name": z.get("name"),
                "gewicht": z.get("gewicht"),
                "score": z.get("score"),
                "status": "ZIEL",
                "begruendung": f"~€{z.get('ziel_eur', 0):,}" if z.get("ziel_eur") else "",
            }
            for i, z in enumerate(raw.get("ziel") or [], 1)
            if isinstance(z, dict)
        ]
        if ziel_rows:
            cap_z = f"{regel_full}\n\n{cap}" if regel_full and cap else (regel_full or cap)
            sections.append(("Ziel-Portfolio", cap_z, ziel_rows, _WARUM_COLS))
        rankings = raw.get("rankings") or []
        if rankings:
            rank_rows = [
                {
                    **r,
                    "im_portfolio": "✓" if r.get("im_portfolio") else "—",
                    "top_n": "✓" if r.get("top_n") else "—",
                    "exit_zone": "✓" if r.get("exit_zone") else "—",
                }
                for r in rankings if isinstance(r, dict)
            ]
            sections.append(("Top-50 Ranking", "", rank_rows, _RM_RANK_COLS))

    return sections


def render_warum_expanders(txn_json):
    """Expander „Warum?“ für alle Strategien mit JSON-Erklärungsdaten."""
    for key in ("haa", "regime_momentum", "kassandra", "sp100", "ivy", "etf", "etf_eodhd", "smallcap"):
        raw = txn_json.get(key) or {}
        sections = _warum_sections(raw, key)
        if not sections:
            continue
        label = CHECK_ZEITEN[key]["label"]
        titel = _WARUM_EXPANDER_TITEL.get(key, "Warum diese Auswahl?")
        with st.expander(f"{label} — {titel} (aus JSON)"):
            if key == "ivy" and _ivy_orders_stale_hinweis(raw):
                st.warning(
                    "IVY-JSON: Handelsanweisungen passen nicht zum Depot — "
                    "Colab „UMSCHICHTUNGS-ANALYSE“ ist maßgeblich."
                )
            for title, cap, records, cols in sections:
                if cap:
                    st.caption(cap)
                if title:
                    st.markdown(f"**{title}**")
                if not records:
                    continue
                df = _warum_df(records, cols)
                if not df.empty:
                    st.dataframe(df, use_container_width=True, hide_index=True)


def _sp100_txn_count(sp100_pos):
    if not isinstance(sp100_pos, dict):
        return 0
    n = len(sp100_pos.get("verkaufen") or []) + len(sp100_pos.get("kaufen") or [])
    depot = sp100_depot_ticker(sp100_pos)
    for ticker, info in (sp100_pos.get("rsl_data") or {}).items():
        if not isinstance(info, dict):
            continue
        if depot is not None and ticker not in depot:
            continue
        puf = _sp100_live_rsl(ticker, info).get("puffer")
        if puf is not None and puf <= 0:
            n += 1
    return n


def count_open_signals(raw, quelle="ivy"):
    """Anzahl offener Handels-Signale (ohne HALTEN)."""
    if not isinstance(raw, dict):
        return 0
    n = len(_handels_aktionen(raw, quelle))
    if quelle == "kassandra" and n == 0:
        n = len(raw.get("verkaufen") or []) + len(raw.get("kaufen") or [])
    if quelle == "sp100":
        n = _sp100_txn_count(raw)
    if quelle == "haa" and n == 0:
        n = len(raw.get("verkaufen") or []) + len(raw.get("kaufen") or [])
    if quelle == "regime_momentum" and n == 0:
        n = len(raw.get("verkaufen") or []) + len(raw.get("kaufen") or [])
    return n


def build_strategy_status(txn_json):
    """Übersicht aller Strategien — auch wenn keine Transaktion ansteht."""
    tj = txn_json or {}
    rows = []

    kass = tj.get("kassandra", _kass_raw) or {}
    kass_pos = positions_merged(kass)
    kass_n = sum(1 for p in kass_pos.values() if position_entry(p))
    kass_sig = count_open_signals(kass, "kassandra")
    rows.append({
        "Strategie": CHECK_ZEITEN["kassandra"]["label"],
        "JSON-Stand": format_letztes_json(kass),
        "Depot / Ziel": f"{kass_n} Position(en)" if kass_n else "— (kein Depot in JSON)",
        "Offene Signale": kass_sig,
        "Trailing Stop": stop_pct_anzeige("kassandra"),
        "Status": (
            f"⚠️ {kass_sig} Signal(e)" if kass_sig
            else (
                "⚠️ JSON ohne Positionen — INVESTMENT ONLY ONE ausführen"
                if kass and kass_n == 0
                else ("⚠️ JSON leer" if not kass else "✅ Keine Aktion")
            )
        ),
    })

    sp = tj.get("sp100", SP100_POS) or {}
    rsl_n = len(sp.get("rsl_data") or {})
    depot_n = len(sp.get("meine_aktien") or [])
    sp_sig = count_open_signals(sp, "sp100")
    rows.append({
        "Strategie": CHECK_ZEITEN["sp100"]["label"],
        "JSON-Stand": format_letztes_json(sp),
        "Depot / Ziel": f"{depot_n} Depot · {rsl_n} RSL" if rsl_n else f"{depot_n} Depot · kein rsl_data",
        "Offene Signale": sp_sig,
        "Trailing Stop": stop_pct_anzeige("sp100"),
        "Status": (
            f"⚠️ {sp_sig} Signal(e)" if sp_sig
            else ("⚠️ rsl_data fehlt" if depot_n and not rsl_n else "✅ Keine Aktion")
        ),
    })

    rm = tj.get("regime_momentum", _RM_RAW) or {}
    rm_ziel = rm.get("ziel_ticker") or []
    rm_sig = count_open_signals(rm, "regime_momentum")
    rm_meine = rm.get("meine_aktien") or []
    gross = rm.get("gross_exposure")
    gross_s = f" · {gross:.0%} investiert" if gross is not None else ""
    if not rm_sig and rm_ziel and set(rm_meine) == set(rm_ziel):
        rm_status = f"✅ Depot = Ziel ({len(rm_ziel)} Titel){gross_s}"
    elif rm_sig:
        rm_status = f"⚠️ {rm_sig} Signal(e)"
    elif not rm_ziel:
        rm_status = "⚠️ JSON fehlt — _regime_momentum_live.py in Colab"
    else:
        rm_status = f"✅ Ziel: {len(rm_ziel)} Titel{gross_s}"
    rows.append({
        "Strategie": CHECK_ZEITEN["regime_momentum"]["label"],
        "JSON-Stand": format_letztes_json(rm),
        "Depot / Ziel": (
            f"Depot {len(rm_meine)} · Ziel {len(rm_ziel)}"
            if rm_meine or rm_ziel else "— (_regime_momentum_live.py)"
        ),
        "Offene Signale": rm_sig,
        "Trailing Stop": stop_pct_anzeige("regime_momentum"),
        "Status": rm_status,
    })

    haa = tj.get("haa", _haa_raw) or {}
    ziel = haa.get("ziel_ticker") or []
    haa_sig = count_open_signals(haa, "haa")
    meine = haa.get("meine_aktien") or []
    ziel_set = set(ziel)
    meine_set = set(meine)
    if not haa_sig and ziel and meine_set == ziel_set:
        haa_status = f"✅ Depot = Ziel ({', '.join(ziel)}) — kein Trade nötig"
    elif haa_sig:
        haa_status = f"⚠️ {haa_sig} Signal(e)"
    elif not ziel:
        haa_status = "⚠️ JSON fehlt"
    else:
        haa_status = f"✅ Ziel: {', '.join(ziel)}"
    rows.append({
        "Strategie": CHECK_ZEITEN["haa"]["label"],
        "JSON-Stand": format_letztes_json(haa),
        "Depot / Ziel": (
            f"Depot {', '.join(meine)} · Ziel {', '.join(ziel)}"
            if meine and ziel else (", ".join(ziel) if ziel else "— (HAA_Live.ipynb ausführen)")
        ),
        "Offene Signale": haa_sig,
        "Trailing Stop": stop_pct_anzeige("haa"),
        "Status": haa_status,
    })

    for key in ("ivy", "etf", "etf_eodhd", "smallcap"):
        if key == "ivy":
            raw = tj.get("ivy", _ivy_raw) or {}
            dep = len(positions_merged(raw))
        elif key == "etf":
            raw = tj.get("etf", _etf_raw) or {}
            dep = len(ETF_POS)
        elif key == "etf_eodhd":
            raw = tj.get("etf_eodhd", _etf_eodhd_raw) or {}
            dep = len(ETF_EODHD_POS)
        else:
            raw = tj.get("smallcap", _sc_raw) or {}
            dep = len(positions_merged(raw))
        sig = count_open_signals(raw, key)
        rows.append({
            "Strategie": CHECK_ZEITEN[key]["label"],
            "JSON-Stand": format_letztes_json(raw),
            "Depot / Ziel": f"{dep} Position(en)" if dep else "—",
            "Offene Signale": sig,
            "Trailing Stop": stop_pct_anzeige(key),
            "Status": f"⚠️ {sig} Signal(e)" if sig else "✅ Keine Aktion",
        })

    return rows


def build_transaction_rows(ivy_ampel=None, txn_json=None):
    """Anstehende Trades aus JSON + Live-Stops."""
    tj = txn_json or {}
    kass_raw = tj.get("kassandra", _kass_raw)
    kass_pos = positions_merged(kass_raw)
    kass_crash = kassandra_crash_exit_pct(kass_raw)
    sp100_pos = tj.get("sp100", SP100_POS)
    sp100_depot = sp100_depot_ticker(sp100_pos)
    ivy_raw = tj.get("ivy", _ivy_raw)
    ivy_pos = portfolio_ohne_meta(ivy_raw)
    etf_raw = tj.get("etf", _etf_raw)
    etf_eodhd_raw = tj.get("etf_eodhd", _etf_eodhd_raw)
    sc_raw = tj.get("smallcap", _sc_raw)
    sc_pos = portfolio_ohne_meta(sc_raw)
    haa_raw = tj.get("haa", _haa_raw)
    rm_raw = tj.get("regime_momentum", _RM_RAW)
    etf_state = tj.get("etf_state", ETF_STATE)
    etf_eodhd_state = tj.get("etf_eodhd_state", ETF_EODHD_STATE)
    etf_pos, etf_ts = parse_etf_portfolio(etf_raw)
    etf_eodhd_pos, etf_eodhd_ts = parse_etf_portfolio(etf_eodhd_raw)

    rows = []
    seen = set()

    def add(key, aktion, ticker, name, grund, prioritaet="Normal"):
        sig = (key, (ticker or "").upper(), aktion[:8])
        if sig in seen:
            return
        seen.add(sig)
        rows.append(_txn_row(key, aktion, ticker, name, grund, prioritaet))

    # ── Kassandra: Stop / Crash → Sofort verkaufen ──
    for ticker, p in kass_pos.items():
        kauf = position_entry(p)
        hoch = position_high(p, kauf)
        if not kauf:
            continue
        q = eodhd_quote(ticker_fix(ticker))
        kurs = q["close"] if q else kauf
        stop = round(hoch * (1 - STOP_CFG["kassandra"]["pct"]), 2)
        puf = puffer_pct(kurs, stop)
        tages_ret = kass_tages_return_pct(ticker)
        name = p.get("name") or ""
        if kass_crash and tages_ret is not None and tages_ret <= -kass_crash * 100:
            add(
                "kassandra", "🔴 VERKAUFEN", ticker, name,
                f"Crash Exit {fmt_pct(tages_ret)} (≥ {int(kass_crash * 100)}%)",
                "Sofort",
            )
        elif puf is not None and puf <= 0:
            add(
                "kassandra", "🔴 VERKAUFEN", ticker, name,
                f"Trailing Stop ({fmt_pct(puf)} zum Stop)",
                "Sofort",
            )

    # ── Kassandra: Modell-Rebalancing aus JSON ──
    kass_ha = _kass_handels_aus_json(kass_raw)
    if kass_ha:
        for rec in kass_ha:
            if not isinstance(rec, dict):
                continue
            aktion = str(rec.get("aktion") or "")
            if "HALTEN" in aktion:
                continue
            ticker = rec.get("ticker") or ""
            parts = [rec.get("grund") or "Modell-Rebalancing"]
            if rec.get("rsl") is not None:
                parts.append(f"RSL {rec['rsl']:.3f}")
            if rec.get("betrag_eur") is not None:
                parts.append(f"Ziel {rec['betrag_eur']:,.0f} €")
            if rec.get("bereich"):
                parts.append(str(rec["bereich"]))
            add(
                "kassandra", aktion or "—", ticker, rec.get("name") or "",
                " · ".join(parts),
                rec.get("prioritaet") or "Plan",
            )
    else:
        for ticker in kass_raw.get("verkaufen") or [] if isinstance(kass_raw, dict) else []:
            p = kass_pos.get(ticker, {})
            add(
                "kassandra", "🔴 VERKAUFEN", ticker, p.get("name") or "",
                "Modell-Signal: nicht mehr im Portfolio", "Plan",
            )
        for ticker in kass_raw.get("kaufen") or [] if isinstance(kass_raw, dict) else []:
            p = kass_pos.get(ticker, {})
            add(
                "kassandra", "🟢 KAUFEN", ticker, p.get("name") or "",
                "Modell-Signal: neu aufgenommen", "Plan",
            )
    if isinstance(kass_raw, dict) and kass_raw.get("kassandra_ampel") == "red":
        score = kass_raw.get("score")
        score_s = f"Score {score:.0f}" if score is not None else "Score < 25"
        add(
            "kassandra", "🔴 ALLE VERKAUFEN", "—", "—",
            f"Ampel ROT — Cash-Regime ({score_s})",
            "Plan",
        )

    # ── S&P 100: JSON-Signale + RSL-Stop ──
    rsl_data = sp100_pos.get("rsl_data", {})
    for ticker in sp100_pos.get("verkaufen") or []:
        info = rsl_data.get(ticker, {})
        add(
            "sp100", "🔴 VERKAUFEN", ticker, info.get("name") or "",
            "Rebalancing: nicht mehr im Signal (Top-5)",
            "Plan",
        )
    for ticker in sp100_pos.get("kaufen") or []:
        info = rsl_data.get(ticker, {})
        rsl = info.get("rsl")
        rsl_s = f"RSL {rsl:.3f}" if rsl is not None else "—"
        add(
            "sp100", "🟢 KAUFEN", ticker, info.get("name") or "",
            f"Rebalancing: neues Signal · {rsl_s}",
            "Plan",
        )
    for ticker, info in rsl_data.items():
        if sp100_depot is not None and ticker not in sp100_depot:
            continue
        live = _sp100_live_rsl(ticker, info)
        puf = live.get("puffer")
        if puf is not None and puf <= 0:
            add(
                "sp100", "🔴 VERKAUFEN", ticker, info.get("name") or "",
                f"RSL-Peak-Trail ausgelöst ({puf:+.1f}% Puffer, live)",
                "Sofort",
            )

    # ── Regime Momentum: wöchentliche Handelsanweisungen ──
    rm_ha = rm_raw.get("handelsanweisungen") or [] if isinstance(rm_raw, dict) else []
    if rm_ha:
        for rec in rm_ha:
            if not isinstance(rec, dict):
                continue
            aktion = str(rec.get("aktion") or "")
            if "HALTEN" in aktion:
                continue
            grund = rec.get("grund") or "Fr-Rebalancing"
            if rec.get("ziel_eur") is not None:
                grund += f" · Ziel ~€{rec['ziel_eur']:,.0f}"
            add(
                "regime_momentum", aktion or "—", rec.get("ticker"),
                rec.get("name") or "", grund, rec.get("prioritaet") or "Plan",
            )
    else:
        ziel_map = {
            z.get("ticker"): z for z in (rm_raw.get("ziel") or [])
            if isinstance(z, dict) and z.get("ticker")
        } if isinstance(rm_raw, dict) else {}
        for ticker in rm_raw.get("verkaufen") or [] if isinstance(rm_raw, dict) else []:
            info = ziel_map.get(ticker, {})
            add(
                "regime_momentum", "🔴 VERKAUFEN", ticker, info.get("name") or "",
                "Rebalancing: Rank-Exit / nicht mehr Top-N", "Plan",
            )
        for ticker in rm_raw.get("kaufen") or [] if isinstance(rm_raw, dict) else []:
            info = ziel_map.get(ticker, {})
            add(
                "regime_momentum", "🟢 KAUFEN", ticker, info.get("name") or "",
                "Rebalancing: neues Top-N Signal", "Plan",
            )

    # ── IVY: monatliche Handelsanweisungen aus JSON ──
    for o in _ivy_orders_aus_json(ivy_raw):
        if not isinstance(o, dict):
            continue
        act = (o.get("action") or o.get("aktion") or "").upper()
        if act == "HALTEN" or not act:
            continue
        if act == "KAUFEN":
            aktion = "🟢 KAUFEN"
        elif act == "VERKAUFEN":
            aktion = "🔴 VERKAUFEN"
        else:
            aktion = act
        prev, nw, delta = o.get("prev"), o.get("new"), o.get("delta")
        grund = o.get("grund") or ""
        if not grund and prev is not None and nw is not None and delta is not None:
            grund = f"Monats-Rebalancing · {prev:.1%} → {nw:.1%} (Δ {delta:+.1%})"
        elif not grund:
            grund = "Monats-Rebalancing (aus Colab)"
        add(
            "ivy", aktion, o.get("ticker"), o.get("name") or "",
            grund, o.get("prioritaet") or "Plan",
        )

    # ── IVY: Ampel ROT → alle verkaufen ──
    if ivy_ampel and ivy_ampel.get("ampel") == "red":
        add(
            "ivy", "🔴 ALLE VERKAUFEN", "—", "—",
            ivy_ampel.get("aktion") or "Ampel ROT — defensiv",
            "Sofort",
        )
    for tk, p in ivy_pos.items():
        if tk in IVY_TS_EXCLUDE or not p.get("entry_price"):
            continue
        ht = ivy_handelstage_seit_kauf(p.get("entry_date"))
        if ht is not None and ht < IVY_WARMUP_DAYS:
            continue
        peak = ivy_peak(p)
        if not peak:
            continue
        kurs, _, _ = ivy_eur_kurs(tk, p, peak)
        if not kurs:
            continue
        stop = round(peak * (1 - STOP_CFG["ivy"]["pct"]), 2)
        puf = puffer_pct(kurs, stop)
        if puf is not None and puf <= 0:
            add(
                "ivy", "🔴 VERKAUFEN", tk, p.get("name") or "",
                f"15% Trailing Stop ({fmt_pct(puf)} zum Stop · Peak {peak:.2f} €)",
                "Sofort",
            )

    # ── ETF: Handelsanweisungen (volle Colab-Liste) oder Fallback empfehlung ──
    _append_etf_transaction_rows(add, etf_raw, etf_state, etf_pos, etf_ts, "etf")
    _append_etf_transaction_rows(
        add, etf_eodhd_raw, etf_eodhd_state, etf_eodhd_pos, etf_eodhd_ts, "etf_eodhd",
    )

    # ── Small Cap: Handelsanweisungen oder verkaufen/kaufen ──
    sc_ha = _smallcap_handels_aus_json(sc_raw)
    if sc_ha:
        for rec in sc_ha:
            if not isinstance(rec, dict):
                continue
            aktion = str(rec.get("aktion") or "")
            if "HALTEN" in aktion:
                continue
            ticker = rec.get("ticker") or rec.get("isin") or ""
            parts = [rec.get("grund") or "Rebalancing"]
            if rec.get("pnl_pct") is not None:
                parts.append(f"G/V {rec['pnl_pct']:+.1f}%")
            if rec.get("delta_eur") is not None:
                parts.append(f"Δ {rec['delta_eur']:+,.0f} €")
            if rec.get("invest_eur") is not None:
                parts.append(f"Invest {rec['invest_eur']:,.0f} €")
            if rec.get("stueck") is not None:
                parts.append(f"{rec['stueck']} Stk")
            if rec.get("ziel_eur") is not None and rec.get("aktuell_eur") is not None:
                parts.append(f"Ziel {rec['ziel_eur']:,.0f} € · ist {rec['aktuell_eur']:,.0f} €")
            prio = rec.get("prioritaet") or (
                "Sofort" if any(x in str(rec.get("grund", "")) for x in ("EMA100", "Trailing Stop"))
                else "Plan"
            )
            add(
                "smallcap", aktion or "—", ticker,
                _sc_name(ticker=ticker, pos=rec, isin=rec.get("isin")) or rec.get("name") or "",
                " · ".join(parts), prio,
            )
    else:
        for isin in sc_raw.get("verkaufen") or [] if isinstance(sc_raw, dict) else []:
            p = sc_pos.get(isin, {})
            add(
                "smallcap", "🔴 VERKAUFEN", p.get("ticker") or isin,
                _sc_name(ticker=p.get("ticker"), pos=p, isin=isin) or p.get("name") or "",
                "Rebalancing: Verkaufssignal", "Plan",
            )
        for isin in sc_raw.get("kaufen") or [] if isinstance(sc_raw, dict) else []:
            p = sc_pos.get(isin, {})
            add(
                "smallcap", "🟢 KAUFEN", p.get("ticker") or isin,
                _sc_name(ticker=p.get("ticker"), pos=p, isin=isin) or p.get("name") or "",
                "Rebalancing: neues Top-10 Signal", "Plan",
            )

    # ── HAA-Balanced: monatliche Handelsanweisungen ──
    haa_ha = _haa_handels_aus_json(haa_raw)
    if haa_ha:
        for rec in haa_ha:
            if not isinstance(rec, dict):
                continue
            aktion = str(rec.get("aktion") or "")
            if "HALTEN" in aktion:
                continue
            if "AUFSTOCK" not in aktion and "REDUZ" not in aktion and "KAUF" not in aktion and "VERKAUF" not in aktion:
                continue
            ticker = rec.get("ticker") or ""
            prev, nw, delta = rec.get("prev"), rec.get("new"), rec.get("delta")
            parts = [rec.get("grund") or "Monats-Rebalancing"]
            if prev is not None and nw is not None and delta is not None:
                parts.append(f"{prev:.1%} → {nw:.1%} (Δ {delta:+.1%})")
            if rec.get("ziel_eur") is not None:
                parts.append(f"Ziel {rec['ziel_eur']:,.0f} €")
            if isinstance(haa_raw, dict) and haa_raw.get("regime_label"):
                parts.append(str(haa_raw["regime_label"]))
            add(
                "haa", aktion or "—", ticker, rec.get("name") or "",
                " · ".join(parts), rec.get("prioritaet") or "Plan",
            )
    else:
        _rev = {
            v: k
            for k, v in ((haa_raw.get("scalable_map") or {}) if isinstance(haa_raw, dict) else {}).items()
        }
        for ticker in haa_raw.get("verkaufen") or [] if isinstance(haa_raw, dict) else []:
            sig = _rev.get(ticker, ticker)
            add(
                "haa", "🔴 VERKAUFEN", ticker, "",
                f"Monats-Rebalancing: Signal {sig} nicht mehr im Ziel", "Plan",
            )
        for ticker in haa_raw.get("kaufen") or [] if isinstance(haa_raw, dict) else []:
            sig = _rev.get(ticker, ticker)
            w = (haa_raw.get("ziel_gewichte") or {}).get(sig)
            w_s = f" · Ziel {w:.0%}" if w is not None else ""
            add(
                "haa", "🟢 KAUFEN", ticker, "",
                f"Monats-Rebalancing: Signal {sig}{w_s}", "Plan",
            )

    rows.sort(key=lambda r: (r.pop("_sort", 9), r.get("Strategie", ""), r.get("Ticker", "")))
    return rows


def build_check_rows():
    rows = []
    for key in ("kassandra", "sp100", "regime_momentum", "smallcap", "ivy", "etf", "etf_eodhd", "haa"):
        ci = check_info(key)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige(key),
            "Rhythmus": ci["frequenz"],
            **signal_spalten(key, ci, {
                "kassandra": _kass_raw,
                "sp100": SP100_POS,
                "regime_momentum": _RM_RAW,
                "smallcap": _sc_raw,
                "ivy": _ivy_raw,
                "etf": _etf_raw,
                "etf_eodhd": _etf_eodhd_raw,
                "haa": _haa_raw,
            }[key]),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Tage bis Check": ci["tage_bis_check"],
            "Tage bis Ausführung": ci["tage_bis"],
            "Hinweis": ci["hinweis"],
        })
    return rows


# ── UI ────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📈 Trading Dashboard")
    st.caption(f"v{APP_VERSION} · Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    if st.button("🔄 Kurse & JSON aktualisieren"):
        st.session_state.json_refresh += 1
        st.cache_data.clear()
        st.rerun()
    st.caption("JSON von GitHub (2 Min.) · EODHD-Kurse (5 Min.)")
    with st.expander("📡 JSON-Sync (GitHub)"):
        st.caption(json_sync_hinweis("Kassandra", _kass_raw))
        st.caption(json_trade_hinweis("Kassandra Trades", _kass_raw, "kassandra"))
        st.caption(json_sync_hinweis("S&P 100", SP100_POS))
        st.caption(json_sync_hinweis("IVY", _ivy_raw))
        st.caption(json_trade_hinweis("IVY Trades", _ivy_raw, "ivy"))
        st.caption(json_sync_hinweis("ETF Yahoo Top10", _etf_raw))
        st.caption(json_trade_hinweis("ETF Yahoo Trades", _etf_raw, "etf"))
        st.caption(json_sync_hinweis("ETF EODHD Voll", _etf_eodhd_raw))
        st.caption(json_trade_hinweis("ETF EODHD Trades", _etf_eodhd_raw, "etf_eodhd"))
        st.caption(json_sync_hinweis("Small Cap", _sc_raw))
        st.caption(json_trade_hinweis("Small Cap Trades", _sc_raw, "smallcap"))
        st.caption(json_sync_hinweis("HAA-Balanced", _haa_raw))
        st.caption(json_trade_hinweis("HAA Trades", _haa_raw, "haa"))
        st.caption(json_sync_hinweis("Regime Momentum", _RM_RAW))
        st.caption(json_trade_hinweis("Regime Momentum Trades", _RM_RAW, "regime_momentum"))
        st.caption(json_sync_hinweis("Kassandra Regime", _REGIME_RAW))

st.title("📅 Handel & Trailing-Stop")
st.caption("Signale aus Colab-JSON auf GitHub · Live-Kurse via EODHD")

_regime = format_regime_banner(_REGIME_RAW)
_ampel_fn = {"green": st.success, "yellow": st.warning, "red": st.error}
_ampel_fn.get(_regime["ampel"], st.info)(
    f"**🌐 Kassandra Regime: {_regime['label']}** — {_regime['aktion']}"
)
if _regime.get("caption"):
    st.caption(
        f"Stand {_regime.get('datum', '—')}  ·  {_regime['caption']}"
    )

st.divider()

st.subheader("Strategie-Übersicht")
st.caption(
    "**Nächster Check** = geplanter Signal-Tag (wöchentlich Di/Mi · monatlich Monatsende) · "
    "**Letztes JSON** = letzter Colab-Upload · "
    "**Tage bis Check** = bis Signal-EOD · **Tage bis Ausführung** = bis Handelstag danach"
)
st.dataframe(pd.DataFrame(build_check_rows()), use_container_width=True, hide_index=True)

st.divider()

with st.spinner("Transaktionen laden..."):
    ivy_ampel = ivy_markt_ampel()
    _txn_refresh = st.session_state.json_refresh
    txn_json = {
        "kassandra": lade_json_github("kassandra_positionen.json", _txn_refresh) or {},
        "sp100": lade_json_github("sp100_positionen.json", _txn_refresh) or {},
        "ivy": lade_json_github("ivy_portfolio.json", _txn_refresh) or {},
        "etf": lade_json_github("etf_eingabe.json", _txn_refresh) or {},
        "etf_eodhd": lade_json_github("etf_eodhd_eingabe.json", _txn_refresh) or {},
        "smallcap": lade_json_github("smallcap_positionen.json", _txn_refresh) or {},
        "haa": lade_json_github("haa_balanced_positionen.json", _txn_refresh) or {},
        "regime_momentum": lade_json_github("regime_momentum_positionen.json", _txn_refresh) or {},
        "etf_state": lade_json_github("portfolio_state.json", _txn_refresh) or {},
        "etf_eodhd_state": lade_json_github("portfolio_state_eodhd.json", _txn_refresh) or {},
    }
    txn_rows = build_transaction_rows(ivy_ampel, txn_json=txn_json)

st.subheader("📋 Anstehende Transaktionen")
st.caption(
    "Nur **offene** Käufe/Verkäufe/Stops erscheinen in der Tabelle unten. "
    "Strategien mit **✅ Keine Aktion** sind trotzdem aktiv — siehe Status-Tabelle."
)
st.dataframe(pd.DataFrame(build_strategy_status(txn_json)), use_container_width=True, hide_index=True)

_kass_txn = txn_json["kassandra"]
_sp100_txn = txn_json["sp100"]
_kass_ha_n = count_open_signals(_kass_txn, "kassandra")
_ivy_ha_n = count_open_signals(txn_json["ivy"], "ivy")
_etf_ha_n = count_open_signals(txn_json["etf"], "etf")
_etf_eodhd_ha_n = count_open_signals(txn_json["etf_eodhd"], "etf_eodhd")
_sc_ha_n = count_open_signals(txn_json["smallcap"], "smallcap")
_haa_ha_n = count_open_signals(txn_json["haa"], "haa")
_rm_ha_n = count_open_signals(txn_json.get("regime_momentum", _RM_RAW), "regime_momentum")
_sp100_ha_n = _sp100_txn_count(_sp100_txn)
st.caption(
    "**Sofort** = Stop/Crash/Ampel ROT · **Plan** = Rebalancing (Handelsanweisungen aus Colab-JSON) · "
    "Strategie fehlt = kein Signal in JSON (nicht vergessen: 🔄 aktualisieren)."
)
st.caption(
    f"JSON-Stand: Kassandra **{_kass_ha_n}** · S&P 100 **{_sp100_ha_n}** · "
    f"Regime Momentum **{_rm_ha_n}** · "
    f"IVY **{_ivy_ha_n}** · ETF Yahoo **{_etf_ha_n}** · ETF EODHD **{_etf_eodhd_ha_n}** · Small Cap **{_sc_ha_n}** · "
    f"HAA **{_haa_ha_n}** · Kassandra-JSON: {format_letztes_json(_kass_txn)}"
)
if not txn_rows:
    st.info(
        "Keine offenen Transaktionen in der Detail-Tabelle — das ist normal, wenn alle Strategien "
        "**✅ Keine Aktion** zeigen (Depot = Ziel, keine Stops ausgelöst)."
    )
else:
    txn_df = pd.DataFrame(txn_rows)
    txn_cols = [
        "Priorität", "Strategie", "Aktion", "Ticker", "Name", "Grund / Details",
        "Nächster Check", "Prüfen & Ausführen", "Letztes JSON",
    ]
    txn_df = txn_df[[c for c in txn_cols if c in txn_df.columns]]
    st.dataframe(
        txn_df.style.map(
            lambda v: (
                "color:#ff1744;font-weight:bold"
                if "VERKAUFEN" in str(v) or "ALLE VERKAUFEN" in str(v)
                else ("color:#00c853;font-weight:bold" if "KAUFEN" in str(v) else "")
            ),
            subset=["Aktion"],
        ).map(
            lambda v: (
                "color:#ff1744;font-weight:bold" if v == "Sofort"
                else ("color:#ffd600" if v == "Hoch" else "")
            ),
            subset=["Priorität"],
        ),
        use_container_width=True,
        hide_index=True,
    )

render_warum_expanders(txn_json)

st.divider()
st.subheader("Trailing-Stop Monitor")
st.caption(
    "Kassandra: Crash Exit ≥ "
    f"{int(KASS_CRASH_PCT * 100)}% Tagesverlust "
    f"({'aktiv' if KASS_CRASH_PCT else 'aus'})  ·  "
    "IVY: **10 Handelstage Warmup** nach Kauf (⏳)  ·  "
    "Kurse: Handelswährung (Kassandra/ETF) / EUR (IVY) / RSL (S&P 100)."
)

with st.spinner("Live-Kurse laden..."):
    stop_rows = build_stop_rows()

if not stop_rows:
    kass_n = sum(1 for p in KASSANDRA_POS.values() if position_entry(p))
    st.warning(
        "Keine Positionen im Trailing-Stop Monitor. "
        f"Kassandra: {kass_n} mit Einstieg · "
        f"S&P 100: {len(SP100_POS.get('rsl_data') or {})} RSL-Einträge · "
        "→ 🔄 aktualisieren."
    )
    if _kass_raw and kass_n == 0:
        st.error(
            "🌍 **Kassandra:** `kassandra_positionen.json` auf GitHub enthält **keine Positionen** "
            "(nur Meta-Daten). Trailing Stop braucht `einstieg` + `hoch` pro Ticker. "
            "**→ `INVESTMENT ONLY ONE.ipynb` in Colab ausführen** (lädt Live-Positionen von Drive hoch)."
        )
else:
    df = pd.DataFrame(stop_rows)
    col_order = [
        "Strategie", "Trailing Stop %", "Nächster Check", "Letztes JSON",
        "Prüfen & Ausführen",
        "Ticker", "Name", "Akt. Kurs", "Peak/Hoch", "Stop-Kurs",
        "Tages %", "% vom Peak", "% zum Stop", "Status",
    ]
    df = df[[c for c in col_order if c in df.columns]]
    for col in ("Tages %", "% vom Peak", "Peak/Hoch"):
        if col in df.columns:
            df[col] = df[col].fillna("—").replace({None: "—", "None": "—"})
    st.caption(
        "**Peak/Hoch** = Höchstkurs seit Kauf (aus Colab-JSON) · "
        "**Akt. Kurs** = EODHD (Datum dahinter) · "
        "**⚠️** = Kurs älter als 1 Tag · "
        "**Tages %** = nur Kassandra · **% vom Peak** = nur IVY · "
        "— = Spalte gilt nicht für diese Strategie."
    )
    st.dataframe(
        df.style.map(
            lambda v: (
                "color:#ff1744;font-weight:bold"
                if "STOP" in str(v) or "CRASH" in str(v)
                else (
                    "color:#ffd600"
                    if "Gefahr" in str(v)
                    else (
                        "color:#29b6f6"
                        if "Warmup" in str(v)
                        else "color:#00c853" if "OK" in str(v) else ""
                    )
                )
            ),
            subset=["Status"],
        ).map(
            lambda v: (
                "color:#ff9800;font-weight:bold"
                if "⚠️" in str(v)
                else ""
            ),
            subset=["Akt. Kurs"],
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
        f"📈 **S&P 100:** Exit-Regel **RSL-Peak-Trail 35%** — "
        f"Puffer/RSL im Monitor **täglich live** (EODHD); "
        f"RSL-Peak aus JSON (Stand {sp100_datum}). "
        "Kurs-Hoch % und RSL-Puffer können abweichen — "
        "Verkauf erst wenn **RSL** 35% unter **RSL-Hoch** fällt."
    )
if not SMALLCAP_POS:
    hinweise.append(
        "🇪🇺 **Small Cap:** `smallcap_positionen.json` fehlt/leer — "
        "aus `live_positions.json` hochladen."
    )
if not _haa_raw.get("ziel_ticker") and not _haa_handels_aus_json(_haa_raw):
    hinweise.append(
        "⚖️ **HAA-Balanced:** `haa_balanced_positionen.json` fehlt/leer — "
        "`HAA_Balanced_Live.ipynb` in Colab ausführen."
    )
if not _RM_RAW.get("ziel_ticker"):
    hinweise.append(
        "🚀 **Regime Momentum:** `regime_momentum_positionen.json` fehlt/leer — "
        "`_regime_momentum_live.py` in Colab ausführen (nach FULL-Validierung)."
    )
for h in hinweise:
    st.warning(h)

if KASSANDRA_POS or (_kass_raw.get("kassandra_ampel") if isinstance(_kass_raw, dict) else None):
    _k_src = (_kass_raw.get("ampel_source") if isinstance(_kass_raw, dict) else "") or ""
    _k_pct = _kass_raw.get("invest_pct") if isinstance(_kass_raw, dict) else None
    _k_amp = _kass_raw.get("kassandra_ampel", "—") if isinstance(_kass_raw, dict) else "—"
    _k_pct_s = f" · Quote **{int(round(float(_k_pct) * 100))}%**" if _k_pct is not None else ""
    _k_src_s = " (Kassandra Regime)" if _k_src == "kassandra_regime" else ""
    st.info(
        f"🌍 **Länder-ETF Kassandra:** {len(KASSANDRA_POS)} Position(en) — "
        f"Ampel **{_k_amp}**{_k_pct_s}{_k_src_s} · 20% TS · 2-Wochen-Rebal."
    )

if SMALLCAP_POS:
    modus = (_sc_raw.get("modus") or "exit_only").replace("_", " ")
    _sc_meta = _sc_raw.get("_kassandra_meta", {}) or {}
    ampel = _sc_meta.get("signal") or "—"
    pct = _sc_meta.get("invest_pct")
    src = _sc_meta.get("ampel_source", "")
    ts_lbl = _sc_raw.get("trailing_pct")
    ts_s = f"{int(round(float(ts_lbl) * 100))}% TS" if ts_lbl else "25% TS"
    pct_s = f" · Quote **{int(round(float(pct) * 100))}%**" if pct is not None else ""
    src_s = " (Kassandra Regime)" if src == "kassandra_regime" else ""
    st.info(
        f"🇪🇺 **Small Cap EU:** {len(SMALLCAP_POS)} Position(en) — "
        f"{ts_s} · {modus} · Ampel **{ampel}**{pct_s}{src_s}. Kein Ranking-Verkauf."
    )

if _RM_RAW.get("ziel_ticker"):
    _rm_p = _RM_RAW.get("params") or {}
    _rm_gross = _RM_RAW.get("gross_exposure")
    _rm_gross_s = f" · Brutto **{_rm_gross:.0%}**" if _rm_gross is not None else ""
    _rm_lbl = _RM_RAW.get("regime_label") or "—"
    _rm_sig_dt = _RM_RAW.get("signal_datum") or _RM_RAW.get("datum") or "—"
    st.info(
        f"🚀 **Regime Momentum:** {len(_RM_RAW['ziel_ticker'])} Ziel-Titel — "
        f"{_rm_lbl}{_rm_gross_s} · Top {int(_rm_p.get('top_n', 20))}/"
        f"{int(_rm_p.get('exit_rank', 25))} · Signal-Fr **{_rm_sig_dt}** · kein TS."
    )

st.caption("Alerts: GitHub Actions (stop_check.py) · Live-Kurse: EODHD")
