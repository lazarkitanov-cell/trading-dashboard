# ═══════════════════════════════════════════════════════════════
#  Tägliche Stop-/Exit-Signale — gemeinsam für app.py + stop_check.py
#  Live EODHD + Fallback JSON (Colab handelsanweisungen)
# ═══════════════════════════════════════════════════════════════

from datetime import date, timedelta

import requests

SOFORT_GRUND_KEYS = (
    "TRAILING STOP", "EMA100", "CRASH", "RSL-PEAK", "RSL-TRAIL",
    "STOP AUS", "STOP AUSGEL", "ALLE VERKAUF",
    "STOP LOSS", "TAKE PROFIT", "ATR", "ATR_SL", "ATR_TP",
)


def safe_float(x):
    if x is None:
        return None
    try:
        v = float(x)
        if v != v or v == float("inf") or v == float("-inf") or v <= 0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def ticker_fix(ticker):
    t = (ticker or "").strip()
    if t.endswith(".L"):
        return t[:-2] + ".LSE"
    if "." not in t:
        return t + ".US"
    return t


def ticker_variants(ticker):
    """Mehrere EODHD-Symbole probieren (.ST, .LSE, …)."""
    t = (ticker or "").strip()
    out = []

    def add(x):
        if x and x not in out:
            out.append(x)

    add(ticker_fix(t))
    add(t)
    if t.endswith(".ST"):
        base = t[:-3]
        add(f"{base}.ST")
        add(f"{base}.STOCKHOLM")
    if t.endswith(".L"):
        add(t[:-2] + ".LSE")
    return out


def _fetch_realtime(api_key, symbol, timeout=10):
    try:
        r = requests.get(
            f"https://eodhd.com/api/real-time/{symbol}",
            params={"api_token": api_key, "fmt": "json"},
            timeout=timeout,
        )
        d = r.json()
        close = safe_float(d.get("close") or d.get("previousClose"))
        if not close:
            return None
        return {"close": close, "source": "RT", "symbol": symbol}
    except Exception:
        return None


def _fetch_eod_last(api_key, symbol, timeout=15):
    try:
        start = (date.today() - timedelta(days=14)).isoformat()
        r = requests.get(
            f"https://eodhd.com/api/eod/{symbol}",
            params={
                "api_token": api_key,
                "fmt": "json",
                "period": "d",
                "from": start,
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        if not rows:
            return None
        last = rows[-1]
        close = safe_float(last.get("adjusted_close") or last.get("close"))
        if not close:
            return None
        qd = last.get("date")
        return {
            "close": close,
            "source": "EOD",
            "symbol": symbol,
            "quote_date": date.fromisoformat(qd[:10]) if qd else None,
        }
    except Exception:
        return None


def _kurs_mismatch(live, eur_hint, ratio=2.5):
    """Live-Kurs weicht stark ab → oft falsche Währung (z. B. SEK statt EUR)."""
    live = safe_float(live)
    hint = safe_float(eur_hint)
    if not live or not hint:
        return False
    r = live / hint
    return r > ratio or r < (1 / ratio)


def fetch_quote(api_key, ticker, fallback_price=None, timeout=10):
    """Bester Kurs: EODHD RT → EOD → JSON-Fallback."""
    for sym in ticker_variants(ticker):
        q = _fetch_realtime(api_key, sym, timeout=timeout)
        if q:
            return q
    for sym in ticker_variants(ticker):
        q = _fetch_eod_last(api_key, sym, timeout=timeout)
        if q:
            return q
    fb = safe_float(fallback_price)
    if fb:
        return {"close": fb, "source": "JSON", "symbol": ticker, "quote_date": None}
    return None


def fetch_quote_eur(api_key, ticker, eur_hint=None, pos=None, timeout=10):
    """EUR-Kurs für Stop-Check — Colab-kurs_eur hat Vorrang bei Währungs-Mismatch."""
    fb = safe_float(eur_hint)
    pos_ccy = str((pos or {}).get("buy_currency") or "EUR").upper()
    q = fetch_quote(api_key, ticker, fallback_price=fb, timeout=timeout)
    if not q:
        return None
    if fb and pos_ccy == "EUR":
        live = q.get("close")
        if q.get("source") == "JSON" or _kurs_mismatch(live, fb):
            return {
                "close": fb,
                "source": "JSON-EUR",
                "symbol": ticker,
                "quote_date": q.get("quote_date"),
                "live_native": live,
            }
    return q


def is_sofort_rec(rec):
    """Täglich handeln (Stop/Exit) — nicht Plan-Rebalancing."""
    if not isinstance(rec, dict):
        return False
    if str(rec.get("prioritaet") or "").strip().lower() == "sofort":
        return True
    act = str(rec.get("aktion") or rec.get("action") or "").upper()
    grund = str(rec.get("grund") or "").upper()
    if "VERKAUF" not in act and "ALLE VERKAUF" not in act:
        return False
    return any(k in grund for k in SOFORT_GRUND_KEYS)


def depot_ticker_keys(pos):
    """ISIN- und Ticker-Schlüssel eines Depot-Dicts (Schlüssel = ISIN)."""
    keys = set()
    if not isinstance(pos, dict):
        return keys
    for isin, p in pos.items():
        if str(isin).startswith("_"):
            continue
        keys.add(str(isin).upper())
        if isinstance(p, dict):
            t = p.get("ticker")
            if t:
                keys.add(str(t).upper())
    return keys


def handels_rec_in_depot(rec, pos):
    """True wenn eine Handelszeile zu einer aktiven Depot-Position gehört."""
    if not isinstance(rec, dict):
        return False
    keys = depot_ticker_keys(pos)
    tk = str(rec.get("ticker") or "").upper()
    isin = str(rec.get("isin") or "").upper()
    return (tk and tk in keys) or (isin and isin in keys)


def filter_smallcap_handelsanweisungen(handelsanweisungen, pos):
    """Verkaufs-Signale für nicht mehr gehaltene Ticker entfernen; KAUF behalten."""
    out = []
    for rec in handelsanweisungen or []:
        if not isinstance(rec, dict):
            continue
        aktion = str(rec.get("aktion") or rec.get("action") or "").upper()
        if handels_rec_in_depot(rec, pos):
            out.append(rec)
        elif "KAUF" in aktion:
            out.append(rec)
    return out


def json_kurs_hints(raw):
    """Ticker/ISIN → kurs_eur aus handelsanweisungen (Colab-Fallback)."""
    hints = {}
    if not isinstance(raw, dict):
        return hints
    for rec in raw.get("handelsanweisungen") or []:
        if not isinstance(rec, dict):
            continue
        kurs = safe_float(rec.get("kurs_eur") or rec.get("kurs"))
        if not kurs:
            continue
        for key in (rec.get("ticker"), rec.get("isin")):
            if key:
                hints[str(key).upper()] = kurs
    return hints


def collect_json_sofort_exits(raw, strategie_label, pos=None):
    """Sofort-VERKAUFEN aus Colab-JSON (wenn Live-Check fehlt)."""
    out = []
    if not isinstance(raw, dict):
        return out
    for rec in raw.get("handelsanweisungen") or []:
        if not isinstance(rec, dict):
            continue
        if pos is not None and not handels_rec_in_depot(rec, pos):
            continue
        if not is_sofort_rec(rec):
            continue
        act = str(rec.get("aktion") or rec.get("action") or "").upper()
        if "VERKAUF" not in act and "ALLE VERKAUF" not in act:
            continue
        ticker = rec.get("ticker") or rec.get("isin") or "—"
        name = rec.get("name") or ""
        ticker_s = f"{ticker} — {name}" if name else str(ticker)
        kurs = safe_float(rec.get("kurs_eur") or rec.get("kurs"))
        pnl = rec.get("pnl_pct")
        out.append({
            "strategie": strategie_label,
            "ticker": ticker_s,
            "ticker_key": str(ticker).upper(),
            "kurs": kurs if kurs else "—",
            "stop": "—",
            "puffer": 0.0,
            "pnl_s": f"{pnl:+.1f}%" if pnl is not None else "",
            "grund": rec.get("grund") or "Sofort-Exit (Colab JSON)",
            "json_sofort": True,
        })
    return out


def smallcap_exit_cfg(raw=None):
    """Stop-Modus aus smallcap_positionen.json (Default: ATR wie Colab FINAL)."""
    raw = raw if isinstance(raw, dict) else {}
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    mode = str(
        raw.get("stop_mode") or params.get("stop_mode") or "atr"
    ).lower().strip()
    if mode in ("trail", "trailing_stop", "ts"):
        mode = "trailing"
    try:
        sl_m = float(raw.get("atr_sl_mult") or params.get("atr_sl_mult") or 2.0)
    except (TypeError, ValueError):
        sl_m = 2.0
    try:
        tp_m = float(raw.get("atr_tp_mult") or params.get("atr_tp_mult") or 8.0)
    except (TypeError, ValueError):
        tp_m = 8.0
    try:
        trail = float(raw.get("trailing_pct") or params.get("trailing_pct") or 0.25)
    except (TypeError, ValueError):
        trail = 0.25
    return {
        "mode": mode,
        "atr_sl_mult": sl_m,
        "atr_tp_mult": tp_m,
        "trailing_pct": trail,
    }


def smallcap_regel_kurz(raw=None):
    """Kompakte Exit-Regel für Monitor / Übersicht."""
    cfg = smallcap_exit_cfg(raw)
    if isinstance(raw, dict) and raw.get("regel_text"):
        return str(raw["regel_text"]).replace("Exit-only · ", "")
    if cfg["mode"] in ("atr", "atr_trailing"):
        s = f"S/L −{cfg['atr_sl_mult']:g}×ATR"
        if cfg["atr_tp_mult"] > 0:
            s += f" · T/P +{cfg['atr_tp_mult']:g}×ATR"
        return s + " · EMA −5%"
    return f"{int(round(cfg['trailing_pct'] * 100))}% TS · EMA −5%"


def smallcap_stop_row(isin, pos, trailing_pct, api_key, kurs_hints=None, raw=None):
    """Small-Cap Stop-Zeile — ATR S/L (FINAL) oder Trailing; Live + JSON-Fallback (EUR)."""
    if not isinstance(pos, dict):
        return None
    try:
        kauf = safe_float(pos.get("buy_price") or pos.get("einstieg"))
        if not kauf:
            return None
        ticker = pos.get("ticker") or isin
        hints = kurs_hints or {}
        fb = hints.get(str(ticker).upper()) or hints.get(str(isin).upper())
        q = fetch_quote_eur(api_key, ticker, eur_hint=fb, pos=pos)
        if not q or not q.get("close"):
            return None
        kurs = float(q["close"])
        hw = safe_float(pos.get("high_water") or pos.get("hoch") or kauf) or kauf
        hw = max(float(hw), kurs)
        cfg = smallcap_exit_cfg(raw)
        mode = cfg["mode"]
        try:
            trail = float(trailing_pct) if trailing_pct is not None else float(cfg["trailing_pct"])
        except (TypeError, ValueError):
            trail = float(cfg["trailing_pct"])
        atr = safe_float(pos.get("atr_entry") or pos.get("atr"))
        stop = safe_float(pos.get("atr_stop") or pos.get("stop_kurs") or pos.get("stop"))
        tp = safe_float(pos.get("atr_tp") or pos.get("take_profit") or pos.get("tp"))

        if mode in ("atr", "atr_trailing"):
            if atr is None or atr <= 0:
                atr = kauf * 0.05  # Notebook-Fallback
            sl_m = float(cfg["atr_sl_mult"])
            tp_m = float(cfg["atr_tp_mult"])
            if stop is None or stop <= 0:
                ref = hw if mode == "atr_trailing" else kauf
                stop = round(ref - sl_m * atr, 2)
            else:
                stop = round(float(stop), 2)
            if (tp is None or tp <= 0) and tp_m > 0:
                tp = round(kauf + tp_m * atr, 2)
            puffer = round((kurs / stop - 1) * 100, 1) if stop > 0 else 0.0
            tp_hit = tp is not None and kurs >= tp
            sl_hit = kurs <= stop
            return {
                "ticker": ticker,
                "isin": isin,
                "pos": pos,
                "kurs": kurs,
                "hw": hw,
                "stop": stop,
                "tp": tp,
                "atr": atr,
                "mode": mode,
                "stop_art": "ATR-TP" if tp_hit else "ATR-SL",
                "puffer": 0.0 if tp_hit else puffer,
                "quote_source": q.get("source", "?"),
                "triggered": sl_hit or tp_hit,
                "tp_hit": tp_hit,
            }

        # Trailing (Legacy / explizit)
        stop = round(hw * (1 - trail), 2)
        puffer = round((kurs / stop - 1) * 100, 1) if stop > 0 else 0.0
        return {
            "ticker": ticker,
            "isin": isin,
            "pos": pos,
            "kurs": kurs,
            "hw": hw,
            "stop": stop,
            "tp": None,
            "atr": None,
            "mode": "trailing",
            "stop_art": "TS",
            "puffer": puffer,
            "quote_source": q.get("source", "?"),
            "triggered": kurs <= stop,
            "tp_hit": False,
        }
    except Exception:
        return None


def _alert_key(alert):
    tk = alert.get("ticker_key") or str(alert.get("ticker", "")).split(" — ")[0][:40].upper()
    return (alert.get("strategie"), tk)


def merge_stop_alerts(live_alerts, json_alerts):
    """JSON-Sofort-Exits haben Vorrang, wenn Live-Check nicht ausgelöst hat."""
    out = list(live_alerts)
    live_keys = {_alert_key(a): i for i, a in enumerate(out)}
    for ja in json_alerts:
        if not ja.get("json_sofort"):
            key = _alert_key(ja)
            if key not in live_keys:
                out.append(ja)
                live_keys[key] = len(out) - 1
            continue
        key = _alert_key(ja)
        idx = live_keys.get(key)
        if idx is None:
            out.append(ja)
            live_keys[key] = len(out) - 1
            continue
        live = out[idx]
        live_triggered = live.get("puffer") is not None and live.get("puffer") <= 0
        if live.get("crash") or live_triggered:
            continue
        out[idx] = {**live, **ja, "json_sofort": True}
    return out


def collect_sofort_orders_all(pairs):
    """Alle Sofort-handelsanweisungen — pairs: [(raw_dict, strategie_label), …]."""
    rows = []
    for raw, label in pairs:
        if not isinstance(raw, dict):
            continue
        for rec in raw.get("handelsanweisungen") or []:
            if not isinstance(rec, dict):
                continue
            if not is_sofort_rec(rec):
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


def sofort_orders_to_alerts(orders):
    """Dashboard-Sofort-Orders → Alert-Zeilen für E-Mail."""
    out = []
    for o in orders:
        ticker = o.get("ticker") or "—"
        name = o.get("name") or ""
        ticker_s = f"{ticker} — {name}" if name else str(ticker)
        kurs = safe_float(o.get("kurs_eur"))
        pnl = o.get("pnl_pct")
        out.append({
            "strategie": o.get("strategie", "?"),
            "ticker": ticker_s,
            "ticker_key": str(ticker).upper(),
            "kurs": kurs if kurs else "—",
            "stop": "—",
            "puffer": 0.0,
            "pnl_s": f"{pnl:+.1f}%" if pnl is not None else "",
            "grund": f"{o.get('aktion', '')} · {o.get('grund') or 'Sofort'}".strip(" ·"),
            "json_sofort": True,
            "dashboard_sofort": True,
        })
    return out


JSON_STRATEGIES = (
    ("smallcap_positionen.json", "🇪🇺 Small Cap EU"),
    ("kassandra_positionen.json", "🌍 Kassandra"),
    ("sp100_positionen.json", "📈 S&P 100"),
    ("ivy_portfolio.json", "🏛 IVY/RAA"),
    ("regime_momentum_positionen.json", "🚀 Regime Momentum"),
)
