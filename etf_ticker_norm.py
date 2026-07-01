"""Gemeinsame Ticker-Normalisierung ETF Yahoo / EODHD (Depot vs. Screening)."""

from __future__ import annotations

_SUFFIXES = (".US", ".TO", ".LSE", ".XETRA", ".L", ".DE", ".PA", ".SW", ".AS", ".MC")


def etf_ticker_canonical(ticker: str) -> str:
    """AMD.US / AMD → AMD"""
    t = str(ticker or "").upper().strip()
    for sfx in _SUFFIXES:
        if t.endswith(sfx):
            t = t[: -len(sfx)]
            break
    return t.split(".")[0] if t else ""


def _prefer_ticker_key(old_key: str, new_key: str) -> bool:
    """EODHD-Format (.US) bevorzugen."""
    o, n = str(old_key).upper(), str(new_key).upper()
    if ".US" in n and ".US" not in o:
        return True
    return False


def _pos_wert(pos: dict) -> float:
    for k in ("wert_eur", "wert", "aktuell_eur"):
        try:
            v = float(pos.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def merge_etf_portfolio_keys(portfolio: dict | None) -> dict:
    """
    Fasst AMD + AMD.US zu einem Eintrag zusammen (höherer Wert / .US-Key gewinnt).
    Behebt Doppel-Trades VERKAUF+KAUF im Handelsplan.
    """
    if not portfolio:
        return {}
    buckets: dict[str, tuple[str, dict]] = {}
    for key, pos in portfolio.items():
        if not isinstance(pos, dict):
            continue
        canon = etf_ticker_canonical(key)
        if not canon:
            continue
        if canon not in buckets:
            buckets[canon] = (key, dict(pos))
            continue
        ok, ov = buckets[canon]
        merged = {**ov, **pos}
        ow, nw = _pos_wert(ov), _pos_wert(pos)
        if nw > ow or _prefer_ticker_key(ok, key):
            buckets[canon] = (key if (nw > ow or _prefer_ticker_key(ok, key)) else ok, merged)
        else:
            buckets[canon] = (ok, {**pos, **ov})
    return {k: v for k, v in buckets.values()}
