from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
NOTIFY_STATE = DATA_DIR / "notification_state.json"
NOTIFY_LOG = DATA_DIR / "notification_log.csv"
CHAT_ID_FILE = DATA_DIR / "telegram_chat_id.txt"


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def settings() -> dict[str, Any]:
    return {
        "bot_token_set": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
        "chat_id": get_chat_id(),
        "notify_trades": _bool_env("NOTIFY_TRADES", True),
        "notify_market": _bool_env("NOTIFY_MARKET_CANDIDATES", True),
        "market_min_rules": max(1, min(10, int(os.environ.get("NOTIFY_MARKET_MIN_RULES", "9")))),
        "notify_leader": _bool_env("NOTIFY_LEADER_CHANGE", True),
        "notify_daily": _bool_env("NOTIFY_DAILY_SUMMARY", True),
        "daily_hour": max(0, min(23, int(os.environ.get("DAILY_SUMMARY_HOUR", "20")))),
        "timezone": os.environ.get("NOTIFY_TIMEZONE", "Europe/Berlin"),
    }


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def get_chat_id() -> str:
    env = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env:
        return env
    try:
        return CHAT_ID_FILE.read_text(encoding="utf-8").strip() if CHAT_ID_FILE.exists() else ""
    except Exception:
        return ""


def save_chat_id(chat_id: str) -> str:
    chat_id = str(chat_id).strip()
    if not chat_id:
        raise ValueError("Leere Telegram Chat-ID.")
    CHAT_ID_FILE.write_text(chat_id, encoding="utf-8")
    return chat_id


def _telegram_call(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ist nicht gesetzt.")
    url = f"https://api.telegram.org/bot{token}/{method}"
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"Telegram HTTP {exc.code}: {detail[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"Telegram-Verbindung fehlgeschlagen: {exc}") from exc
    if not data.get("ok"):
        raise RuntimeError("Telegram API: " + str(data.get("description", "unbekannter Fehler")))
    return data


def discover_chat_id() -> tuple[str, str]:
    """Find the most recent private chat after the user has messaged the bot."""
    data = _telegram_call("getUpdates")
    updates = data.get("result", [])
    candidates = []
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        candidates.append((str(cid), str(chat.get("first_name") or chat.get("title") or chat.get("username") or "Telegram-Chat")))
    if not candidates:
        raise RuntimeError("Noch keine Nachricht an den Bot gefunden. Öffne den Bot in Telegram, drücke Start und sende z. B. 'Hallo'.")
    chat_id, label = candidates[-1]
    save_chat_id(chat_id)
    return chat_id, label


def send_telegram(text: str) -> tuple[bool, str]:
    cfg = settings()
    if not cfg["bot_token_set"]:
        return False, "Telegram Bot-Token fehlt."
    chat_id = cfg["chat_id"]
    if not chat_id:
        return False, "Telegram Chat-ID fehlt."
    text = str(text).strip()
    if not text:
        return False, "Leere Nachricht."
    # Telegram supports up to 4096 chars per message. Keep some safety margin.
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    try:
        for chunk in chunks:
            _telegram_call("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})
        _append_log("sent", text)
        return True, "Telegram-Nachricht gesendet."
    except Exception as exc:
        _append_log("error", f"{exc} | {text}")
        return False, str(exc)


def _append_log(status: str, message: str) -> None:
    row = pd.DataFrame([{
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "message": str(message),
    }])
    if NOTIFY_LOG.exists():
        try:
            old = pd.read_csv(NOTIFY_LOG)
            row = pd.concat([old, row], ignore_index=True)
        except Exception:
            pass
    if len(row) > 2000:
        row = row.tail(2000)
    row.to_csv(NOTIFY_LOG, index=False)


def notification_log(limit: int = 100) -> pd.DataFrame:
    if not NOTIFY_LOG.exists():
        return pd.DataFrame(columns=["timestamp", "status", "message"])
    try:
        return pd.read_csv(NOTIFY_LOG).tail(limit)
    except Exception:
        return pd.DataFrame(columns=["timestamp", "status", "message"])


def _event_key(event: dict[str, Any]) -> str:
    return "|".join(str(event.get(k, "")) for k in ["system", "strategy", "timestamp", "asset", "action", "price", "reason"])


def _trade_line(e: dict[str, Any]) -> str:
    action = str(e.get("action", "")).upper()
    icon = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "🔔"
    system = e.get("system", "")
    strategy = e.get("strategy", "")
    asset = e.get("asset", "")
    try:
        price = float(e.get("price"))
        price_s = f"{price:,.4f} €" if price < 1000 else f"{price:,.2f} €"
    except Exception:
        price_s = str(e.get("price", "–"))
    pnl = e.get("pnl_eur", e.get("realized_pnl"))
    pnl_s = ""
    try:
        if action == "SELL" and pnl is not None and pd.notna(float(pnl)):
            pnl_s = f" · G/V {float(pnl):+.2f} €"
    except Exception:
        pass
    reason = str(e.get("reason", "")).strip()
    reason_s = f" · {reason}" if reason else ""
    return f"{icon} {system} {strategy}: {action} {asset} @ {price_s}{pnl_s}{reason_s}"


def _leader_from_table(table: pd.DataFrame | None) -> tuple[str, float] | None:
    if table is None or table.empty or "Kontowert €" not in table.columns:
        return None
    x = table.copy()
    x["_value"] = pd.to_numeric(x["Kontowert €"], errors="coerce")
    x = x[x["_value"].notna()]
    if x.empty:
        return None
    r = x.sort_values("_value", ascending=False).iloc[0]
    return str(r.get("Strategie", "–")), float(r["_value"])


def _daily_summary(table: pd.DataFrame | None, monitor: dict[str, Any] | None) -> str:
    lines = ["📊 Trading-Zentrale – Tagesübersicht"]
    if table is not None and not table.empty:
        x = table.copy()
        x["_value"] = pd.to_numeric(x.get("Kontowert €"), errors="coerce")
        x = x[x["_value"].notna()].sort_values("_value", ascending=False)
        for _, r in x.head(6).iterrows():
            ret = pd.to_numeric(pd.Series([r.get("Rendite %")]), errors="coerce").iloc[0]
            ret_s = f"{ret:+.2f}%" if pd.notna(ret) else "–"
            lines.append(f"• {r.get('Strategie','–')}: {float(r['_value']):,.2f} € ({ret_s})")
    if monitor:
        reg = monitor.get("regime") or {}
        rows = monitor.get("rows") or []
        if reg:
            lines.append(f"Markt: {reg.get('emoji','')} {reg.get('label','–')}")
        if rows:
            top = rows[0]
            lines.append(f"Top-Kandidat: {top.get('Coin','–')} · {top.get('Quality','–')} Regeln · Edge {float(top.get('Edge',0)):.1f}")
    return "\n".join(lines)


def process_notifications(
    table: pd.DataFrame | None,
    monitor_payload: dict[str, Any] | None,
    v52_payload: dict[str, Any] | None,
    v6_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare the current state against the persistent notification baseline.

    On the very first call, old trades/candidates are baselined instead of sent so an upgrade
    does not flood the user with historical messages.
    """
    cfg = settings()
    if not cfg["bot_token_set"] or not cfg["chat_id"]:
        return {"configured": False, "sent": 0, "message": "Telegram nicht vollständig eingerichtet"}

    state = _read_json(NOTIFY_STATE, {}) or {}
    first_run = not bool(state.get("initialized"))
    sent = 0
    errors = []

    # --- Trade events -------------------------------------------------------
    events: list[dict[str, Any]] = []
    for payload in (v52_payload or {}, v6_payload or {}):
        for e in payload.get("trade_events", []) or []:
            if isinstance(e, dict):
                events.append(e)
    event_keys = [_event_key(e) for e in events]
    prev_keys = set(state.get("seen_trade_events", []) or [])
    if cfg["notify_trades"] and not first_run:
        new_events = [e for e in events if _event_key(e) not in prev_keys]
        if new_events:
            # Keep alert size sensible if several hourly grid actions happened at once.
            text = "🔔 Neue Paper-Trade-Aktion" + ("en" if len(new_events) != 1 else "") + ":\n" + "\n".join(_trade_line(e) for e in new_events[-12:])
            ok, msg = send_telegram(text)
            sent += int(ok)
            if not ok:
                errors.append(msg)
    # Remember all currently known events, bounded.
    state["seen_trade_events"] = event_keys[-1000:]

    # --- Leader change ------------------------------------------------------
    leader = _leader_from_table(table)
    current_leader = leader[0] if leader else ""
    previous_leader = str(state.get("leader", ""))
    if cfg["notify_leader"] and not first_run and current_leader and previous_leader and current_leader != previous_leader:
        ok, msg = send_telegram(f"🏆 Neuer Führender im Paper-Wettkampf: {current_leader} · Kontowert {leader[1]:,.2f} €")
        sent += int(ok)
        if not ok:
            errors.append(msg)
    state["leader"] = current_leader

    # --- Market candidates -------------------------------------------------
    rows = (monitor_payload or {}).get("rows", []) or []
    threshold = int(cfg["market_min_rules"])
    active = {str(r.get("Coin")) for r in rows if bool(r.get("quality_ready")) or int(r.get("rules_ok", 0)) >= threshold}
    previous_active = set(state.get("active_market_candidates", []) or [])
    if cfg["notify_market"] and not first_run:
        newly_active = active - previous_active
        for coin in sorted(newly_active):
            r = next((z for z in rows if str(z.get("Coin")) == coin), {})
            quality = r.get("Quality", f"{r.get('rules_ok',0)}/{r.get('rules_total',10)}")
            text = f"📡 Markt-Signal: {coin} erreicht {quality} Regeln · Quality-Edge {float(r.get('Edge',0)):.1f} · RSI {float(r.get('RSI',0)):.1f}."
            if r.get("Fehlt"):
                text += f"\nFehlt noch: {r.get('Fehlt')}"
            ok, msg = send_telegram(text)
            sent += int(ok)
            if not ok:
                errors.append(msg)
    state["active_market_candidates"] = sorted(active)

    # --- Daily summary ------------------------------------------------------
    try:
        tz = ZoneInfo(str(cfg["timezone"]))
    except Exception:
        tz = ZoneInfo("Europe/Berlin")
    local_now = datetime.now(tz)
    today = local_now.date().isoformat()
    last_summary = str(state.get("last_daily_summary_date", ""))
    if cfg["notify_daily"] and local_now.hour >= int(cfg["daily_hour"]) and last_summary != today:
        ok, msg = send_telegram(_daily_summary(table, monitor_payload))
        sent += int(ok)
        if ok:
            state["last_daily_summary_date"] = today
        else:
            errors.append(msg)

    state["initialized"] = True
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(NOTIFY_STATE, state)
    return {
        "configured": True,
        "sent": sent,
        "first_run_baseline": first_run,
        "message": "Baseline gesetzt" if first_run else ("Benachrichtigungen geprüft" if not errors else " · ".join(errors[:2])),
    }
