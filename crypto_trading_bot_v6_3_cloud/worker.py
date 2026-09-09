from __future__ import annotations
import os
import time
from datetime import datetime

from cloud_core import (
    evaluate_all, scan_market, write_json, read_json,
    WORKER_STATUS, V52_STATE, V6_STATE, V52_LATEST, V6_LATEST, MONITOR_LATEST,
)
from notifications import process_notifications
from signal_lab import process_signal_lab

MINUTES = max(15, int(os.environ.get("AUTO_UPDATE_MINUTES", "60")))
SCAN_MARKET = os.environ.get("AUTO_MARKET_SCAN", "1").strip().lower() not in {"0", "false", "no"}


def status(**kwargs):
    payload = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"), **kwargs}
    write_json(WORKER_STATUS, payload)


def cycle():
    messages = []
    try:
        table = None
        monitor_payload = read_json(MONITOR_LATEST, {}) or {}
        if V52_STATE.exists() or V6_STATE.exists():
            table = evaluate_all()
            messages.append(f"Paper-Auswertung: {len(table)} Zeilen")
        else:
            messages.append("Noch keine State-Dateien importiert")
        if SCAN_MARKET:
            monitor_payload = scan_market()
            messages.append(f"Markt-Monitor: {len(monitor_payload.get('rows', []))} Coins")
            lab = process_signal_lab(monitor_payload)
            if lab.get("ok"):
                messages.append(f"Signal-Labor: {lab.get('total', 0)} Signale, +{lab.get('new_signals', 0)} neu")
        else:
            lab = process_signal_lab(monitor_payload) if monitor_payload else {"ok": False}

        note = process_notifications(
            table,
            monitor_payload,
            read_json(V52_LATEST, {}) or {},
            read_json(V6_LATEST, {}) or {},
        )
        if note.get("configured"):
            messages.append(f"Telegram: {note.get('message')} · gesendet {note.get('sent', 0)}")
        else:
            messages.append("Telegram: nicht eingerichtet")
        status(ok=True, message=" · ".join(messages), interval_minutes=MINUTES, notifications=note)
    except Exception as exc:
        status(ok=False, message=str(exc), interval_minutes=MINUTES)


if __name__ == "__main__":
    while True:
        cycle()
        time.sleep(MINUTES * 60)
