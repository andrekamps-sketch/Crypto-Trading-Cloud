from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta
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
        "notify_quality_9": _bool_env("NOTIFY_QUALITY_9", True),
        "notify_quality_10": _bool_env("NOTIFY_QUALITY_10", True),
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
    # A Chat-ID discovered/saved in the persistent volume should take precedence.
    # This lets the dashboard repair an accidentally wrong TELEGRAM_CHAT_ID env value.
    try:
        file_value = CHAT_ID_FILE.read_text(encoding="utf-8").strip() if CHAT_ID_FILE.exists() else ""
        if file_value:
            return file_value
    except Exception:
        pass
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def save_chat_id(chat_id: str) -> str:
    chat_id = str(chat_id).strip()
    if not chat_id:
        raise ValueError("Leere Telegram Chat-ID.")
    CHAT_ID_FILE.write_text(chat_id, encoding="utf-8")
    return chat_id


def clear_saved_chat_id() -> None:
    try:
        CHAT_ID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _bot_identity() -> tuple[str, str]:
    data = _telegram_call("getMe")
    user = data.get("result", {}) or {}
    return str(user.get("id", "")), str(user.get("username", ""))


def validate_chat_id(chat_id: str) -> tuple[bool, str]:
    chat_id = str(chat_id).strip()
    if not chat_id:
        return False, "Leere Telegram Chat-ID."
    try:
        bot_id, _ = _bot_identity()
        if bot_id and chat_id == bot_id:
            return False, "Die Chat-ID gehört zum Bot selbst, nicht zu deinem privaten Telegram-Chat."
        data = _telegram_call("getChat", {"chat_id": chat_id})
        chat = data.get("result", {}) or {}
        if str(chat.get("type", "")) != "private":
            return False, f"Gefundener Chat ist vom Typ {chat.get('type','unbekannt')} statt private."
        return True, str(chat.get("first_name") or chat.get("username") or "privater Telegram-Chat")
    except Exception as exc:
        return False, str(exc)


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
    """Find the most recent real user private chat after the user messaged the bot."""
    bot_id, _ = _bot_identity()
    data = _telegram_call("getUpdates")
    updates = sorted(data.get("result", []) or [], key=lambda x: int(x.get("update_id", 0)))
    candidates = []
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or {}
        if not msg:
            continue
        sender = msg.get("from") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        if sender.get("is_bot") is True:
            continue
        if str(chat.get("type", "")) != "private":
            continue
        cid_s = str(cid)
        if bot_id and cid_s == bot_id:
            continue
        label = str(chat.get("first_name") or chat.get("username") or sender.get("first_name") or sender.get("username") or "privater Telegram-Chat")
        candidates.append((int(upd.get("update_id", 0)), cid_s, label))
    if not candidates:
        raise RuntimeError("Keine private Nachricht von dir gefunden. Öffne deinen eigenen Bot in Telegram, drücke Start und sende 'Hallo'. Danach hier erneut erkennen.")
    _, chat_id, label = candidates[-1]
    ok, detail = validate_chat_id(chat_id)
    if not ok:
        raise RuntimeError(f"Gefundene Chat-ID wurde aus Sicherheitsgründen abgelehnt: {detail}")
    save_chat_id(chat_id)
    return chat_id, label


def send_telegram(text: str) -> tuple[bool, str]:
    cfg = settings()
    if not cfg["bot_token_set"]:
        return False, "Telegram Bot-Token fehlt."
    chat_id = cfg["chat_id"]
    if not chat_id:
        return False, "Telegram Chat-ID fehlt."
    valid, detail = validate_chat_id(chat_id)
    if not valid:
        return False, f"Telegram Chat-ID ungültig: {detail}"
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


def _as_float(value: Any) -> float | None:
    try:
        x = float(value)
        return x if pd.notna(x) else None
    except Exception:
        return None


def _money(value: Any, signed: bool = False) -> str:
    x = _as_float(value)
    if x is None:
        return "–"
    return f"{x:+,.2f} €" if signed else f"{x:,.2f} €"


def _event_time_local(value: Any, timezone: str) -> str:
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts.tz_convert(timezone).strftime("%d.%m. %H:%M")
    except Exception:
        return str(value or "")


def _strategy_snapshot(table: pd.DataFrame | None, strategy: str) -> str:
    if table is None or table.empty:
        return ""
    try:
        r = table[table["Strategie"].astype(str) == str(strategy)]
        if r.empty:
            return ""
        row = r.iloc[0]
        value = _as_float(row.get("Kontowert €"))
        ret = _as_float(row.get("Rendite %"))
        if value is None:
            return ""
        return f" · Konto {value:,.2f} €" + (f" ({ret:+.2f}%)" if ret is not None else "")
    except Exception:
        return ""


def _trade_line(e: dict[str, Any], table: pd.DataFrame | None = None, timezone: str = "Europe/Berlin") -> str:
    action = str(e.get("action", "")).upper()
    strategy = str(e.get("strategy", ""))
    system = str(e.get("system", ""))
    asset = str(e.get("asset", ""))
    price = _as_float(e.get("price"))
    fee = _as_float(e.get("fee"))
    pnl = _as_float(e.get("pnl_eur", e.get("realized_pnl")))
    reason = str(e.get("reason", "")).strip()
    when = _event_time_local(e.get("timestamp"), timezone)

    price_s = f"{price:,.4f} €" if price is not None and price < 1000 else (f"{price:,.2f} €" if price is not None else "–")
    fee_s = f" · Gebühr {fee:.2f} €" if fee is not None and fee > 0 else ""
    reason_s = f" · {reason}" if reason else ""
    account_s = _strategy_snapshot(table, strategy)

    if action == "BUY":
        return f"🟢 KAUF · {asset} @ {price_s}\n{system} · {strategy} · {when}{fee_s}{reason_s}{account_s}"
    if action == "SELL":
        if pnl is None:
            result_s = "Ergebnis –"
            icon = "🔴"
        else:
            icon = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
            result_s = f"G/V {pnl:+.2f} €"
        return f"{icon} VERKAUF · {asset} @ {price_s} · {result_s}\n{system} · {strategy} · {when}{fee_s}{reason_s}{account_s}"
    return f"🔔 {system} · {strategy}: {action} {asset} @ {price_s} · {when}{fee_s}{reason_s}{account_s}"


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


def _all_events(v52_payload: dict[str, Any] | None, v6_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for payload in (v52_payload or {}, v6_payload or {}):
        for e in payload.get("trade_events", []) or []:
            if isinstance(e, dict):
                events.append(e)
    events.sort(key=lambda e: str(e.get("timestamp", "")))
    return events


def _events_since(events: list[dict[str, Any]], since: datetime, timezone: str) -> list[dict[str, Any]]:
    out = []
    for e in events:
        try:
            ts = pd.Timestamp(e.get("timestamp"))
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            ts = ts.tz_convert(timezone).to_pydatetime()
            if ts >= since:
                out.append(e)
        except Exception:
            continue
    return out


def _portfolio_accounting_since_start(
    table: pd.DataFrame | None,
    v52_payload: dict[str, Any] | None,
    v6_payload: dict[str, Any] | None,
) -> dict[str, float | int] | None:
    """Aggregate currently valued paper accounts using liquidation-equivalent values.

    Net P/L is current account value minus each strategy's frozen start capital.
    Gross-before-fees is defined as net P/L plus the fees already included in those
    account values. This is deliberately portfolio P/L (realized + unrealized), not
    the sum of winning trades.
    """
    if table is None or table.empty or "Kontowert €" not in table.columns:
        return None

    caps = {
        "V5.2": _as_float((v52_payload or {}).get("start_capital")),
        "V6": _as_float((v6_payload or {}).get("start_capital")),
    }
    x = table.copy()
    x["_value"] = pd.to_numeric(x.get("Kontowert €"), errors="coerce")
    x["_fees"] = pd.to_numeric(x.get("Gebühren €"), errors="coerce").fillna(0.0) if "Gebühren €" in x.columns else 0.0
    x = x[x["_value"].notna()]
    if x.empty:
        return None

    net = 0.0
    fees = 0.0
    counted = 0
    missing_cap = False
    for _, r in x.iterrows():
        system = str(r.get("System", ""))
        cap = caps.get(system)
        if cap is None:
            missing_cap = True
            continue
        net += float(r["_value"]) - float(cap)
        fees += float(r["_fees"] or 0.0)
        counted += 1

    if counted <= 0 or missing_cap:
        return None
    return {
        "accounts": counted,
        "gross_before_fees": net + fees,
        "fees": fees,
        "net_after_fees": net,
    }


def build_daily_summary(
    table: pd.DataFrame | None,
    monitor: dict[str, Any] | None,
    v52_payload: dict[str, Any] | None = None,
    v6_payload: dict[str, Any] | None = None,
    previous_values: dict[str, Any] | None = None,
    since: datetime | None = None,
    timezone: str = "Europe/Berlin",
) -> str:
    try:
        tz = ZoneInfo(str(timezone))
    except Exception:
        tz = ZoneInfo("Europe/Berlin")
    now = datetime.now(tz)
    lines = [f"📊 Trading-Zentrale · Tagesübersicht {now.strftime('%d.%m.%Y')}"]

    previous_values = previous_values or {}
    if table is not None and not table.empty:
        x = table.copy()
        x["_value"] = pd.to_numeric(x.get("Kontowert €"), errors="coerce")
        x = x[x["_value"].notna()].sort_values("_value", ascending=False)
        if not x.empty:
            leader = x.iloc[0]
            lines.append(f"🏆 Vorne: {leader.get('Strategie','–')} · {float(leader['_value']):,.2f} €")
            lines.append("")
            for _, r in x.head(8).iterrows():
                name = str(r.get("Strategie", "–"))
                value = float(r["_value"])
                ret = _as_float(r.get("Rendite %"))
                ret_s = f"{ret:+.2f}%" if ret is not None else "–"
                delta_s = ""
                prev = _as_float(previous_values.get(name))
                if prev is not None:
                    delta = value - prev
                    delta_s = f" · Δ {delta:+.2f} €"
                lines.append(f"• {name}: {value:,.2f} € ({ret_s}){delta_s}")
            total_trades = int(pd.to_numeric(x.get("Trades"), errors="coerce").fillna(0).sum()) if "Trades" in x.columns else 0
            total_fees = float(pd.to_numeric(x.get("Gebühren €"), errors="coerce").fillna(0).sum()) if "Gebühren €" in x.columns else 0.0
            lines.append(f"Trades seit Start: {total_trades} · Gebühren seit Start: {total_fees:.2f} €")

            accounting = _portfolio_accounting_since_start(table, v52_payload, v6_payload)
            if accounting:
                gross = float(accounting["gross_before_fees"])
                fees = float(accounting["fees"])
                net = float(accounting["net_after_fees"])
                lines.append(
                    f"💰 Konten-P/L seit Start: Brutto {gross:+.2f} € · Gebühren -{fees:.2f} € · Netto {net:+.2f} €"
                )

    events = _all_events(v52_payload, v6_payload)
    if since is None:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    recent_events = _events_since(events, since, str(tz))
    sells = [e for e in recent_events if str(e.get("action", "")).upper() == "SELL"]
    realized = sum((_as_float(e.get("pnl_eur", e.get("realized_pnl"))) or 0.0) for e in sells)
    if recent_events:
        lines.append("")
        lines.append(f"🔔 Seit letzter Übersicht: {len(recent_events)} Aktionen · {len(sells)} Verkäufe · realisiert {realized:+.2f} €")
        for e in recent_events[-4:]:
            lines.append("• " + _trade_line(e, None, str(tz)).split("\n", 1)[0])

    if monitor:
        reg = monitor.get("regime") or {}
        rows = monitor.get("rows") or []
        lines.append("")
        if reg:
            lines.append(f"🌍 Markt: {reg.get('emoji','')} {reg.get('label','–')}")
        if rows:
            lines.append("📡 Top-Kandidaten:")
            for r in rows[:3]:
                rules = int(r.get("rules_ok", 0))
                total = int(r.get("rules_total", 10))
                q = "READY" if bool(r.get("quality_ready")) else f"{rules}/{total}"
                lines.append(f"• {r.get('Coin','–')}: {q} · Edge {float(r.get('Edge',0)):.1f} · RSI {float(r.get('RSI',0)):.1f}")
    lines.append("")
    lines.append("ℹ️ Paper-Trading – keine echten Orders.")
    return "\n".join(lines)


def _quality_stage(r: dict[str, Any]) -> int:
    rules = int(r.get("rules_ok", 0) or 0)
    total = int(r.get("rules_total", 10) or 10)
    if bool(r.get("quality_ready")) or rules >= total or rules >= 10:
        return 10
    if rules >= 9:
        return 9
    return 0


def _quality_message(r: dict[str, Any], stage: int) -> str:
    coin = str(r.get("Coin", "–"))
    edge = float(r.get("Edge", 0) or 0)
    rsi = float(r.get("RSI", 0) or 0)
    price = _as_float(r.get("Preis €"))
    price_s = _money(price)
    missing = str(r.get("Fehlt", "")).strip()
    if stage >= 10:
        text = f"🚀 QUALITY-ALARM 10/10 · {coin}\nAlle Quality-Regeln erfüllt · Edge {edge:.1f} · RSI {rsi:.1f} · Preis {price_s}"
    else:
        text = f"🟡 QUALITY-WARNUNG 9/10 · {coin}\nNur noch eine Regel fehlt · Edge {edge:.1f} · RSI {rsi:.1f} · Preis {price_s}"
    if missing:
        text += f"\nFehlt: {missing}"
    text += "\nℹ️ Beobachtungssignal, keine Kaufempfehlung."
    return text


def process_notifications(
    table: pd.DataFrame | None,
    monitor_payload: dict[str, Any] | None,
    v52_payload: dict[str, Any] | None,
    v6_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare current data with the persistent Telegram baseline.

    V6.5 adds richer trade messages, separate 9/10 and 10/10 quality stages and a
    daily summary with change since the previous summary. Existing /data state is
    migrated without changing the frozen paper tests.
    """
    cfg = settings()
    if not cfg["bot_token_set"] or not cfg["chat_id"]:
        return {"configured": False, "sent": 0, "message": "Telegram nicht vollständig eingerichtet"}

    state = _read_json(NOTIFY_STATE, {}) or {}
    first_run = not bool(state.get("initialized"))
    sent = 0
    errors: list[str] = []

    # --- Trade events -------------------------------------------------------
    events = _all_events(v52_payload, v6_payload)
    event_keys = [_event_key(e) for e in events]
    prev_keys = set(state.get("seen_trade_events", []) or [])
    if cfg["notify_trades"] and not first_run:
        new_events = [e for e in events if _event_key(e) not in prev_keys]
        if new_events:
            # One compact Telegram message per worker cycle, but every action is shown.
            sell_pnl = sum((_as_float(e.get("pnl_eur", e.get("realized_pnl"))) or 0.0) for e in new_events if str(e.get("action", "")).upper() == "SELL")
            title = "🔔 Neue Paper-Trade-Aktion" + ("en" if len(new_events) != 1 else "")
            text = title + f" · realisiert {sell_pnl:+.2f} €\n\n" + "\n\n".join(_trade_line(e, table, str(cfg["timezone"])) for e in new_events[-12:])
            ok, msg = send_telegram(text)
            sent += int(ok)
            if not ok:
                errors.append(msg)
    state["seen_trade_events"] = event_keys[-1000:]

    # --- Leader change ------------------------------------------------------
    leader = _leader_from_table(table)
    current_leader = leader[0] if leader else ""
    previous_leader = str(state.get("leader", ""))
    if cfg["notify_leader"] and not first_run and current_leader and previous_leader and current_leader != previous_leader:
        ok, msg = send_telegram(f"🏆 Neuer Führender im Paper-Wettkampf: {current_leader}\nKontowert {leader[1]:,.2f} €")
        sent += int(ok)
        if not ok:
            errors.append(msg)
    state["leader"] = current_leader

    # --- Market candidates: separate 9/10 and 10/10 stages -----------------
    rows = (monitor_payload or {}).get("rows", []) or []
    old_stages_raw = state.get("quality_stages")
    quality_initialized = isinstance(old_stages_raw, dict)
    previous_stages = {str(k): int(v or 0) for k, v in (old_stages_raw or {}).items()}
    current_stages: dict[str, int] = {}
    for r in rows:
        coin = str(r.get("Coin", ""))
        if not coin:
            continue
        stage = _quality_stage(r)
        current_stages[coin] = stage
        old_stage = previous_stages.get(coin, 0)
        if not cfg["notify_market"] or first_run or not quality_initialized:
            continue
        # A fall back to 9 re-arms a later 10/10 alert; below 9 re-arms 9/10 too.
        if stage > old_stage:
            should_send = (stage == 9 and cfg["notify_quality_9"]) or (stage >= 10 and cfg["notify_quality_10"])
            if should_send:
                ok, msg = send_telegram(_quality_message(r, stage))
                sent += int(ok)
                if not ok:
                    errors.append(msg)
    state["quality_stages"] = current_stages
    # Keep the legacy field for backward compatibility with V6.4.
    state["active_market_candidates"] = sorted([coin for coin, stage in current_stages.items() if stage >= int(cfg["market_min_rules"])])

    # --- Daily summary ------------------------------------------------------
    try:
        tz = ZoneInfo(str(cfg["timezone"]))
    except Exception:
        tz = ZoneInfo("Europe/Berlin")
    local_now = datetime.now(tz)
    today = local_now.date().isoformat()
    last_summary_date = str(state.get("last_daily_summary_date", ""))
    if cfg["notify_daily"] and local_now.hour >= int(cfg["daily_hour"]) and last_summary_date != today:
        previous_values = state.get("last_daily_values", {}) or {}
        since_raw = state.get("last_daily_summary_at")
        since = None
        if since_raw:
            try:
                since = pd.Timestamp(since_raw).tz_convert(str(tz)).to_pydatetime()
            except Exception:
                since = None
        text = build_daily_summary(table, monitor_payload, v52_payload, v6_payload, previous_values, since, str(tz))
        ok, msg = send_telegram(text)
        sent += int(ok)
        if ok:
            state["last_daily_summary_date"] = today
            state["last_daily_summary_at"] = local_now.isoformat(timespec="seconds")
            vals: dict[str, float] = {}
            if table is not None and not table.empty and "Strategie" in table.columns:
                for _, r in table.iterrows():
                    v = _as_float(r.get("Kontowert €"))
                    if v is not None:
                        vals[str(r.get("Strategie", "–"))] = v
            state["last_daily_values"] = vals
        else:
            errors.append(msg)

    state["initialized"] = True
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(NOTIFY_STATE, state)
    return {
        "configured": True,
        "sent": sent,
        "first_run_baseline": first_run,
        "quality_baseline_added": not quality_initialized,
        "message": "Baseline gesetzt" if first_run else ("Benachrichtigungen geprüft" if not errors else " · ".join(errors[:2])),
    }
