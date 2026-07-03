# ═══════════════════════════════════════════════════════════════════════════════
#  breakout_meta_page.py  —  Breakout + Meta-Labeling Live-Scanner (Streamlit)
#  Signals kommen von Colab via breakout_meta_signals.json auf GitHub.
#  Portfolio-Positionen werden lokal gespeichert.
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# ── Konstanten (müssen mit breakout_meta.py übereinstimmen) ──────────────────
PROFIT_TARGET  = 0.10
STOP_LOSS      = 0.05
HOLD_DAYS      = 20
KAPITAL_DEFAULT = 100_000
PS_DEFAULT      = 0.12
MAX_POS         = 10

_PORTFOLIO_FILE = Path(__file__).parent / "breakout_meta_portfolio.json"


# ────────────────────────────────────────────────────────────────────────────
#  Hilfsfunktionen
# ────────────────────────────────────────────────────────────────────────────

def _naechster_montag(heute: date | None = None) -> date:
    d = heute or date.today()
    tage = (7 - d.weekday()) % 7
    return d + timedelta(days=tage if tage > 0 else 7)


def _handelstage_zwischen(start: date, end: date) -> int:
    """Grobe Schätzung: Werktage (Mo–Fr)."""
    d, n = start, 0
    while d < end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _load_portfolio() -> dict[str, dict]:
    if _PORTFOLIO_FILE.exists():
        try:
            return json.loads(_PORTFOLIO_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_portfolio(portfolio: dict[str, dict]) -> None:
    _PORTFOLIO_FILE.write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    try:
        return pd.Timestamp(val).date()
    except Exception:
        return None


def _fmt_pct(v: float | None, plus: bool = True) -> str:
    if v is None:
        return "—"
    s = f"{v:+.1%}" if plus else f"{v:.1%}"
    return s


def _ampel(val: float | None, good_pos: bool = True) -> str:
    if val is None:
        return "⬜"
    if good_pos:
        return "🟢" if val > 0 else ("🔴" if val < -0.02 else "🟡")
    return "🟢" if val < 0 else "🔴"


# ────────────────────────────────────────────────────────────────────────────
#  Portfolio-Editor
# ────────────────────────────────────────────────────────────────────────────

def _render_portfolio_editor(portfolio: dict) -> dict:
    """Zeigt einen Inline-Editor für aktuelle Positionen. Gibt aktualisiertes Dict zurück."""
    st.markdown("#### 📋 Meine offenen Positionen")
    st.caption(
        "Trage hier alle offenen Positionen ein. "
        "Klicke nach Änderungen auf **Speichern**."
    )

    cols = st.columns([2, 2, 2, 1])
    cols[0].markdown("**Ticker**")
    cols[1].markdown("**Einstiegskurs**")
    cols[2].markdown("**Einstiegsdatum**")
    cols[3].markdown("")

    # Session-State zum Bearbeiten
    if "bm_portfolio_edit" not in st.session_state:
        st.session_state.bm_portfolio_edit = {
            k: dict(v) for k, v in portfolio.items()
        }

    edit = st.session_state.bm_portfolio_edit
    to_delete = []

    for ticker, pos in list(edit.items()):
        c0, c1, c2, c3 = st.columns([2, 2, 2, 1])
        new_ticker = c0.text_input("Ticker", ticker, key=f"bm_tk_{ticker}",
                                   label_visibility="collapsed").upper().strip()
        new_price  = c1.number_input("Einstieg", value=float(pos.get("entry_price", 0)),
                                     min_value=0.0, step=0.01, format="%.2f",
                                     key=f"bm_ep_{ticker}", label_visibility="collapsed")
        raw_date   = c2.date_input("Datum",
                                   value=_parse_date(pos.get("entry_date")) or date.today(),
                                   key=f"bm_ed_{ticker}", label_visibility="collapsed")
        if c3.button("🗑", key=f"bm_del_{ticker}"):
            to_delete.append(ticker)
        # Übertrage Änderungen
        if new_ticker and new_ticker != ticker:
            edit[new_ticker] = {"entry_price": new_price,
                                "entry_date": str(raw_date)}
            to_delete.append(ticker)
        else:
            edit[ticker] = {"entry_price": new_price, "entry_date": str(raw_date)}

    for tk in to_delete:
        edit.pop(tk, None)

    # Neue Position hinzufügen
    st.markdown("---")
    with st.expander("➕ Neue Position hinzufügen"):
        a, b, c, d = st.columns([2, 2, 2, 1])
        new_tk  = a.text_input("Ticker", key="bm_new_tk",  placeholder="z.B. NVDA")
        new_ep  = b.number_input("Einstiegskurs", min_value=0.0, step=0.01,
                                  key="bm_new_ep", format="%.2f")
        new_ed  = c.date_input("Datum", value=date.today(), key="bm_new_ed")
        if d.button("➕ Add", key="bm_add_btn"):
            tk = new_tk.upper().strip()
            if tk:
                edit[tk] = {"entry_price": float(new_ep), "entry_date": str(new_ed)}
                st.rerun()

    if st.button("💾 Portfolio speichern", type="primary"):
        _save_portfolio(edit)
        st.session_state.bm_portfolio_edit = edit
        st.success("Portfolio gespeichert.")
        return edit

    return edit


# ────────────────────────────────────────────────────────────────────────────
#  Handelsanweisungen berechnen
# ────────────────────────────────────────────────────────────────────────────

def _compute_actions(
    signals: list[dict],
    portfolio: dict,
    kapital: float,
    ps: float,
    fetch_quote_fn=None,
) -> tuple[list, list, list]:
    """
    Gibt (kaufen, halten, verkaufen) als Listen von Dicts zurück.
    fetch_quote_fn(ticker) → float|None   (Live-Kurs, optional)
    """
    heute  = date.today()
    signal_tickers = {s["ticker"] for s in signals}

    verkaufen = []
    halten    = []
    pos_tickers_holding = set()

    for ticker, pos in portfolio.items():
        ep      = float(pos.get("entry_price", 0))
        if ep <= 0:
            continue
        edate   = _parse_date(pos.get("entry_date"))
        target  = ep * (1 + PROFIT_TARGET)
        stop    = ep * (1 - STOP_LOSS)
        days_held = _handelstage_zwischen(edate, heute) if edate else None

        # Aktuellen Kurs holen
        curr: float | None = None
        if fetch_quote_fn:
            try:
                curr = fetch_quote_fn(ticker)
            except Exception:
                curr = None

        ret = ((curr / ep) - 1) if curr else None

        # Exit-Regeln
        if curr and curr >= target:
            grund = f"🎯 Ziel erreicht (+{PROFIT_TARGET:.0%})"
            verkaufen.append(dict(ticker=ticker, grund=grund, entry=ep, curr=curr,
                                  ret=ret, days=days_held, target=target, stop=stop))
        elif curr and curr <= stop:
            grund = f"🛑 Stop-Loss (−{STOP_LOSS:.0%})"
            verkaufen.append(dict(ticker=ticker, grund=grund, entry=ep, curr=curr,
                                  ret=ret, days=days_held, target=target, stop=stop))
        elif days_held is not None and days_held >= HOLD_DAYS:
            curr_s = f"{curr:.2f}" if curr else "?"
            grund = f"⏱ Zeitlimit ({days_held}/{HOLD_DAYS} Tage)"
            verkaufen.append(dict(ticker=ticker, grund=grund, entry=ep, curr=curr,
                                  ret=ret, days=days_held, target=target, stop=stop))
        else:
            tage_rest = (HOLD_DAYS - days_held) if days_held is not None else "?"
            halten.append(dict(ticker=ticker, entry=ep, curr=curr, ret=ret,
                               days=days_held, rest=tage_rest,
                               target=target, stop=stop,
                               hat_signal=ticker in signal_tickers))
            pos_tickers_holding.add(ticker)

    # Neue Kaufsignale: nicht im Portfolio-Halten
    freie_slots = max(0, MAX_POS - len(halten))
    kaufen = []
    for s in signals:
        if s["ticker"] not in portfolio:
            kaufen.append(dict(
                ticker    = s["ticker"],
                meta_prob = s.get("meta_prob"),
                vol_ratio = s.get("vol_ratio", 0),
                target    = s.get("target"),
                stop      = s.get("stop"),
                betrag    = kapital * ps,
            ))

    return kaufen[:freie_slots], halten, verkaufen


# ────────────────────────────────────────────────────────────────────────────
#  Hauptrender-Funktion (wird von app.py aufgerufen)
# ────────────────────────────────────────────────────────────────────────────

def render_breakout_meta_section(
    lade_json_github_fn,
    eodhd_realtime_fn=None,
    json_refresh: int = 0,
):
    """
    Rendert die vollständige Breakout Meta-Labeling Sektion.

    Parameters
    ----------
    lade_json_github_fn : callable(dateiname, _refresh) → dict|list|None
    eodhd_realtime_fn   : callable(ticker) → dict|None  (optional Live-Kurs)
    json_refresh        : int  (Cache-Buster)
    """
    st.subheader("🚀 Breakout Meta-Labeling — S&P 500 Scanner")
    st.caption(
        "Volumen-Ausbruch + Random-Forest Meta-Filter · "
        "Top-20% Signale · CAGR ~60% (OOS 2024-2026) · "
        "Wöchentlicher Check (empfohlen: jeden Montag)"
    )

    # ── Signals von GitHub laden ──────────────────────────────────────────
    raw_signals = lade_json_github_fn("breakout_meta_signals.json", json_refresh)
    signals: list[dict] = []
    scan_date: str      = "unbekannt"

    if isinstance(raw_signals, dict):
        signals   = raw_signals.get("signals", [])
        scan_date = raw_signals.get("scan_date", "unbekannt")
    elif isinstance(raw_signals, list):
        signals = raw_signals

    # ── Header-Metriken ───────────────────────────────────────────────────
    heute  = date.today()
    naechster_check = _naechster_montag(heute)
    tage_bis_check  = (naechster_check - heute).days

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("📅 Letzter Scan", scan_date)
    col2.metric("📡 Neue Signale", len(signals))
    col3.metric("📆 Nächster Check", naechster_check.strftime("%d.%m.%Y"))
    col4.metric("⏳ Tage bis Check", tage_bis_check if tage_bis_check > 0 else "Heute ✓")

    st.info(
        f"**Wann Trades ausführen?** "
        f"Scanne montags nach Börsenschluss (US-Zeit). "
        f"Kauforder am nächsten Handelstag (Dienstag) bei Marktöffnung. "
        f"Verkauforder ebenfalls beim nächsten Handelstag nach dem Signal."
    )

    # ── Portfolio laden ───────────────────────────────────────────────────
    portfolio = _load_portfolio()
    if "bm_portfolio_edit" not in st.session_state:
        st.session_state.bm_portfolio_edit = {k: dict(v) for k, v in portfolio.items()}

    # ── Layout: Portfolio links, Aktionen rechts ──────────────────────────
    tab_check, tab_signale, tab_portfolio = st.tabs(
        ["📊 Handelsanweisungen", "🔍 Scanner-Signale", "📋 Mein Portfolio"]
    )

    # ── Tab: Portfolio bearbeiten ─────────────────────────────────────────
    with tab_portfolio:
        portfolio = _render_portfolio_editor(portfolio)

    # ── Live-Kurs-Funktion ────────────────────────────────────────────────
    def _get_curr(ticker: str) -> float | None:
        if eodhd_realtime_fn is None:
            return None
        try:
            q = eodhd_realtime_fn(f"{ticker}.US")
            return float(q["close"]) if q else None
        except Exception:
            return None

    # ── Handelsanweisungen berechnen ──────────────────────────────────────
    portfolio_edit = st.session_state.bm_portfolio_edit

    kapital_eur = st.session_state.get("bm_kapital", KAPITAL_DEFAULT)
    ps_pct      = st.session_state.get("bm_ps", PS_DEFAULT)

    kaufen, halten, verkaufen = _compute_actions(
        signals, portfolio_edit, kapital_eur, ps_pct,
        fetch_quote_fn=_get_curr if eodhd_realtime_fn else None,
    )

    # ── Tab: Handelsanweisungen ───────────────────────────────────────────
    with tab_check:
        left, right = st.columns([3, 1])
        with right:
            kapital_eur = st.number_input(
                "Kapital (€)", value=KAPITAL_DEFAULT, step=5000, min_value=10000,
                key="bm_kapital"
            )
            ps_pct = st.number_input(
                "Position Size (%)", value=int(PS_DEFAULT * 100),
                min_value=1, max_value=50, step=1, key="bm_ps_pct"
            ) / 100
            st.session_state["bm_ps"] = ps_pct
            betrag = kapital_eur * ps_pct
            st.metric("Betrag/Position", f"€{betrag:,.0f}")
            freie_slots = max(0, MAX_POS - len(halten))
            st.metric("Freie Slots", f"{freie_slots}/{MAX_POS}")

        with left:
            # ── VERKAUFEN ────────────────────────────────────────────────
            if verkaufen:
                st.markdown(f"### 🔴 VERKAUFEN ({len(verkaufen)})")
                rows_vk = []
                for r in verkaufen:
                    curr_s = f"{r['curr']:.2f}" if r["curr"] else "?"
                    ret_s  = _fmt_pct(r["ret"])
                    rows_vk.append({
                        "Ticker"     : r["ticker"],
                        "Grund"      : r["grund"],
                        "Einstieg"   : f"{r['entry']:.2f}",
                        "Aktuell"    : curr_s,
                        "Return"     : ret_s,
                        "Tage"       : f"{r['days']}" if r["days"] else "?",
                        "Ziel"       : f"{r['target']:.2f}",
                        "Stop"       : f"{r['stop']:.2f}",
                    })
                st.dataframe(pd.DataFrame(rows_vk), use_container_width=True,
                             hide_index=True)
            else:
                st.success("✅ Keine Verkäufe nötig.")

            st.markdown("---")

            # ── KAUFEN ───────────────────────────────────────────────────
            if kaufen:
                st.markdown(f"### 🟢 KAUFEN ({len(kaufen)} von {freie_slots} Slots)")
                rows_kauf = []
                for r in kaufen:
                    mp_s = f"{r['meta_prob']:.0%}" if r["meta_prob"] else "—"
                    rows_kauf.append({
                        "Ticker"     : r["ticker"],
                        "Betrag (€)" : f"€{r['betrag']:,.0f}",
                        "Meta P"     : mp_s,
                        "Vol-Ratio"  : f"×{r['vol_ratio']:.1f}",
                        "Ziel"       : f"{r['target']:.2f}" if r["target"] else "—",
                        "Stop"       : f"{r['stop']:.2f}" if r["stop"] else "—",
                    })
                st.dataframe(pd.DataFrame(rows_kauf), use_container_width=True,
                             hide_index=True)
            elif signals:
                st.info("ℹ️ Alle Signale bereits im Portfolio oder keine freien Slots.")
            else:
                st.warning(
                    "⚠️ Keine Signale geladen. Colab-Scan noch nicht hochgeladen? "
                    "→ Führe Zelle 5 + Zelle 6 im Notebook aus."
                )

            st.markdown("---")

            # ── HALTEN ───────────────────────────────────────────────────
            if halten:
                st.markdown(f"### 🟡 HALTEN ({len(halten)})")
                rows_halt = []
                for r in halten:
                    curr_s = f"{r['curr']:.2f}" if r["curr"] else "?"
                    ret_s  = _fmt_pct(r["ret"])
                    ampel  = _ampel(r["ret"])
                    signal_hint = "🔁 Signal vorhanden" if r["hat_signal"] else ""
                    rows_halt.append({
                        ""           : ampel,
                        "Ticker"     : r["ticker"],
                        "Einstieg"   : f"{r['entry']:.2f}",
                        "Aktuell"    : curr_s,
                        "Return"     : ret_s,
                        "Tage"       : f"{r['days']}/{HOLD_DAYS}" if r["days"] else "?",
                        "Restlaufzeit": f"{r['rest']} Tage",
                        "Ziel"       : f"{r['target']:.2f}",
                        "Stop"       : f"{r['stop']:.2f}",
                        "Info"       : signal_hint,
                    })
                st.dataframe(pd.DataFrame(rows_halt), use_container_width=True,
                             hide_index=True)
            elif portfolio_edit:
                st.info("Keine offenen Haltepositionen (alle geschlossen/verkauft?).")
            else:
                st.caption(
                    "Keine Portfolio-Positionen eingegeben. "
                    "→ Tab **Mein Portfolio** zum Eintragen."
                )

        # ── Handelstiming-Hinweis ─────────────────────────────────────────
        with st.expander("ℹ️ Handelstiming & Regeln"):
            st.markdown(f"""
**Wann scannen?** Jeden Montag nach US-Börsenschluss (22:00 Uhr MEZ).

**Wann Trades ausführen?** Am nächsten Handelstag (Dienstag, Marktöffnung, ~15:30 MEZ).

**Exit-Regeln (automatisch):**
- 🎯 **Gewinn-Ziel:** +{PROFIT_TARGET:.0%} vom Einstieg → sofort verkaufen
- 🛑 **Stop-Loss:** −{STOP_LOSS:.0%} vom Einstieg → sofort verkaufen
- ⏱ **Zeitlimit:** {HOLD_DAYS} Handelstage → schließen

**Position-Sizing:** {int(PS_DEFAULT*100)}% Kapital pro Signal · max. {MAX_POS} Positionen gleichzeitig.

**Nächster Check:** {naechster_check.strftime('%A, %d.%m.%Y')}
            """)

    # ── Tab: Scanner-Signale ──────────────────────────────────────────────
    with tab_signale:
        st.markdown(f"#### Scanner-Signale vom {scan_date}")
        if signals:
            df_sig = pd.DataFrame(signals)
            rename = {
                "ticker"    : "Ticker",
                "meta_prob" : "Meta P",
                "vol_ratio" : "Vol-Ratio",
                "target"    : "Ziel",
                "stop"      : "Stop",
                "take"      : "Gefiltert",
            }
            df_sig = df_sig.rename(columns={k: v for k, v in rename.items()
                                             if k in df_sig.columns})
            if "Meta P" in df_sig.columns:
                df_sig["Meta P"] = df_sig["Meta P"].apply(
                    lambda x: f"{x:.0%}" if pd.notna(x) else "—"
                )
            if "Vol-Ratio" in df_sig.columns:
                df_sig["Vol-Ratio"] = df_sig["Vol-Ratio"].apply(
                    lambda x: f"×{x:.1f}" if pd.notna(x) else "—"
                )
            st.dataframe(df_sig, use_container_width=True, hide_index=True)
        else:
            st.info(
                "Noch keine Signale vorhanden. "
                "Führe im Colab-Notebook Zelle 5 + Zelle 6 aus, "
                "damit die Signale als JSON auf GitHub landen."
            )
