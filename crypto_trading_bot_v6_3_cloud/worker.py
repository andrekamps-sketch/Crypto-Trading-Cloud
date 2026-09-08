from __future__ import annotations
import os
import time
from datetime import datetime

from cloud_core import evaluate_all, scan_market, write_json, WORKER_STATUS, V52_STATE, V6_STATE

MINUTES = max(15, int(os.environ.get("AUTO_UPDATE_MINUTES", "60")))
SCAN_MARKET = os.environ.get("AUTO_MARKET_SCAN", "1").strip().lower() not in {"0", "false", "no"}


def status(**kwargs):
    payload = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"), **kwargs}
    write_json(WORKER_STATUS, payload)


def cycle():
    messages = []
    try:
        if V52_STATE.exists() or V6_STATE.exists():
            df = evaluate_all()
            messages.append(f"Paper-Auswertung: {len(df)} Zeilen")
        else:
            messages.append("Noch keine State-Dateien importiert")
        if SCAN_MARKET:
            m = scan_market()
            messages.append(f"Markt-Monitor: {len(m.get('rows', []))} Coins")
        status(ok=True, message=" · ".join(messages), interval_minutes=MINUTES)
    except Exception as exc:
        status(ok=False, message=str(exc), interval_minutes=MINUTES)


if __name__ == "__main__":
    while True:
        cycle()
        time.sleep(MINUTES * 60)
