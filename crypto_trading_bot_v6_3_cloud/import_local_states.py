from __future__ import annotations
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


def find_state(folder: str, filename: str):
    bases = [PARENT, Path.home()/"Desktop", Path.home()/"OneDrive"/"Desktop"]
    candidates = []
    for base in bases:
        candidates += [base/folder/filename, base/folder/folder/filename]
    for p in candidates:
        if p.exists():
            return p
    return None

pairs = [
    ("crypto_trading_bot_v5_2", "forward_competition.json"),
    ("crypto_trading_bot_v6", "v6_forward_state.json"),
]
found = 0
for folder, name in pairs:
    src = find_state(folder, name)
    if src:
        # Quick JSON validation only; the app performs full validation later.
        json.loads(src.read_text(encoding="utf-8"))
        shutil.copy2(src, DATA/name)
        print(f"OK: {name} <- {src}")
        found += 1
    else:
        print(f"NICHT GEFUNDEN: {folder}/{name}")
print(f"\n{found}/2 State-Dateien nach {DATA} kopiert. Die Originale wurden nicht verändert.")
input("Enter zum Schließen ...")
