import importlib.util, json, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
def main():
    if not os.environ.get("EODHD_API_KEY"): return 1
    spec = importlib.util.spec_from_file_location("kr", ROOT/"_kassandra_regime.py")
    kr = importlib.util.module_from_spec(spec); spec.loader.exec_module(kr)
    out = kr.live_signal(quiet=False, github=True)
    (ROOT/"kassandra_regime_live.json").write_text(json.dumps(out,indent=2),encoding="utf-8")
    print("OK", out.get("signal")); return 0
if __name__=="__main__": sys.exit(main())
