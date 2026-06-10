# ═══════════════════════════════════════════════════════════════════════════
#  TRADING DASHBOARD v4.6.1 — Live-Sync von GitHub
#  Nächster Check + Trailing-Stop (5 Strategien, JSON von GitHub / Colab)
# ═══════════════════════════════════════════════════════════════════════════

APP_VERSION = "4.6.1"
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

st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📈",
    layout="wide",
)

if "json_refresh" not in st.session_state:
    st.session_state.json_refresh = 0

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


def sp100_erlaubte_ticker(sp100_pos):
    """Nur Positionen anzeigen, die noch im Portfolio oder Strategie-Signal sind."""
    if not sp100_pos:
        return None
    meine = sp100_pos.get("meine_aktien")
    if meine is None:
        return None
    return set(meine) | set(sp100_pos.get("tickers") or [])


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


def _handels_aktionen(data, quelle="ivy"):
    if not isinstance(data, dict):
        return []
    if quelle == "ivy":
        roh = data.get("handelsanweisungen") or data.get("orders") or []
    else:
        roh = data.get("handelsanweisungen") or []
    out = []
    for o in roh:
        if not isinstance(o, dict):
            continue
        act = (o.get("action") or o.get("aktion") or "").upper()
        if act and act != "HALTEN" and "HALTEN" not in act:
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
        k = sum(1 for o in ha if _aktion_typ(o.get("action") or o.get("aktion")) == "kauf")
        v = sum(1 for o in ha if _aktion_typ(o.get("action") or o.get("aktion")) == "verkauf")
        extra = ""
        if quelle == "smallcap":
            a = sum(1 for o in ha if _aktion_typ(o.get("action") or o.get("aktion")) == "aufstock")
            r = sum(1 for o in ha if _aktion_typ(o.get("action") or o.get("aktion")) == "reduz")
            if a:
                extra += f" · {a} Aufstocken"
            if r:
                extra += f" · {r} Reduzieren"
        return f"{label}: {len(ha)} Trades ({k} Kaufen · {v} Verkaufen{extra})"
    if quelle == "etf" and isinstance(data, dict) and data.get("empfehlung"):
        n = len(data.get("empfehlung") or [])
        return f"{label}: keine Handelsanweisungen — {n} Kandidaten (empfehlung)"
    return f"{label}: keine Handelsanweisungen in JSON"


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
        "regel": "20% Trailing Stop (vom Hoch) + Crash Exit (≥8% Tagesverlust)",
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


def format_naechster_check(key, ci):
    """Geplanter nächster Signal-Check (Rhythmus der Strategie)."""
    base = format_datum(ci["check_datum"])
    cfg = CHECK_ZEITEN[key]
    if cfg["frequenz"] == "monatlich":
        return f"{base} · Monatsende"
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][cfg["check_tag"]]
    if cfg["frequenz"] == "2-wöchentlich":
        return f"{base} · {wd} EOD (2-wöchentlich)"
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
KASSANDRA_POS = portfolio_ohne_meta(_kass_raw)
KASS_CRASH_PCT = kassandra_crash_exit_pct(_kass_raw)
SP100_POS = lade_json_github("sp100_positionen.json", _JSON_REFRESH) or {}
_ivy_raw = lade_json_github("ivy_portfolio.json", _JSON_REFRESH) or {}
IVY_POS = portfolio_ohne_meta(_ivy_raw)
_etf_raw = lade_json_github("etf_eingabe.json", _JSON_REFRESH) or {}
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
ETF_STATE = lade_json_github("portfolio_state.json", _JSON_REFRESH) or {}
_sc_raw = lade_json_github("smallcap_positionen.json", _JSON_REFRESH) or {}
SMALLCAP_POS = portfolio_ohne_meta(_sc_raw)
SP100_ALLOWED = sp100_erlaubte_ticker(SP100_POS)

# ── Trailing-Stop Zeilen ──────────────────────────────────────────────────────

def build_stop_rows():
    rows = []

    # Kassandra — 20% Trailing + Crash Exit (≥8% Tagesverlust)
    ci = check_info("kassandra")
    for ticker, p in KASSANDRA_POS.items():
        kauf = p.get("einstieg", 0)
        hoch = p.get("hoch", kauf)
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
        if SP100_ALLOWED is not None and ticker not in SP100_ALLOWED:
            continue
        trail = info.get("trail")
        rsl_now = info.get("rsl", 0)
        puf = info.get("puffer")
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
            stop = round(hoch * (1 - ETF_TS), 2)
        kurs_f = kurs or hoch
        puf = puffer_pct(kurs_f, stop)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige("etf"),
            **signal_spalten("etf", ci, _etf_raw),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker.replace(".US", "").replace(".TO", ""),
            "Name": pos.get("name") or "—",
            "Akt. Kurs": format_akt_kurs(kurs_f, ticker, q),
            "Peak/Hoch": format_kurs(hoch, ticker),
            "Stop-Kurs": format_kurs(stop, ticker),
            "% zum Stop": fmt_pct(puf),
            "Status": status_icon(puf, 3),
        })

    # Small Cap EU: kein Trailing Stop im Live-Betrieb (nur Rebalancing / EMA100 / Kassandra)

    return rows


_JSON_BY_STRATEGY = {
    "kassandra": lambda: _kass_raw,
    "sp100": lambda: SP100_POS,
    "ivy": lambda: _ivy_raw,
    "etf": lambda: _etf_raw,
    "smallcap": lambda: _sc_raw,
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


def _ivy_orders_aus_json(data):
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or data.get("orders") or []


def _etf_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or []


def _smallcap_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or []


def _kass_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    return data.get("handelsanweisungen") or []


def _sp100_txn_count(sp100_pos):
    if not isinstance(sp100_pos, dict):
        return 0
    n = len(sp100_pos.get("verkaufen") or []) + len(sp100_pos.get("kaufen") or [])
    for info in (sp100_pos.get("rsl_data") or {}).values():
        if isinstance(info, dict) and info.get("puffer") is not None and info.get("puffer") <= 0:
            n += 1
    return n


def build_transaction_rows(ivy_ampel=None, txn_json=None):
    """Anstehende Trades aus JSON + Live-Stops."""
    tj = txn_json or {}
    kass_raw = tj.get("kassandra", _kass_raw)
    kass_pos = portfolio_ohne_meta(kass_raw)
    kass_crash = kassandra_crash_exit_pct(kass_raw)
    sp100_pos = tj.get("sp100", SP100_POS)
    sp100_allowed = sp100_erlaubte_ticker(sp100_pos)
    ivy_raw = tj.get("ivy", _ivy_raw)
    ivy_pos = portfolio_ohne_meta(ivy_raw)
    etf_raw = tj.get("etf", _etf_raw)
    sc_raw = tj.get("smallcap", _sc_raw)
    sc_pos = portfolio_ohne_meta(sc_raw)
    etf_state = tj.get("etf_state", ETF_STATE)
    if isinstance(etf_raw, dict) and "positionen" in etf_raw:
        etf_pos = {
            p["ticker"]: p
            for p in etf_raw.get("positionen", [])
            if isinstance(p, dict) and p.get("ticker")
        }
        etf_ts = etf_raw.get("trailing_pct", 0.10)
    else:
        etf_pos = etf_raw if isinstance(etf_raw, dict) else {}
        etf_ts = 0.10

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
        kauf = p.get("einstieg", 0)
        hoch = p.get("hoch", kauf)
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
        if sp100_allowed is not None and ticker not in sp100_allowed:
            continue
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
        if sp100_allowed is not None and ticker not in sp100_allowed:
            continue
        puf = info.get("puffer")
        if puf is not None and puf <= 0:
            add(
                "sp100", "🔴 VERKAUFEN", ticker, info.get("name") or "",
                f"RSL-Peak-Trail ausgelöst ({puf:+.1f}% Puffer)",
                "Sofort",
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
        if prev is not None and nw is not None and delta is not None:
            grund = f"Monats-Rebalancing · {prev:.1%} → {nw:.1%} (Δ {delta:+.1%})"
        else:
            grund = "Monats-Rebalancing (aus Colab)"
        add(
            "ivy", aktion, o.get("ticker"), o.get("name") or "",
            grund, "Plan",
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
                "etf", "🔴 VERKAUFEN",
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
                "etf", aktion or "—",
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
                "etf", "🟢 KAUFEN", ticker.replace(".US", "").replace(".TO", ""),
                rec.get("name") or "",
                f"Monats-Rebalancing · {score_s} (noch nicht im Portfolio)",
                "Plan",
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
                "Sofort" if "EMA100" in str(rec.get("grund", "")) else "Plan"
            )
            add(
                "smallcap", aktion or "—", ticker, rec.get("name") or "",
                " · ".join(parts), prio,
            )
    else:
        for isin in sc_raw.get("verkaufen") or [] if isinstance(sc_raw, dict) else []:
            p = sc_pos.get(isin, {})
            add(
                "smallcap", "🔴 VERKAUFEN", p.get("ticker") or isin, p.get("name") or "",
                "Rebalancing: Verkaufssignal", "Plan",
            )
        for isin in sc_raw.get("kaufen") or [] if isinstance(sc_raw, dict) else []:
            p = sc_pos.get(isin, {})
            add(
                "smallcap", "🟢 KAUFEN", p.get("ticker") or isin, p.get("name") or "",
                "Rebalancing: neues Top-10 Signal", "Plan",
            )

    rows.sort(key=lambda r: (r.pop("_sort", 9), r.get("Strategie", ""), r.get("Ticker", "")))
    return rows


def build_check_rows():
    rows = []
    for key in ("kassandra", "sp100", "smallcap", "ivy", "etf"):
        ci = check_info(key)
        rows.append({
            "Strategie": ci["label"],
            "Trailing Stop %": stop_pct_anzeige(key),
            "Rhythmus": ci["frequenz"],
            **signal_spalten(key, ci, {
                "kassandra": _kass_raw,
                "sp100": SP100_POS,
                "smallcap": _sc_raw,
                "ivy": _ivy_raw,
                "etf": _etf_raw,
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
        st.caption(json_sync_hinweis("ETF", _etf_raw))
        st.caption(json_trade_hinweis("ETF Trades", _etf_raw, "etf"))
        st.caption(json_sync_hinweis("Small Cap", _sc_raw))
        st.caption(json_trade_hinweis("Small Cap Trades", _sc_raw, "smallcap"))

st.title("📅 Handel & Trailing-Stop")
st.caption("Signale aus Colab-JSON auf GitHub · Live-Kurse via EODHD")

st.subheader("Strategie-Übersicht")
st.caption(
    "**Nächster Check** = geplanter Signal-Tag (wöchentlich Di/Mi · monatlich Monatsende) · "
    "**Letztes JSON** = letzter Colab-Upload · "
    "**Tage bis Check** = bis Signal-EOD · **Tage bis Ausführung** = bis Handelstag danach"
)
st.dataframe(pd.DataFrame(build_check_rows()), use_container_width=True, hide_index=True)

st.divider()

with st.spinner("Transaktionen & IVY-Ampel laden..."):
    ivy_ampel = ivy_markt_ampel()
    _txn_refresh = st.session_state.json_refresh
    txn_json = {
        "kassandra": lade_json_github("kassandra_positionen.json", _txn_refresh) or {},
        "sp100": lade_json_github("sp100_positionen.json", _txn_refresh) or {},
        "ivy": lade_json_github("ivy_portfolio.json", _txn_refresh) or {},
        "etf": lade_json_github("etf_eingabe.json", _txn_refresh) or {},
        "smallcap": lade_json_github("smallcap_positionen.json", _txn_refresh) or {},
        "etf_state": lade_json_github("portfolio_state.json", _txn_refresh) or {},
    }
    txn_rows = build_transaction_rows(ivy_ampel, txn_json=txn_json)

st.subheader("📋 Anstehende Transaktionen")
_kass_txn = txn_json["kassandra"]
_sp100_txn = txn_json["sp100"]
_kass_ha_n = len(_handels_aktionen(_kass_txn, "kassandra"))
_ivy_ha_n = len(_handels_aktionen(txn_json["ivy"], "ivy"))
_etf_ha_n = len(_handels_aktionen(txn_json["etf"], "etf"))
_sc_ha_n = len(_handels_aktionen(txn_json["smallcap"], "smallcap"))
_sp100_ha_n = _sp100_txn_count(_sp100_txn)
st.caption(
    "**Sofort** = Stop/Crash/Ampel ROT · **Plan** = Rebalancing (Handelsanweisungen aus Colab-JSON) · "
    "Strategie fehlt = kein Signal in JSON (nicht vergessen: 🔄 aktualisieren)."
)
st.caption(
    f"JSON-Stand: Kassandra **{_kass_ha_n}** · S&P 100 **{_sp100_ha_n}** · "
    f"IVY **{_ivy_ha_n}** · ETF **{_etf_ha_n}** · Small Cap **{_sc_ha_n}** · "
    f"Kassandra-JSON: {format_letztes_json(_kass_txn)}"
)
if not txn_rows:
    st.success("Keine anstehenden Transaktionen — keine Stops und keine Rebalancing-Signale in der JSON.")
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

st.divider()

ampel_fn = {"green": st.success, "yellow": st.warning, "red": st.error}
ampel_fn.get(ivy_ampel["ampel"], st.info)(
    f"**🏛 IVY Markt-Ampel: {ivy_ampel['label']}** — {ivy_ampel['aktion']}"
)
_spy = f"${ivy_ampel['spy']:.2f}" if ivy_ampel.get("spy") else "—"
_sma = f"${ivy_ampel['sma']:.2f}" if ivy_ampel.get("sma") else "—"
_vix = f"{ivy_ampel['vix']:.1f}" if ivy_ampel.get("vix") else "—"
_vs = f" ({ivy_ampel['spy_vs_sma_pct']:+.1f}% vs. SMA{IVY_TREND_MONTHS}M)" if ivy_ampel.get("spy_vs_sma_pct") is not None else ""
st.caption(f"SPY: {_spy}  |  SMA{IVY_TREND_MONTHS}M: {_sma}{_vs}  |  VIX: {_vix}  (Schwelle: {IVY_VIX_THRESHOLD:.0f})")

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
    st.warning("Keine Positionen oder keine Stop-Daten gefunden.")
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
    st.info(
        f"🇪🇺 **Small Cap EU:** {len(SMALLCAP_POS)} Position(en) — "
        f"{stop_regel('smallcap')}. "
        "Positionen erscheinen nicht im Trailing-Stop Monitor."
    )

st.caption("Alerts: GitHub Actions (stop_check.py) · Live-Kurse: EODHD")
