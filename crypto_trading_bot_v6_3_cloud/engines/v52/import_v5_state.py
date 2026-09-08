from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
source = ROOT.parent / "crypto_trading_bot_v5"
if not source.exists():
    print("V5-Ordner nicht gefunden. Lege V5.1 direkt neben den V5-Ordner.")
    raise SystemExit(1)
for name in ["scanner_config.json", "scanner_state.json", "scanner_status.json", "scanner_trades.csv"]:
    src = source / name
    if src.exists():
        shutil.copy2(src, ROOT / name)
        print("Übernommen:", name)
print("Fertig. Jetzt start_dashboard.bat ausführen.")
input("Enter zum Schließen ...")
