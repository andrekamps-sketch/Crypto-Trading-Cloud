from __future__ import annotations
import shutil
from pathlib import Path
ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
FILES = ["scanner_config.json", "scanner_state.json", "scanner_status.json", "scanner_trades.csv"]
CANDIDATES = ["crypto_trading_bot_v4_6", "crypto_trading_bot_v4_5", "crypto_trading_bot_v4_4"]

def main() -> None:
    if (ROOT / "scanner_state.json").exists():
        print("In V4.8 existiert bereits ein Paper-Konto. Import abgebrochen, damit nichts ueberschrieben wird.")
        input("Enter druecken zum Schliessen ..."); return
    source = next((PARENT / name for name in CANDIDATES if (PARENT / name / "scanner_state.json").exists()), None)
    if source is None:
        print("Kein V4.6/V4.5-Paper-Konto im gleichen uebergeordneten Ordner gefunden.")
        print("Lege V4.8 direkt neben den alten Bot-Ordner.")
        input("Enter druecken zum Schliessen ..."); return
    copied=[]
    for filename in FILES:
        src=source/filename
        if src.exists(): shutil.copy2(src, ROOT/filename); copied.append(filename)
    stop=ROOT/"SCANNER_STOP"
    if stop.exists(): stop.unlink()
    print(f"Paper-Konto aus {source.name} importiert.")
    print("Kopiert: " + ", ".join(copied))
    print("WICHTIG: Alten Bot vorher stoppen und danach nur V4.8 starten.")
    input("Enter druecken zum Schliessen ...")
if __name__ == "__main__": main()
