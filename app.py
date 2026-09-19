# ═══════════════════════════════════════════════════════════════════════════
#  TRADING DASHBOARD v5.1.0 — Live-Sync von GitHub
#  Nächster Check + Trailing-Stop (Strategien, JSON von GitHub / Colab)
# ═══════════════════════════════════════════════════════════════════════════

APP_VERSION = "5.9.3"
GITHUB_REPO = "lazarkitanov-cell/trading-dashboard"
GITHUB_BRANCH = "main"
GITHUB_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/"

import json
import math
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ_BERLIN = ZoneInfo("Europe/Berlin")


def _now_berlin():
    return datetime.now(_TZ_BERLIN)

try:
    from etf_ticker_norm import etf_ticker_canonical
except ImportError:
    def etf_ticker_canonical(ticker: str) -> str:
        t = str(ticker or "").upper().strip()
        for sfx in (".US", ".TO", ".LSE", ".XETRA", ".L", ".DE", ".PA", ".SW"):
            if t.endswith(sfx):
                t = t[: -len(sfx)]
                break
        return t.split(".")[0] if t else ""

import pandas as pd
import requests
import streamlit as st

try:
    from name_lookup import is_weak_name, resolve_stock_name
except ImportError:
    def is_weak_name(name, ticker):
        if not name or not str(name).strip():
            return True
        short = (ticker or "").replace(".US", "").replace(".TO", "").split(".")[0].upper()
        return str(name).strip().upper() == short

    def resolve_stock_name(ticker=None, pos=None, signals=None, api_key=None, cache=None):
        if isinstance(pos, dict):
            nm = (pos.get("name") or "").strip()
            if nm and not is_weak_name(nm, ticker):
                return nm
        for s in signals or []:
            if not isinstance(s, dict):
                continue
            if (s.get("ticker") or "").upper() == (ticker or "").upper():
                nm = (s.get("name") or "").strip()
                if nm and not is_weak_name(nm, ticker):
                    return nm
        return (ticker or "").replace(".US", "").replace(".TO", "").split(".")[0]
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


def _stock_name(ticker=None, pos=None, signals=None):
    return resolve_stock_name(
        ticker=ticker, pos=pos, signals=signals,
        api_key=API_KEY, cache=st.session_state.name_cache,
    )


def _etf_name(ticker, pos=None, rec=None):
    raw = (rec or pos or {}).get("name") if isinstance(rec or pos, dict) else ""
    if raw and not is_weak_name(raw, ticker):
        return raw
    return _stock_name(ticker, pos=rec or pos)


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


def _levy_positions(raw):
    """Depot aus rsl_levy_positionen.json (positionen{} oder Ticker-Top-Level)."""
    if not isinstance(raw, dict):
        return {}
    pos = raw.get("positionen")
    if isinstance(pos, dict):
        return {k: v for k, v in pos.items() if isinstance(v, dict)}
    return portfolio_ohne_meta(raw)


def _ranking_positions(raw):
    """Depot aus positionen{} / Top-Level-Ticker / meine_aktien."""
    if not isinstance(raw, dict):
        return {}
    pos = raw.get("positionen")
    if isinstance(pos, dict) and pos:
        return {str(k): v for k, v in pos.items() if isinstance(v, dict)}
    out = portfolio_ohne_meta(raw)
    if out:
        return out
    for tk in raw.get("meine_aktien") or []:
        t = str(tk)
        if not t:
            continue
        info = raw.get(t) if isinstance(raw.get(t), dict) else {}
        out[t] = info or {"name": t}
    return out


def _levy_params(raw):
    return (raw or {}).get("params") or {}


def _levy_sltp_basis(params=None):
    """prozent | atr — aus Colab params.sl_tp_basis."""
    if params is None:
        params = _levy_params(_levy_raw if "_levy_raw" in globals() else {})
    return str((params or {}).get("sl_tp_basis") or "prozent").lower().strip()


def _pct_from_entry(entry, level):
    """Abstand eines Kurslevels zum Kaufpreis/Einstand in Prozent."""
    entry = safe_float(entry)
    level = safe_float(level)
    if entry and entry > 0 and level is not None:
        return (level / entry - 1.0) * 100.0
    return None


def _levy_level_vs_entry(level, entry=None, pct=None, atr_mult=None):
    """z.B. $24.16 (−10.7% / 3×ATR) — $ und % vom eingegebenen Kaufpreis."""
    if not level:
        return None
    if pct is None:
        pct = _pct_from_entry(entry, level)
    extra = []
    if pct is not None:
        extra.append(f"{pct:+.1f}%")
    if atr_mult is not None:
        extra.append(f"{atr_mult:g}×ATR")
    label = f"${level:.2f}"
    if extra:
        label += f" ({' / '.join(extra)})"
    return label


def _levy_exit_regel_kurz(params=None):
    """Kompakte Exit-Regel: %-SL/TP oder n×ATR + RSL."""
    p = params if isinstance(params, dict) else _levy_params(
        _levy_raw if "_levy_raw" in globals() else {}
    )
    rsl_x = safe_float(p.get("rsl_exit_below")) or 0.99
    trail = safe_float(p.get("trailing_stop")) or 0
    if _levy_sltp_basis(p) == "atr":
        sl_m = safe_float(p.get("sl_atr_mult")) or 3.0
        tp_m = safe_float(p.get("tp_atr_mult")) or 4.0
        s = f"S/L {sl_m:g}×ATR · T/P {tp_m:g}×ATR · RSL<{rsl_x:.2f}"
    else:
        sl = abs(safe_float(p.get("stop_loss")) or 0.15)
        tp = safe_float(p.get("take_profit")) or 0.34
        s = f"S/L −{int(round(sl * 100))}% · T/P +{int(round(tp * 100))}% · RSL<{rsl_x:.2f}"
    if trail > 0:
        s += f" · Trail −{int(round(trail * 100))}%"
    return s


def _levy_live_position(ticker, info, raw):
    """Kurs + Puffer zum Stop — JSON-Basis, Kurs live via EODHD."""
    if not isinstance(info, dict):
        return {}
    stop = safe_float(info.get("stop_level"))
    tp = safe_float(info.get("tp_level"))
    kurs = safe_float(info.get("kurs_usd"))
    q = eodhd_quote(ticker_fix(ticker))
    if q and q.get("close"):
        kurs = float(q["close"])
    puf = puffer_pct(kurs, stop) if kurs and stop else safe_pct(info.get("puffer_pct"))
    rsl = safe_float(info.get("rsl"))
    rsl_exit = safe_float(_levy_params(raw).get("rsl_exit_below")) or 0.99
    status = info.get("status") or "OK"
    if puf is not None and puf <= 0:
        status = "STOP"
    elif rsl is not None and rsl < rsl_exit:
        status = "RSL-EXIT"
    elif kurs and tp and kurs >= tp:
        status = "TP"
    return {
        "kurs": kurs,
        "stop": stop,
        "tp": tp,
        "puffer": puf,
        "rsl": rsl,
        "quote": q,
        "status": status,
    }


def levy_status_display(puf, rsl, rsl_exit, raw_status=None):
    if raw_status in ("VERKAUF", "STOP") or (puf is not None and puf <= 0):
        return "🔴 STOP"
    if rsl is not None and rsl < (rsl_exit or 0.99):
        return "🔴 RSL-EXIT"
    if raw_status == "TP":
        return "🟢 TP"
    if puf is not None and puf < 5:
        return "🟡 Gefahr"
    return "🟢 OK"

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
        holdings = sp100_pos.get("current_holdings")
        if isinstance(holdings, list):
            meine = [
                h.get("ticker") for h in holdings
                if isinstance(h, dict) and h.get("ticker")
            ]
        else:
            return None
    return set(str(t) for t in meine if t)


def _sp100_from_v6_signal(raw):
    """Colab v6.4.x live_signal → Dashboard-Schema (rsl_data / kaufen / verkaufen)."""
    trail_pct = 0.35
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    trail_pct = safe_float(params.get("rsl_peak_trail")) or trail_pct
    holdings = {
        str(h.get("ticker")): h
        for h in (raw.get("current_holdings") or [])
        if isinstance(h, dict) and h.get("ticker")
    }
    orders = [o for o in (raw.get("orders") or []) if isinstance(o, dict)]
    meine = [t for t in holdings] or [
        str(o.get("ticker"))
        for o in orders
        if o.get("current_shares") or o.get("action") == "HALTEN"
    ]
    kaufen, verkaufen = [], []
    rsl_data = {}
    peak_by_tk = {
        t: safe_float(h.get("rsl_peak"))
        for t, h in holdings.items()
        if safe_float(h.get("rsl_peak"))
    }
    for o in orders:
        tk = str(o.get("ticker") or "")
        if not tk:
            continue
        act = str(o.get("action") or o.get("aktion") or "").upper()
        rsl = safe_float(o.get("rsl"))
        peak = peak_by_tk.get(tk) or rsl
        trail = round(peak * (1.0 - trail_pct), 4) if peak else None
        puffer = None
        if rsl and trail:
            puffer = round((rsl / trail - 1.0) * 100.0, 1)
        status = "OK"
        if act == "VERKAUFEN":
            status = "SELL"
            verkaufen.append(tk)
        elif act == "KAUFEN":
            status = "BUY"
            kaufen.append(tk)
        rsl_data[tk] = {
            "name": o.get("company_name") or o.get("name") or "",
            "sektor": o.get("sektor") or "—",
            "rsl": rsl,
            "rsl_peak": peak,
            "trail": trail,
            "puffer": puffer,
            "status": status,
            "kurs_usd": o.get("estimated_price_usd"),
            "kurs_eur": o.get("estimated_price_eur"),
            "rang": o.get("rank"),
            "grund": o.get("reason") or o.get("grund") or "",
        }
    for tk, h in holdings.items():
        if tk in rsl_data:
            continue
        peak = peak_by_tk.get(tk)
        rsl_data[tk] = {
            "name": h.get("name") or "",
            "rsl_peak": peak,
            "trail": round(peak * (1.0 - trail_pct), 4) if peak else None,
            "status": "OK",
        }
    regime = str(raw.get("regime") or "").upper()
    ampel = "GRÜN" if regime == "INVEST" else ("ROT" if regime == "CASH" else raw.get("ampel") or "—")
    if raw.get("status") == "WAITING_FOR_ENTRY_CLOSE":
        ampel = "GELB"
    asof = raw.get("asof_close") or raw.get("datum") or ""
    ha = []
    for o in orders:
        act = str(o.get("action") or "").upper()
        if not act or act == "HALTEN":
            continue
        ha.append({
            "action": act,
            "aktion": (
                "🔴 VERKAUFEN" if act == "VERKAUFEN"
                else ("🟢 KAUFEN" if act == "KAUFEN" else act)
            ),
            "ticker": o.get("ticker"),
            "name": o.get("company_name") or o.get("name") or "",
            "grund": o.get("reason") or o.get("grund") or "",
            "prioritaet": "Plan",
        })
    out = dict(raw)
    out.update({
        "version": raw.get("version") or "6.4.3",
        "strategie": raw.get("strategie") or f"S&P 100 Momentum v{raw.get('version') or '6.4.3'}",
        "meine_aktien": meine,
        "tickers": raw.get("target_tickers") or meine,
        "rsl_data": rsl_data,
        "kaufen": kaufen,
        "verkaufen": verkaufen,
        "handelsanweisungen": ha,
        "ampel": ampel,
        "datum": asof,
        "datum_heute": asof,
        "sync_ts": raw.get("sync_ts") or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "score_smooth": raw.get("score_smooth"),
        "score_raw": raw.get("score_raw"),
    })
    return out


def normalize_sp100_json(raw):
    """v5 Dashboard-JSON oder v6.4.x Live-Signal → einheitliches Schema."""
    if not isinstance(raw, dict):
        return {}
    if raw.get("rsl_data"):
        return raw
    if raw.get("orders") is not None or raw.get("current_holdings") is not None:
        return _sp100_from_v6_signal(raw)
    return raw


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
        or data.get("generated_at")
        or data.get("scan_date")
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
    """Normiere Ivy-Ticker für Depot/Order-Vergleich (.US/.TO/.SW/.HK/…)."""
    t = str(ticker or "").strip().upper()
    for sfx in (
        ".XETRA", ".LSE", ".US", ".TO", ".SW", ".HK", ".DE", ".F",
        ".AS", ".PA", ".L", ".MI", ".BR", ".ST", ".OL", ".CO", ".HE",
    ):
        if t.endswith(sfx):
            t = t[: -len(sfx)]
            break
    return t.split(".")[0] if t else ""


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
    if not act or act == "HALTEN" or "HALTEN" in act:
        return False
    # VERKAUF vor KAUF prüfen — „VERKAUFEN“ enthält sonst „KAUF“ als Teilstring
    # Ivy 3.8+/3.9 Live-Plan: Aufstocken, Teilverkauf, PRÜFEN (Istgewicht fehlt)
    if any(k in order for k in ("zielgewicht", "prev", "new", "delta")):
        return True
    if "VERKAUF" in act:
        return tk in depot_norm
    if "KAUF" in act:
        return tk not in depot_norm
    return True


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
    if quelle in ("rsl_levy", "lowprice", "dividend") and isinstance(data, dict):
        k = len(data.get("kaufen") or [])
        v = len(data.get("verkaufen") or [])
        if k or v:
            return f"{label}: {k + v} Trades ({k} Kaufen · {v} Verkaufen)"
        if not (data.get("meine_aktien") or _ranking_positions(data)):
            return f"{label}: Depot leer — LIVE-Zelle in Colab ausführen"
        meine = data.get("meine_aktien") or []
        if data.get("ziel_ticker") and not meine:
            return f"{label}: Depot leer — MEINE_POSITIONEN in Colab setzen"
    return f"{label}: keine Handelsanweisungen in JSON"


JSON_TOP_META_KEYS = frozenset({
    "handelsanweisungen", "orders", "verkaufen", "kaufen",
    "kassandra_ampel", "score", "score_smooth", "score_raw", "score_heute",
    "score_details", "naechster_check", "naechster_handel", "etf_check_heute", "depot",
    "rebal_freq", "crash_exit_day",
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
    # RSL Levy Momentum
    "positionen", "params", "cash_usd", "depotwert_usd",
    "exit_dist_max", "exit_dist_min", "ma_period", "universe", "n_positions",
    "breadth_pct", "spy_ok", "invest_quote", "kandidaten",
    "stop_mode", "atr_period", "atr_sl_mult", "atr_tp_mult", "trailing_pct",
    "modus", "top_isins", "regel_text",
    "version", "n_us", "n_eu", "n_apac", "ts_live_enabled",
    "build_phase", "cash_eur", "isin",
    "zielportfolio", "signal_status", "signal_monat", "strategie",
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


def _etf_exit_cfg(raw=None, state=None):
    """Exit-Modus aus etf_eingabe / portfolio_state (v6.1: SL fest ab Rebal-Kurs)."""
    raw = raw if isinstance(raw, dict) else {}
    state = state if isinstance(state, dict) else {}
    modus = str(state.get("exit_modus") or raw.get("exit_modus") or "ts").lower()
    ts_pct = safe_float(state.get("trailing_pct")) or safe_float(raw.get("trailing_pct")) or 0.10
    sl_pct = safe_float(state.get("stop_loss_pct")) or safe_float(raw.get("stop_loss_pct"))
    if sl_pct is None and modus == "sl":
        sl_pct = ts_pct
    if sl_pct is None:
        sl_pct = 0.10
    return modus, sl_pct, ts_pct


def parse_etf_portfolio(raw, state=None):
    """positionen[] aus etf_eingabe.json → Ticker-Dict + aktive Exit-%-Einstellung."""
    if isinstance(raw, dict) and "positionen" in raw:
        pos = {
            p["ticker"]: p
            for p in raw.get("positionen", [])
            if isinstance(p, dict) and p.get("ticker")
        }
        modus, sl_pct, ts_pct = _etf_exit_cfg(raw, state)
        pct = sl_pct if modus == "sl" else ts_pct
        return pos, pct
    modus, sl_pct, ts_pct = _etf_exit_cfg(raw, state)
    return (raw if isinstance(raw, dict) else {}), (sl_pct if modus == "sl" else ts_pct)


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
    if weekday is None:
        return heute + timedelta(days=7)
    tage = (weekday - heute.weekday()) % 7
    if tage == 0:
        tage = 7
    return heute + timedelta(days=tage)


def naechster_check_tag(weekday):
    """Nächster Signal-Check — heute zählt mit, wenn heute Check-Tag ist."""
    heute = date.today()
    if weekday is None:
        return heute
    tage = (weekday - heute.weekday()) % 7
    return heute + timedelta(days=tage)


def handel_nach_check(check_datum, handel_wd):
    """Erster Handelstag nach dem Signal-Check."""
    d = check_datum + timedelta(days=1)
    if handel_wd is None:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    for _ in range(8):
        if d.weekday() == handel_wd:
            return d
        d += timedelta(days=1)
    return d


def letzter_wochentag(weekday):
    heute = date.today()
    if weekday is None:
        return heute - timedelta(days=7)
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


def sp100_status_display(puffer, raw_status=None):
    """RSL-Trail-Status — STOP bei puffer ≤ 0 (wie Anstehende Transaktionen)."""
    if puffer is not None and puffer <= 0:
        return "🔴 STOP"
    if raw_status == "WARNUNG" or (puffer is not None and puffer < 10):
        return "🟡 WARNUNG"
    if raw_status == "Beobachten":
        return "🟡 Beobachten"
    if raw_status == "OK":
        return "🟢 OK"
    return status_icon(puffer, 10)


# Abgestimmt mit Colab-Hauptscripts (Stand Jun 2026)
CHECK_ZEITEN = {
    "sp100": {
        "label": "📈 S&P 100",
        "frequenz": "wöchentlich",
        "check_tag": 2,       # Mi Signal
        "handel_tag": 3,      # Do 15:30 US
        "handel_uhrzeit": "15:30",
        "hinweis": "Mi EOD → Do 15:30 US",
    },
    "rsl_levy": {
        "label": "📐 RSL Levy Momentum",
        "frequenz": "täglich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "09:30",
        "hinweis": "Täglich EOD → nächste US-Eröffnung · S&P500+Vol",
    },
    "ivy": {
        "label": "🏛 Ivy Hybrid-RAA",
        "frequenz": "monatlich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "09:00 / 15:30",
        "hinweis": "Monatsende → 1. Handelstag · QM-Exit Top40% · Ampel SPY/VIX · TS aus",
    },
    "lowprice": {
        "label": "💵 LowPrice Rank",
        "frequenz": "täglich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "15:30",
        "hinweis": "Täglich EOD → nächste US-Eröffnung · Preisrang + ATR-Stop 6×",
    },
    "dividend": {
        "label": "💰 Dividende Einfach",
        "frequenz": "monatlich",
        "check_tag": None,
        "handel_tag": None,
        "handel_uhrzeit": "09:00",
        "hinweis": "Monatsende → 1. Handelstag · Research-Exit · max. 20 Titel",
    },
}

STOP_CFG = {
    "sp100": {
        "pct": 0.35, "typ": "RSL-Trail", "basis": "rsl_peak", "active": True,
        "regel": (
            "35% RSL-Peak-Trail — Verkauf wenn RSL 35% unter dem "
            "eigenen RSL-Hoch fällt (kein Kurs-Trailing-Stop)"
        ),
    },
    "rsl_levy": {
        "pct": None, "typ": "RSL+SL/TP", "basis": "entry", "active": True,
        "regel": (
            "RSL-Exit unter Schwelle · SL/TP %- oder ATR-basiert · optional Trail · Ampel"
        ),
    },
    "ivy": {
        "pct": None, "typ": None, "basis": None, "active": False,
        "regel": (
            "Ivy Hybrid-RAA · Quality-Momentum Exit (Score < Top 40%) · "
            "TAA-Ampel SPY/VIX · n=4/4/7 · kein Live-Trailing"
        ),
    },
    "lowprice": {
        "pct": None, "typ": "ATR S/L", "basis": "entry_atr", "active": True,
        "regel": (
            "LowPrice Rank · SL 6×ATR (Kauf-ATR) · Preisband-Exit · Ampel · Next Open"
        ),
    },
    "dividend": {
        "pct": None, "typ": None, "basis": None, "active": False,
        "regel": (
            "Dividende Einfach · Research-Exit (schwache Monate + Drawdown) · "
            "kein Trailing-Stop · monatlich"
        ),
    },
}


def stop_regel(key):
    """Ausführliche Stop-Regel (Hinweise / Info-Box)."""
    raw = None
    if key == "rsl_levy":
        raw = _levy_raw if "_levy_raw" in globals() else {}
    elif key == "lowprice":
        raw = _LP_RAW if "_LP_RAW" in globals() else {}
    elif key == "dividend":
        raw = _DIV_RAW if "_DIV_RAW" in globals() else {}
    if isinstance(raw, dict) and raw.get("regel_text"):
        return raw["regel_text"]
    return STOP_CFG[key]["regel"]


def stop_pct_anzeige(key):
    """Kompakte Exit-Regel je Strategie (Trailing %, RSL, S/L·T/P, ATR)."""
    if key == "rsl_levy":
        return _levy_exit_regel_kurz(_levy_params(_levy_raw if "_levy_raw" in globals() else {}))
    if key == "lowprice":
        raw = _LP_RAW if "_LP_RAW" in globals() else {}
        if isinstance(raw, dict) and raw.get("regel_text"):
            rt = str(raw["regel_text"])
            return rt[:42] + ("…" if len(rt) > 42 else "")
        p = (raw.get("params") or {}) if isinstance(raw, dict) else {}
        sl_m = p.get("sl_atr_mult") or 6.0
        return f"S/L {sl_m:g}×ATR · Preisband"
    if key == "dividend":
        raw = _DIV_RAW if "_DIV_RAW" in globals() else {}
        if isinstance(raw, dict) and raw.get("regel_text"):
            rt = str(raw["regel_text"])
            return rt[:42] + ("…" if len(rt) > 42 else "")
        return "Research-Exit · monatlich"
    if key == "ivy":
        raw = _ivy_raw if "_ivy_raw" in globals() else {}
        if isinstance(raw, dict) and raw.get("regel_text"):
            rt = str(raw["regel_text"])
            if "Top 40" in rt or "Top40" in rt.replace(" ", ""):
                return "QM-Exit < Top40% · Ampel"
            return rt[:42] + ("…" if len(rt) > 42 else "")
        return "QM-Exit < Top40% · Ampel"
    if not STOP_CFG[key].get("active"):
        return "—"
    pct = STOP_CFG[key]["pct"]
    if key == "sp100":
        return f"{int(round(pct * 100))}% RSL"
    if pct is None:
        return STOP_CFG[key].get("typ") or "—"
    return f"{int(round(pct * 100))}%"


def exit_regel_spalte(key, stop=None, tp=None, stop_art=None, entry=None, sl_pct=None, tp_pct=None):
    """Pro Monitor-Zeile: konkrete S/L·T/P-Kurse ($) und % vom Kaufpreis."""
    if key == "rsl_levy" and stop:
        art = stop_art or "SL"
        p = _levy_params(_levy_raw if "_levy_raw" in globals() else {})
        sl_m = tp_m = None
        if _levy_sltp_basis(p) == "atr":
            sl_m = safe_float(p.get("sl_atr_mult")) or 3.0
            tp_m = safe_float(p.get("tp_atr_mult")) or 4.0
        sl_lbl = _levy_level_vs_entry(stop, entry=entry, pct=sl_pct, atr_mult=sl_m)
        if tp:
            tp_lbl = _levy_level_vs_entry(tp, entry=entry, pct=tp_pct, atr_mult=tp_m)
            return f"{art} {sl_lbl} · T/P {tp_lbl}"
        return f"{art} {sl_lbl}"
    if key == "lowprice" and stop:
        art = stop_art or "SL"
        raw = _LP_RAW if "_LP_RAW" in globals() else {}
        p = (raw.get("params") or {}) if isinstance(raw, dict) else {}
        sl_m = safe_float(p.get("sl_atr_mult")) or 6.0
        sl_lbl = _levy_level_vs_entry(stop, entry=entry, pct=sl_pct, atr_mult=sl_m)
        if tp:
            tp_m = safe_float(p.get("tp_atr_mult")) or 0
            tp_lbl = _levy_level_vs_entry(tp, entry=entry, pct=tp_pct, atr_mult=tp_m or None)
            return f"{art} {sl_lbl} · T/P {tp_lbl}"
        return f"{art} {sl_lbl}"
    return stop_pct_anzeige(key)


EXIT_REGEL_COL = "Exit-Regel"
STOP_EXEC_COL = "Exit-Timing"

# Wann ein ausgelöster SL / TP / Trailing-Stop ausgeführt wird:
#   "Gleicher Tag (Intraday)" — sofort / GTC am Markt
#   "Gleicher Tag (Close)"    — zum Tagesende (MOC)
#   "Nächster Tag (Open)"     — nach Close-Check → Verkauf zur nächsten Eröffnung
STOP_EXEC_CFG = {
    "sp100": "Nächster Tag (Open)",           # RSL-Peak-Trail nach EOD → nächste Session
    "rsl_levy": None,                         # dynamisch aus params.sl_mode
    "ivy": "Nächster Tag (Open)",             # QM-/Ampel-Exit am Monats-Rebal
    "lowprice": "Nächster Tag (Open)",        # ATR-Stop / Preisband → Folge-Open
    "dividend": "Nächster Tag (Open)",        # Research-Exit am Monats-Rebal
}

_STOP_EXEC_LABELS = {
    "intraday": "Gleicher Tag (Intraday)",
    "gleicher tag (intraday)": "Gleicher Tag (Intraday)",
    "close": "Gleicher Tag (Close)",
    "eod": "Gleicher Tag (Close)",
    "moc": "Gleicher Tag (Close)",
    "markt_close": "Gleicher Tag (Close)",
    "market_close": "Gleicher Tag (Close)",
    "close_same_day": "Gleicher Tag (Close)",
    "gleicher tag (close)": "Gleicher Tag (Close)",
    "next_open": "Nächster Tag (Open)",
    "open": "Nächster Tag (Open)",
    "next": "Nächster Tag (Open)",
    "folge_open": "Nächster Tag (Open)",
    "nächster tag (open)": "Nächster Tag (Open)",
}


def stop_ausfuehrung_anzeige(key):
    """Wann SL/TP/Stop verkauft wird: Intraday · Close · Next Open."""
    if key == "rsl_levy":
        raw = _levy_raw if "_levy_raw" in globals() else {}
        mode = str(_levy_params(raw).get("sl_mode") or "intraday").lower().strip()
        return _STOP_EXEC_LABELS.get(mode, "Gleicher Tag (Intraday)")
    if key == "lowprice":
        raw = _LP_RAW if "_LP_RAW" in globals() else {}
        mode = str(((raw or {}).get("params") or {}).get("sl_mode") or "next_open").lower().strip()
        return _STOP_EXEC_LABELS.get(mode, "Nächster Tag (Open)")
    val = STOP_EXEC_CFG.get(key, "—")
    if val is None:
        return "—"
    return _STOP_EXEC_LABELS.get(str(val).lower(), val)


def exit_timing_kurz(key):
    """Kurzform: Sofort (Intraday) · Markt Close · Next Open."""
    full = stop_ausfuehrung_anzeige(key)
    if "Intraday" in full:
        return "Sofort (Intraday)"
    if "Close" in full:
        return "Markt Close"
    if "Open" in full:
        return "Next Open"
    return full or "—"


def _strategie_key_from_label(label):
    for k, cfg in CHECK_ZEITEN.items():
        if cfg.get("label") == label:
            return k
    s = str(label or "")
    if "S&P 100" in s or "SP100" in s:
        return "sp100"
    if "Levy" in s:
        return "rsl_levy"
    if "LowPrice" in s or "Low Price" in s:
        return "lowprice"
    if "Dividende" in s or "Dividend" in s:
        return "dividend"
    if "IVY" in s or "RAA" in s:
        return "ivy"
    return None


def _letzter_boersentag(ref=None):
    d = ref or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _naechster_boersentag(ref=None):
    d = (ref or date.today()) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def format_naechster_check(key, ci):
    """Geplanter nächster Signal-Check (Rhythmus der Strategie)."""
    base = format_datum(ci["check_datum"])
    cfg = CHECK_ZEITEN[key]
    freq = cfg.get("frequenz") or ""
    if freq == "täglich":
        return f"{base} · täglich EOD"
    if freq == "monatlich":
        return f"{base} · Monatsende"
    check_tag = cfg.get("check_tag")
    # z. B. Strategie ohne festen Wochentag → kein List-Index auf None
    if not isinstance(check_tag, int) or not (0 <= check_tag <= 6):
        return f"{base} · {freq} EOD" if freq else f"{base} · EOD"
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][check_tag]
    if freq == "2-wöchentlich":
        return f"{base} · {wd} EOD (2-wöchentlich)"
    if freq == "4-wöchentlich":
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
    if cfg["frequenz"] == "täglich":
        daten = _letzter_boersentag()
        handel = _naechster_boersentag(daten)
    elif cfg["frequenz"] == "monatlich":
        daten = letzter_handelstag_monat()
        heute = date.today()
        if heute > daten:
            daten = naechster_monatscheck()
        handel = daten + timedelta(days=1)
        while handel.weekday() >= 5:
            handel += timedelta(days=1)
    else:
        check_wd = cfg.get("check_tag")
        handel_wd = cfg.get("handel_tag")
        if check_wd is None:
            # ohne festen Wochentag — kein Crash in Einzelorders
            daten = date.today()
            handel = handel_nach_check(daten, handel_wd)
        else:
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


# ── Breakout Meta-Labeling ───────────────────────────────────────────────────
_BM_PROFIT = 0.10
_BM_STOP = 0.05
_BM_HOLD = 20
_BM_MAX_POS = 10
_BM_PORTFOLIO_FILE = Path(__file__).resolve().parent / "breakout_meta_portfolio.json"


def _bm_parse_date(val):
    if val is None:
        return None
    try:
        return pd.Timestamp(val).date()
    except Exception:
        return None


def _bm_handelstage(start, end):
    d, n = start, 0
    while d < end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _bm_load_portfolio_file():
    if _BM_PORTFOLIO_FILE.exists():
        try:
            return json.loads(_BM_PORTFOLIO_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _bm_save_portfolio(portfolio):
    try:
        _BM_PORTFOLIO_FILE.write_text(
            json.dumps(portfolio, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _bm_get_portfolio(bm_raw=None):
    """Depot: Session-State → JSON-Feld → lokale Datei."""
    if "bm_portfolio_edit" in st.session_state and st.session_state.bm_portfolio_edit:
        return dict(st.session_state.bm_portfolio_edit)
    if isinstance(bm_raw, dict) and bm_raw.get("portfolio"):
        return dict(bm_raw["portfolio"])
    return _bm_load_portfolio_file()


def _bm_signals(bm_raw):
    if isinstance(bm_raw, dict):
        return bm_raw.get("signals") or []
    if isinstance(bm_raw, list):
        return bm_raw
    return []


def _bm_quote(ticker):
    q = eodhd_quote(ticker_fix(f"{ticker}.US"))
    if not q:
        q = eodhd_quote(ticker_fix(ticker))
    return q


def _bm_live_usd(ticker):
    q = _bm_quote(ticker)
    return float(q["close"]) if q and q.get("close") else None


def _bm_compute_actions(signals, portfolio):
    heute = date.today()
    verkaufen, halten, kaufen = [], [], []
    for ticker, pos in (portfolio or {}).items():
        ep = float(pos.get("entry_price") or 0)
        if ep <= 0:
            continue
        edate = _bm_parse_date(pos.get("entry_date"))
        target = ep * (1 + _BM_PROFIT)
        stop = ep * (1 - _BM_STOP)
        days = _bm_handelstage(edate, heute) if edate else None
        curr = _bm_live_usd(ticker)
        ret = ((curr / ep) - 1) if curr else None
        if curr and curr >= target:
            verkaufen.append((ticker, f"🎯 Ziel +{_BM_PROFIT:.0%} (${curr:.2f})", "Sofort"))
        elif curr and curr <= stop:
            verkaufen.append((ticker, f"🛑 Stop −{_BM_STOP:.0%} (${curr:.2f})", "Sofort"))
        elif days is not None and days >= _BM_HOLD:
            verkaufen.append((ticker, f"⏱ Zeitlimit {days}/{_BM_HOLD} Tage", "Plan"))
        else:
            halten.append(ticker)
    freie = max(0, _BM_MAX_POS - len(halten))
    for s in signals:
        if not s.get("take", True):
            continue
        tk = s.get("ticker")
        if not tk or tk in portfolio:
            continue
        mp = s.get("meta_prob")
        ziel = s.get("target")
        stp = s.get("stop")
        sig_d = s.get("signal_date") or s.get("date")
        kurs_d = s.get("price_date")
        grund = f"Meta Top-20%"
        if sig_d:
            try:
                grund += f" · Signal {pd.Timestamp(sig_d).strftime('%d.%m.%Y')}"
            except Exception:
                grund += f" · Signal {sig_d}"
        if kurs_d:
            try:
                grund += f" · Kurs {pd.Timestamp(kurs_d).strftime('%d.%m.%Y')}"
            except Exception:
                grund += f" · Kurs {kurs_d}"
        if mp is not None:
            grund += f" · P={float(mp):.0%}"
        if ziel and stp:
            grund += f" · Ziel ${ziel:.2f} / Stop ${stp:.2f}"
        kaufen.append((tk, grund, mp, "Plan"))
    return verkaufen, halten, kaufen[:freie]


def _bm_txn_count(bm_raw, portfolio=None):
    sigs = _bm_signals(bm_raw)
    port = portfolio if portfolio is not None else _bm_get_portfolio(bm_raw)
    vk, _, kf = _bm_compute_actions(sigs, port)
    return len(vk) + len(kf)


def _bm_stop_status(kurs, stop, target):
    if kurs is None:
        return "⬜ —"
    if kurs <= stop:
        return "🔴 STOP"
    if kurs >= target:
        return "🟢 ZIEL"
    puf = puffer_pct(kurs, stop)
    if puf is not None and puf < 3:
        return "🟡 Nahe Stop"
    return "🟢 OK"


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


def _normalize_ivy_ampel(val):
    """Colab/JSON/Live → green|yellow|red|None (kein Verkauf bei unbekannt)."""
    if val is None:
        return None
    s = str(val).strip().upper()
    for ch in ("🟢", "🟡", "🔴", "⚪", "✅", "⚠️"):
        s = s.replace(ch, "")
    s = (
        s.replace("Ü", "U").replace("Ä", "A").replace("Ö", "O")
        .replace("É", "E").strip()
    )
    if not s or s in ("UNKNOWN", "N/A", "NA", "NONE", "—", "-"):
        return None
    if s in ("GREEN", "GRUEN", "GRUN") or s.startswith("GRUN") or s.startswith("GREEN"):
        return "green"
    if s in ("YELLOW", "GELB") or s.startswith("GELB") or s.startswith("YELLOW"):
        return "yellow"
    if s in ("RED", "ROT") or s.startswith("ROT") or s.startswith("RED"):
        return "red"
    return None


_IVY_AMPEL_META = {
    "green": ("🟢 GRÜN", "Voll investiert"),
    "yellow": ("🟡 GELB", f"Defensiv — {int(IVY_YELLOW_SAFE_W * 100)}% {IVY_SAFE_ASSET}"),
    "red": ("🔴 ROT", f"100% {IVY_SAFE_ASSET} — alle Aktien verkaufen!"),
}


def _ivy_ampel_effective(live=None, ivy_raw=None):
    """
    Effektive Ivy-Ampel für Orders: Colab-JSON hat Vorrang.
    Live nur wenn bestätigt (green/yellow/red) — nie bei Datenlücken.
    """
    json_code = None
    if isinstance(ivy_raw, dict):
        json_code = _normalize_ivy_ampel(ivy_raw.get("ampel"))
    live = live if isinstance(live, dict) else {}
    live_code = live.get("ampel") if live.get("ampel") in ("green", "yellow", "red") else None
    code = json_code or live_code
    if not code:
        return {
            "ampel": "unknown",
            "label": "⚪ UNBEKANNT",
            "aktion": "Ampel nicht bestätigt — kein Sofort-Verkauf",
            "source": "none",
        }
    label, aktion = _IVY_AMPEL_META[code]
    src = "json" if json_code else "live"
    if code == live_code and live.get("aktion") and not json_code:
        aktion = live.get("aktion") or aktion
        label = live.get("label") or label
    return {"ampel": code, "label": label, "aktion": aktion, "source": src}


@st.cache_data(ttl=3600)
def ivy_markt_ampel():
    monthly = eodhd_eod_series(IVY_SPY_TICKER)
    spy_rt = eodhd_kurs(IVY_SPY_TICKER)
    vix = None
    for vt in IVY_VIX_TICKERS:
        vix = eodhd_kurs(vt)
        if vix:
            break
    incomplete = {
        "ampel": "unknown",
        "label": "⚪ UNBEKANNT",
        "aktion": "Ampel nicht berechenbar — EOD-Daten fehlen",
        "spy": spy_rt, "sma": None, "vix": vix, "spy_vs_sma_pct": None,
        "incomplete": True,
    }
    if monthly is None or monthly.empty:
        return incomplete
    monthly = monthly.resample("ME").last().dropna()
    if spy_rt and len(monthly) > 0:
        monthly.iloc[-1] = spy_rt
    spy_now = float(monthly.iloc[-1]) if len(monthly) else spy_rt
    if spy_now is None or len(monthly) < IVY_TREND_MONTHS:
        return {**incomplete, "spy": spy_now}
    sma_now = float(monthly.rolling(IVY_TREND_MONTHS).mean().iloc[-1])
    if spy_now >= sma_now:
        ampel = "yellow" if (vix and vix > IVY_VIX_THRESHOLD) else "green"
    else:
        ampel = "red"
    label, aktion = _IVY_AMPEL_META[ampel]
    spy_vs = round((spy_now / sma_now - 1) * 100, 2) if sma_now else None
    return {"ampel": ampel, "label": label, "aktion": aktion,
            "spy": spy_now, "sma": sma_now, "vix": vix, "spy_vs_sma_pct": spy_vs,
            "incomplete": False}


def safe_float(x, *, allow_nonpositive=False):
    """None/NaN → None. Preise default: ≤0 → None. Abstände/%: allow_nonpositive=True."""
    if x is None:
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        if not allow_nonpositive and v <= 0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def safe_pct(x):
    """Prozent-/Abstands-Werte inkl. 0 und negativ (z. B. MA-Dist −2.5%)."""
    return safe_float(x, allow_nonpositive=True)


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

SP100_POS = normalize_sp100_json(lade_json_github("sp100_positionen.json", _JSON_REFRESH) or {})
_levy_raw = lade_json_github("rsl_levy_positionen.json", _JSON_REFRESH) or {}
_ivy_raw = lade_json_github("ivy_portfolio.json", _JSON_REFRESH) or {}
IVY_POS = portfolio_ohne_meta(_ivy_raw)
_ivy_ver = str((_ivy_raw or {}).get("version") or "").strip()
if _ivy_ver:
    CHECK_ZEITEN["ivy"]["label"] = f"🏛 Ivy {_ivy_ver} Hybrid-RAA"
_ivy_regel = (_ivy_raw or {}).get("regel_text") if isinstance(_ivy_raw, dict) else None
if _ivy_regel:
    STOP_CFG["ivy"]["regel"] = _ivy_regel
_etf_raw = {}
ETF_STATE = {}
ETF_POS, ETF_TS = {}, 0.10
_LP_RAW = lade_json_github("lowprice_positionen.json", _JSON_REFRESH) or {}
_DIV_RAW = lade_json_github("dividend_positionen.json", _JSON_REFRESH) or {}
SP100_DEPOT = sp100_depot_ticker(SP100_POS)


def _dauer_positions(raw):
    """Depot aus dauerlaeufer_positionen.json (Top-Level-Ticker mit einstieg)."""
    if not isinstance(raw, dict):
        return {}
    pos = portfolio_ohne_meta(raw)
    if pos:
        return pos
    # Fallback: meine_aktien / ziel + stock_data (falls Top-Level-Marker fehlen)
    out = {}
    for tk in raw.get("meine_aktien") or raw.get("ziel_aktien") or raw.get("ziel_ticker") or []:
        short = _dauer_short(tk)
        if not short or short in out:
            continue
        info = _dauer_stock_info(raw, tk)
        top = raw.get(short) or raw.get(tk) or {}
        if not isinstance(top, dict):
            top = {}
        entry = {
            "name": (top.get("name") or info.get("name") or short),
            "einstieg": top.get("einstieg") or info.get("kurs_usd"),
            "hoch": top.get("hoch") or info.get("kurs_usd"),
            "ma_dist_pct": top.get("ma_dist_pct") or info.get("ma_dist_pct"),
            "shares": top.get("shares"),
        }
        out[short] = entry
    return out


def _dauer_name(ticker, pos=None, info=None):
    """Firmenname für Dauerläufer — ignoriert Ticker-als-Name aus JSON."""
    short = _dauer_short(ticker)
    merged = {}
    if isinstance(info, dict):
        merged.update(info)
    if isinstance(pos, dict):
        merged.update(pos)
    raw_name = (merged.get("name") or "").strip()
    if raw_name and not is_weak_name(raw_name, short):
        return raw_name
    # Weak/fehlend → Lookup (EODHD / Maps), ohne Weak-Name in pos zu erzwingen
    clean = {k: v for k, v in merged.items() if k != "name"}
    return _stock_name(short, pos=clean or None) or ""


def _dauer_short(ticker):
    return str(ticker or "").replace(".US", "").split(".")[0].upper()


def _dauer_stock_info(raw, ticker):
    """stock_data-Eintrag für Ticker (mit/ohne .US)."""
    if not isinstance(raw, dict):
        return {}
    sd = raw.get("stock_data") or {}
    if not isinstance(sd, dict):
        return {}
    tk = str(ticker or "")
    short = _dauer_short(tk)
    return sd.get(tk) or sd.get(short) or sd.get(f"{short}.US") or {}


def _dauer_exit_max(raw):
    """Exit-Schwelle (MA-Abstand %), Default −6."""
    if isinstance(raw, dict):
        for k in ("exit_dist_max", "exit_max"):
            v = safe_float(raw.get(k))
            if v is not None:
                return v
        params = raw.get("params") or {}
        if isinstance(params, dict):
            v = safe_float(params.get("exit_dist_max"))
            if v is not None:
                return v
    return -6.0


def _dauer_is_exit(ma_dist_pct, raw=None):
    """True wenn MA-Abstand im Exit-Band (Default −100 … −6)."""
    if ma_dist_pct is None:
        return False
    try:
        d = float(ma_dist_pct)
    except (TypeError, ValueError):
        return False
    lo = -100.0
    hi = _dauer_exit_max(raw)
    if isinstance(raw, dict):
        v = safe_float(raw.get("exit_dist_min"))
        if v is not None:
            lo = v
        params = raw.get("params") or {}
        if isinstance(params, dict):
            v = safe_float(params.get("exit_dist_min"))
            if v is not None:
                lo = v
    return lo <= d <= hi


# ── Trailing-Stop Zeilen ──────────────────────────────────────────────────────

def build_stop_rows():
    rows = []

    # S&P 100 — RSL-Peak-Trail 35% (RSL-Werte, nicht EUR/USD-Kurs!)
    ci = check_info("sp100")
    rsl_data = SP100_POS.get("rsl_data", {})
    _sp100_verk = {str(t).upper() for t in (SP100_POS.get("verkaufen") or [])}
    for ticker, info in rsl_data.items():
        if SP100_DEPOT is not None and ticker not in SP100_DEPOT:
            continue
        # Rebalance-Verkäufe stehen unter Transaktionen, nicht als Trailing-Stop
        if str(ticker).upper() in _sp100_verk:
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
            EXIT_REGEL_COL: stop_pct_anzeige("sp100"),
            **signal_spalten("sp100", ci, SP100_POS),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker_anzeige,
            "Name": name or "—",
            "Akt. Kurs": kurs_anzeige,
            "Peak/Hoch": peak_anzeige,
            "Stop-Kurs": f"RSL {trail:.3f}",
            "% zum Stop": f"{puf:+.1f}% (RSL)" if puf is not None else "—",
            "Status": sp100_status_display(puf, live.get("status")),
        })

    # RSL Levy Momentum — RSL-Exit · SL · TP (USD; %- oder ATR-basiert)
    ci = check_info("rsl_levy")
    levy_params = _levy_params(_levy_raw)
    rsl_exit = safe_float(levy_params.get("rsl_exit_below")) or 0.99
    levy_atr = _levy_sltp_basis(levy_params) == "atr"
    sl_atr_m = safe_float(levy_params.get("sl_atr_mult")) or 3.0
    tp_atr_m = safe_float(levy_params.get("tp_atr_mult")) or 4.0
    for tk, p in _levy_positions(_levy_raw).items():
        if not p.get("entry_price"):
            continue
        live = _levy_live_position(tk, p, _levy_raw)
        stop = live.get("stop")
        if not stop:
            continue
        kurs = live.get("kurs")
        tp = live.get("tp")
        puf = live.get("puffer")
        rsl = live.get("rsl")
        q = live.get("quote")
        peak = safe_float(p.get("peak_usd"))
        art = p.get("stop_art") or "SL"
        entry = p.get("entry_price")
        sl_pct = p.get("sl_pct")
        tp_pct = p.get("tp_pct")
        stop_core = _levy_level_vs_entry(
            stop, entry=entry, pct=sl_pct, atr_mult=sl_atr_m if levy_atr else None)
        stop_lbl = f"{stop_core} ({art})" if stop_core else "—"
        if tp:
            tp_core = _levy_level_vs_entry(
                tp, entry=entry, pct=tp_pct, atr_mult=tp_atr_m if levy_atr else None)
            peak_lbl = ((f"Peak ${peak:.2f} · " if peak else "") + f"TP {tp_core}")
        else:
            peak_lbl = f"${peak:.2f}" if peak else "—"
        rows.append({
            "Strategie": ci["label"],
            EXIT_REGEL_COL: exit_regel_spalte(
                "rsl_levy", stop=stop, tp=tp, stop_art=art,
                entry=entry, sl_pct=sl_pct, tp_pct=tp_pct,
            ),
            **signal_spalten("rsl_levy", ci, _levy_raw),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": tk,
            "Name": p.get("name") or _stock_name(tk, pos=p) or "—",
            "Akt. Kurs": format_akt_kurs(kurs, tk, q, currency="USD") if kurs else "—",
            "Peak/Hoch": peak_lbl,
            "Stop-Kurs": stop_lbl,
            "% zum Stop": fmt_pct(puf) if puf is not None else "—",
            "Status": levy_status_display(puf, rsl, rsl_exit, live.get("status")),
        })

    # LowPrice Rank — ATR-Stop (USD, Next Open)
    ci = check_info("lowprice")
    lp_params = (_LP_RAW.get("params") or {}) if isinstance(_LP_RAW, dict) else {}
    sl_atr_m = safe_float(lp_params.get("sl_atr_mult")) or 6.0
    tp_atr_m = safe_float(lp_params.get("tp_atr_mult")) or 0
    for tk, p in _ranking_positions(_LP_RAW).items():
        if not isinstance(p, dict):
            continue
        stop = safe_float(p.get("stop_level"))
        if not stop:
            continue
        entry = safe_float(p.get("entry_price") or p.get("einstieg"))
        tp = safe_float(p.get("tp_level"))
        kurs = safe_float(p.get("kurs_usd") or p.get("kurs"))
        q = eodhd_quote(ticker_fix(tk))
        if q and q.get("close"):
            kurs = float(q["close"])
        puf = puffer_pct(kurs, stop) if kurs and stop else safe_pct(p.get("puffer_pct"))
        art = p.get("stop_art") or "SL"
        stop_core = _levy_level_vs_entry(stop, entry=entry, atr_mult=sl_atr_m)
        stop_lbl = f"{stop_core} ({art})" if stop_core else f"${stop:.2f}"
        peak = safe_float(p.get("peak_usd") or p.get("hoch"))
        if tp:
            tp_core = _levy_level_vs_entry(
                tp, entry=entry, atr_mult=tp_atr_m if tp_atr_m else None)
            peak_lbl = ((f"Peak ${peak:.2f} · " if peak else "") + f"TP {tp_core}")
        else:
            peak_lbl = f"${peak:.2f}" if peak else "—"
        status = "🔴 STOP" if (puf is not None and puf <= 0) else (
            "🟡 Gefahr" if (puf is not None and puf < 5) else "🟢 OK"
        )
        if kurs and tp and kurs >= tp:
            status = "🟢 TP"
        rows.append({
            "Strategie": ci["label"],
            EXIT_REGEL_COL: exit_regel_spalte(
                "lowprice", stop=stop, tp=tp, stop_art=art, entry=entry,
            ),
            **signal_spalten("lowprice", ci, _LP_RAW),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": tk,
            "Name": p.get("name") or _stock_name(tk, pos=p) or "—",
            "Akt. Kurs": format_akt_kurs(kurs, tk, q, currency="USD") if kurs else "—",
            "Peak/Hoch": peak_lbl,
            "Stop-Kurs": stop_lbl,
            "% zum Stop": fmt_pct(puf) if puf is not None else "—",
            "Status": status,
        })

    for r in rows:
        key = _strategie_key_from_label(r.get("Strategie"))
        r[STOP_EXEC_COL] = exit_timing_kurz(key) if key else "—"
    return rows


_JSON_BY_STRATEGY = {
    "sp100": lambda: SP100_POS,
    "rsl_levy": lambda: _levy_raw,
    "ivy": lambda: _ivy_raw,
    "lowprice": lambda: _LP_RAW,
    "dividend": lambda: _DIV_RAW,
}

_TXN_PRIO = {"Sofort": 0, "Hoch": 1, "Normal": 2, "Plan": 3}
# Reihenfolge wie Colab-Notebooks / Nutzerwunsch
_TXN_STRATEGY_ORDER = (
    "rsl_levy",
    "lowprice",
    "dividend",
    "sp100",
    "ivy",
)


def _txn_side(aktion):
    a = str(aktion or "").upper()
    if "ALLE VERKAUF" in a or "VERKAUF" in a or "REDUZ" in a:
        return "verk"
    if "KAUF" in a or "AUFSTOCK" in a:
        return "kauf"
    if "PRÜF" in a or "PRUEF" in a:
        return "pruef"
    return "other"


def _txn_aktion_kurz(aktion):
    a = str(aktion or "").upper()
    if "ALLE VERKAUF" in a:
        return "🔴 Alle"
    if "VERKAUF" in a or "REDUZ" in a:
        return "🔴 Verkaufen"
    if "AUFSTOCK" in a:
        return "🟢 Aufstocken"
    if "KAUF" in a:
        return "🟢 Kaufen"
    if "PRÜF" in a or "PRUEF" in a:
        return "🟡 Prüfen"
    s = str(aktion or "—").strip()
    return s[:18] + "…" if len(s) > 18 else s


def _strategy_depot_simple(key, raw, etf_state=None):
    """Aktuelle Positionen: nur Ticker + Name."""
    raw = raw if isinstance(raw, dict) else {}
    rows = []

    def _add(tk, name=""):
        tk = str(tk or "").strip()
        if not tk or tk == "—":
            return
        rows.append({"Ticker": tk, "Name": str(name or "").strip() or "—"})

    if key == "sp100":
        rsl = raw.get("rsl_data") or {}
        for tk in raw.get("meine_aktien") or []:
            info = rsl.get(tk) if isinstance(rsl, dict) else {}
            _add(tk, (info or {}).get("name") if isinstance(info, dict) else "")
    elif key == "rsl_levy":
        for tk, p in sorted(_levy_positions(raw).items()):
            _add(tk, p.get("name") if isinstance(p, dict) else "")
    elif key in ("lowprice", "dividend"):
        pos = _ranking_positions(raw)
        if pos:
            for tk, p in sorted(pos.items()):
                _add(tk, p.get("name") if isinstance(p, dict) else "")
        else:
            for tk in raw.get("meine_aktien") or []:
                _add(tk, "")
    elif key == "ivy":
        for tk, p in sorted(portfolio_ohne_meta(raw).items()):
            _add(tk, p.get("name") if isinstance(p, dict) else "")
    return rows


def _simple_order_df(rows):
    """Anstehende Käufe/Verkäufe: Aktion, Ticker, Name."""
    return pd.DataFrame([
        {
            "Aktion": _txn_aktion_kurz(r.get("Aktion")),
            "Ticker": r.get("Ticker") or "—",
            "Name": (r.get("Name") or "—")[:40],
        }
        for r in rows
    ])


def _style_simple_orders(df):
    return df.style.map(
        lambda v: (
            "color:#ff1744;font-weight:600"
            if str(v).startswith("🔴")
            else ("color:#00c853;font-weight:600" if str(v).startswith("🟢") else "")
        ),
        subset=["Aktion"],
    )


def _render_simple_table(title, df):
    st.markdown(f"**{title}**")
    if df is None or df.empty:
        st.caption("— keine —")
        return
    st.dataframe(
        df if "Aktion" not in df.columns else _style_simple_orders(df),
        use_container_width=True,
        hide_index=True,
        height=min(max(38 + len(df) * 35, 72), 320),
    )


def render_transactions_by_strategy(txn_rows, txn_json=None):
    """Einzelorders: pro Strategie nur Depot (Ticker/Name) + anstehende Käufe/Verkäufe."""
    tj = txn_json or {}
    groups = {}
    for r in txn_rows or []:
        groups.setdefault(r.get("_key", "other"), []).append(r)

    def _sort_txn(lst):
        return sorted(
            lst,
            key=lambda r: (_TXN_PRIO.get(r.get("Priorität"), 9), r.get("Ticker", "")),
        )

    def _raw_for(key):
        mapping = {
            "sp100": tj.get("sp100", SP100_POS),
            "rsl_levy": tj.get("rsl_levy", _levy_raw),
            "lowprice": tj.get("lowprice", _LP_RAW),
            "dividend": tj.get("dividend", _DIV_RAW),
            "ivy": tj.get("ivy", _ivy_raw),
        }
        return mapping.get(key) or {}

    etf_state = {}

    for key in _TXN_STRATEGY_ORDER:
        if key not in CHECK_ZEITEN:
            continue
        ci = check_info(key)
        group = groups.get(key) or []
        verk = _sort_txn([r for r in group if _txn_side(r.get("Aktion")) == "verk"])
        kauf = _sort_txn([r for r in group if _txn_side(r.get("Aktion")) == "kauf"])
        pruef = _sort_txn([r for r in group if _txn_side(r.get("Aktion")) == "pruef"])
        other = _sort_txn([r for r in group if _txn_side(r.get("Aktion")) == "other"])
        # Sonstige (z. B. Ampel) den Verkäufen zuordnen, wenn Aktion Verkauf nahelegt
        for r in other:
            side = _txn_side(r.get("Aktion"))
            if side == "verk":
                verk.append(r)
            elif side == "kauf":
                kauf.append(r)
            else:
                # ALLE / defensiv → Verkäufe
                a = str(r.get("Aktion") or "").upper()
                if "ALLE" in a or "SHY" in str(r.get("Grund / Details") or "").upper():
                    verk.append(r)

        depot = _strategy_depot_simple(key, _raw_for(key), etf_state=etf_state)
        n_open = len(verk) + len(kauf) + len(pruef)
        title = f"{ci['label']} · Depot {len(depot)}"
        if n_open:
            title += f" · {n_open} Trade{'s' if n_open != 1 else ''}"

        with st.expander(title, expanded=False):
            _render_simple_table(
                "Aktuelle Positionen",
                pd.DataFrame(depot) if depot else pd.DataFrame(columns=["Ticker", "Name"]),
            )
            st.markdown("**Anstehende Käufe / Verkäufe**")
            if verk or kauf or pruef:
                c1, c2 = st.columns(2)
                with c1:
                    _render_simple_table("Verkäufe", _simple_order_df(verk) if verk else pd.DataFrame())
                with c2:
                    _render_simple_table("Käufe", _simple_order_df(kauf) if kauf else pd.DataFrame())
                if pruef:
                    _render_simple_table("Prüfen", _simple_order_df(pruef))
            else:
                st.caption("Keine anstehenden Käufe oder Verkäufe.")


def _txn_row(key, aktion, ticker, name, grund, prioritaet="Normal", meta_prob=None):
    ci = check_info(key)
    row = {
        "Strategie": ci["label"],
        "Priorität": prioritaet,
        "Aktion": aktion,
        "Ticker": ticker or "—",
        "Name": name or "—",
        "Meta P": "—",
        "Grund / Details": grund,
        **signal_spalten(key, ci, _JSON_BY_STRATEGY[key]()),
        "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
        "_key": key,
        "_sort": _TXN_PRIO.get(prioritaet, 9),
    }
    if meta_prob is not None:
        try:
            row["Meta P"] = f"{float(meta_prob):.0%}"
        except (TypeError, ValueError):
            row["Meta P"] = str(meta_prob)
    return row


def _etf_ticker_key(ticker):
    """AAPL.US / AAPL → AAPL (Abgleich JSON ↔ Portfolio)."""
    return etf_ticker_canonical(ticker)


def _etf_aktion_kind(aktion: str) -> str:
    a = (aktion or "").upper()
    if "VERKAUF" in a or "REDUZ" in a:
        return "sell"
    if "KAUF" in a or "AUFSTOCK" in a:
        return "buy"
    if "HALTEN" in a:
        return "hold"
    return "other"


def _collapse_etf_alias_trades(handels, etf_pos):
    """
    Colab-Bug: Depot AMD.US vs. Ziel AMD → JSON mit VERKAUF (ziel 0) + KAUF (ist 0).
    Zusammenführen zu einem netto AUFSTOCKEN/REDUZIEREN oder verwerfen (≈ HALTEN).
    """
    if not handels:
        return []
    groups: dict[str, list] = {}
    for rec in handels:
        if not isinstance(rec, dict):
            continue
        tk = _etf_ticker_key(rec.get("ticker"))
        if not tk:
            continue
        groups.setdefault(tk, []).append(rec)

    out = []
    for tk, recs in groups.items():
        if len(recs) == 1:
            out.append(recs[0])
            continue

        sells = [r for r in recs if _etf_aktion_kind(r.get("aktion")) == "sell"]
        buys = [r for r in recs if _etf_aktion_kind(r.get("aktion")) == "buy"]

        def _full_exit(r):
            return (safe_float(r.get("ziel_eur")) or 0.0) < 1.0

        def _full_entry(r):
            return (safe_float(r.get("aktuell_eur")) or 0.0) < 1.0

        phantom = (
            sells
            and buys
            and _etf_in_portfolio(etf_pos, tk)
            and all(_full_exit(r) for r in sells)
            and all(_full_entry(r) for r in buys)
        )

        if phantom:
            ist = max((safe_float(r.get("aktuell_eur")) or 0.0 for r in sells), default=0.0)
            ziel = max((safe_float(r.get("ziel_eur")) or 0.0 for r in buys), default=0.0)
            for k, p in (etf_pos or {}).items():
                if _etf_ticker_key(k) != tk:
                    continue
                ist = max(ist, safe_float(p.get("wert_eur")) or 0.0)
            delta = ziel - ist
            tol = max(50.0, max(ziel, ist) * 0.05)
            if abs(delta) < tol:
                continue
            name = next(
                (r.get("name") for r in buys if r.get("name") and len(str(r.get("name"))) > 4),
                None,
            ) or next((r.get("name") for r in recs if r.get("name")), tk)
            ticker_out = next(
                (r.get("ticker") for r in buys if r.get("ticker")),
                recs[0].get("ticker"),
            )
            out.append({
                "ticker": ticker_out,
                "name": name,
                "aktion": "🟡 AUFSTOCKEN" if delta > 0 else "🟠 REDUZIEREN",
                "ziel_eur": round(ziel, 2),
                "aktuell_eur": round(ist, 2),
                "delta_eur": round(delta, 2),
            })
            continue

        out.extend(recs)
    return out


def _etf_in_portfolio(etf_pos, ticker_key):
    """Ticker im Depot? (kauf_kurs oder Stückzahl gesetzt)."""
    if not ticker_key:
        return False
    for tk, pos in (etf_pos or {}).items():
        if _etf_ticker_key(tk) != ticker_key:
            continue
        if not isinstance(pos, dict):
            continue
        if safe_float(pos.get("kauf_kurs")) or safe_float(pos.get("stueck")):
            return True
    return False


def _filter_etf_handelsanweisungen(handels, etf_pos):
    """
    Entfernt erledigte Monats-Trades + Alias-Doppeltrades (AMD.US vs AMD).
    Gleiche 5%-Toleranz wie erstelle_handelsanweisungen() im Notebook.
    """
    if not handels:
        return []
    handels = _collapse_etf_alias_trades(handels, etf_pos)
    out = []
    for rec in handels:
        if not isinstance(rec, dict):
            continue
        aktion = str(rec.get("aktion") or "")
        if "HALTEN" in aktion:
            continue
        tk = _etf_ticker_key(rec.get("ticker"))
        ziel = safe_float(rec.get("ziel_eur")) or 0.0
        delta = rec.get("delta_eur")
        if delta is not None:
            try:
                d = float(delta)
            except (TypeError, ValueError):
                d = None
            if d is not None and ziel and abs(d) < max(50.0, ziel * 0.05):
                continue
        act_u = aktion.upper()
        is_buy = "KAUF" in act_u or "AUFSTOCK" in act_u
        is_sell = "VERKAUF" in act_u or "REDUZ" in act_u
        in_depot = _etf_in_portfolio(etf_pos, tk)
        if is_sell and not in_depot:
            continue
        if is_buy and in_depot and delta is not None:
            try:
                if abs(float(delta)) < max(50.0, ziel * 0.05):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(rec)
    return out


def _etf_stop_puffer(ticker, pos_item, st, raw=None, state=None):
    """Stop-Puffer: SL fix ab Rebal-Kurs (sl) oder Trailing vom Hoch (ts)."""
    modus, sl_pct, ts_pct = _etf_exit_cfg(raw, state)
    q = eodhd_quote(ticker)
    kurs = safe_float(q["close"]) if q else None
    kurs = kurs or safe_float(pos_item.get("akt_kurs"))
    hoch = (
        safe_float(st.get("hoch_kurs"))
        or safe_float(pos_item.get("hoch_kurs"))
        or kurs
    )
    ref = (
        safe_float(st.get("sl_basis"))
        or safe_float(pos_item.get("sl_basis"))
        or hoch
    )
    stop = safe_float(st.get("stop_level")) or safe_float(pos_item.get("stop_nativ"))
    if stop is None:
        if modus == "sl" and ref:
            stop = round(ref * (1 - sl_pct), 4)
        elif modus == "ts" and hoch:
            stop = round(hoch * (1 - ts_pct), 4)
    kurs_f = kurs or ref or hoch
    puf = puffer_pct(kurs_f, stop)
    if (
        kurs_f and stop
        and min(kurs_f, stop) > 0
        and max(kurs_f, stop) / min(kurs_f, stop) > 8
    ):
        akt_e = safe_float(pos_item.get("akt_eur"))
        stop_e = safe_float(pos_item.get("stop_eur"))
        if akt_e and stop_e:
            return puffer_pct(akt_e, stop_e), akt_e, stop_e, ref, stop, q, modus
    return puf, kurs_f, stop, ref, stop, q, modus


def _etf_exit_label(raw=None, state=None):
    modus, sl_pct, ts_pct = _etf_exit_cfg(raw, state)
    if modus == "sl":
        return f"Stop-Loss −{int(round(sl_pct * 100))}%"
    return f"{int(round(ts_pct * 100))}% Trailing Stop"


def _etf_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    pos, _ = parse_etf_portfolio(data)
    return _filter_etf_handelsanweisungen(data.get("handelsanweisungen") or [], pos)


def _append_etf_stop_rows(rows, pos, state, ts, key, raw):
    """Stop-Zeilen für ETF Yahoo (SL ab Rebal oder Trailing)."""
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
        st = state_pos.get(ticker, {})
        puf, kurs_f, stop, ref, _, q, _ = _etf_stop_puffer(
            ticker, pos_item, st, raw=raw, state=state,
        )
        rows.append({
            "Strategie": ci["label"],
            EXIT_REGEL_COL: stop_pct_anzeige(key),
            **signal_spalten(key, ci, raw),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Ticker": ticker.replace(".US", "").replace(".TO", ""),
            "Name": _etf_name(ticker, pos=pos_item) or "—",
            "Akt. Kurs": format_akt_kurs(kurs_f, ticker, q),
            "Peak/Hoch": format_kurs(ref, ticker),
            "Stop-Kurs": format_kurs(stop, ticker),
            "% zum Stop": fmt_pct(puf),
            "Status": status_icon(puf, 3),
        })


def _append_etf_transaction_rows(add, etf_raw, etf_state, etf_pos, etf_ts, key):
    """Transaktionszeilen für ETF Yahoo oder EODHD."""
    state_pos = etf_state.get("positionen", {}) if isinstance(etf_state, dict) else {}
    active_keys = {
        _etf_ticker_key(t)
        for t in (state_pos.keys() if state_pos else etf_pos.keys())
    }
    stop_keys = set()
    for ticker, pos in etf_pos.items():
        if not isinstance(pos, dict):
            continue
        if state_pos and ticker not in state_pos:
            continue
        if not pos.get("kauf_kurs"):
            continue
        st = state_pos.get(ticker, {})
        puf, _, _, _, _, _, _ = _etf_stop_puffer(
            ticker, pos, st, raw=etf_raw, state=etf_state,
        )
        if puf is not None and puf <= 0:
            tk = _etf_ticker_key(ticker)
            stop_keys.add(tk)
            add(
                key, "🔴 VERKAUFEN",
                ticker.replace(".US", "").replace(".TO", ""),
                _etf_name(ticker, pos=pos),
                f"{_etf_exit_label(etf_raw, etf_state)} ({fmt_pct(puf)} zum Stop)",
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
            tk = _etf_ticker_key(ticker)
            act_u = aktion.upper()
            if tk in stop_keys and ("KAUF" in act_u or "AUFSTOCK" in act_u):
                continue
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
                _etf_name(ticker, rec=rec),
                " · ".join(parts),
                "Plan",
            )
    else:
        for rec in (etf_raw.get("empfehlung") or [] if isinstance(etf_raw, dict) else []):
            if not isinstance(rec, dict):
                continue
            ticker = rec.get("ticker")
            if not ticker or _etf_ticker_key(ticker) in active_keys:
                continue
            score = rec.get("score")
            score_s = f"Score {score:.2f}" if score is not None else "Screening-Kandidat"
            add(
                key, "🟢 KAUFEN", ticker.replace(".US", "").replace(".TO", ""),
                _etf_name(ticker, rec=rec),
                f"Monats-Rebalancing · {score_s} (noch nicht im Portfolio)",
                "Plan",
            )


def _smallcap_handels_aus_json(data):
    if not isinstance(data, dict):
        return []
    ha = data.get("handelsanweisungen") or []
    if not ha:
        return []
    return filter_smallcap_handelsanweisungen(ha, portfolio_ohne_meta(data))


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

_WARUM_EXPANDER_TITEL = {
    "rsl_levy": "Depot & Signale",
    "lowprice": "Depot & Ranking",
    "dividend": "Depot & Research",
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


def _levy_depot_table(raw):
    """Live-Depot aus rsl_levy_positionen.json."""
    params = _levy_params(raw)
    rsl_exit = safe_float(params.get("rsl_exit_below")) or 0.99
    atr_mode = _levy_sltp_basis(params) == "atr"
    sl_m = safe_float(params.get("sl_atr_mult")) or 3.0
    tp_m = safe_float(params.get("tp_atr_mult")) or 4.0
    rows = []
    for i, (tk, p) in enumerate(sorted(_levy_positions(raw).items()), 1):
        if not isinstance(p, dict):
            continue
        live = _levy_live_position(tk, p, raw)
        stop = live.get("stop")
        tp = live.get("tp")
        parts = [f"Kauf {p.get('entry_date') or '—'}"]
        if p.get("entry_price"):
            parts.append(f"${p.get('entry_price')}")
        if live.get("rsl") is not None:
            parts.append(f"RSL {live.get('rsl'):.3f}")
        if stop and tp:
            entry = p.get("entry_price")
            sl_pct = p.get("sl_pct")
            tp_pct = p.get("tp_pct")
            sl_lbl = _levy_level_vs_entry(
                stop, entry=entry, pct=sl_pct, atr_mult=sl_m if atr_mode else None)
            tp_lbl = _levy_level_vs_entry(
                tp, entry=entry, pct=tp_pct, atr_mult=tp_m if atr_mode else None)
            parts.append(f"SL {sl_lbl} · TP {tp_lbl}")
        rows.append({
            "rang": i,
            "ticker": tk,
            "name": p.get("name") or "",
            "rsl": live.get("rsl"),
            "stop_level": stop,
            "tp_level": tp,
            "puffer_pct": live.get("puffer"),
            "pnl_pct": p.get("pnl_pct"),
            "status": live.get("status") or "DEPOT",
            "begruendung": " · ".join(parts),
        })
    if rows and rsl_exit:
        for row in rows:
            rsl = safe_float(row.get("rsl"))
            if rsl is not None and rsl < rsl_exit:
                row["status"] = "RSL-EXIT"
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
        atr = p.get("atr_entry") or p.get("atr")
        sl = p.get("atr_stop")
        tp = p.get("atr_tp")
        extra = ""
        if sl or tp:
            parts = []
            if sl:
                parts.append(f"SL {sl}")
            if tp:
                parts.append(f"TP {tp}")
            if atr:
                parts.append(f"ATR {atr}")
            extra = " · " + " / ".join(parts)
        rows.append({
            "rang": i,
            "ticker": ticker,
            "name": name,
            "einstieg_eur": kauf,
            "peak_eur": hw,
            "status": "DEPOT",
            "begruendung": f"Kauf {kdat}" + (f" · {kauf} €" if kauf else "") + extra,
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

    if key == "sp100" and not sections:
        rsl_rows = _sp100_rsl_table(raw)
        if rsl_rows:
            regel = (
                "Regel: RSL-Peak-Trail 35% — Verkauf wenn RSL 35% unter "
                "eigenem RSL-Hoch fällt (nicht Kurs-Trailing)."
            )
            cap = f"{regel}\n\n{caption}" if caption else regel
            sections.append(("Depot · RSL-Stand", cap, rsl_rows, _WARUM_COLS))

    if key == "rsl_levy":
        params = _levy_params(raw)
        regel = raw.get("regel_text") or _levy_exit_regel_kurz(params)
        pct = raw.get("invest_pct")
        amp = raw.get("ampel") or "—"
        pct_s = f" · Quote {int(round(float(pct) * 100))}%" if pct is not None else ""
        basis = _levy_sltp_basis(params)
        basis_s = " · SL/TP: ATR" if basis == "atr" else " · SL/TP: %"
        regel_full = f"Regel: {regel} · Ampel {amp}{pct_s}{basis_s}"
        cap = regel_full
        if raw.get("hinweis"):
            cap += f"\n\n{raw['hinweis']}"
        if basis == "atr":
            sl_m = safe_float(params.get("sl_atr_mult")) or 3.0
            tp_m = safe_float(params.get("tp_atr_mult")) or 4.0
            cap += (
                f"\n\nATR-Stops: SL = Entry − {sl_m:g}×ATR · "
                f"TP = Entry + {tp_m:g}×ATR (Wilder ATR14, Fixierung am Kauf)."
            )
        depot_rows = _levy_depot_table(raw)
        if depot_rows:
            sections.append(("Mein Depot", cap, depot_rows, _WARUM_COLS))
        ha = _handels_aktionen(raw, "rsl_levy")
        if ha:
            sections.append((
                "Handelsplan (JSON)",
                "" if sections else cap,
                ha,
                _WARUM_COLS,
            ))

    if key in ("lowprice", "dividend"):
        regel = raw.get("regel_text") or stop_regel(key)
        cap = f"Regel: {regel}"
        if raw.get("hinweis"):
            cap += f"\n\n{raw['hinweis']}"
        depot_rows = []
        for i, (tk, p) in enumerate(sorted(_ranking_positions(raw).items()), 1):
            if not isinstance(p, dict):
                p = {}
            depot_rows.append({
                "rang": i,
                "ticker": tk,
                "name": p.get("name") or "",
                "status": p.get("status") or "DEPOT",
                "puffer_pct": p.get("puffer_pct"),
                "begruendung": (
                    f"Kauf {p.get('entry_date') or '—'}"
                    + (f" · {p.get('entry_price')}" if p.get("entry_price") else "")
                ),
            })
        if depot_rows:
            sections.append(("Mein Depot", cap, depot_rows, _WARUM_COLS))
        ranks = raw.get("rankings") or []
        if ranks:
            sections.append(("Ranking", "" if sections else cap, ranks, _WARUM_COLS))
        ha = _handels_aktionen(raw, key)
        if ha:
            sections.append(("Handelsplan (JSON)", "" if sections else cap, ha, _WARUM_COLS))

    if key == "ivy":
        raw_regel = raw.get("regel_text") if isinstance(raw, dict) else None
        ver = (raw.get("version") if isinstance(raw, dict) else None) or "3.2"
        amp = (raw.get("ampel") if isinstance(raw, dict) else None) or "—"
        n_us = raw.get("n_us") if isinstance(raw, dict) else 4
        n_eu = raw.get("n_eu") if isinstance(raw, dict) else 4
        n_ap = raw.get("n_apac") if isinstance(raw, dict) else 7
        regel = raw_regel or (
            f"Ivy {ver} Hybrid-RAA · TAA-Ampel (SPY/VIX) · Quality-Momentum "
            f"n={n_us}/{n_eu}/{n_ap} · Exit Score < Top 40% · kein Live-Trailing"
        )
        cap = f"Regel: {regel} · Ampel JSON {amp}"
        if isinstance(raw, dict) and raw.get("signal_monat"):
            cap += f" · Signal {raw.get('signal_monat')}"
        if isinstance(raw, dict) and raw.get("signal_status"):
            cap += f" · Status {raw.get('signal_status')}"
        if isinstance(raw, dict) and raw.get("hinweis"):
            cap += f"\n\n{raw['hinweis']}"
        if _ivy_orders_stale_hinweis(raw):
            cap += (
                "\n\n⚠️ JSON-Handelsanweisungen passen nicht zum Depot — "
                "Colab „UMSCHICHTUNGS-ANALYSE“ + Upload erneut ausführen."
            )
        depot_rows = _ivy_depot_table(raw)
        if depot_rows:
            sections.append(("Mein Depot", cap if not sections else "", depot_rows, _WARUM_COLS))
        ziel = raw.get("zielportfolio") if isinstance(raw, dict) else None
        if isinstance(ziel, dict) and ziel:
            ziel_rows = [
                {
                    "rang": i,
                    "ticker": tk,
                    "ziel_gewicht": f"{float(w)*100:.1f}%" if w is not None else "—",
                    "begruendung": "Live-Zielgewicht (Ivy 3.9)",
                }
                for i, (tk, w) in enumerate(ziel.items(), 1)
            ]
            sections.append(("Zielportfolio", "" if sections else cap, ziel_rows, _WARUM_COLS))
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
                + "\n\nℹ️ Keine plausiblen Trades im JSON — Colab LIVE + Upload prüfen.",
                [],
                _WARUM_COLS,
            ))

    if key == "smallcap":
        regel = "Regel: " + (
            raw.get("regel_text")
            or smallcap_regel_kurz(raw)
            or "Exit-only · ATR S/L · EMA100 −5% · Ampel nur Quote (kein Ranking-Verkauf)."
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
        meta = raw.get("meta_labeling") or {}
        labels = meta.get("labels") or {}
        if labels:
            meta_rows = [
                {
                    "ticker": tk,
                    "take": "✓" if v.get("take") else "✗",
                    "prob": v.get("prob"),
                    "size_factor": v.get("size_factor"),
                }
                for tk, v in sorted(labels.items())
                if isinstance(v, dict)
            ]
            thr = meta.get("threshold", 0.55)
            sections.append((
                "Meta-Labeling (KAUFEN-Filter)",
                f"Schwelle P ≥ {thr:.0%} · Modell: {meta.get('model', '—')}",
                meta_rows,
                ["ticker", "take", "prob", "size_factor"],
            ))

    if key == "dauerlaeufer":
        amp = raw.get("ampel") or "—"
        exit_max = _dauer_exit_max(raw)
        regel = raw.get("regel_text") or (
            f"MA-Abstand-Exit ≤ {exit_max:.0f}% · wöchentliches Top-Ranking · "
            f"Ampel SPY/Breadth"
        )
        regel_full = f"Regel: {regel} · Ampel {amp}"
        depot_rows = []
        for i, (tk, p) in enumerate(_dauer_positions(raw).items(), 1):
            if not isinstance(p, dict):
                continue
            info = _dauer_stock_info(raw, tk)
            dist = safe_pct(p.get("ma_dist_pct"))
            if dist is None:
                dist = safe_pct(info.get("ma_dist_pct"))
            status = "EXIT" if (
                str(info.get("status") or "").upper() == "EXIT" or _dauer_is_exit(dist, raw)
            ) else "DEPOT"
            depot_rows.append({
                "rang": i,
                "ticker": _dauer_short(tk),
                "name": _dauer_name(tk, pos=p, info=info) or "",
                "status": status,
                "momentum_pct": dist,
                "einstieg_eur": p.get("einstieg"),
                "begruendung": (
                    f"MA-Dist {dist:+.1f}%" if dist is not None else ""
                ),
            })
        if not depot_rows:
            for i, tk in enumerate(raw.get("meine_aktien") or [], 1):
                info = _dauer_stock_info(raw, tk)
                dist = safe_pct(info.get("ma_dist_pct"))
                depot_rows.append({
                    "rang": i,
                    "ticker": _dauer_short(tk),
                    "name": _dauer_name(tk, info=info) or "",
                    "status": "DEPOT",
                    "momentum_pct": dist,
                    "einstieg_eur": None,
                    "begruendung": (
                        f"MA-Dist {dist:+.1f}%" if dist is not None else ""
                    ),
                })
        if depot_rows:
            sections.append(("Mein Depot", regel_full, depot_rows, _WARUM_COLS))
        sd = raw.get("stock_data") or {}
        if isinstance(sd, dict) and sd:
            rank_rows = []
            for i, (tk, info) in enumerate(
                sorted(
                    ((k, v) for k, v in sd.items() if isinstance(v, dict)),
                    key=lambda kv: (
                        safe_pct(kv[1].get("ma_dist_pct"))
                        if safe_pct(kv[1].get("ma_dist_pct")) is not None
                        else -999.0
                    ),
                    reverse=True,
                )[:20],
                1,
            ):
                dist = safe_pct(info.get("ma_dist_pct"))
                rank_rows.append({
                    "rang": i,
                    "ticker": _dauer_short(tk),
                    "name": _dauer_name(tk, info=info) or "",
                    "momentum_pct": dist,
                    "status": info.get("status") or "",
                    "begruendung": f"Kurs ${info.get('kurs_usd')}" if info.get("kurs_usd") else "",
                })
            if rank_rows:
                sections.append((
                    "Top-20 MA-Abstand",
                    "" if sections else regel_full,
                    rank_rows,
                    _WARUM_COLS,
                ))
        kands = raw.get("kandidaten") or []
        if isinstance(kands, list) and kands:
            kand_rows = []
            for k in kands:
                if not isinstance(k, dict):
                    continue
                kand_rows.append({
                    "rang": k.get("rang"),
                    "ticker": k.get("ticker"),
                    "name": k.get("name") or "",
                    "momentum_pct": safe_pct(k.get("ma_dist_pct")),
                    "status": "DEPOT" if k.get("im_depot") else "KANDIDAT",
                    "begruendung": (
                        f"Kurs ${k.get('kurs_usd')}" if k.get("kurs_usd") else ""
                    ),
                })
            if kand_rows:
                sections.append((
                    "Entry-Kandidaten (Preis-Score + MA)",
                    "Neue Käufe nur bei freien Slots und ausreichendem Cash.",
                    kand_rows,
                    _WARUM_COLS,
                ))

    return sections


def render_regime_momentum_meta_panel(txn_json):
    """Meta-Labeling-Tabelle — auch wenn keine offenen KAUFEN in der Transaktionsliste."""
    rm = (txn_json or {}).get("regime_momentum", _RM_RAW) or {}
    meta = rm.get("meta_labeling") or {}
    labels = meta.get("labels") or {}
    with st.expander("Regime Momentum — Meta-Labeling (KAUFEN-Filter)", expanded=bool(labels)):
        if not labels:
            st.info(
                "**Meta P fehlt**, weil `regime_momentum_positionen.json` auf GitHub noch **ohne** "
                "`meta_labeling` ist.\n\n"
                "**Fix in Colab** (`Meta_Labeling_Step1.ipynb`):\n"
                "1. Zelle 7 ausführen (Live mit Meta)\n"
                "2. Zelle **Upload GitHub** ausführen (Secret `GITHUB_TOKEN`)\n"
                "3. Dashboard **🔄 aktualisieren**\n\n"
                "*Hinweis:* Meta P erscheint nur bei **neuen KAUFEN** — wenn Depot = Ziel "
                "(nur HALTEN), ist die Meta-Tabelle leer bis zum nächsten Rebalancing."
            )
            return
        thr = meta.get("threshold", 0.55)
        st.caption(f"Schwelle P ≥ {thr:.0%} · Modell: {meta.get('model', '—')}")
        rows = [
            {
                "Ticker": tk,
                "Take": "✓" if v.get("take") else "✗",
                "Meta P": f"{float(v['prob']):.0%}" if v.get("prob") is not None else "—",
                "Size": v.get("size_factor"),
            }
            for tk, v in sorted(labels.items())
            if isinstance(v, dict)
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_warum_expanders(txn_json):
    """Expander „Warum?“ für alle Strategien mit JSON-Erklärungsdaten."""
    for key in ("sp100", "rsl_levy", "lowprice", "dividend", "ivy"):
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
    if quelle == "sp100":
        n = _sp100_txn_count(raw)
    if quelle in ("rsl_levy", "lowprice", "dividend") and n == 0:
        n = len(raw.get("verkaufen") or []) + len(raw.get("kaufen") or [])
    return n


def build_strategy_status(txn_json):
    """Übersicht aller Strategien — auch wenn keine Transaktion ansteht."""
    tj = txn_json or {}
    rows = []

    sp = tj.get("sp100", SP100_POS) or {}
    rsl_n = len(sp.get("rsl_data") or {})
    depot_n = len(sp.get("meine_aktien") or [])
    sp_sig = count_open_signals(sp, "sp100")
    rows.append({
        "Strategie": CHECK_ZEITEN["sp100"]["label"],
        "JSON-Stand": format_letztes_json(sp),
        "Depot / Ziel": f"{depot_n} Depot · {rsl_n} RSL" if rsl_n else f"{depot_n} Depot · kein rsl_data",
        "Offene Signale": sp_sig,
        EXIT_REGEL_COL: stop_pct_anzeige("sp100"),
        "Status": (
            f"⚠️ {sp_sig} Signal(e)" if sp_sig
            else ("⚠️ rsl_data fehlt" if depot_n and not rsl_n else "✅ Keine Aktion")
        ),
    })

    levy = tj.get("rsl_levy", _levy_raw) or {}
    levy_dep = len(_levy_positions(levy))
    levy_sig = count_open_signals(levy, "rsl_levy")
    amp = levy.get("ampel") or "—"
    rows.append({
        "Strategie": CHECK_ZEITEN["rsl_levy"]["label"],
        "JSON-Stand": format_letztes_json(levy),
        "Depot / Ziel": (
            f"{levy_dep} Position(en) · Ampel {amp}"
            if levy_dep else "— (Colab LIVE-Signale ausführen)"
        ),
        "Offene Signale": levy_sig,
        EXIT_REGEL_COL: stop_pct_anzeige("rsl_levy"),
        "Status": (
            f"⚠️ {levy_sig} Signal(e)" if levy_sig
            else ("⚠️ JSON leer" if not levy else "✅ Keine Aktion")
        ),
    })

    for key in ("lowprice", "dividend", "ivy"):
        if key == "lowprice":
            raw = tj.get("lowprice", _LP_RAW) or {}
        elif key == "dividend":
            raw = tj.get("dividend", _DIV_RAW) or {}
        else:
            raw = tj.get("ivy", _ivy_raw) or {}
        if key == "ivy":
            dep = len(positions_merged(raw))
        else:
            dep = len(_ranking_positions(raw)) or len(raw.get("meine_aktien") or [])
        sig = count_open_signals(raw, key)
        amp = raw.get("ampel") or "—"
        depot_s = f"{dep} Position(en)" if dep else "—"
        if key != "ivy" and amp and amp != "—":
            depot_s = f"{depot_s} · Ampel {amp}" if dep else f"— (Colab LIVE) · Ampel {amp}"
        rows.append({
            "Strategie": CHECK_ZEITEN[key]["label"],
            "JSON-Stand": format_letztes_json(raw),
            "Depot / Ziel": depot_s,
            "Offene Signale": sig,
            EXIT_REGEL_COL: stop_pct_anzeige(key),
            "Status": f"⚠️ {sig} Signal(e)" if sig else (
                "⚠️ JSON leer" if not raw else "✅ Keine Aktion"
            ),
        })

    return rows


def build_transaction_rows(ivy_ampel=None, txn_json=None):
    """Anstehende Trades aus JSON + Live-Stops."""
    tj = txn_json or {}
    sp100_pos = tj.get("sp100", SP100_POS)
    sp100_depot = sp100_depot_ticker(sp100_pos)
    levy_raw = tj.get("rsl_levy", _levy_raw)
    ivy_raw = tj.get("ivy", _ivy_raw)
    ivy_pos = portfolio_ohne_meta(ivy_raw)

    rows = []
    seen = set()

    def add(key, aktion, ticker, name, grund, prioritaet="Normal", meta_prob=None):
        sig = (key, (ticker or "").upper(), aktion[:8])
        if sig in seen:
            return
        seen.add(sig)
        rows.append(_txn_row(key, aktion, ticker, name, grund, prioritaet, meta_prob=meta_prob))

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

    # ── RSL Levy: Handelsanweisungen + Live-Stops ──
    levy_params = _levy_params(levy_raw)
    rsl_exit = safe_float(levy_params.get("rsl_exit_below")) or 0.99
    levy_ha = _handels_aktionen(levy_raw, "rsl_levy")
    if levy_ha:
        for rec in levy_ha:
            if not isinstance(rec, dict):
                continue
            aktion = str(rec.get("aktion") or rec.get("action") or "")
            if "HALTEN" in aktion:
                continue
            parts = [rec.get("grund") or "Signal"]
            if rec.get("rsl") is not None:
                parts.append(f"RSL {rec['rsl']:.3f}")
            if rec.get("pnl_pct") is not None:
                parts.append(f"G/V {rec['pnl_pct']:+.1f}%")
            if rec.get("stueck") is not None:
                parts.append(f"{rec['stueck']} Stk")
            add(
                "rsl_levy", aktion or "—", rec.get("ticker") or "",
                rec.get("name") or "", " · ".join(parts),
                rec.get("prioritaet") or "Plan",
            )
    else:
        if isinstance(levy_raw, dict) and str(levy_raw.get("ampel", "")).upper() == "ROT":
            add(
                "rsl_levy", "🔴 ALLE VERKAUFEN", "—", "—",
                "Ampel ROT — alles verkaufen", "Sofort",
            )
        for ticker in levy_raw.get("verkaufen") or [] if isinstance(levy_raw, dict) else []:
            p = _levy_positions(levy_raw).get(ticker, {})
            add(
                "rsl_levy", "🔴 VERKAUFEN", ticker, p.get("name") or "",
                "Exit-Signal (Colab)", "Sofort",
            )
        for ticker in levy_raw.get("kaufen") or [] if isinstance(levy_raw, dict) else []:
            p = _levy_positions(levy_raw).get(ticker, {})
            add(
                "rsl_levy", "🟢 KAUFEN", ticker, p.get("name") or "",
                "Neues Signal (Colab)", "Plan",
            )
    for tk, p in _levy_positions(levy_raw).items():
        live = _levy_live_position(tk, p, levy_raw)
        puf = live.get("puffer")
        rsl = live.get("rsl")
        name = p.get("name") or ""
        if puf is not None and puf <= 0:
            add(
                "rsl_levy", "🔴 VERKAUFEN", tk, name,
                f"Stop-Level ({fmt_pct(puf)} zum Stop · {p.get('stop_art') or 'SL'})",
                "Sofort",
            )
        elif rsl is not None and rsl < rsl_exit:
            add(
                "rsl_levy", "🔴 VERKAUFEN", tk, name,
                f"RSL {rsl:.3f} < {rsl_exit:.2f}",
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
        elif "PRÜF" in act or "PRUEF" in act:
            aktion = "🟡 PRÜFEN"
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

    # ── IVY: Ampel ROT → alle verkaufen (nur bestätigt; JSON vor Live) ──
    ivy_eff = _ivy_ampel_effective(ivy_ampel, ivy_raw)
    if ivy_eff.get("ampel") == "red":
        add(
            "ivy", "🔴 ALLE VERKAUFEN", "—", "—",
            ivy_eff.get("aktion") or "Ampel ROT — defensiv",
            "Sofort",
        )
    # ── Ranking-Strategien: Handelsplan aus JSON ──
    def _add_ranking_txn(key, raw):
        if not isinstance(raw, dict):
            return
        pos = _ranking_positions(raw)
        ha = raw.get("handelsanweisungen") or []
        if ha:
            for rec in ha:
                if not isinstance(rec, dict):
                    continue
                aktion = str(rec.get("aktion") or rec.get("action") or "")
                if "HALTEN" in aktion.upper():
                    continue
                tk = rec.get("ticker") or rec.get("isin") or ""
                add(
                    key, aktion or "—", tk,
                    rec.get("name") or "",
                    rec.get("grund") or "Rebalancing",
                    rec.get("prioritaet") or "Plan",
                )
            return
        for ticker in raw.get("verkaufen") or []:
            p = pos.get(ticker) or {}
            add(
                key, "🔴 VERKAUFEN", ticker,
                p.get("name") if isinstance(p, dict) else "",
                "Ranking/Exit", "Plan",
            )
        for ticker in raw.get("kaufen") or []:
            p = pos.get(ticker) or {}
            add(
                key, "🟢 KAUFEN", ticker,
                p.get("name") if isinstance(p, dict) else "",
                "Ranking-Kauf", "Plan",
            )

    _add_ranking_txn("lowprice", tj.get("lowprice", _LP_RAW) or {})
    _add_ranking_txn("dividend", tj.get("dividend", _DIV_RAW) or {})

    rows.sort(key=lambda r: (r.get("_sort", 9), r.get("_key", ""), r.get("Ticker", "")))
    return rows


def build_check_rows():
    rows = []
    _check_json = {
        "sp100": SP100_POS,
        "rsl_levy": _levy_raw,
        "lowprice": _LP_RAW,
        "dividend": _DIV_RAW,
        "ivy": _ivy_raw,
    }
    for key in ("sp100", "rsl_levy", "lowprice", "dividend", "ivy"):
        ci = check_info(key)
        rows.append({
            "Strategie": ci["label"],
            EXIT_REGEL_COL: stop_pct_anzeige(key),
            STOP_EXEC_COL: stop_ausfuehrung_anzeige(key),
            "Rhythmus": ci["frequenz"],
            **signal_spalten(key, ci, _check_json[key]),
            "Prüfen & Ausführen": format_pruefen_ausfuehren(ci),
            "Tage bis Check": ci["tage_bis_check"],
            "Tage bis Ausführung": ci["tage_bis"],
            "Hinweis": ci["hinweis"],
        })
    return rows


def _bm_portfolio_editor(portfolio):
    """Depot-Eingabe für Breakout Meta (USD-Kurse)."""
    st.caption("Ticker + Einstiegskurs in **USD ($)** · Datum = Kauftag")
    if "bm_portfolio_edit" not in st.session_state:
        st.session_state.bm_portfolio_edit = {k: dict(v) for k, v in portfolio.items()}
    edit = st.session_state.bm_portfolio_edit
    to_delete = []
    for ticker, pos in list(edit.items()):
        c0, c1, c2, c3 = st.columns([2, 2, 2, 1])
        new_ticker = c0.text_input("Ticker", ticker, key=f"bm_tk_{ticker}",
                                   label_visibility="collapsed").upper().strip()
        new_price = c1.number_input("Einstieg $", value=float(pos.get("entry_price", 0)),
                                    min_value=0.0, step=0.01, format="%.2f",
                                    key=f"bm_ep_{ticker}", label_visibility="collapsed")
        raw_date = c2.date_input("Datum",
                                 value=_bm_parse_date(pos.get("entry_date")) or date.today(),
                                 key=f"bm_ed_{ticker}", label_visibility="collapsed")
        if c3.button("🗑", key=f"bm_del_{ticker}"):
            to_delete.append(ticker)
        if new_ticker and new_ticker != ticker:
            edit[new_ticker] = {"entry_price": new_price, "entry_date": str(raw_date)}
            to_delete.append(ticker)
        else:
            edit[ticker] = {"entry_price": new_price, "entry_date": str(raw_date)}
    for tk in to_delete:
        edit.pop(tk, None)
    with st.expander("➕ Neue Position"):
        a, b, c, d = st.columns([2, 2, 2, 1])
        new_tk = a.text_input("Ticker", key="bm_new_tk", placeholder="NVDA")
        new_ep = b.number_input("Einstieg $", min_value=0.0, step=0.01,
                                key="bm_new_ep", format="%.2f")
        new_ed = c.date_input("Datum", value=date.today(), key="bm_new_ed")
        if d.button("➕", key="bm_add_btn"):
            tk = new_tk.upper().strip()
            if tk:
                edit[tk] = {"entry_price": float(new_ep), "entry_date": str(new_ed)}
                st.rerun()
    if st.button("💾 Breakout-Depot speichern", key="bm_save_btn"):
        _bm_save_portfolio(edit)
        st.session_state.bm_portfolio_edit = edit
        st.success("Gespeichert — Transaktionen & Stop-Monitor aktualisieren sich.")
        st.caption(
            "Für **E-Mail-Alerts** (08:00 / 14:30): "
            "`breakout_meta_portfolio.json` auf GitHub hochladen."
        )
        st.rerun()


# ── UI ────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📈 Trading Dashboard")
    st.caption(f"v{APP_VERSION} · Stand: {_now_berlin().strftime('%d.%m.%Y %H:%M')} MEZ")
    if st.button("🔄 Kurse & JSON aktualisieren"):
        st.session_state.json_refresh += 1
        st.cache_data.clear()
        st.rerun()
    st.caption("JSON von GitHub (2 Min.) · EODHD-Kurse (5 Min.)")
    with st.expander("📡 JSON-Sync (GitHub)"):
        st.caption(json_sync_hinweis("S&P 100", SP100_POS))
        st.caption(json_sync_hinweis("RSL Levy Momentum", _levy_raw))
        st.caption(json_trade_hinweis("RSL Levy Trades", _levy_raw, "rsl_levy"))
        st.caption(json_sync_hinweis("IVY", _ivy_raw))
        st.caption(json_trade_hinweis("IVY Trades", _ivy_raw, "ivy"))
        st.caption(json_sync_hinweis("LowPrice Rank", _LP_RAW))
        st.caption(json_trade_hinweis("LowPrice Trades", _LP_RAW, "lowprice"))
        st.caption(json_sync_hinweis("Dividende Einfach", _DIV_RAW))
        st.caption(json_trade_hinweis("Dividende Trades", _DIV_RAW, "dividend"))

st.title("📅 Handel & Trailing-Stop")
st.caption("Signale aus Colab-JSON auf GitHub · Live-Kurse via EODHD")

st.divider()

st.subheader("Strategie-Übersicht")
st.caption(
    "**Exit-Regel** = was den Verkauf auslöst (Trailing / RSL / S/L·T/P / ATR) · "
    "**Exit-Timing** = wann SL/TP ausgeführt wird: "
    "Gleicher Tag (Intraday) · Gleicher Tag (Close) · Nächster Tag (Open) · "
    "**Nächster Check** = geplanter Signal-Tag · "
    "**Letztes JSON** = letzter Colab-Upload · "
    "**Tage bis Check / Ausführung** = bis Signal bzw. Handelstag"
)
st.dataframe(pd.DataFrame(build_check_rows()), use_container_width=True, hide_index=True)

st.divider()

with st.spinner("Transaktionen laden..."):
    ivy_ampel = ivy_markt_ampel()
    _txn_refresh = st.session_state.json_refresh
    txn_json = {
        "sp100": normalize_sp100_json(lade_json_github("sp100_positionen.json", _txn_refresh) or {}),
        "rsl_levy": lade_json_github("rsl_levy_positionen.json", _txn_refresh) or {},
        "ivy": lade_json_github("ivy_portfolio.json", _txn_refresh) or {},
        "lowprice": lade_json_github("lowprice_positionen.json", _txn_refresh) or {},
        "dividend": lade_json_github("dividend_positionen.json", _txn_refresh) or {},
    }
    txn_rows = build_transaction_rows(ivy_ampel, txn_json=txn_json)

st.subheader("📋 Anstehende Transaktionen")
st.markdown("**Strategie-Status**")
st.dataframe(pd.DataFrame(build_strategy_status(txn_json)), use_container_width=True, hide_index=True)

st.markdown("**Einzelorders**")
st.caption("Pro Strategie: aktuelle Positionen (Ticker/Name) und darunter anstehende Käufe/Verkäufe.")
render_transactions_by_strategy(txn_rows, txn_json=txn_json)

st.divider()
st.subheader("Trailing-Stop / S/L · T/P Monitor")
st.caption(
    "Live-Kurse vs. Stop/TP · **RSL Levy:** Exit-Regel zeigt **%** oder **n×ATR** "
    "(aus Colab `sl_tp_basis`) · Stop/TP-Kurse in $ am Kauf fixiert."
)
st.caption(
    "RSL Levy: **SL/TP + RSL-Exit** (USD, täglich)  ·  "
    "LowPrice Rank: **ATR-Stop 6×** (USD, Next Open)  ·  "
    "IVY: **Hybrid-RAA** (JSON-Version) · QM-Exit < Top40% · Ampel SPY/VIX · kein Live-Trailing  ·  "
    "Dividende: **Research-Exit** (monatlich, kein Trailing)."
)

with st.spinner("Live-Kurse laden..."):
    stop_rows = build_stop_rows()

if not stop_rows:
    st.warning(
        "Keine Positionen im Trailing-Stop Monitor. "
        f"S&P 100: {len(SP100_POS.get('rsl_data') or {})} RSL-Einträge · "
        "→ 🔄 aktualisieren."
    )
else:
    df = pd.DataFrame(stop_rows)
    col_order = [
        "Strategie", EXIT_REGEL_COL, STOP_EXEC_COL, "Nächster Check", "Letztes JSON",
        "Prüfen & Ausführen",
        "Ticker", "Name", "Akt. Kurs", "Peak/Hoch", "Stop-Kurs",
        "Tages %", "% vom Peak", "% zum Stop", "Status",
    ]
    df = df[[c for c in col_order if c in df.columns]]
    for col in ("Tages %", "% vom Peak", "Peak/Hoch"):
        if col in df.columns:
            df[col] = df[col].fillna("—").replace({None: "—", "None": "—"})
    st.caption(
        "**Peak/Hoch** bzw. **SL-Basis** = Referenzkurs (Hoch oder Rebal-Kurs aus Colab) · "
        "**Akt. Kurs** = EODHD (Datum dahinter) · "
        "**⚠️** = Kurs älter als 1 Tag · "
        "**Exit-Regel** = Trailing-% · RSL · **S/L·T/P $** · Levy auch **n×ATR** · "
        "**Exit-Timing** = Sofort (Intraday) · Markt Close · Next Open · "
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
infos = []
_sp100_ver = str((SP100_POS or {}).get("version") or "").strip()
if SP100_POS.get("tickers") and not SP100_POS.get("rsl_data"):
    hinweise.append(
        "📈 **S&P 100:** `sp100_positionen.json` enthält keine `rsl_data` — "
        "Notebook ausführen und JSON erneut auf GitHub laden."
    )
elif SP100_POS.get("rsl_data") and not _sp100_ver.startswith("6."):
    depot = ", ".join(SP100_POS.get("meine_aktien") or []) or "—"
    hinweise.append(
        "📈 **S&P 100:** Trailing-Stop zeigt noch das **alte v5.3-Depot** "
        f"(JSON {SP100_POS.get('datum') or SP100_POS.get('sync_ts') or 'ohne Datum'}: {depot}). "
        "In `S_P_100_Strategie_v6_4_3.ipynb` Live-Zelle **und** die letzte Zelle "
        "**Dashboard-Upload** ausführen (Secret `GITHUB_TOKEN`)."
    )
elif SP100_POS.get("rsl_data"):
    sp100_datum = SP100_POS.get("datum", "—")
    infos.append(
        f"📈 **S&P 100 v{_sp100_ver or '?'}:** Exit-Regel **RSL-Peak-Trail 35%** — "
        f"Puffer/RSL im Monitor **täglich live** (EODHD); "
        f"RSL-Peak aus JSON (Stand {sp100_datum}). "
        "Verkauf erst wenn **RSL** 35% unter **RSL-Hoch** fällt."
    )
if not _ranking_positions(_LP_RAW) and not (_LP_RAW.get("meine_aktien") if isinstance(_LP_RAW, dict) else None):
    infos.append(
        "💵 **LowPrice Rank:** noch kein Depot auf GitHub. "
        "In `LowPrice_Rank_Strategie_V5_5.ipynb`: Eingabe-Maske → LIVE-Orderentwürfe → "
        "letzte Zelle **Dashboard-Upload** (Secret `GITHUB_TOKEN`). "
        "Ohne Upload bleibt die Datei leer — das ist kein Streamlit-Fehler."
    )
if not _ranking_positions(_DIV_RAW) and not (_DIV_RAW.get("meine_aktien") if isinstance(_DIV_RAW, dict) else None):
    infos.append(
        "💰 **Dividende Einfach:** noch kein Depot auf GitHub. "
        "In `dividend_strategy_einfach_v8_8_4.ipynb`: Zellen 1–4, dann **Dashboard-Upload** "
        "(Secret `GITHUB_TOKEN`)."
    )
for h in hinweise:
    st.warning(h)
for h in infos:
    st.info(h)

st.caption("Alerts: GitHub Actions (stop_check.py) · Live-Kurse: EODHD")
