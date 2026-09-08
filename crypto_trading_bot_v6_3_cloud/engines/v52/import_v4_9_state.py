from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
candidates = [ROOT.parent / "crypto_trading_bot_v4_9", ROOT.parent / "crypto_trading_bot_v4.9"]
source = next((p for p in candidates if p.exists()), None)
if source is None:
    print("V4.9-Ordner nicht gefunden. Lege V5 direkt neben den V4.9-Ordner.")
    raise SystemExit(1)
for name in ["scanner_config.json", "scanner_state.json", "scanner_status.json", "scanner_trades.csv"]:
    src = source / name
    if src.exists():
        shutil.copy2(src, ROOT / name)
        print("Übernommen:", name)
print("Fertig. Jetzt start_dashboard.bat ausführen.")
input("Enter zum Schließen ...")
