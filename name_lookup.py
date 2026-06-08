# ═══════════════════════════════════════════════════════════════
# Namens-Auflösung — app.py + stop_check.py
# Priorität: JSON (Colab) → statische Map → EODHD
# ═══════════════════════════════════════════════════════════════

import re

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
}

# Große IVY-Map optional — Namen kommen meist aus ivy_portfolio.json
IVY_TICKER_NAMES = {}


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
