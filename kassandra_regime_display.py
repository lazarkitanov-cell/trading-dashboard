"""Kassandra Regime — Anzeige für Dashboard & E-Mail (JSON: kassandra_regime_live.json)."""

from __future__ import annotations


def regime_ampel_key(signal: str | None) -> str:
    s = (signal or "").upper()
    if "ROT" in s:
        return "red"
    if "GELB" in s:
        return "yellow"
    return "green"


def regime_signal_emoji(signal: str | None) -> str:
    k = regime_ampel_key(signal)
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(k, "⚪")


def regime_aktion_text(invest_pct: float | None, signal: str | None = None) -> str:
    pct = int(round(float(invest_pct or 0) * 100))
    if pct >= 100:
        return "Voll investiert"
    if pct <= 0:
        return "0% — komplett defensiv (Cash)"
    if signal and "CRASH" in signal.upper():
        return f"{pct}% (Crash-Overlay aktiv)"
    return f"{pct}% Marktexposition"


def regime_components_lines(data: dict) -> list[str]:
    comps = data.get("components") or {}
    return [
        f"{'✅' if ok else '❌'} {name}"
        for name, ok in comps.items()
    ]


def format_regime_banner(data: dict | None) -> dict:
    """Streamlit/E-Mail: einheitliches Banner-Dict."""
    if not data or not isinstance(data, dict) or not data.get("signal"):
        return {
            "ampel": "yellow",
            "label": "⚠️ Kein Regime-Signal",
            "aktion": "Colab: kassandra_regime(0) → JSON auf GitHub hochladen",
            "caption": "",
            "datum": "—",
            "score": None,
            "invest_pct": None,
            "quotes": "",
            "overlay": "",
        }
    signal = str(data.get("signal") or "—")
    invest = float(data.get("invest_pct") or 0)
    emoji = regime_signal_emoji(signal)
    base = signal.split("+")[0].strip()
    return {
        "ampel": regime_ampel_key(signal),
        "label": f"{emoji} {base}",
        "aktion": regime_aktion_text(invest, signal),
        "caption": _regime_caption(data),
        "datum": data.get("datum") or "—",
        "score": data.get("score"),
        "invest_pct": invest,
        "quotes": data.get("quotes") or "",
        "overlay": data.get("overlay") or "",
        "components": regime_components_lines(data),
        "crash_overlay_active": bool(data.get("crash_overlay_active")),
    }


def _regime_caption(data: dict) -> str:
    parts = []
    if data.get("score") is not None:
        parts.append(f"Score {data['score']}/100")
    if data.get("quotes"):
        parts.append(f"Quoten {data['quotes']}")
    if data.get("overlay") and data.get("overlay") != "—":
        parts.append(f"Overlay {data['overlay']}")
    comps = data.get("components") or {}
    for name, ok in comps.items():
        parts.append(f"{'✓' if ok else '✗'} {name}")
    if data.get("crash_overlay_active"):
        parts.append("⚠ Crash-Overlay heute")
    return "  ·  ".join(parts)


def regime_email_html(data: dict | None) -> str:
    b = format_regime_banner(data)
    colors = {"green": "#00c853", "yellow": "#ffd600", "red": "#ff1744"}
    border = colors.get(b["ampel"], "#888")
    comp_rows = ""
    if data and data.get("components"):
        for name, ok in data["components"].items():
            comp_rows += (
                f'<tr><td style="padding:4px 8px;color:#ccc">'
                f'{"✅" if ok else "❌"} {name}</td></tr>'
            )
    comp_table = (
        f'<table style="margin-top:8px;font-size:13px">{comp_rows}</table>'
        if comp_rows else ""
    )
    return f"""
    <div style="background:#1a1a2e;border:2px solid {border};border-radius:8px;
                padding:15px;margin:0 0 15px 0">
        <h2 style="color:{border};margin:0 0 6px 0">
            🌐 Kassandra Regime — {b['label']}
        </h2>
        <p style="margin:0 0 4px 0;font-size:16px"><strong>{b['aktion']}</strong></p>
        <p style="margin:0;color:#aaa;font-size:13px">{b['caption']}</p>
        <p style="margin:6px 0 0;color:#888;font-size:12px">Stand: {b['datum']}</p>
        {comp_table}
    </div>"""
