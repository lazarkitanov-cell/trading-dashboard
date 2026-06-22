# ═══════════════════════════════════════════════════════════════
# Colab-Zelle: Dashboard auf GitHub hochladen (v3.5+)
# Secret: GITHUB_TOKEN (Schlüssel-Symbol links → Secrets)
# ═══════════════════════════════════════════════════════════════

import base64
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "requests"])
    import requests

from google.colab import userdata

REPO = "lazarkitanov-cell/trading-dashboard"
FILES = (
    "app.py", "stop_check.py", "name_lookup.py", "smallcap_names.json", "sp100_rsl.py",
    "kass_etf_bereich.json",
    "kassandra_regime_display.py", "kassandra_regime_live.json",
    "regime_live_update.py", "regime_export_cache.py", "regime_automation_setup.py",
    "_haa_balanced_live.py", "_regime_momentum_live.py", "_taa_strategies.py",
    "regime_momentum_bt.py",
    "_kassandra_regime.py", "_kassandra_regime.b64", "_kr_bootstrap.py",
    "_smallcap_regime_live.py", "_laender_kass_regime.py", "_etf_ampel_regime.py",
    "kassandra_ampel_research.py", "_kar_bootstrap.py",
)
OPTIONAL = (
    "regime_cache/regime_final.json",
    "regime_cache/breadth_panel.pkl",
    "regime_cache/market_panel.pkl",
)
WORKFLOW = (".github/workflows/stop_check.yml", ".github/workflows/stop_check.yml")

KANDIDATEN = [
    Path("/content/drive/MyDrive/Meine Ablage/Colab Notebooks/trading-dashboard"),
    Path("/content/drive/MyDrive/Colab Notebooks/trading-dashboard"),
    Path("/content/drive/Meine Ablage/Colab Notebooks/trading-dashboard"),
]

try:
    from google.colab import drive
    if not Path("/content/drive/MyDrive").exists():
        drive.mount("/content/drive")
except Exception:
    pass

here = next((p for p in KANDIDATEN if (p / "app.py").exists()), None)
if not here:
    print("❌ app.py nicht gefunden.")
    raise SystemExit(1)

print(f"📂 Ordner: {here}")

try:
    token = userdata.get("GITHUB_TOKEN")
except Exception as e:
    print(f"❌ GITHUB_TOKEN: {e}")
    raise SystemExit(1)

app_path = here / "app.py"
ver_m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', app_path.read_text(encoding="utf-8"))
version = ver_m.group(1) if ver_m else "?"
print(f"🚀 Upload → {REPO} (Dashboard v{version})")

headers = {"Authorization": f"Bearer {token.strip()}", "Accept": "application/vnd.github+json"}


def upload_file(local: Path, repo_path: str, *, optional: bool = False) -> bool:
    url = f"https://api.github.com/repos/{REPO}/contents/{repo_path}"
    content_b64 = base64.b64encode(local.read_bytes()).decode()
    sha = None
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code not in (404,):
        if optional:
            print(f"  ⚠️  GET {repo_path}: HTTP {r.status_code} — übersprungen")
            return False
        raise RuntimeError(f"GET {repo_path}: HTTP {r.status_code}")

    body = {
        "message": f"Update {repo_path} (Dashboard v{version})",
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha

    r = requests.put(url, headers=headers, json=body, timeout=120)
    if r.status_code not in (200, 201):
        if optional:
            hint = ""
            if r.status_code == 403 and "workflow" in repo_path:
                hint = (
                    " (Token braucht Scope «workflow» — für Dashboard-Update nicht nötig)"
                )
            print(f"  ⚠️  PUT {repo_path}: HTTP {r.status_code}{hint}")
            if r.text:
                print(f"      {r.text[:200]}")
            return False
        raise RuntimeError(f"PUT {repo_path}: HTTP {r.status_code} — {r.text[:300]}")
    print(f"  ✅ {repo_path} ({local.stat().st_size:,} Bytes)")
    return True


for name in FILES:
    path = here / name
    if not path.exists():
        print(f"❌ Datei fehlt: {path}")
        raise SystemExit(1)
    upload_file(path, name)

for name in OPTIONAL:
    path = here / name
    if path.exists():
        upload_file(path, name)
    else:
        print(f"  ⚠️  optional fehlt: {name} (regime_export_cache.py ausführen)")

local_wf, repo_wf = WORKFLOW
wf_path = here / local_wf
if wf_path.exists():
    upload_file(wf_path, repo_wf, optional=True)
else:
    print(f"  ⚠️  Workflow fehlt: {local_wf}")

print(f"\n✅ Fertig — Dashboard v{version} auf GitHub.")
print("   Streamlit: Reboot in Cloud, dann «Kurse & JSON aktualisieren».")
