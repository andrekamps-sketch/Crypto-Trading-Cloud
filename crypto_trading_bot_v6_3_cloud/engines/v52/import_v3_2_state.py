from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
FILES = ["scanner_config.json", "scanner_state.json", "scanner_trades.csv"]
CANDIDATES = ["crypto_trading_bot_v3_2", "crypto_trading_bot_v3_1", "crypto_trading_bot_v3"]


def main() -> None:
    if (ROOT / "scanner_state.json").exists():
        print("In V4.3 existiert bereits ein Paper-Konto. Import abgebrochen, damit nichts ueberschrieben wird.")
        input("Enter druecken zum Schliessen ..."); return
    source = None
    for name in CANDIDATES:
        folder = PARENT / name
        if folder.exists() and (folder / "scanner_state.json").exists(): source = folder; break
    if source is None:
        print("Kein V3.2/V3-Paper-Konto im gleichen uebergeordneten Ordner gefunden.")
        print("Lege V4.3 neben den alten Ordner oder kopiere scanner_state.json/scanner_trades.csv manuell.")
        input("Enter druecken zum Schliessen ..."); return
    copied=[]
    for filename in FILES:
        src=source/filename; dst=ROOT/filename
        if src.exists(): shutil.copy2(src,dst); copied.append(filename)
    print(f"Paper-Konto aus {source.name} importiert.")
    print("Kopiert: " + ", ".join(copied))
    print("WICHTIG: Alten Bot vorher stoppen und danach nur V4.3 starten.")
    input("Enter druecken zum Schliessen ...")


if __name__ == "__main__": main()
