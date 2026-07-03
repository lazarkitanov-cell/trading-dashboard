# ═══════════════════════════════════════════════════════════════
# Namens-Auflösung — app.py + stop_check.py
# Priorität: JSON (Colab) → Small-Cap-Map → statische Map → EODHD
# ═══════════════════════════════════════════════════════════════

import json
import re
from pathlib import Path

KNOWN_NAMES = {
    "SMH": "VanEck Semiconductor ETF",
    "EWY": "iShares MSCI South Korea ETF",
    "QTUM": "Defiance Quantum ETF",
    "IUIT.L": "iShares S&P 500 Information Technology UCITS ETF",
    "IUIT.LSE": "iShares S&P 500 Information Technology UCITS ETF",
    "IUVL.L": "iShares S&P 500 Value UCITS ETF",
    "IUVL.LSE": "iShares S&P 500 Value UCITS ETF",
    "IFX.DE": "Infineon Technologies AG",
    "IFX.XETRA": "Infineon Technologies AG",
    "IFX.F": "Infineon Technologies AG",
    "ASM.AS": "ASM International NV",
    "WDC": "Western Digital Corporation",
    "WDC.US": "Western Digital Corporation",
    "WDC.F": "Western Digital Corporation",
    "ABBN.SW": "ABB Ltd",
    "TSEM.US": "Tower Semiconductor Ltd",
    "FN.US": "Fabrinet",
    "STMPA.PA": "STMicroelectronics NV",
    "FLEX.US": "Flex Ltd",
    "LRCX": "Lam Research Corporation",
    "ESLT.US": "Elbit Systems Ltd",
    "TECK-B.TO": "Teck Resources Ltd",
    "CIEN": "Ciena Corporation",
    "FIX": "Comfort Systems",
    "LYTR.XETRA": "Amundi Bloomberg Commodity ex-Agriculture",
    "SXRS": "iShares Diversified Commodity Swap UCITS ETF",
}

# Große IVY-Map optional — Namen kommen meist aus ivy_portfolio.json
IVY_TICKER_NAMES = {}

_SMALLCAP_NAMES = None
_JSON_NAMES = None
_SP500_TICKER_NAMES = None


def load_smallcap_names():
    """ISIN/Ticker → Name aus smallcap_names.json (Small Cap Europe.csv)."""
    global _SMALLCAP_NAMES
    if _SMALLCAP_NAMES is not None:
        return _SMALLCAP_NAMES
    data = {"by_isin": {}, "by_ticker": {}}
    for path in (
        Path(__file__).resolve().parent / "smallcap_names.json",
        Path("smallcap_names.json"),
    ):
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                break
            except Exception:
                pass
    _SMALLCAP_NAMES = {
        "by_isin": {k.upper(): v for k, v in (data.get("by_isin") or {}).items()},
        "by_ticker": {k.upper(): v for k, v in (data.get("by_ticker") or {}).items()},
    }
    return _SMALLCAP_NAMES


def _smallcap_name_from_maps(ticker=None, isin=None):
    maps = load_smallcap_names()
    isin_u = (isin or "").strip().upper()
    if len(isin_u) == 12 and isin_u in maps["by_isin"]:
        return maps["by_isin"][isin_u]
    for cand in ticker_candidates(ticker):
        hit = maps["by_ticker"].get(cand.upper())
        if hit:
            return hit
    return None


def resolve_smallcap_name(ticker=None, pos=None, isin=None, api_key=None, cache=None):
    """Small Cap EU: Name aus JSON, Universum-CSV-Map oder EODHD."""
    if cache is None:
        cache = {}
    isin = isin or (pos.get("isin") if isinstance(pos, dict) else None)
    ticker = ticker or (pos.get("ticker") if isinstance(pos, dict) else None)
    cache_key = ("sc", ticker or "", isin or "")
    if cache_key in cache:
        return cache[cache_key]

    json_name = (pos.get("name") or "").strip() if isinstance(pos, dict) else ""
    if json_name and not is_weak_name(json_name, ticker or isin or ""):
        cache[cache_key] = json_name
        return json_name

    mapped = _smallcap_name_from_maps(ticker=ticker, isin=isin)
    if mapped:
        cache[cache_key] = mapped
        return mapped

    name = lookup_name(ticker, pos, api_key, cache)
    cache[cache_key] = name
    return name


def ticker_short(ticker):
    return (ticker or "").replace(".US", "").replace(".TO", "").split(".")[0].upper()


def is_weak_name(name, ticker):
    if not name or not str(name).strip():
        return True
    name = str(name).strip()
    short = ticker_short(ticker)
    if name.upper() == short:
        return True
    if re.fullmatch(r"[A-Z0-9\-]{1,6}", name.upper()):
        if len(name) <= max(5, len(short)):
            return True
    return False


def ticker_fix_basic(ticker):
    if ticker.endswith(".L"):
        return ticker[:-2] + ".LSE"
    if "." not in ticker:
        return ticker + ".US"
    return ticker


def ticker_candidates(ticker, pos=None):
    t = (ticker or "").strip().upper()
    out = []

    def add(x):
        x = (x or "").strip().upper()
        if x and x not in out:
            out.append(x)

    if isinstance(pos, dict):
        ffm = (pos.get("ffm_ticker") or "").strip().upper()
        if ffm:
            add(ffm if ffm.endswith(".F") else ffm + ".F")

    add(t)
    add(ticker_fix_basic(t))
    if t.endswith(".DE"):
        add(t[:-3] + ".XETRA")
    if t.endswith(".L"):
        add(t[:-2] + ".LSE")
    if "." not in t:
        add(t + ".US")
    return out


def _fundamentals_name(ticker, api_key):
    try:
        import requests
        r = requests.get(
            f"https://eodhd.com/api/fundamentals/{ticker}",
            params={"api_token": api_key, "filter": "General"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        general = r.json().get("General") or {}
        name = (general.get("Name") or "").strip()
        code = (general.get("Code") or "").strip()
        if name and name.upper() != code.upper() and not is_weak_name(name, ticker):
            return name
    except Exception:
        pass
    return None


def name_from_signals(ticker, signal_list):
    """Name aus Scanner-/Signal-Liste (Breakout Meta, Regime, …)."""
    short = ticker_short(ticker)
    for s in signal_list or []:
        if not isinstance(s, dict):
            continue
        st = (s.get("ticker") or "").strip().upper()
        if st not in ((ticker or "").strip().upper(), short):
            continue
        nm = (s.get("name") or "").strip()
        if nm and not is_weak_name(nm, ticker):
            return nm
    return None


def load_sp500_ticker_names():
    """Optional: statische Ticker→Name-Map (data/sp500_ticker_names.json)."""
    global _SP500_TICKER_NAMES
    if _SP500_TICKER_NAMES is not None:
        return _SP500_TICKER_NAMES
    names = {}
    base = Path(__file__).resolve().parent
    for path in (base / "data" / "sp500_ticker_names.json", base / "sp500_ticker_names.json"):
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                names = {str(k).upper(): v for k, v in data.items()}
                break
            except Exception:
                pass
    _SP500_TICKER_NAMES = names
    return names


def _merge_json_names(data, names):
    """Ticker→Name aus bekannten JSON-Strukturen (Strategie-Exports)."""
    if not isinstance(data, dict):
        return
    list_keys = (
        "signals", "kandidaten", "kaufen", "verkaufen", "top_n",
        "empfehlung", "handelsanweisungen", "positionen_liste",
    )
    for key in list_keys:
        items = data.get(key)
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                tk = item.get("ticker")
                nm = (item.get("name") or "").strip()
                if tk and nm and not is_weak_name(nm, tk):
                    names[ticker_short(tk)] = nm
    pos = data.get("positionen")
    if isinstance(pos, dict):
        for tk, p in pos.items():
            if not isinstance(p, dict):
                continue
            nm = (p.get("name") or "").strip()
            if nm and not is_weak_name(nm, tk):
                names[ticker_short(tk)] = nm
    rsl = data.get("rsl_data")
    if isinstance(rsl, dict):
        for tk, info in rsl.items():
            if not isinstance(info, dict):
                continue
            nm = (info.get("name") or "").strip()
            if nm and not is_weak_name(nm, tk):
                names[ticker_short(tk)] = nm


def load_json_name_map():
    """Namen aus lokalen Strategie-JSONs (Breakout, Regime, S&P100, ETF, …)."""
    global _JSON_NAMES
    if _JSON_NAMES is not None:
        return _JSON_NAMES
    names = {}
    base = Path(__file__).resolve().parent
    for fname in (
        "breakout_meta_signals.json",
        "regime_momentum_positionen.json",
        "sp100_positionen.json",
        "etf_eingabe.json",
        "etf_eodhd_eingabe.json",
    ):
        path = base / fname
        if not path.is_file():
            continue
        try:
            _merge_json_names(json.loads(path.read_text(encoding="utf-8")), names)
        except Exception:
            pass
    _JSON_NAMES = names
    return names


def resolve_stock_name(ticker=None, pos=None, signals=None, api_key=None, cache=None):
    """US-/ETF-Aktienname: Pos/Signale → JSON-Map → SP500-Map → EODHD."""
    if cache is None:
        cache = {}
    ck = ("stock", (ticker or "").strip().upper())
    if ck in cache:
        return cache[ck]

    json_name = (pos.get("name") or "").strip() if isinstance(pos, dict) else ""
    if json_name and not is_weak_name(json_name, ticker or ""):
        cache[ck] = json_name
        return json_name

    sig_name = name_from_signals(ticker, signals)
    if sig_name:
        cache[ck] = sig_name
        return sig_name

    short = ticker_short(ticker)
    for src in (load_json_name_map(), load_sp500_ticker_names()):
        hit = src.get(short) or src.get((ticker or "").strip().upper())
        if hit and not is_weak_name(hit, ticker):
            cache[ck] = hit
            return hit

    name = lookup_name(ticker, pos, api_key, cache)
    cache[ck] = name
    return name


def lookup_name(ticker, pos=None, api_key=None, cache=None):
    if cache is None:
        cache = {}
    cache_key = (ticker or "", tuple(ticker_candidates(ticker, pos)))
    if cache_key in cache:
        return cache[cache_key]

    json_name = (pos.get("name") or "").strip() if isinstance(pos, dict) else ""

    # 0) JSON aus Colab — maßgeblich (Kassandra-Strategielabel z.B. „Taiwan“)
    if json_name:
        cache[cache_key] = json_name
        return json_name

    t_up = (ticker or "").strip().upper()
    if t_up in IVY_TICKER_NAMES:
        cache[cache_key] = IVY_TICKER_NAMES[t_up]
        return IVY_TICKER_NAMES[t_up]

    short = ticker_short(ticker)
    if short in IVY_TICKER_NAMES:
        cache[cache_key] = IVY_TICKER_NAMES[short]
        return IVY_TICKER_NAMES[short]

    for key in ticker_candidates(ticker, pos):
        if key in KNOWN_NAMES:
            cache[cache_key] = KNOWN_NAMES[key]
            return KNOWN_NAMES[key]

    if api_key:
        for cand in ticker_candidates(ticker, pos):
            name = _fundamentals_name(cand, api_key)
            if name:
                cache[cache_key] = name
                return name

    fallback = short
    cache[cache_key] = fallback
    return fallback


def ticker_label(ticker, pos=None, api_key=None, cache=None):
    name = lookup_name(ticker, pos, api_key, cache)
    short = ticker_short(ticker)
    if name and name.upper() != short:
        return f"{short} — {name}"
    return short
