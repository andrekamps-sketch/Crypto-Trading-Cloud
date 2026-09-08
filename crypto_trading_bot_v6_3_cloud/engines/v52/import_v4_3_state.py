from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
FILES = ["scanner_config.json", "scanner_state.json", "scanner_trades.csv"]
CANDIDATES = ["crypto_trading_bot_v4_3", "crypto_trading_bot_v4_2", "crypto_trading_bot_v4_1", "crypto_trading_bot_v4"]

def main() -> None:
    if (ROOT / "scanner_state.json").exists():
        print("In V4.4 existiert bereits ein Paper-Konto. Import abgebrochen, damit nichts ueberschrieben wird.")
        input("Enter druecken zum Schliessen ...")
        return
    source = None
    for name in CANDIDATES:
        folder = PARENT / name
        if folder.exists() and (folder / "scanner_state.json").exists():
            source = folder
            break
    if source is None:
        print("Kein V4.3/V4.2-Paper-Konto im gleichen uebergeordneten Ordner gefunden.")
        print("Lege V4.4 neben den alten Ordner oder kopiere scanner_state.json/scanner_trades.csv manuell.")
        input("Enter druecken zum Schliessen ...")
        return
    copied = []
    for filename in FILES:
        src = source / filename
        dst = ROOT / filename
        if src.exists():
            shutil.copy2(src, dst)
            copied.append(filename)
    print(f"Paper-Konto aus {source.name} importiert.")
    print("Kopiert: " + ", ".join(copied))
    print("WICHTIG: Alten Bot vorher stoppen und danach nur V4.4 starten.")
    input("Enter druecken zum Schliessen ...")

if __name__ == "__main__":
    main()
