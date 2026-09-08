from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data import CRYPTO_UNIVERSE, latest_price
from scanner import scan_universe, market_regime_from_scan, regime_allows_entries
from multi_strategy import strategy_candidates, exit_reason as multi_exit_reason, STRATEGY_LABELS

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "scanner_config.json"
STATE_FILE = ROOT / "scanner_state.json"
STATUS_FILE = ROOT / "scanner_status.json"
LOG_FILE = ROOT / "scanner_trades.csv"
STOP_FILE = ROOT / "SCANNER_STOP"

LOG_FIELDS = [
    "timestamp", "asset", "action", "price", "qty", "cash_after", "equity_after",
    "pnl_eur", "reason", "score", "entry_score", "fee_eur", "holding_hours",
    "trend_score", "momentum_score", "rsi", "breakout_score", "atr_pct", "volume_ratio", "market_regime", "strategy",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def default_config() -> dict[str, Any]:
    return {
        "assets": list(CRYPTO_UNIVERSE.keys()), "start_capital": 1000.0, "allocation_pct": 20.0,
        "stop_loss_pct": 4.0, "take_profit_pct": 8.0, "fee_pct": 0.25, "poll_seconds": 300,
        "minimum_score": 70.0, "exit_score": 45.0, "max_positions": 2, "daily_loss_limit_pct": 3.0,
        "trailing_stop_pct": 3.0, "trailing_activation_pct": 4.0, "regime_filter": "all", "strategy_mode": "AUTO_ROUTER",
    }


def load_config() -> dict[str, Any]:
    cfg = default_config(); cfg.update(read_json(CONFIG_FILE, {}))
    cfg["assets"] = [a for a in cfg.get("assets", []) if a in CRYPTO_UNIVERSE] or ["BTC", "ETH"]
    cfg["allocation_pct"] = max(1.0, min(float(cfg.get("allocation_pct", 20)), 100.0))
    cfg["stop_loss_pct"] = max(0.0, float(cfg.get("stop_loss_pct", 4)))
    cfg["take_profit_pct"] = max(0.0, float(cfg.get("take_profit_pct", 8)))
    cfg["fee_pct"] = max(0.0, float(cfg.get("fee_pct", 0.25)))
    cfg["poll_seconds"] = max(60, int(cfg.get("poll_seconds", 300)))
    cfg["minimum_score"] = max(0.0, min(100.0, float(cfg.get("minimum_score", 70))))
    cfg["exit_score"] = max(0.0, min(100.0, float(cfg.get("exit_score", 45))))
    cfg["max_positions"] = max(1, min(5, int(cfg.get("max_positions", 2))))
    cfg["daily_loss_limit_pct"] = max(0.0, float(cfg.get("daily_loss_limit_pct", 3)))
    cfg["trailing_stop_pct"] = max(0.0, float(cfg.get("trailing_stop_pct", 3)))
    cfg["trailing_activation_pct"] = max(0.0, float(cfg.get("trailing_activation_pct", 4)))
    cfg["regime_filter"] = str(cfg.get("regime_filter", "all"))
    if cfg["regime_filter"] not in {"all", "no_bear", "bull_only"}:
        cfg["regime_filter"] = "all"
    cfg["strategy_mode"] = str(cfg.get("strategy_mode", "AUTO_ROUTER"))
    if cfg["strategy_mode"] not in set(STRATEGY_LABELS) | {"LEGACY_SCORE"}:
        cfg["strategy_mode"] = "AUTO_ROUTER"
    return cfg


def default_state(start_capital: float) -> dict[str, Any]:
    return {
        "cash": float(start_capital), "positions": {}, "realized_pnl": 0.0,
        "created_at": now_iso(), "last_update": now_iso(),
        "day_key": datetime.now().astimezone().date().isoformat(), "day_start_equity": float(start_capital),
        "blocked_today": False,
    }


def load_state(cfg: dict[str, Any]) -> dict[str, Any]:
    state = read_json(STATE_FILE, None)
    if not isinstance(state, dict) or "cash" not in state:
        state = default_state(float(cfg["start_capital"])); write_json(STATE_FILE, state)
    state.setdefault("positions", {}); state.setdefault("realized_pnl", 0.0); state.setdefault("blocked_today", False)
    for pos in state["positions"].values():
        pos.setdefault("peak_price", float(pos.get("last_price", pos.get("entry", 0.0))))
        pos.setdefault("strategy", "LEGACY_SCORE")
    return state


def write_status(**kwargs: Any) -> None:
    status = read_json(STATUS_FILE, {}); status.update(kwargs); status["heartbeat"] = now_iso(); write_json(STATUS_FILE, status)


def ensure_log_schema() -> None:
    if not LOG_FILE.exists():
        return
    try:
        with LOG_FILE.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        current = list(rows[0].keys()) if rows else []
        if current == LOG_FIELDS:
            return
        with LOG_FILE.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS); w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in LOG_FIELDS})
    except Exception:
        pass


def log_trade(row: dict[str, Any]) -> None:
    ensure_log_schema(); new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new: writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in LOG_FIELDS})


def current_equity(state: dict[str, Any], prices: dict[str, float]) -> float:
    eq = float(state.get("cash", 0.0))
    for asset, pos in state.get("positions", {}).items():
        p = prices.get(asset, float(pos.get("last_price", pos.get("entry", 0.0))))
        eq += float(pos.get("qty", 0.0)) * float(p)
    return eq


def refresh_day_limit(state: dict[str, Any], equity: float) -> None:
    today = datetime.now().astimezone().date().isoformat()
    if state.get("day_key") != today:
        state["day_key"] = today; state["day_start_equity"] = float(equity); state["blocked_today"] = False


def _scan_metrics(scan) -> dict[str, Any]:
    if not scan:
        return {}
    return {
        "trend_score": getattr(scan, "trend_score", ""), "momentum_score": getattr(scan, "momentum_score", ""),
        "rsi": getattr(scan, "rsi", ""), "breakout_score": getattr(scan, "breakout_score", ""),
        "atr_pct": getattr(scan, "atr_pct", ""), "volume_ratio": getattr(scan, "volume_ratio", ""),
    }


def _scan_dict(scan) -> dict[str, Any]:
    if scan is None:
        return {}
    try:
        return scan.to_dict()
    except Exception:
        return {k: getattr(scan, k, None) for k in [
            "asset","price","score","signal","risk_level","trend_score","momentum_score","rsi_score",
            "breakout_score","volatility_score","volume_score","rsi","return_24h_pct","return_7d_pct","atr_pct","volume_ratio"
        ]}


def check_once(state: dict[str, Any]) -> None:
    cfg = load_config(); fee = cfg["fee_pct"] / 100.0; alloc = cfg["allocation_pct"] / 100.0
    sl = cfg["stop_loss_pct"] / 100.0; tp = cfg["take_profit_pct"] / 100.0
    trail = cfg["trailing_stop_pct"] / 100.0; trail_activation = cfg["trailing_activation_pct"] / 100.0
    scans = scan_universe(cfg["assets"]); scan_map = {r.asset: r for r in scans}; prices = {r.asset: r.price for r in scans if r.price > 0}
    benchmark = scan_map.get("BTC") or (scans[0] if scans else None)
    regime = market_regime_from_scan(benchmark)
    regime_code = regime["code"]
    regime_ok = regime_allows_entries(cfg.get("regime_filter", "all"), regime_code)

    for asset, pos in list(state.get("positions", {}).items()):
        if asset not in prices:
            try: prices[asset] = latest_price(CRYPTO_UNIVERSE[asset])
            except Exception: prices[asset] = float(pos.get("last_price", pos.get("entry", 0.0)))
        pos["last_price"] = prices[asset]
        pos["peak_price"] = max(float(pos.get("peak_price", pos.get("entry", 0.0))), float(prices[asset]))

    equity_before = current_equity(state, prices); refresh_day_limit(state, equity_before)
    day_start = float(state.get("day_start_equity", equity_before))
    if cfg["daily_loss_limit_pct"] > 0 and equity_before <= day_start * (1 - cfg["daily_loss_limit_pct"] / 100.0): state["blocked_today"] = True
    messages = []

    for asset, pos in list(state.get("positions", {}).items()):
        price = float(prices.get(asset, pos.get("entry", 0))); entry = float(pos["entry"]); qty = float(pos["qty"])
        if price <= 0: continue
        scan = scan_map.get(asset); score = float(scan.score) if scan else 0.0; reason = None
        if cfg.get("strategy_mode") != "LEGACY_SCORE":
            reason = multi_exit_reason(_scan_dict(scan) if scan else None, pos, regime_code, price)
        else:
            peak = max(float(pos.get("peak_price", entry)), price); pos["peak_price"] = peak
            trail_level = peak * (1 - trail) if trail > 0 and peak >= entry * (1 + trail_activation) else None
            if sl > 0 and price <= entry * (1 - sl): reason = "Stop-Loss"
            elif tp > 0 and price >= entry * (1 + tp): reason = "Take-Profit"
            elif trail_level is not None and price <= trail_level: reason = "Trailing-Stop"
            elif scan and scan.score < cfg["exit_score"]: reason = "Score-Exit"

        if reason:
            gross = qty * price; sell_fee = gross * fee; proceeds = gross - sell_fee; state["cash"] = float(state["cash"]) + proceeds
            pnl = proceeds - float(pos["entry_cost_total"]); state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl
            try:
                holding_hours = (datetime.now().astimezone() - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600.0
            except Exception: holding_hours = ""
            del state["positions"][asset]; eq = current_equity(state, prices)
            row = {
                "timestamp": now_iso(), "asset": asset, "action": "SELL", "price": round(price, 6), "qty": f"{qty:.10f}",
                "cash_after": round(state["cash"], 2), "equity_after": round(eq, 2), "pnl_eur": round(pnl, 2),
                "reason": reason, "score": round(score, 1), "entry_score": pos.get("entry_score", ""),
                "fee_eur": round(sell_fee, 4), "holding_hours": round(float(holding_hours), 2) if holding_hours != "" else "",
                "strategy": pos.get("strategy", "LEGACY_SCORE"),
            }; row.update(_scan_metrics(scan)); row["market_regime"] = regime_code; log_trade(row)
            messages.append(f"{asset}: SELL {reason} | {pnl:+.2f} €")

    equity_mid = current_equity(state, prices); refresh_day_limit(state, equity_mid); day_start = float(state.get("day_start_equity", equity_mid))
    if cfg["daily_loss_limit_pct"] > 0 and equity_mid <= day_start * (1 - cfg["daily_loss_limit_pct"] / 100.0): state["blocked_today"] = True

    slots = max(0, cfg["max_positions"] - len(state["positions"]))
    strategy_mode = cfg.get("strategy_mode", "AUTO_ROUTER")
    strategy_regime_ok = regime_ok if strategy_mode == "LEGACY_SCORE" else True
    if not state.get("blocked_today", False) and strategy_regime_ok and slots > 0:
        if strategy_mode == "LEGACY_SCORE":
            candidate_items = [(r, "LEGACY_SCORE", float(r.score)) for r in scans if r.price > 0 and r.score >= cfg["minimum_score"] and r.signal in {"STARK", "INTERESSANT"} and r.asset not in state["positions"] and r.risk_level != "hoch"]
        else:
            candidate_items = []
            for rd, strategy, edge in strategy_candidates([_scan_dict(r) for r in scans], regime_code, strategy_mode):
                scan_obj = scan_map.get(str(rd.get("asset")))
                if scan_obj is not None and scan_obj.asset not in state["positions"]:
                    candidate_items.append((scan_obj, strategy, edge))
        for scan, strategy, edge in candidate_items[:slots]:
            if float(state["cash"]) <= 10: break
            equity_now = current_equity(state, prices); budget_total = min(float(state["cash"]), equity_now * alloc)
            if budget_total <= 10: break
            buy_value = budget_total / (1 + fee); buy_fee = buy_value * fee; qty = buy_value / scan.price; total_cost = buy_value + buy_fee
            state["cash"] = float(state["cash"]) - total_cost
            state["positions"][scan.asset] = {
                "qty": qty, "entry": scan.price, "entry_cost_total": total_cost, "opened_at": now_iso(),
                "entry_score": scan.score, "last_price": scan.price, "peak_price": scan.price, "strategy": strategy,
            }
            eq = current_equity(state, prices)
            row = {
                "timestamp": now_iso(), "asset": scan.asset, "action": "BUY", "price": round(scan.price, 6), "qty": f"{qty:.10f}",
                "cash_after": round(state["cash"], 2), "equity_after": round(eq, 2), "pnl_eur": "", "reason": f"{strategy}-Einstieg",
                "score": scan.score, "entry_score": scan.score, "fee_eur": round(buy_fee, 4), "holding_hours": "", "strategy": strategy,
            }; row.update(_scan_metrics(scan)); row["market_regime"] = regime_code; log_trade(row)
            messages.append(f"{scan.asset}: BUY {strategy} | Edge {edge:.1f}")

    if state.get("blocked_today", False): messages.append("Tagesverlust-Limit aktiv: heute keine neuen Einstiege")
    if not regime_ok: messages.append(f"Marktphasen-Filter blockiert Neueinstiege ({regime['label']})")
    state["last_update"] = now_iso(); write_json(STATE_FILE, state); equity = current_equity(state, prices)
    write_status(running=True, pid=os.getpid(), last_check=now_iso(), message=" | ".join(messages) if messages else "Scan abgeschlossen – kein Trade",
                 rankings=[r.to_dict() for r in scans], prices=prices, equity=round(equity, 2), cash=round(float(state["cash"]), 2),
                 realized_pnl=round(float(state.get("realized_pnl", 0.0)), 2), blocked_today=bool(state.get("blocked_today", False)),
                 market_regime=regime, regime_filter=cfg.get("regime_filter", "all"), regime_allows_entries=regime_ok,
                 strategy_mode=cfg.get("strategy_mode", "AUTO_ROUTER"), error="")


def sleep_interruptible(seconds: int) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if STOP_FILE.exists(): return False
        write_status(running=True, pid=os.getpid()); time.sleep(min(5, max(0.2, end-time.time())))
    return True


def main() -> None:
    if STOP_FILE.exists(): STOP_FILE.unlink()
    cfg = load_config(); state = load_state(cfg); ensure_log_schema()
    write_status(running=True, pid=os.getpid(), started_at=now_iso(), error="", message="Scanner-Bot gestartet")
    try:
        while not STOP_FILE.exists():
            try: check_once(state)
            except Exception as exc: write_status(running=True, pid=os.getpid(), error=str(exc), message=f"Fehler: {exc}")
            cfg = load_config()
            if not sleep_interruptible(int(cfg["poll_seconds"])): break
    finally: write_status(running=False, stopped_at=now_iso(), message="Scanner-Bot gestoppt")


if __name__ == "__main__": main()
