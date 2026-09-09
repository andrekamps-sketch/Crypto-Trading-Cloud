from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from market_data import CRYPTO_UNIVERSE, load_fast_prices

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

LIVE_STATE = DATA_DIR / "live_paper_state.json"
LIVE_HISTORY = DATA_DIR / "live_paper_history.csv"
LIVE_TRADES = DATA_DIR / "live_paper_trades.csv"

DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "LINK", "LTC", "DOT", "BCH"]
DEFAULTS = {
    "start_capital": 1000.0,
    "position_pct": 20.0,
    "max_positions": 3,
    "max_invested_pct": 60.0,
    "fee_pct": 0.25,
    "slippage_pct": 0.10,
    "stop_loss_pct": 4.0,
    "take_profit_pct": 8.0,
    "trailing_activation_pct": 4.0,
    "trailing_stop_pct": 3.0,
    "min_rules": 9,
    "min_edge": 85.0,
    "confirm_scans": 1,
    "cooldown_hours": 6,
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


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _append_csv(path: Path, row: dict[str, Any], limit: int = 20000) -> None:
    frame = pd.DataFrame([row])
    if path.exists():
        try:
            old = pd.read_csv(path)
            frame = pd.concat([old, frame], ignore_index=True)
        except Exception:
            pass
    if len(frame) > limit:
        frame = frame.tail(limit)
    frame.to_csv(path, index=False)


def state() -> dict[str, Any] | None:
    x = _read_json(LIVE_STATE, None)
    return x if isinstance(x, dict) else None


def start_live_paper(
    assets: list[str] | None = None,
    start_capital: float = 1000.0,
    **params,
) -> dict[str, Any]:
    assets = [str(a).upper() for a in (assets or DEFAULT_ASSETS)]
    assets = [a for a in assets if a in CRYPTO_UNIVERSE]
    if not assets:
        raise ValueError("Mindestens ein unterstützter Coin muss gewählt sein.")
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in params.items() if k in cfg})
    cfg["start_capital"] = float(start_capital)
    if cfg["start_capital"] <= 0:
        raise ValueError("Startkapital muss > 0 sein.")
    if int(cfg["max_positions"]) < 1:
        raise ValueError("Maximale Positionen muss mindestens 1 sein.")
    if float(cfg["position_pct"]) <= 0 or float(cfg["position_pct"]) > 100:
        raise ValueError("Positionsgröße muss zwischen 0 und 100% liegen.")

    now = _utc_now().isoformat()
    payload = {
        "version": "6.8",
        "mode": "LIVE_PAPER_ONLY",
        "started_at": now,
        "active": True,
        "entry_paused": False,
        "assets": assets,
        "params": cfg,
        "cash": float(cfg["start_capital"]),
        "equity": float(cfg["start_capital"]),
        "peak_equity": float(cfg["start_capital"]),
        "max_drawdown_pct": 0.0,
        "fees_total": 0.0,
        "realized_pnl": 0.0,
        "positions": {},
        "confirmations": {},
        "cooldown_until": {},
        "last_confirmation_scan": None,
        "last_update_at": None,
        "last_monitor_at": None,
        "last_prices": {},
        "trade_count": 0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
    }
    _write_json(LIVE_STATE, payload)
    return payload


def set_entry_paused(paused: bool) -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Live-Paper wurde noch nicht gestartet.")
    s["entry_paused"] = bool(paused)
    _write_json(LIVE_STATE, s)
    return s


def stop_live_paper() -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Live-Paper wurde noch nicht gestartet.")
    s["active"] = False
    _write_json(LIVE_STATE, s)
    return s


def resume_live_paper() -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Live-Paper wurde noch nicht gestartet.")
    s["active"] = True
    _write_json(LIVE_STATE, s)
    return s


def _monitor_rows(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {str(r.get("Coin", "")).upper(): r for r in ((payload or {}).get("rows", []) or []) if r.get("Coin")}


def _qualifies(row: dict[str, Any] | None, regime_code: str, cfg: dict[str, Any]) -> bool:
    if not row:
        return False
    return (
        int(row.get("rules_ok", 0) or 0) >= int(cfg["min_rules"])
        and float(row.get("Edge", 0) or 0) >= float(cfg["min_edge"])
        and str(row.get("Risiko", "")) != "hoch"
        and regime_code != "BEAR"
    )


def _price_dict(snapshot: dict[str, Any]) -> dict[str, float]:
    out = {}
    for a, x in (snapshot or {}).items():
        try:
            p = float(x.get("price") if isinstance(x, dict) else x)
            if p > 0:
                out[str(a).upper()] = p
        except Exception:
            pass
    return out


def _equity(s: dict[str, Any], prices: dict[str, float]) -> tuple[float, float]:
    cfg = s["params"]
    fee = float(cfg["fee_pct"]) / 100.0
    slip = float(cfg["slippage_pct"]) / 100.0
    invested = 0.0
    for asset, pos in (s.get("positions") or {}).items():
        p = prices.get(asset, float(pos.get("last_price", pos.get("entry_price", 0))))
        # liquidation-equivalent mark: what would remain after sell slippage + fee
        invested += float(pos.get("qty", 0)) * p * (1.0 - slip) * (1.0 - fee)
    return float(s.get("cash", 0)) + invested, invested


def _trade_event(kind: str, asset: str, price: float, fee: float, pnl: float, reason: str, equity: float) -> dict[str, Any]:
    ts = _utc_now().isoformat()
    icon = "🟢" if kind == "BUY" else ("✅" if pnl > 0 else "❌" if pnl < 0 else "➖")
    if kind == "BUY":
        text = f"🎮 {icon} LIVE-PAPER KAUF · {asset} @ {price:,.4f} €\nGebühr {fee:.2f} € · {reason} · Konto {equity:,.2f} €"
    else:
        text = f"🎮 {icon} LIVE-PAPER VERKAUF · {asset} @ {price:,.4f} € · G/V {pnl:+.2f} €\nGebühr {fee:.2f} € · {reason} · Konto {equity:,.2f} €"
    return {"timestamp": ts, "action": kind, "asset": asset, "price": price, "fee": fee, "pnl_eur": pnl, "reason": reason, "telegram": text}


def process_live_paper(monitor_payload: dict[str, Any] | None = None, price_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    s = state()
    if not s:
        return {"active": False, "message": "Live-Paper noch nicht gestartet", "events": []}
    if not s.get("active", True):
        return {"active": False, "message": "Live-Paper gestoppt", "events": []}

    cfg = s["params"]
    assets = list(s.get("assets") or [])
    monitor_payload = monitor_payload or {}
    rows = _monitor_rows(monitor_payload)
    regime = ((monitor_payload.get("regime") or {}).get("code") or "NEUTRAL").upper()

    if price_snapshot is None:
        price_snapshot = load_fast_prices(assets, interval="5m", period="5d")
    prices = _price_dict(price_snapshot)
    if not prices:
        return {"active": True, "message": "Keine aktuellen 5-Minuten-Kurse verfügbar", "events": []}

    now = _utc_now()
    events: list[dict[str, Any]] = []
    fee_rate = float(cfg["fee_pct"]) / 100.0
    slip_rate = float(cfg["slippage_pct"]) / 100.0

    # Confirmations only advance on a new 1h monitor scan, never on every 5m price refresh.
    monitor_at = str(monitor_payload.get("evaluated_at") or "")
    if monitor_at and monitor_at != s.get("last_confirmation_scan"):
        for asset in assets:
            if _qualifies(rows.get(asset), regime, cfg):
                s["confirmations"][asset] = int(s.get("confirmations", {}).get(asset, 0) or 0) + 1
            else:
                s["confirmations"][asset] = 0
        s["last_confirmation_scan"] = monitor_at
        s["last_monitor_at"] = monitor_at

    # Manage open positions with near-live prices.
    for asset in list((s.get("positions") or {}).keys()):
        pos = s["positions"][asset]
        p = prices.get(asset)
        if not p:
            continue
        pos["last_price"] = p
        pos["peak_price"] = max(float(pos.get("peak_price", p)), p)
        entry = float(pos["entry_price"])
        stop = entry * (1.0 - float(cfg["stop_loss_pct"]) / 100.0)
        take = entry * (1.0 + float(cfg["take_profit_pct"]) / 100.0)
        trail_active = float(pos["peak_price"]) >= entry * (1.0 + float(cfg["trailing_activation_pct"]) / 100.0)
        trail = float(pos["peak_price"]) * (1.0 - float(cfg["trailing_stop_pct"]) / 100.0) if trail_active else None
        reason = None
        if p <= stop:
            reason = "Stop-Loss"
        elif p >= take:
            reason = "Take-Profit"
        elif trail is not None and p <= trail:
            reason = "Trailing-Stop"
        if reason:
            exec_price = p * (1.0 - slip_rate)
            qty = float(pos["qty"])
            gross = qty * exec_price
            sell_fee = gross * fee_rate
            net_proceeds = gross - sell_fee
            cost_basis = qty * float(pos["entry_price"]) + float(pos.get("buy_fee", 0))
            pnl = net_proceeds - cost_basis
            s["cash"] = float(s.get("cash", 0)) + net_proceeds
            s["fees_total"] = float(s.get("fees_total", 0)) + sell_fee
            s["realized_pnl"] = float(s.get("realized_pnl", 0)) + pnl
            s["closed_trades"] = int(s.get("closed_trades", 0)) + 1
            if pnl > 0:
                s["wins"] = int(s.get("wins", 0)) + 1
            elif pnl < 0:
                s["losses"] = int(s.get("losses", 0)) + 1
            del s["positions"][asset]
            s.setdefault("cooldown_until", {})[asset] = (now + pd.Timedelta(hours=float(cfg["cooldown_hours"]))).isoformat()
            eq, _ = _equity(s, prices)
            e = _trade_event("SELL", asset, exec_price, sell_fee, pnl, reason, eq)
            events.append(e)
            _append_csv(LIVE_TRADES, e)

    # New entries: strongest current qualifying coins first.
    eq_before, invested_before = _equity(s, prices)
    if not s.get("entry_paused", False) and len(s.get("positions", {})) < int(cfg["max_positions"]):
        candidates = []
        for asset in assets:
            if asset in s.get("positions", {}) or asset not in prices:
                continue
            cd = (s.get("cooldown_until") or {}).get(asset)
            if cd:
                try:
                    if pd.Timestamp(cd) > now:
                        continue
                except Exception:
                    pass
            row = rows.get(asset)
            if not _qualifies(row, regime, cfg):
                continue
            if int((s.get("confirmations") or {}).get(asset, 0)) < int(cfg["confirm_scans"]):
                continue
            candidates.append((bool(row.get("quality_ready")), int(row.get("rules_ok", 0)), float(row.get("Edge", 0)), float(row.get("Score", 0)), asset, row))
        candidates.sort(reverse=True)

        for _, _, _, _, asset, row in candidates:
            if len(s.get("positions", {})) >= int(cfg["max_positions"]):
                break
            eq_now, invested_now = _equity(s, prices)
            if eq_now <= 0:
                break
            max_invested = eq_now * float(cfg["max_invested_pct"]) / 100.0
            room = max(0.0, max_invested - invested_now)
            target = min(eq_now * float(cfg["position_pct"]) / 100.0, room, float(s.get("cash", 0)))
            if target < 10.0:
                break
            raw_price = prices[asset]
            exec_price = raw_price * (1.0 + slip_rate)
            qty = target / (exec_price * (1.0 + fee_rate))
            gross = qty * exec_price
            buy_fee = gross * fee_rate
            total = gross + buy_fee
            if total > float(s.get("cash", 0)) + 1e-8:
                continue
            s["cash"] = float(s.get("cash", 0)) - total
            s["fees_total"] = float(s.get("fees_total", 0)) + buy_fee
            s.setdefault("positions", {})[asset] = {
                "qty": qty,
                "entry_price": exec_price,
                "signal_price": raw_price,
                "buy_fee": buy_fee,
                "opened_at": now.isoformat(),
                "peak_price": raw_price,
                "last_price": raw_price,
                "entry_rules": int(row.get("rules_ok", 0)),
                "entry_edge": float(row.get("Edge", 0)),
                "entry_score": float(row.get("Score", 0)),
            }
            s["trade_count"] = int(s.get("trade_count", 0)) + 1
            eq_after, _ = _equity(s, prices)
            why = f"{int(row.get('rules_ok',0))}/10 · Edge {float(row.get('Edge',0)):.1f}"
            e = _trade_event("BUY", asset, exec_price, buy_fee, 0.0, why, eq_after)
            events.append(e)
            _append_csv(LIVE_TRADES, e)

    equity, invested = _equity(s, prices)
    s["equity"] = equity
    s["peak_equity"] = max(float(s.get("peak_equity", equity)), equity)
    if s["peak_equity"] > 0:
        dd = max(0.0, (float(s["peak_equity"]) - equity) / float(s["peak_equity"]) * 100.0)
        s["max_drawdown_pct"] = max(float(s.get("max_drawdown_pct", 0)), dd)
    s["last_update_at"] = now.isoformat()
    s["last_prices"] = price_snapshot
    _write_json(LIVE_STATE, s)

    _append_csv(LIVE_HISTORY, {
        "timestamp": now.isoformat(),
        "equity_eur": round(equity, 6),
        "cash_eur": round(float(s.get("cash", 0)), 6),
        "invested_eur": round(invested, 6),
        "positions": len(s.get("positions", {})),
        "fees_eur": round(float(s.get("fees_total", 0)), 6),
        "realized_pnl_eur": round(float(s.get("realized_pnl", 0)), 6),
        "drawdown_pct": round(float(s.get("max_drawdown_pct", 0)), 6),
    })
    return {"active": True, "message": f"{len(prices)} Kurse · {len(s.get('positions',{}))} Positionen · Konto {equity:.2f} €", "events": events, "state": s}


def positions_dataframe(s: dict[str, Any] | None = None) -> pd.DataFrame:
    s = s or state() or {}
    rows = []
    last_prices = _price_dict(s.get("last_prices") or {})
    cfg = s.get("params") or DEFAULTS
    for asset, pos in (s.get("positions") or {}).items():
        cur = last_prices.get(asset, float(pos.get("last_price", pos.get("entry_price", 0))))
        entry = float(pos.get("entry_price", 0))
        qty = float(pos.get("qty", 0))
        pnl_pct = (cur / entry - 1) * 100 if entry > 0 else 0.0
        rows.append({
            "Coin": asset,
            "Einstieg €": round(entry, 6),
            "Aktuell €": round(cur, 6),
            "Menge": qty,
            "Kurs-G/V %": round(pnl_pct, 2),
            "Stop €": round(entry * (1 - float(cfg.get("stop_loss_pct",4))/100), 6),
            "Ziel €": round(entry * (1 + float(cfg.get("take_profit_pct",8))/100), 6),
            "Peak €": round(float(pos.get("peak_price", cur)), 6),
            "Entry-Regeln": int(pos.get("entry_rules", 0)),
            "Entry-Edge": round(float(pos.get("entry_edge", 0)), 1),
            "Seit": str(pos.get("opened_at", "")),
        })
    return pd.DataFrame(rows)


def market_dataframe(monitor_payload: dict[str, Any] | None = None, s: dict[str, Any] | None = None) -> pd.DataFrame:
    s = s or state() or {}
    monitor_payload = monitor_payload or {}
    rows = _monitor_rows(monitor_payload)
    prices = _price_dict(s.get("last_prices") or {})
    out = []
    for asset in s.get("assets", DEFAULT_ASSETS):
        r = rows.get(asset, {})
        out.append({
            "Coin": asset,
            "Live €": prices.get(asset),
            "Quality": "READY" if r.get("quality_ready") else (f"{int(r.get('rules_ok',0))}/{int(r.get('rules_total',10))}" if r else "–"),
            "Edge": r.get("Edge"),
            "Score": r.get("Score"),
            "RSI": r.get("RSI"),
            "Signal": r.get("Signal", "–"),
            "Position": "OFFEN" if asset in (s.get("positions") or {}) else "Cash",
            "Bestätigung": int((s.get("confirmations") or {}).get(asset, 0)),
            "Fehlt": r.get("Fehlt", ""),
        })
    return pd.DataFrame(out)


def history_dataframe(limit: int = 1000) -> pd.DataFrame:
    if not LIVE_HISTORY.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(LIVE_HISTORY).tail(limit)
    except Exception:
        return pd.DataFrame()


def trades_dataframe(limit: int = 1000) -> pd.DataFrame:
    if not LIVE_TRADES.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(LIVE_TRADES).tail(limit)
    except Exception:
        return pd.DataFrame()
