
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  HAA-BALANCED — Live (monatlich · Colab → JSON → GitHub → Streamlit)           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

MEINE_POSITIONEN = ""   # Signal (IWM, DBC…) oder Scalable-Ticker (XRS2, SXRS…) — kommagetrennt
KAPITAL_EUR      = 100_000

import base64
import json
import os
import time
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

warnings.filterwarnings("ignore")

DRIVE_CANDIDATES = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
]
try:
    from google.colab import drive, userdata
    if not Path("/content/drive/MyDrive").exists():
        drive.mount("/content/drive")
    NOTEBOOK_DIR = next((p for p in DRIVE_CANDIDATES if p.exists()), Path("."))
    try:
        EODHD_TOKEN = userdata.get("EODHD_API_KEY") or userdata.get("EODHD_TOKEN")
        GITHUB_TOKEN = userdata.get("GITHUB_TOKEN")
    except Exception:
        EODHD_TOKEN = os.environ.get("EODHD_API_KEY", "")
        GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
except ImportError:
    NOTEBOOK_DIR = Path(r"h:\Meine Ablage\Colab Notebooks")
    EODHD_TOKEN = os.environ.get("EODHD_API_KEY", "")
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

if not EODHD_TOKEN:
    raise RuntimeError("EODHD_API_KEY fehlt (Colab Secrets oder Umgebungsvariable).")

POSITIONEN_FILE = NOTEBOOK_DIR / "haa_meine_positionen.txt"
OUTPUT_JSON = NOTEBOOK_DIR / "haa_balanced_positionen.json"
GITHUB_REPO = "lazarkitanov-cell/trading-dashboard"
GITHUB_PATH = "haa_balanced_positionen.json"

ETF_NAMES = {
    "SPY": "S&P 500", "IWM": "Russell 2000", "VWO": "EM Stocks", "VEA": "Dev. ex-US",
    "VNQ": "US REITs", "DBC": "Commodities", "IEF": "Treasury 7-10Y", "TLT": "Treasury 20+Y",
    "SHY": "Treasury 1-3Y", "TIP": "TIPS (Canary)",
}

# Backtest-Signal (US) → Scalable/gettex-Kauf (UCITS). Backtest in _taa_strategies.py unverändert.
HAA_SCALABLE_EXEC = {
    "SPY": "I500",
    "IWM": "XRS2",   # Xtrackers Russell 2000 (physisch, Xetra/gettex)
    "VWO": "LEMA",
    "VEA": "EXUS",
    "VNQ": "H4ZL",
    "DBC": "SXRS",   # iShares Diversified Commodity Swap (BCOM) — Scalable/gettex
    "IEF": "7USH",   # Amundi US Treasury 7-10Y EUR-Hedged (Acc)
    "TLT": "SXRC",
    "SHY": "IS0F",
    "TIP": "9V60",
}
HAA_SCALABLE_ISIN = {
    "SPY": "IE00BMTX1Y45",
    "IWM": "IE00BJZ2DD79",
    "VWO": "LU2573967036",
    "VEA": "IE0006WW1TQ4",
    "VNQ": "IE00B5L01S80",
    "DBC": "IE00BDFL4P12",
    "IEF": "LU1407888137",
    "TLT": "IE00BFM6TC58",
    "SHY": "IE00BYXPSP02",
    "TIP": "LU1452600197",
}
HAA_EXEC_NAMES = {
    "I500": "iShares S&P 500 Swap UCITS (Acc)",
    "XRS2": "Xtrackers Russell 2000 UCITS ETF 1C",
    "LEMA": "Amundi Core MSCI EM Swap UCITS (Acc)",
    "EXUS": "Xtrackers MSCI World ex USA UCITS 1C",
    "H4ZL": "HSBC FTSE EPRA NAREIT Developed UCITS (Dist)",
    "SXRS": "iShares Diversified Commodity Swap UCITS ETF (Acc)",
    "CMOD": "Invesco Bloomberg Commodity UCITS ETF (Acc)",  # frühere Map (Scalable nicht handelbar)
    "LYTR": "Amundi Bloomberg Commodity ex-Agriculture UCITS (Acc)",
    "7USH": "Amundi US Treasury 7-10Y UCITS EUR-Hedged (Acc)",
    "SXRC": "iShares USD Treasury 20+yr UCITS (Acc)",
    "IS0F": "iShares USD Treasury 1-3yr UCITS (Acc)",
    "9V60": "Amundi US TIPS UCITS (Acc)",
}
_EXEC_TO_SIGNAL = {exe.upper(): sig for sig, exe in HAA_SCALABLE_EXEC.items()}
for _alias, _sig in (
    ("LYTR", "DBC"),       # Ivy-Ticker im HAA-Depot → weiter als DBC-Signal lesen
    ("LYTR.XETRA", "DBC"),
    ("CMOD", "DBC"),       # frühere Map
    ("RU2K", "IWM"),       # frühere Map
    ("SXRM", "IEF"),       # frühere Map (unhedged iShares)
):
    _EXEC_TO_SIGNAL[_alias.upper()] = _sig


def _signal_to_exec(signal):
    return HAA_SCALABLE_EXEC.get(str(signal).upper(), str(signal).upper())


def _exec_to_signal(ticker):
    t = str(ticker).strip().upper()
    if t in ETF_NAMES:
        return t
    if t in _EXEC_TO_SIGNAL:
        return _EXEC_TO_SIGNAL[t]
    base = t.split(".")[0]
    if base in _EXEC_TO_SIGNAL:
        return _EXEC_TO_SIGNAL[base]
    return t


def _normalize_depot(tickers):
    return [_exec_to_signal(t) for t in tickers]


def _exec_name(exec_ticker):
    return HAA_EXEC_NAMES.get(exec_ticker.upper(), exec_ticker)


def _load_taa():
    ns = {}
    src = (NOTEBOOK_DIR / "_taa_strategies.py").read_text(encoding="utf-8")
    exec(compile(src, "_taa_strategies.py", "exec"), ns)
    return ns


def upload_json_zu_github(lokal_pfad, repo_pfad=GITHUB_PATH, repo=GITHUB_REPO, commit_msg=None):
    if not GITHUB_TOKEN:
        print("  ℹ️  GITHUB_TOKEN fehlt — nur lokal gespeichert")
        return False
    path = Path(lokal_pfad)
    content = base64.b64encode(path.read_bytes()).decode()
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    url = f"https://api.github.com/repos/{repo}/contents/{repo_pfad}"
    r = requests.get(url, headers=headers, timeout=30)
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {
        "message": commit_msg or f"HAA-Balanced update {datetime.now():%Y-%m-%d %H:%M}",
        "content": content,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=60)
    if r.status_code in (200, 201):
        print(f"  ✅ GitHub: {repo}/{repo_pfad}")
        return True
    print(f"  ⚠ GitHub Upload: {r.status_code} {r.text[:200]}")
    return False


def _positionen_laden():
    if POSITIONEN_FILE.is_file():
        raw = POSITIONEN_FILE.read_text(encoding="utf-8").strip()
        if raw:
            return [t.strip().upper() for t in raw.replace(";", ",").split(",") if t.strip()]
    return []


def _positionen_speichern(tickers):
    try:
        POSITIONEN_FILE.write_text(", ".join(tickers), encoding="utf-8")
    except Exception:
        pass


def parse_meine_positionen():
    if MEINE_POSITIONEN and str(MEINE_POSITIONEN).strip():
        return [t.strip().upper() for t in str(MEINE_POSITIONEN).replace(";", ",").split(",") if t.strip()]
    return _positionen_laden()


def _fmt_mom(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+.1%}"


def run_live(meine_positionen=None, kapital_eur=None):
    global MEINE_POSITIONEN, KAPITAL_EUR
    if meine_positionen is not None:
        MEINE_POSITIONEN = ", ".join(str(t).strip().upper() for t in meine_positionen if str(t).strip())
    if kapital_eur is not None:
        KAPITAL_EUR = float(kapital_eur)

    print("═" * 72)
    print("  HAA-BALANCED — LIVE (Keller Hybrid Asset Allocation)")
    print("═" * 72)

    taa = _load_taa()
    monthly = taa["load_monthly"](sorted(set(taa["HAA_O"] + taa["HAA_D"] + taa["HAA_P"])))
    sig = taa["haa_signal_detail"](monthly)
    weights = sig["weights"]
    target = [t for t, w in sorted(weights.items(), key=lambda x: -x[1]) if w > 0.001]

    meine_raw = parse_meine_positionen()
    meine = _normalize_depot(meine_raw)
    if meine_raw:
        _positionen_speichern(meine_raw)

    dt = datetime.now()
    signal_date = monthly.index[-1].strftime("%Y-%m-%d")
    tip_m = sig["p_m"].get("TIP")
    crash = sig["crash"]
    regime = "defensive" if crash else sig.get("regime", "offensive")
    regime_label = "🔴 DEFENSIV — TIP-Canary negativ" if crash else "🟢 OFFENSIV — Top-4 Momentum"

    print(f"\n  Signal-Monat : {signal_date}")
    print(f"  Regime       : {regime_label}")
    print(f"  TIP (Canary) : {_fmt_mom(tip_m)}")
    if not crash:
        print(f"  Top-4 Picks  : {', '.join(sig.get('picks', []))}")
    print(f"  Cash-Fallback: {sig.get('cash', '—')}")
    print(f"\n  Ziel-Allokation — Signal / Scalable-Kauf ({len(target)} ETFs):")
    for tk in target:
        w = weights[tk]
        exe = _signal_to_exec(tk)
        isin = HAA_SCALABLE_ISIN.get(tk, "—")
        print(
            f"    · {tk:6} → {exe:6}  {ETF_NAMES.get(tk, tk):22}  {w:6.1%}  "
            f"(~€{KAPITAL_EUR * w:,.0f})  ISIN {isin}"
        )
    if "DBC" in target:
        print("  ℹ️  DBC → SXRS (iShares BCOM Swap): getrennt von Ivy-Rohstoff LYTR")
    if "IEF" in target:
        print("  ℹ️  IEF → 7USH: EUR-hedged UCITS (Backtest nutzt unhedged US-Signal IEF)")

    selection = taa["print_haa_selection_explanation"](sig, weights)

    prev_weights = {}
    if meine:
        n = len(meine)
        prev_weights = {t: 1.0 / n for t in meine}

    kaufen_sig = [t for t in target if t not in meine]
    verkaufen_sig = [t for t in meine if t not in target]
    kaufen = [_signal_to_exec(t) for t in kaufen_sig]
    verkaufen = [_signal_to_exec(t) for t in verkaufen_sig]

    def _ha_row(aktion, signal_tk, prev_w, nw, delta, grund, ziel_eur=None):
        exe = _signal_to_exec(signal_tk)
        row = {
            "aktion": aktion,
            "ticker": exe,
            "signal_ticker": signal_tk,
            "isin": HAA_SCALABLE_ISIN.get(signal_tk),
            "name": _exec_name(exe),
            "grund": grund,
            "prev": prev_w,
            "new": nw,
            "delta": delta,
            "prioritaet": "Plan",
        }
        if ziel_eur is not None:
            row["ziel_eur"] = ziel_eur
        return row

    ha = []
    for tk in verkaufen_sig:
        prev_w = prev_weights.get(tk, 0)
        ha.append(_ha_row(
            "🔴 VERKAUFEN", tk, prev_w, 0.0, -prev_w,
            f"Monats-Rebalancing · Signal {tk} → Verkauf {_signal_to_exec(tk)}",
        ))
    for tk in target:
        nw = weights[tk]
        prev_w = prev_weights.get(tk, 0.0)
        delta = nw - prev_w
        if tk in meine and abs(delta) < 0.005:
            ha.append(_ha_row(
                "⚪ HALTEN", tk, prev_w, nw, delta,
                f"Gewicht unverändert · Ziel {nw:.0%}",
                round(KAPITAL_EUR * nw),
            ))
            continue
        if tk not in meine:
            ha.append(_ha_row(
                "🟢 KAUFEN", tk, prev_w, nw, delta,
                f"HAA Top-4 / Defensive · Signal {tk} → Kauf {_signal_to_exec(tk)} · Ziel {nw:.0%}",
                round(KAPITAL_EUR * nw),
            ))
        elif abs(delta) >= 0.005:
            aktion = "🟢 AUFSTOCKEN" if delta > 0 else "🔴 REDUZIEREN"
            ha.append(_ha_row(
                aktion, tk, prev_w, nw, delta,
                f"Gewicht anpassen · {prev_w:.0%} → {nw:.0%}",
                round(KAPITAL_EUR * nw),
            ))

    if meine_raw:
        print(f"\n  Mein Depot (Eingabe): {', '.join(meine_raw)}")
        if meine_raw != meine:
            print(f"  → Signal-Abgleich:    {', '.join(meine)}")
        if kaufen:
            print(f"  🟢 KAUFEN   : {', '.join(f'{s}→{e}' for s, e in zip(kaufen_sig, kaufen))}")
        if verkaufen:
            print(f"  🔴 VERKAUFEN: {', '.join(f'{s}→{e}' for s, e in zip(verkaufen_sig, verkaufen))}")
        if not kaufen and not verkaufen:
            print("  ✅ Keine Ticker-Wechsel — ggf. nur Gewichtsanpassung")

    rankings = sorted(
        sig.get("o_m", {}).items(),
        key=lambda x: x[1] if x[1] is not None else -999,
        reverse=True,
    )
    payload = {
        "datum": dt.strftime("%Y-%m-%d"),
        "stand": dt.strftime("%Y-%m-%d %H:%M"),
        "sync_ts": dt.strftime("%Y-%m-%d %H:%M"),
        "signal_monat": signal_date,
        "strategie": "HAA-Balanced",
        "regime": regime,
        "regime_label": regime_label,
        "tip_momentum": tip_m,
        "crash": crash,
        "cash_fallback": sig.get("cash"),
        "ziel": [
            {
                "ticker": t,
                "signal_ticker": t,
                "exec_ticker": _signal_to_exec(t),
                "isin": HAA_SCALABLE_ISIN.get(t),
                "name": ETF_NAMES.get(t, t),
                "gewicht": weights[t],
            }
            for t in target
        ],
        "ziel_ticker": target,
        "ziel_gewichte": weights,
        "ziel_gewichte_exec": {_signal_to_exec(t): w for t, w in weights.items() if w > 0.001},
        "scalable_map": dict(HAA_SCALABLE_EXEC),
        "scalable_isin": dict(HAA_SCALABLE_ISIN),
        "rankings_offensive": [
            {"ticker": t, "momentum": m, "momentum_pct": None if pd.isna(m) else round(m * 100, 2)}
            for t, m in rankings
        ],
        "selection_erklaerung": selection,
        "vergleich_offensiv": selection.get("offensiv", []),
        "vergleich_defensiv": selection.get("defensiv", []),
        "canary_detail": selection.get("canary", {}),
        "regel_text": selection.get("regel_text", ""),
        "momentum_methode": selection.get("momentum_methode", ""),
        "verkaufen": verkaufen,
        "kaufen": kaufen,
        "verkaufen_signal": verkaufen_sig,
        "kaufen_signal": kaufen_sig,
        "meine_aktien": meine,
        "meine_aktien_eingabe": meine_raw,
        "kapital_eur": KAPITAL_EUR,
        "handelsanweisungen": ha,
        "rebal_freq": "monthly",
        "hinweis": (
            "Monatsende-Signal (US-ETF) → Umschichtung 1. Handelstag · "
            "Kauf/Verkauf = Scalable UCITS (gettex)"
        ),
    }

    OUTPUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  💾 {OUTPUT_JSON.name}")
    upload_json_zu_github(OUTPUT_JSON)
    print("\n✅ Fertig.")
    return payload


if __name__ == "__main__":
    run_live()
