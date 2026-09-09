from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHALLENGER_STATE = DATA_DIR / "strategy_challenger.json"
CHALLENGER_HISTORY = DATA_DIR / "strategy_challenger_history.csv"

CANDIDATES: dict[str, dict[str, Any]] = {
    "Quality 10/10 Strict": {
        "min_rules": 10,
        "min_edge": 85.0,
        "confirm_scans": 1,
        "allocation_pct": 20.0,
        "stop_loss_pct": 4.0,
        "take_profit_pct": 8.0,
        "trailing_activation_pct": 5.0,
        "trailing_stop_pct": 3.0,
        "signal_exit_below_rules": 8,
        "signal_exit_min_hold_hours": 6,
        "max_holding_hours": 120,
        "description": "Nur vollständige 10/10-Quality-Setups; ein Coin zur Zeit.",
    },
    "Quality 9/10 Confirmed": {
        "min_rules": 9,
        "min_edge": 90.0,
        "confirm_scans": 2,
        "allocation_pct": 20.0,
        "stop_loss_pct": 4.0,
        "take_profit_pct": 8.0,
        "trailing_activation_pct": 5.0,
        "trailing_stop_pct": 3.0,
        "signal_exit_below_rules": 8,
        "signal_exit_min_hold_hours": 6,
        "max_holding_hours": 120,
        "description": "Mindestens 9/10 + Edge ≥90 in zwei aufeinanderfolgenden Cloud-Scans.",
    },
}

PROMOTION_RULES = {
    "min_days": 30.0,
    "min_completed_trades": 8,
    "min_outperformance_pp": 2.0,
    "min_profit_factor": 1.20,
    "max_drawdown_pct": 10.0,
    "qualification_hold_hours": 72.0,
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


def _ts(value: Any) -> pd.Timestamp:
    t = pd.Timestamp(value if value is not None else datetime.now().astimezone())
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        x = float(value)
        return x if pd.notna(x) else default
    except Exception:
        return default


def _strategy_row(table: pd.DataFrame | None, strategy: str) -> dict[str, Any] | None:
    if table is None or table.empty or "Strategie" not in table.columns:
        return None
    rows = table[table["Strategie"].astype(str) == str(strategy)]
    if rows.empty:
        return None
    return rows.iloc[0].to_dict()


def _new_shadow(name: str, capital: float) -> dict[str, Any]:
    return {
        "name": name,
        "cash": float(capital),
        "position": None,
        "fees_paid": 0.0,
        "realized_pnl": 0.0,
        "trades": [],
        "equity_history": [],
        "last_candidate": "",
        "confirmation_count": 0,
        "qualification_since": None,
        "promotion_ready": False,
        "last_status": "BEOBACHTEN",
    }


def start_challenge(
    table: pd.DataFrame,
    champion: str = "Quality Breakout",
    assets: list[str] | None = None,
    start_capital: float = 1000.0,
    fee_pct: float = 0.25,
) -> dict[str, Any]:
    row = _strategy_row(table, champion)
    if not row:
        raise ValueError(f"Champion '{champion}' ist in der aktuellen Paper-Tabelle nicht verfügbar.")
    baseline = _float(row.get("Kontowert €"))
    if baseline is None or baseline <= 0:
        raise ValueError("Der Champion hat noch keinen auswertbaren Kontowert.")
    now = pd.Timestamp.now(tz="UTC")
    allowed = [str(a).upper() for a in (assets or ["BTC", "ETH", "SOL", "LINK", "AVAX"])]
    allowed = list(dict.fromkeys(allowed))
    state = {
        "version": "6.7",
        "active": True,
        "started_at": now.isoformat(),
        "start_capital": float(start_capital),
        "fee_pct": float(fee_pct),
        "assets": allowed,
        "champion": champion,
        "champion_baseline_value": float(baseline),
        "champion_baseline_trades": int(_float(row.get("Trades"), 0) or 0),
        "champion_baseline_fees": float(_float(row.get("Gebühren €"), 0.0) or 0.0),
        "champion_history": [],
        "candidate_rules": CANDIDATES,
        "promotion_rules": PROMOTION_RULES,
        "challengers": {name: _new_shadow(name, start_capital) for name in CANDIDATES},
        "last_processed_scan_at": None,
        "last_updated_at": now.isoformat(),
        "rules_locked": True,
    }
    _write_json(CHALLENGER_STATE, state)
    return state


def challenge_state() -> dict[str, Any] | None:
    return _read_json(CHALLENGER_STATE, None)


def _row_stage(row: dict[str, Any]) -> int:
    rules = int(row.get("rules_ok", 0) or 0)
    total = int(row.get("rules_total", 10) or 10)
    if bool(row.get("quality_ready")) or rules >= total:
        return 10
    return rules


def _liquidation_equity(shadow: dict[str, Any], row_map: dict[str, dict[str, Any]], fee_rate: float) -> float:
    cash = float(shadow.get("cash", 0.0))
    pos = shadow.get("position") or None
    if not pos:
        return cash
    asset = str(pos.get("asset", ""))
    row = row_map.get(asset)
    if not row:
        return cash + float(pos.get("last_value", 0.0))
    price = float(row.get("Preis €", 0.0) or 0.0)
    gross = float(pos.get("qty", 0.0)) * price
    pos["last_value"] = gross * (1.0 - fee_rate)
    return cash + gross * (1.0 - fee_rate)


def _buy(shadow: dict[str, Any], row: dict[str, Any], scan_time: pd.Timestamp, fee_rate: float, rules: dict[str, Any], reason: str) -> None:
    if shadow.get("position"):
        return
    price = float(row.get("Preis €", 0.0) or 0.0)
    if price <= 0:
        return
    equity = float(shadow.get("cash", 0.0))
    gross = min(equity * float(rules["allocation_pct"]) / 100.0, float(shadow.get("cash", 0.0)) / (1.0 + fee_rate))
    if gross < 5.0:
        return
    fee = gross * fee_rate
    qty = gross / price
    shadow["cash"] = float(shadow.get("cash", 0.0)) - gross - fee
    shadow["fees_paid"] = float(shadow.get("fees_paid", 0.0)) + fee
    shadow["position"] = {
        "asset": str(row.get("Coin", "")),
        "qty": qty,
        "entry_price": price,
        "entry_cost": gross + fee,
        "opened_at": scan_time.isoformat(),
        "high_price": price,
        "last_value": gross,
        "entry_edge": _float(row.get("Edge"), 0.0),
        "entry_rules": _row_stage(row),
    }
    shadow.setdefault("trades", []).append({
        "timestamp": scan_time.isoformat(), "asset": str(row.get("Coin", "")), "action": "BUY",
        "price": price, "fee": fee, "pnl_eur": 0.0, "reason": reason,
    })


def _sell(shadow: dict[str, Any], row: dict[str, Any], scan_time: pd.Timestamp, fee_rate: float, reason: str) -> None:
    pos = shadow.get("position") or None
    if not pos:
        return
    price = float(row.get("Preis €", 0.0) or 0.0)
    if price <= 0:
        return
    gross = float(pos.get("qty", 0.0)) * price
    fee = gross * fee_rate
    proceeds = gross - fee
    pnl = proceeds - float(pos.get("entry_cost", 0.0))
    shadow["cash"] = float(shadow.get("cash", 0.0)) + proceeds
    shadow["fees_paid"] = float(shadow.get("fees_paid", 0.0)) + fee
    shadow["realized_pnl"] = float(shadow.get("realized_pnl", 0.0)) + pnl
    shadow.setdefault("trades", []).append({
        "timestamp": scan_time.isoformat(), "asset": str(pos.get("asset", "")), "action": "SELL",
        "price": price, "fee": fee, "pnl_eur": pnl, "reason": reason,
    })
    shadow["position"] = None
    shadow["last_candidate"] = ""
    shadow["confirmation_count"] = 0


def _manage_open_position(shadow: dict[str, Any], row_map: dict[str, dict[str, Any]], regime_code: str, scan_time: pd.Timestamp, fee_rate: float, rules: dict[str, Any]) -> None:
    pos = shadow.get("position") or None
    if not pos:
        return
    asset = str(pos.get("asset", ""))
    row = row_map.get(asset)
    if not row:
        return
    price = float(row.get("Preis €", 0.0) or 0.0)
    entry = float(pos.get("entry_price", 0.0) or 0.0)
    if price <= 0 or entry <= 0:
        return
    pos["high_price"] = max(float(pos.get("high_price", entry)), price)
    hold_h = max(0.0, (scan_time - _ts(pos.get("opened_at"))).total_seconds() / 3600.0)
    pnl_pct = (price / entry - 1.0) * 100.0
    high_pct = (float(pos["high_price"]) / entry - 1.0) * 100.0
    trailing_level = float(pos["high_price"]) * (1.0 - float(rules["trailing_stop_pct"]) / 100.0)

    reason = None
    if regime_code == "BEAR":
        reason = "Marktphase BEAR"
    elif pnl_pct <= -float(rules["stop_loss_pct"]):
        reason = f"Stop-Loss {pnl_pct:.2f}%"
    elif pnl_pct >= float(rules["take_profit_pct"]):
        reason = f"Take-Profit {pnl_pct:.2f}%"
    elif high_pct >= float(rules["trailing_activation_pct"]) and price <= trailing_level:
        reason = f"Trailing-Stop nach Hoch {high_pct:.2f}%"
    elif hold_h >= float(rules["max_holding_hours"]):
        reason = f"Max. Haltedauer {hold_h:.0f}h"
    elif hold_h >= float(rules["signal_exit_min_hold_hours"]) and _row_stage(row) < int(rules["signal_exit_below_rules"]):
        reason = f"Quality auf {_row_stage(row)}/10 gefallen"
    if reason:
        _sell(shadow, row, scan_time, fee_rate, reason)


def _candidate_row(rows: list[dict[str, Any]], assets: list[str], rules: dict[str, Any], regime_code: str) -> dict[str, Any] | None:
    if regime_code == "BEAR":
        return None
    allowed = set(assets)
    eligible = []
    for row in rows:
        if str(row.get("Coin", "")) not in allowed:
            continue
        if str(row.get("Risiko", "")) == "hoch":
            continue
        if _row_stage(row) < int(rules["min_rules"]):
            continue
        if float(row.get("Edge", 0.0) or 0.0) < float(rules["min_edge"]):
            continue
        eligible.append(row)
    if not eligible:
        return None
    return max(eligible, key=lambda r: (float(r.get("Edge", 0.0) or 0.0), float(r.get("Score", 0.0) or 0.0)))


def _process_shadow(shadow: dict[str, Any], rules: dict[str, Any], rows: list[dict[str, Any]], row_map: dict[str, dict[str, Any]], assets: list[str], regime_code: str, scan_time: pd.Timestamp, fee_rate: float) -> None:
    had_position = bool(shadow.get("position"))
    _manage_open_position(shadow, row_map, regime_code, scan_time, fee_rate, rules)
    exited_this_scan = had_position and not bool(shadow.get("position"))
    # Nach einem Exit nicht im selben Stunden-Scan wieder einsteigen.
    if not shadow.get("position") and not exited_this_scan:
        cand = _candidate_row(rows, assets, rules, regime_code)
        if cand is None:
            shadow["last_candidate"] = ""
            shadow["confirmation_count"] = 0
        else:
            coin = str(cand.get("Coin", ""))
            if coin == str(shadow.get("last_candidate", "")):
                shadow["confirmation_count"] = int(shadow.get("confirmation_count", 0) or 0) + 1
            else:
                shadow["last_candidate"] = coin
                shadow["confirmation_count"] = 1
            if int(shadow["confirmation_count"]) >= int(rules["confirm_scans"]):
                _buy(
                    shadow, cand, scan_time, fee_rate, rules,
                    f"Shadow Entry: {_row_stage(cand)}/10 · Edge {float(cand.get('Edge',0)):.1f} · Bestätigung {shadow['confirmation_count']}/{rules['confirm_scans']}",
                )
    equity = _liquidation_equity(shadow, row_map, fee_rate)
    hist = shadow.setdefault("equity_history", [])
    hist.append({"timestamp": scan_time.isoformat(), "equity": round(float(equity), 6)})
    if len(hist) > 5000:
        del hist[:-5000]


def _metrics_shadow(shadow: dict[str, Any], initial: float) -> dict[str, Any]:
    hist = shadow.get("equity_history", []) or []
    eq = pd.Series([_float(x.get("equity"), initial) for x in hist], dtype=float) if hist else pd.Series([initial], dtype=float)
    end = float(eq.iloc[-1])
    peak = eq.cummax()
    dd = ((eq / peak) - 1.0) * 100.0
    sells = [float(t.get("pnl_eur", 0.0) or 0.0) for t in (shadow.get("trades", []) or []) if str(t.get("action", "")).upper() == "SELL"]
    gains = sum(x for x in sells if x > 0)
    losses = -sum(x for x in sells if x < 0)
    pf = (gains / losses) if losses > 1e-12 else (math.inf if gains > 0 else 0.0)
    return {
        "value": end,
        "return_pct": (end / initial - 1.0) * 100.0,
        "drawdown_pct": abs(float(dd.min())) if not dd.empty else 0.0,
        "profit_factor": pf,
        "completed_trades": len(sells),
        "fees": float(shadow.get("fees_paid", 0.0) or 0.0),
        "realized_pnl": float(shadow.get("realized_pnl", 0.0) or 0.0),
        "position": str((shadow.get("position") or {}).get("asset") or "Cash"),
    }


def _champion_metrics(state: dict[str, Any], table: pd.DataFrame, scan_time: pd.Timestamp) -> dict[str, Any]:
    row = _strategy_row(table, str(state.get("champion", "")))
    initial = float(state.get("start_capital", 1000.0))
    if not row:
        return {"value": initial, "return_pct": 0.0, "drawdown_pct": 0.0, "completed_trades": 0, "fees": 0.0, "pf": None, "available": False}
    current = _float(row.get("Kontowert €"))
    baseline = float(state.get("champion_baseline_value", 0.0) or 0.0)
    if current is None or baseline <= 0:
        return {"value": initial, "return_pct": 0.0, "drawdown_pct": 0.0, "completed_trades": 0, "fees": 0.0, "pf": None, "available": False}
    normalized = initial * current / baseline
    history = state.setdefault("champion_history", [])
    history.append({"timestamp": scan_time.isoformat(), "equity": round(normalized, 6)})
    if len(history) > 5000:
        del history[:-5000]
    eq = pd.Series([float(x.get("equity", initial)) for x in history], dtype=float)
    dd = abs(float(((eq / eq.cummax()) - 1.0).min() * 100.0)) if not eq.empty else 0.0
    trades_now = int(_float(row.get("Trades"), 0) or 0)
    fees_now = float(_float(row.get("Gebühren €"), 0.0) or 0.0)
    return {
        "value": normalized,
        "return_pct": (normalized / initial - 1.0) * 100.0,
        "drawdown_pct": dd,
        "completed_trades": max(0, trades_now - int(state.get("champion_baseline_trades", 0) or 0)),
        "fees": max(0.0, fees_now - float(state.get("champion_baseline_fees", 0.0) or 0.0)),
        "pf": row.get("PF"),
        "available": True,
    }


def _qualification(state: dict[str, Any], shadow: dict[str, Any], metrics: dict[str, Any], champion: dict[str, Any], scan_time: pd.Timestamp) -> tuple[str, list[str], bool]:
    rules = state.get("promotion_rules") or PROMOTION_RULES
    elapsed_days = max(0.0, (scan_time - _ts(state.get("started_at"))).total_seconds() / 86400.0)
    pf = metrics.get("profit_factor")
    pf_num = float(pf) if pf != math.inf else 999.0
    checks = [
        (elapsed_days >= float(rules["min_days"]), f"Laufzeit {elapsed_days:.1f}/{rules['min_days']:.0f} Tage"),
        (int(metrics["completed_trades"]) >= int(rules["min_completed_trades"]), f"Trades {metrics['completed_trades']}/{rules['min_completed_trades']}"),
        (float(metrics["return_pct"]) > 0.0, f"Netto-Rendite {metrics['return_pct']:+.2f}% > 0%"),
        (float(metrics["return_pct"]) >= float(champion.get("return_pct", 0.0)) + float(rules["min_outperformance_pp"]), f"Vorsprung ggü. Champion ≥ {rules['min_outperformance_pp']:.1f} pp"),
        (pf_num >= float(rules["min_profit_factor"]), f"PF {('∞' if pf == math.inf else f'{pf_num:.2f}')} ≥ {rules['min_profit_factor']:.2f}"),
        (float(metrics["drawdown_pct"]) <= float(rules["max_drawdown_pct"]), f"Drawdown {metrics['drawdown_pct']:.2f}% ≤ {rules['max_drawdown_pct']:.1f}%"),
    ]
    base_ok = all(ok for ok, _ in checks)
    if base_ok:
        if not shadow.get("qualification_since"):
            shadow["qualification_since"] = scan_time.isoformat()
        held_h = max(0.0, (scan_time - _ts(shadow.get("qualification_since"))).total_seconds() / 3600.0)
    else:
        shadow["qualification_since"] = None
        held_h = 0.0
    ready = base_ok and held_h >= float(rules["qualification_hold_hours"])
    checks.append((ready, f"Qualifikation {held_h:.0f}/{rules['qualification_hold_hours']:.0f}h stabil"))
    status = "🏆 PROMOTION EMPFOHLEN" if ready else ("🟢 QUALIFIZIERT – HALTEZEIT" if base_ok else "🟡 BEOBACHTEN")
    return status, [f"{'✅' if ok else '⬜'} {label}" for ok, label in checks], ready


def _append_history(state: dict[str, Any], scan_time: pd.Timestamp, champion: dict[str, Any], challenger_metrics: dict[str, dict[str, Any]]) -> None:
    rows = [{
        "timestamp": scan_time.isoformat(), "Typ": "Champion", "Strategie": state.get("champion"),
        "Kontowert €": champion.get("value"), "Rendite %": champion.get("return_pct"), "Drawdown %": champion.get("drawdown_pct"),
        "Trades": champion.get("completed_trades"), "Gebühren €": champion.get("fees"),
    }]
    for name, m in challenger_metrics.items():
        rows.append({
            "timestamp": scan_time.isoformat(), "Typ": "Challenger", "Strategie": name,
            "Kontowert €": m.get("value"), "Rendite %": m.get("return_pct"), "Drawdown %": m.get("drawdown_pct"),
            "Trades": m.get("completed_trades"), "Gebühren €": m.get("fees"),
        })
    df = pd.DataFrame(rows)
    if CHALLENGER_HISTORY.exists():
        try:
            old = pd.read_csv(CHALLENGER_HISTORY)
            df = pd.concat([old, df], ignore_index=True)
        except Exception:
            pass
    if len(df) > 20000:
        df = df.tail(20000)
    df.to_csv(CHALLENGER_HISTORY, index=False)


def process_challenge(table: pd.DataFrame | None, monitor_payload: dict[str, Any] | None) -> dict[str, Any]:
    state = challenge_state()
    if not state or not state.get("active"):
        return {"active": False, "message": "Strategy Challenger noch nicht gestartet", "events": []}
    payload = monitor_payload or {}
    rows = payload.get("rows", []) or []
    if table is None or table.empty or not rows:
        return {"active": True, "message": "Warte auf Paper-Tabelle und Markt-Scan", "events": []}
    scan_time = _ts(payload.get("evaluated_at") or datetime.now().astimezone())
    if scan_time <= _ts(state.get("started_at")):
        return {"active": True, "message": "Warte auf ersten neuen Scan nach Challenge-Start", "events": []}
    last = state.get("last_processed_scan_at")
    if last and scan_time <= _ts(last):
        return {"active": True, "message": "Dieser Markt-Scan wurde bereits verarbeitet", "events": [], "state": state}

    fee_rate = float(state.get("fee_pct", 0.25)) / 100.0
    regime_code = str((payload.get("regime") or {}).get("code") or "NEUTRAL")
    row_map = {str(r.get("Coin", "")): r for r in rows if r.get("Coin")}
    assets = [str(a) for a in state.get("assets", [])]
    for name, rules in (state.get("candidate_rules") or CANDIDATES).items():
        shadow = state["challengers"][name]
        _process_shadow(shadow, rules, rows, row_map, assets, regime_code, scan_time, fee_rate)

    champion = _champion_metrics(state, table, scan_time)
    metrics: dict[str, dict[str, Any]] = {}
    events: list[str] = []
    for name, shadow in state["challengers"].items():
        m = _metrics_shadow(shadow, float(state.get("start_capital", 1000.0)))
        status, checks, ready = _qualification(state, shadow, m, champion, scan_time)
        previous_status = str(shadow.get("last_status", ""))
        shadow["last_status"] = status
        shadow["promotion_ready"] = bool(ready)
        m["status"] = status
        m["checks"] = checks
        metrics[name] = m
        if ready and previous_status != "🏆 PROMOTION EMPFOHLEN":
            events.append(
                f"🏆 Strategy Challenger: {name} erfüllt die eingefrorenen Promotion-Regeln. "
                f"Netto {m['return_pct']:+.2f}% vs. Champion {champion.get('return_pct',0):+.2f}%, "
                f"DD {m['drawdown_pct']:.2f}%, PF {('∞' if m['profit_factor']==math.inf else f'{m['profit_factor']:.2f}')}. "
                "Keine automatische Umschaltung – bitte erst prüfen."
            )

    state["last_processed_scan_at"] = scan_time.isoformat()
    state["last_updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    state["last_metrics"] = {"champion": champion, "challengers": metrics}
    _write_json(CHALLENGER_STATE, state)
    _append_history(state, scan_time, champion, metrics)
    return {"active": True, "message": f"{len(metrics)} Challenger aktualisiert", "events": events, "state": state, "champion": champion, "challengers": metrics}


def challenge_table(state: dict[str, Any] | None = None) -> pd.DataFrame:
    state = state or challenge_state()
    if not state:
        return pd.DataFrame()
    last = state.get("last_metrics") or {}
    champ = last.get("champion") or {}
    rows = [{
        "Rolle": "👑 Champion", "Strategie": state.get("champion"), "Status": "eingefroren",
        "Kontowert €": round(float(champ.get("value", state.get("start_capital", 1000.0))), 2),
        "Rendite %": round(float(champ.get("return_pct", 0.0)), 2), "Drawdown %": round(float(champ.get("drawdown_pct", 0.0)), 2),
        "PF": champ.get("pf"), "Trades": champ.get("completed_trades", 0), "Gebühren €": round(float(champ.get("fees", 0.0)), 2),
        "Position": "–",
    }]
    for name, shadow in (state.get("challengers") or {}).items():
        m = (last.get("challengers") or {}).get(name) or _metrics_shadow(shadow, float(state.get("start_capital", 1000.0)))
        pf = m.get("profit_factor", 0.0)
        rows.append({
            "Rolle": "🧪 Challenger", "Strategie": name, "Status": m.get("status", shadow.get("last_status", "🟡 BEOBACHTEN")),
            "Kontowert €": round(float(m.get("value", state.get("start_capital", 1000.0))), 2),
            "Rendite %": round(float(m.get("return_pct", 0.0)), 2), "Drawdown %": round(float(m.get("drawdown_pct", 0.0)), 2),
            "PF": "∞" if pf == math.inf else round(float(pf), 2), "Trades": int(m.get("completed_trades", 0)),
            "Gebühren €": round(float(m.get("fees", 0.0)), 2), "Position": m.get("position", "Cash"),
        })
    return pd.DataFrame(rows)


def challenge_history() -> pd.DataFrame:
    if not CHALLENGER_HISTORY.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(CHALLENGER_HISTORY)
    except Exception:
        return pd.DataFrame()
