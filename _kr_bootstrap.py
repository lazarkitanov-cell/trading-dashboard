"""Colab-Bootstrap für Kassandra Regime."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REGIME_VERSION = 5

DRIVE_CANDIDATES = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
]
GITHUB_RAW = "https://raw.githubusercontent.com/lazarkitanov-cell/trading-dashboard/main/"


def _read_regime_version(path: Path) -> int:
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:40]:
            if line.strip().startswith("REGIME_VERSION"):
                return int(line.split("=", 1)[1].strip().strip('"').strip("'"))
    except Exception:
        pass
    return 0


def _script_ok(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return False
    return "def run_stage_3" in text and _read_regime_version(path) >= REGIME_VERSION


def _install_bytes(dest: Path, data: bytes, label: str) -> Path:
    dest.write_bytes(data)
    print(f"  {label}")
    return dest


def _ensure_regime_script(notebook_dir: Path) -> Path:
    """Neuestes Skript: trading-dashboard/ → Root → GitHub → .b64"""
    import base64
    import gzip
    import shutil
    import urllib.request

    fname = "_kassandra_regime.py"
    dest = notebook_dir / fname

    if _script_ok(dest):
        print(f"  ✓ {fname} v{_read_regime_version(dest)}")
        return dest

    if dest.is_file():
        print(f"  🔄 {fname} veraltet — suche Update…")

    candidates = []
    for src_dir in (notebook_dir / "trading-dashboard", notebook_dir):
        src = src_dir / fname
        if _script_ok(src):
            candidates.append(src)
    if candidates:
        best = max(candidates, key=_read_regime_version)
        shutil.copy2(best, dest)
        print(f"  📂 {fname} von {best.parent}")
        return dest

    try:
        data = urllib.request.urlopen(GITHUB_RAW + fname, timeout=45).read()
        if data and b"def run_stage_3" in data:
            return _install_bytes(dest, data, f"⬇️  {fname} von GitHub")
    except Exception as e:
        print(f"  ⚠️  GitHub: {e}")

    for bp in (
        notebook_dir / "_kassandra_regime.b64",
        notebook_dir / "trading-dashboard" / "_kassandra_regime.b64",
    ):
        if not bp.is_file():
            continue
        try:
            raw = gzip.decompress(base64.b64decode(bp.read_text(encoding="utf-8").strip()))
            if b"def run_stage_3" in raw:
                return _install_bytes(dest, raw, f"📦 {fname} aus {bp.name}")
        except Exception as e:
            print(f"  ⚠️  Bundle {bp.name}: {e}")

    if dest.is_file():
        raise RuntimeError(
            f"{fname} auf Drive ist veraltet (kein run_stage_3).\n"
            "→ Datei löschen oder neu syncen: _kassandra_regime.py + _kassandra_regime.b64"
        )
    raise FileNotFoundError(
        f"{fname} fehlt in {notebook_dir}\n"
        "→ Google Drive Sync: _kassandra_regime.py in Colab Notebooks"
    )


def _load_regime_module(script: Path):
    for name in list(sys.modules):
        if name == "kassandra_regime" or name.startswith("kassandra_regime."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("kassandra_regime", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run_stage_3"):
        raise RuntimeError(f"Modul zu alt: {script}")
    return mod


def _resolve_notebook_dir() -> Path:
    try:
        from google.colab import drive
        if not Path("/content/drive/MyDrive").exists():
            drive.mount("/content/drive")
        return next((p for p in DRIVE_CANDIDATES if p.exists()), Path("."))
    except ImportError:
        return Path(r"h:\Meine Ablage\Colab Notebooks")


NOTEBOOK_DIR = _resolve_notebook_dir()
SCRIPT = _ensure_regime_script(NOTEBOOK_DIR)
kr = _load_regime_module(SCRIPT)


def kassandra_regime(choice=None):
    """0=Live · 1–6=Stufen · 9=0–2 · 10=0–5"""
    if choice is None:
        print("""+-------------------------------------------------------------+
|  KASSANDRA REGIME                                           |
+----+--------------------------------------------------------+
|  0 |  Heutiges Signal (Investitionsquote)                   |
|  1 |  Stufe 0 — Buy & Hold Baseline (5 Indizes)             |
|  2 |  Stufe 1 — Einzelkomponenten → Top-3                   |
|  3 |  Stufe 2 — Kombinationen (Top-3 fest)                  |
|  4 |  Stufe 3 — Quoten & Glättung (Gewinner)                |
|  5 |  Stufe 4 — Crash-Overlay                               |
|  6 |  Stufe 5 — Multi-Index-Validierung                     |
|  9 |  Stufe 0 + 1 + 2 komplett                              |
| 10 |  Stufe 0–5 komplett (nach Stufe 2 schneller)           |
+----+--------------------------------------------------------+
  kassandra_regime(NUMMER)
""")
        return
    c = int(choice)
    dispatch = {
        0: kr.live_signal,
        1: kr.run_stage_0,
        2: kr.run_stage_1,
        3: kr.run_stage_2,
        4: kr.run_stage_3,
        5: kr.run_stage_4,
        6: kr.run_stage_5,
        9: lambda: kr.run_all_stages(through=2),
        10: lambda: kr.run_all_stages(through=5),
    }
    fn = dispatch.get(c)
    if fn:
        return fn()
    print("Unbekannt. kassandra_regime() fuer Menue.")


if __name__ == "__main__":
    print("✅ Kassandra Regime bereit.  kassandra_regime()  oder  kassandra_regime(10)")
    kassandra_regime()
