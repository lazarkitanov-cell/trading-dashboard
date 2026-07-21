
[579 lines collapsed]

        if quelle == "dauerlaeufer" and not meine and not (data.get("ziel_aktien") or data.get("ziel_ticker")):
            return f"{label}: Depot leer — Colab LIVE-Signale ausführen"
    if quelle == "breakout_meta" and isinstance(data, dict):
        n = data.get("n_filtered")
        if n is None:
            n = sum(1 for s in (data.get("signals") or []) if s.get("take"))
        n = sum(
            1 for s in (data.get("signals") or [])
            if _bm_is_take(s) and _bm_signal_fresh(s)
        )
        if n:
            return f"{label}: {n} Kauf-Signale (Meta Top-20%)"
            return f"{label}: {n} Kauf-Signale (Meta Top-20%, frisch)"
        if data.get("signals"):
            return f"{label}: Scan ohne Top-20%-Signale"
            return f"{label}: Scan ohne frische Top-20%-Signale"
    if quelle == "etf" and isinstance(data, dict) and data.get("empfehlung"):
        n = len(data.get("empfehlung") or [])
        return f"{label}: keine Handelsanweisungen — {n} Kandidaten (empfehlung)"

[687 lines collapsed]

_BM_STOP = 0.05
_BM_HOLD = 20
_BM_MAX_POS = 10
_BM_SCAN_LOOKBACK = 10  # wie Colab-Scanner: nur Signale der letzten N Handelstage
_BM_PORTFOLIO_FILE = Path(__file__).resolve().parent / "breakout_meta_portfolio.json"

[62 lines collapsed]

    return float(q["close"]) if q and q.get("close") else None
def _bm_is_take(s):
    """Nur explizites Meta-Pass (take=True) — Default False, nie meta_filtered."""
    if not isinstance(s, dict):
        return False
    reason = str(s.get("reason") or "").lower()
    if reason in ("meta_filtered", "features_incomplete", "regime_red"):
        return False
    t = s.get("take")
    if t is True or t == 1:
        return True
    if isinstance(t, str) and t.strip().lower() in ("true", "1", "yes"):
        return True
    return False
def _bm_signal_fresh(s, ref=None):
    """True wenn Signal-Datum innerhalb des Scanner-Lookbacks liegt."""
    sig_d = _bm_parse_date(s.get("signal_date") or s.get("date"))
    if sig_d is None:
        return False
    ende = ref or date.today()
    if sig_d > ende:
        return False
    return _bm_handelstage(sig_d, ende) <= _BM_SCAN_LOOKBACK
def _bm_compute_actions(signals, portfolio):
    heute = date.today()
    verkaufen, halten, kaufen = [], [], []

[16 lines collapsed]

        else:
            halten.append(ticker)
    freie = max(0, _BM_MAX_POS - len(halten))
    for s in signals:
        if not s.get("take", True):
    port_keys = {str(k).upper() for k in (portfolio or {})}
    for s in signals or []:
        if not _bm_is_take(s) or not _bm_signal_fresh(s, heute):
            continue
        tk = s.get("ticker")
        if not tk or tk in portfolio:
        if not tk or str(tk).upper() in port_keys:
            continue
        mp = s.get("meta_prob")
        ziel = s.get("target")

[343 lines collapsed]

    return sd.get(tk) or sd.get(short) or sd.get(f"{short}.US") or {}
def _dauer_name(raw, ticker, pos=None, rec=None):
    """Firmenname für Dauerläufer-Transaktionen / Monitor."""
    for src in (rec, pos):
        if isinstance(src, dict):
            nm = (src.get("name") or "").strip()
            if nm and not is_weak_name(nm, ticker):
                return nm
    info = _dauer_stock_info(raw, ticker)
    nm = (info.get("name") or "").strip()
    if nm and not is_weak_name(nm, ticker):
        return nm
    if isinstance(raw, dict):
        short = _dauer_short(ticker)
        for key in (ticker, short, f"{short}.US"):
            p = raw.get(key)
            if isinstance(p, dict):
                nm = (p.get("name") or "").strip()
                if nm and not is_weak_name(nm, ticker):
                    return nm
    return _stock_name(ticker, pos=pos or info) or "—"
def _dauer_exit_max(raw):
    """Exit-Schwelle (MA-Abstand %), Default −6."""
    if isinstance(raw, dict):

[1889 lines collapsed]

            if "HALTEN" in aktion:
                continue
            tk = rec.get("ticker") or ""
            dl_exit_seen.add(_dauer_short(tk))
            short = _dauer_short(tk)
            dl_exit_seen.add(short)
            p = _dauer_positions(dl_raw).get(tk) or _dauer_positions(dl_raw).get(short) or {}
            grund = rec.get("grund") or "Fr-Rebalancing"
            dist = rec.get("ma_dist_pct")
            if dist is not None:

[5 lines collapsed]

            ):
                prio = "Sofort"
            add(
                "dauerlaeufer", aktion or "—", _dauer_short(tk),
                rec.get("name") or "", grund, prio,
                "dauerlaeufer", aktion or "—", short,
                _dauer_name(dl_raw, tk, pos=p, rec=rec), grund, prio,
            )
    else:
        for ticker in dl_raw.get("verkaufen") or [] if isinstance(dl_raw, dict) else []:
            short = _dauer_short(ticker)
            dl_exit_seen.add(short)
            p = _dauer_positions(dl_raw).get(ticker) or _dauer_positions(dl_raw).get(short) or {}
            info = _dauer_stock_info(dl_raw, ticker)
            add(
                "dauerlaeufer", "🔴 VERKAUFEN", short,
                p.get("name") or info.get("name") or "",
                _dauer_name(dl_raw, ticker, pos=p),
                "MA-Exit / Rebalancing", "Sofort",
            )
        for ticker in dl_raw.get("kaufen") or [] if isinstance(dl_raw, dict) else []:

[5 lines collapsed]

                grund += f" · Dist {dist:+.1f}%"
            add(
                "dauerlaeufer", "🟢 KAUFEN", short,
                info.get("name") or "", grund, "Plan",
                _dauer_name(dl_raw, ticker, pos=info), grund, "Plan",
            )
    for tk, p in _dauer_positions(dl_raw).items():
        short = _dauer_short(tk)

[7 lines collapsed]

            grund = f"MA-Exit (Dist {dist:+.1f}%)" if dist is not None else "MA-Exit (stock_data)"
            add(
                "dauerlaeufer", "🔴 VERKAUFEN", short,
                p.get("name") or info.get("name") or "",
                _dauer_name(dl_raw, tk, pos=p),
                grund, "Sofort",
            )
    # ── Breakout Meta: S/L · T/P · Zeitlimit · Meta-Käufe ──
    # ── Breakout Meta: S/L · T/P · Zeitlimit · Meta-Käufe (nur take=True + frisch) ──
    bm_raw = tj.get("breakout_meta", _BM_RAW) or {}
    bm_port = _bm_get_portfolio(bm_raw)
    bm_vk, _, bm_kf = _bm_compute_actions(_bm_signals(bm_raw), bm_port)
    for ticker, grund, prio in bm_vk:
        add("breakout_meta", "🔴 VERKAUFEN", ticker, _bm_name(ticker, bm_raw=bm_raw), grund, prio)
        pos = (bm_port or {}).get(ticker) or {}
        add(
            "breakout_meta", "🔴 VERKAUFEN", ticker,
            _bm_name(ticker, bm_raw=bm_raw, pos=pos) or _stock_name(ticker, pos=pos) or "—",
            grund, prio,
        )
    for item in bm_kf:
        ticker, grund, mp, prio = item
        add("breakout_meta", "🟢 KAUFEN", ticker, _bm_name(ticker, bm_raw=bm_raw), grund, prio, meta_prob=mp)
        add(
            "breakout_meta", "🟢 KAUFEN", ticker,
            _bm_name(ticker, bm_raw=bm_raw) or _stock_name(ticker) or "—",
            grund, prio, meta_prob=mp,
        )
    # ── IVY: monatliche Handelsanweisungen aus JSON ──
    for o in _ivy_orders_aus_json(ivy_raw):

[512 lines collapsed]
