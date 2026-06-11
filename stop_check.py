# ═══════════════════════════════════════════════════════════════
# Colab-Zelle: Dashboard auf GitHub hochladen (v3.4+)
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
FILES = ("app.py", "stop_check.py", "name_lookup.py")

# Drive-Pfad finden (Google Drive Sync-Ordner)
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
    print("❌ app.py nicht gefunden. Geprüfte Pfade:")
    for p in KANDIDATEN:
        print(f"   {p}  →  existiert: {p.exists()}, app.py: {(p / 'app.py').exists()}")
    raise SystemExit(1)

print(f"📂 Ordner: {here}")

try:
    token = userdata.get("GITHUB_TOKEN")
except Exception as e:
    print(f"❌ GITHUB_TOKEN nicht lesbar: {e}")
    print("   Colab: 🔑 Secrets → Name: GITHUB_TOKEN → Notebook neu starten")
    raise SystemExit(1)

if not token or not token.strip():
    print("❌ GITHUB_TOKEN ist leer.")
    raise SystemExit(1)

app_path = here / "app.py"
ver_m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', app_path.read_text(encoding="utf-8"))
version = ver_m.group(1) if ver_m else "?"
print(f"🚀 Upload → {REPO} (Dashboard v{version})")

headers = {"Authorization": f"Bearer {token.strip()}", "Accept": "application/vnd.github+json"}

for name in FILES:
    path = here / name
    if not path.exists():
        print(f"❌ Datei fehlt: {path}")
        raise SystemExit(1)

    url = f"https://api.github.com/repos/{REPO}/contents/{name}"
    content_b64 = base64.b64encode(path.read_bytes()).decode()
    sha = None
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code not in (404,):
        print(f"❌ GET {name}: HTTP {r.status_code} — {r.text[:300]}")
        raise SystemExit(1)

    body = {
        "message": f"Update {name} (Dashboard v{version})",
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha

    r = requests.put(url, headers=headers, json=body, timeout=60)
    if r.status_code not in (200, 201):
        print(f"❌ PUT {name}: HTTP {r.status_code} — {r.text[:400]}")
        if r.status_code == 401:
            print("   → Token ungültig oder abgelaufen (neuen PAT anlegen)")
        elif r.status_code == 403:
            print("   → Keine Schreibrechte auf lazarkitanov-cell/trading-dashboard")
        raise SystemExit(1)
    print(f"  ✅ {name} ({path.stat().st_size:,} Bytes)")

print("\n✅ Fertig. Streamlit zeigt in ~1 Min. v" + version + " in der Sidebar.")
