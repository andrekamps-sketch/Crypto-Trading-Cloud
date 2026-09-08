from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
source = ROOT.parent / "crypto_trading_bot_v5_1"
if not source.exists():
    print("V5.1-Ordner nicht gefunden. Lege V5.2 direkt neben den V5.1-Ordner.")
    raise SystemExit(1)
for name in ["scanner_config.json", "scanner_state.json", "scanner_status.json", "scanner_trades.csv"]:
    src = source / name
    if src.exists():
        shutil.copy2(src, ROOT / name)
        print("Übernommen:", name)
print("Fertig. Der neue Forward-Wettkampf wird absichtlich NICHT aus einer alten Version übernommen und beginnt erst, wenn du ihn in V5.2 startest.")
input("Enter zum Schließen ...")
