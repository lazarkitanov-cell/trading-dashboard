# ═══════════════════════════════════════════════════════════════
# EINMALIG in Colab — Regime-Automatisierung (Drive-unabhängig)
# Secrets: GITHUB_TOKEN, EODHD_API_KEY
# ═══════════════════════════════════════════════════════════════

import base64
import json
import os
import pickle
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "requests"])
    import requests

from google.colab import drive, userdata

REPO = "lazarkitanov-cell/trading-dashboard"
LOCAL = Path("/content/regime_work")          # Colab-Lokal (zuverlaessig)
LOCAL.mkdir(parents=True, exist_ok=True)

DRIVE_ROOTS = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks"),
    Path("/content/drive/MyDrive/Colab Notebooks"),
]

if not Path("/content/drive/MyDrive").exists():
    drive.mount("/content/drive")

ROOT = next((p for p in DRIVE_ROOTS if p.exists()), None)
if ROOT is None:
    raise SystemExit("Colab Notebooks Ordner auf Drive nicht gefunden")

TD = ROOT / "trading-dashboard"
RC_DST = TD / "regime_cache"
RC_DST.mkdir(parents=True, exist_ok=True)
LOCAL_CACHE = LOCAL / "regime_cache"
LOCAL_CACHE.mkdir(parents=True, exist_ok=True)

# Fallback falls regime_final.json auf Drive fehlt
REGIME_FINAL_FALLBACK = {
    "components": ["C5_BREADTH_50", "C1_SPY_EMA200"],
    "active": "C5_BREADTH_50 + C1_SPY_EMA200",
    "quotes": "100/50/0",
    "q_green": 1.0,
    "q_yellow": 0.5,
    "q_red": 0.0,
    "smooth": 3,
    "score_green": 100,
    "score_yellow": 50,
    "crash_overlay": ["spy_crash"],
    "crash_cap": 0.5,
    "overlay_name": "SPY −3%",
}

print("=" * 60)
print("  REGIME AUTOMATION SETUP")
print("=" * 60)
print(f"  Drive: {ROOT}")
print(f"  Lokal: {LOCAL}")


def _read_bytes(path: Path, retries: int = 12) -> bytes | None:
    for _ in range(retries):
        try:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    data = f.read()
                if len(data) > 1000:
                    return data
        except OSError:
            pass
        time.sleep(2)
    return None


def _drive_copy_to_local(src: Path, dst: Path) -> bytes | None:
    """Drive-FUSE: cp funktioniert manchmal wenn Python-open scheitert."""
    import subprocess
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["cp", "-f", str(src), str(dst)], check=True, capture_output=True)
        return _read_bytes(dst, retries=2)
    except Exception:
        return None


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _load_kr():
    import importlib.util
    for src in (TD / "_kassandra_regime.py", ROOT / "_kassandra_regime.py"):
        if src.is_file():
            spec = importlib.util.spec_from_file_location("kr_setup", src)
            kr = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(kr)
            return kr
    raise SystemExit("FEHLER: _kassandra_regime.py nicht gefunden")


# ── 1. Breadth-Bytes in RAM / Colab-Lokal ───────────────────────
print("\n[1/4] breadth_panel.pkl")
breadth_blob: bytes | None = None
for candidate in (
    LOCAL_CACHE / "breadth_panel.pkl",
    RC_DST / "breadth_panel.pkl",
    ROOT / "regime_cache" / "breadth_panel.pkl",
):
    breadth_blob = _read_bytes(candidate)
    if breadth_blob:
        print(f"  gelesen: {candidate} ({len(breadth_blob):,} B)")
        break
    breadth_blob = _drive_copy_to_local(candidate, LOCAL_CACHE / "breadth_panel.pkl")
    if breadth_blob:
        print(f"  per cp: {candidate} ({len(breadth_blob):,} B)")
        break

if breadth_blob is None:
    print("  fehlt — baue Breadth-Cache (~15 Min.)...")
    kr = _load_kr()
    panel = kr._load_breadth_panel(force=True)
    if panel is None or getattr(panel, "empty", True):
        raise SystemExit("FEHLER: Breadth-Panel leer (EODHD_API_KEY?)")
    breadth_blob = pickle.dumps(panel, protocol=4)
    print(f"  gebaut im RAM ({len(breadth_blob):,} B)")

_write_bytes(LOCAL_CACHE / "breadth_panel.pkl", breadth_blob)
_write_bytes(RC_DST / "breadth_panel.pkl", breadth_blob)
print(f"  OK lokal + trading-dashboard ({len(breadth_blob):,} B)")

# ── 2. Weitere Cache-Dateien ────────────────────────────────────
print("\n[2/4] regime_cache vorbereiten")
_cache_names = (
    "regime_final.json", "market_panel.pkl",
    "regime_tuned.json", "regime_winner.json", "kassandra_regime_live.json",
)
for name in _cache_names:
    blob = None
    for base in (ROOT / "regime_cache", RC_DST, TD):
        blob = _read_bytes(base / name, retries=3) if name.endswith(".pkl") else None
        if blob:
            break
        p = base / name
        if p.is_file():
            try:
                blob = p.read_bytes()
                break
            except OSError:
                pass
    if name == "regime_final.json" and not blob:
        blob = json.dumps(REGIME_FINAL_FALLBACK, indent=2, ensure_ascii=False).encode()
        print(f"  OK {name} (Fallback-Konfig)")
    elif blob:
        print(f"  OK {name} ({len(blob):,} B)")
    else:
        print(f"  -- {name} fehlt (optional)")
        continue
    _write_bytes(LOCAL_CACHE / name, blob)
    if name == "kassandra_regime_live.json":
        _write_bytes(TD / name, blob)
    else:
        _write_bytes(RC_DST / name, blob)

# breadth nochmal sicherstellen
_write_bytes(RC_DST / "breadth_panel.pkl", breadth_blob)

# ── 3. GitHub Upload (von Colab-Lokal / TD) ───────────────────
print("\n[3/4] GitHub Upload...")
token = userdata.get("GITHUB_TOKEN").strip()
headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
ver_m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', (TD / "app.py").read_text(encoding="utf-8"))
version = ver_m.group(1) if ver_m else "?"

FILES = (
    "app.py", "stop_check.py", "name_lookup.py", "smallcap_names.json", "sp100_rsl.py", "daily_stops.py",
    "kassandra_regime_display.py", "kassandra_regime_live.json",
    "regime_live_update.py", "regime_export_cache.py", "regime_automation_setup.py",
    "_haa_balanced_live.py", "_taa_strategies.py",
    "_kassandra_regime.py", "_kassandra_regime.b64", "_kr_bootstrap.py",
)
CACHE_UPLOAD = (
    "regime_cache/regime_final.json",
    "regime_cache/breadth_panel.pkl",
    "regime_cache/market_panel.pkl",
)
WORKFLOW = ".github/workflows/stop_check.yml"


def upload(local: Path, repo_path: str) -> None:
    url = f"https://api.github.com/repos/{REPO}/contents/{repo_path}"
    body = {
        "message": f"Regime automation setup v{version}",
        "content": base64.b64encode(local.read_bytes()).decode(),
        "branch": "main",
    }
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 200:
        body["sha"] = r.json().get("sha")
    r = requests.put(url, headers=headers, json=body, timeout=300)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PUT {repo_path}: {r.status_code} {r.text[:200]}")
    print(f"  OK {repo_path} ({local.stat().st_size:,} B)")


for name in FILES:
    p = TD / name
    if not p.is_file():
        raise SystemExit(f"FEHLER: {p} fehlt auf Drive")
    upload(p, name)

for repo_path in CACHE_UPLOAD:
    fname = repo_path.split("/", 1)[1]
    local = RC_DST / fname
    if not local.is_file():
        local = LOCAL_CACHE / fname
    if local.is_file():
        upload(local, repo_path)
    else:
        print(f"  -- uebersprungen: {repo_path}")

wf = TD / WORKFLOW
if wf.is_file():
    upload(wf, WORKFLOW)
else:
    print(f"  WARNUNG: {WORKFLOW} fehlt")

# ── 4. Workflow starten ─────────────────────────────────────────
print("\n[4/4] GitHub Actions Workflow starten...")
r = requests.post(
    f"https://api.github.com/repos/{REPO}/actions/workflows/stop_check.yml/dispatches",
    headers=headers,
    json={"ref": "main"},
    timeout=30,
)
if r.status_code == 204:
    print("  OK Workflow gestartet")
else:
    print(f"  Hinweis: HTTP {r.status_code} — manuell in Actions starten")

print("\n" + "=" * 60)
print("  FERTIG — Regime laeuft ab jetzt automatisch 2x/Tag")
print("=" * 60)
