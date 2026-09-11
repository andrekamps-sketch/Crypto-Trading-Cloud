from __future__ import annotations
import os
import time
from datetime import datetime

from cloud_core import (
    evaluate_all, scan_market, write_json, read_json,
    WORKER_STATUS, V52_STATE, V6_STATE, V52_LATEST, V6_LATEST, MONITOR_LATEST,
)
from notifications import process_notifications, send_telegram
from signal_lab import process_signal_lab
from strategy_challenger import process_challenge
from live_paper import process_live_paper, state as live_paper_state
from live_paper_long_short import process_long_short_paper, state as long_short_state
from fee_aware_grid import process_fee_grid, state as fee_grid_state
from crypto_radar import scan_crypto_radar, build_new_alerts
from candlestick_quality import process_candlestick_v2, state as candlestick_v2_state

FULL_MINUTES = max(15, int(os.environ.get("AUTO_UPDATE_MINUTES", "60")))
LIVE_MINUTES = max(5, int(os.environ.get("LIVE_PAPER_UPDATE_MINUTES", "5")))
SCAN_MARKET = os.environ.get("AUTO_MARKET_SCAN", "1").strip().lower() not in {"0", "false", "no"}
RADAR_ENABLED = os.environ.get("CRYPTO_RADAR_ENABLED", "1").strip().lower() not in {"0", "false", "no"}


def status(**kwargs):
    old = read_json(WORKER_STATUS, {}) or {}
    payload = {**old, **kwargs, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "full_interval_minutes": FULL_MINUTES, "live_paper_interval_minutes": LIVE_MINUTES}
    write_json(WORKER_STATUS, payload)


def full_cycle():
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

        if RADAR_ENABLED:
            radar = scan_crypto_radar()
            messages.append(f"Crypto-Radar: {radar.get('liquid_universe', 0)} liquide Märkte · {radar.get('deep_scanned', 0)} tief geprüft")
            radar_alerts = build_new_alerts(radar)
            sent_radar = 0
            for alert in radar_alerts:
                ok, _ = send_telegram(alert)
                sent_radar += 1 if ok else 0
            if radar_alerts:
                messages.append(f"Radar-Alarme: {sent_radar}/{len(radar_alerts)} gesendet")

        challenger = process_challenge(table, monitor_payload)
        if challenger.get("active"):
            messages.append("Strategy Challenger: " + str(challenger.get("message", "aktualisiert")))
            for alert in challenger.get("events", []) or []:
                send_telegram(alert)

        fg = fee_grid_state()
        if fg and fg.get("active", True):
            fee_res = process_fee_grid()
            messages.append("Fee-Aware Grid: " + str(fee_res.get("message", "aktualisiert")))

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
        status(ok=True, full_ok=True, last_full_at=datetime.now().astimezone().isoformat(timespec="seconds"),
               full_message=" · ".join(messages), message=" · ".join(messages), notifications=note)
    except Exception as exc:
        status(ok=False, full_ok=False, last_full_at=datetime.now().astimezone().isoformat(timespec="seconds"),
               full_message=str(exc), message=f"Full-Cycle Fehler: {exc}")


def live_cycle():
    long_only = live_paper_state()
    long_short = long_short_state()
    candle_v2 = candlestick_v2_state()
    if not (long_only and long_only.get("active", True)) and not (long_short and long_short.get("active", True)) and not (candle_v2 and candle_v2.get("active", False)):
        status(live_paper_active=False, long_short_active=False, candlestick_v2_active=False,
               last_live_at=datetime.now().astimezone().isoformat(timespec="seconds"),
               live_message="Live-Paper-Systeme nicht aktiv")
        return
    try:
        monitor_payload = read_json(MONITOR_LATEST, {}) or {}
        sent = 0
        messages = []
        if long_only and long_only.get("active", True):
            res = process_live_paper(monitor_payload)
            messages.append("Long-only: " + str(res.get("message", "aktualisiert")))
            for event in res.get("events", []) or []:
                txt = str(event.get("telegram", "")).strip()
                if txt:
                    ok, _ = send_telegram(txt)
                    sent += 1 if ok else 0
        if long_short and long_short.get("active", True):
            res_ls = process_long_short_paper(monitor_payload)
            messages.append("Long+Short: " + str(res_ls.get("message", "aktualisiert")))
            for event in res_ls.get("events", []) or []:
                txt = str(event.get("telegram", "")).strip()
                if txt:
                    ok, _ = send_telegram(txt)
                    sent += 1 if ok else 0
        if candle_v2 and candle_v2.get("active", False):
            res_cv2 = process_candlestick_v2()
            messages.append("Candlestick V2: " + str(res_cv2.get("message", "aktualisiert")))
            for event in res_cv2.get("events", []) or []:
                txt = str(event.get("telegram", "")).strip()
                if txt:
                    ok, _ = send_telegram(txt)
                    sent += 1 if ok else 0
        msg = " · ".join(messages)
        status(ok=True, live_paper_active=bool(long_only and long_only.get("active", True)),
               long_short_active=bool(long_short and long_short.get("active", True)),
               candlestick_v2_active=bool(candle_v2 and candle_v2.get("active", False)),
               last_live_at=datetime.now().astimezone().isoformat(timespec="seconds"),
               live_message=msg, live_telegram_sent=sent, message=msg)
    except Exception as exc:
        status(ok=False, live_paper_active=bool(long_only and long_only.get("active", True)),
               long_short_active=bool(long_short and long_short.get("active", True)),
               candlestick_v2_active=bool(candle_v2 and candle_v2.get("active", False)),
               last_live_at=datetime.now().astimezone().isoformat(timespec="seconds"),
               live_message=str(exc), message=f"Live-Paper Fehler: {exc}")


if __name__ == "__main__":
    last_full = 0.0
    last_live = 0.0
    while True:
        now = time.time()
        if now - last_full >= FULL_MINUTES * 60:
            full_cycle()
            last_full = time.time()
        if now - last_live >= LIVE_MINUTES * 60:
            live_cycle()
            last_live = time.time()
        time.sleep(20)
