from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


STRATEGY_LABELS = {
    "AUTO_ROUTER": "Auto-Router (Marktphase wählt Strategie)",
    "MOMENTUM": "Trend / Momentum",
    "BREAKOUT": "Breakout + Volumen",
    "PULLBACK": "Pullback im Aufwärtstrend",
    "MEAN_REVERSION": "Mean-Reversion (Seitwärts)",
    "CASH": "Cash / keine Neueinstiege",
}

# Absichtlich unterschiedliche Profile: nicht nur dieselbe Score-Regel mit anderen Grenzen.
STRATEGY_RISK = {
    "MOMENTUM": dict(sl=4.0, tp=10.0, trail=4.0, trail_activate=5.0),
    "BREAKOUT": dict(sl=4.0, tp=12.0, trail=5.0, trail_activate=6.0),
    "PULLBACK": dict(sl=3.5, tp=7.0, trail=3.0, trail_activate=4.0),
    "MEAN_REVERSION": dict(sl=3.0, tp=5.0, trail=2.5, trail_activate=3.0),
}


@dataclass
class MultiStrategyResult:
    final_value: float
    return_pct: float
    max_drawdown_pct: float
    completed_trades: int
    win_rate_pct: float
    profit_factor: float
    total_fees: float
    equity: pd.Series
    trade_log: pd.DataFrame


def _f(r: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(r.get(key, default) or default)
    except Exception:
        return float(default)


def _eligible(r: dict, strategy: str, regime: str) -> tuple[bool, float]:
    if _f(r, "price") <= 0 or str(r.get("risk_level", "")) == "hoch":
        return False, -1e9

    trend = _f(r, "trend_score")
    mom = _f(r, "momentum_score", 50)
    score = _f(r, "score")
    rv = _f(r, "rsi", 50)
    br = _f(r, "breakout_score", 50)
    vol_score = _f(r, "volume_score", 50)
    vol_ratio = _f(r, "volume_ratio", 1)
    r24 = _f(r, "return_24h_pct")
    r7 = _f(r, "return_7d_pct")
    atr = _f(r, "atr_pct", 2)

    if strategy == "MOMENTUM":
        ok = regime == "BULL" and trend >= 65 and mom >= 62 and score >= 65 and 48 <= rv <= 74 and r7 > 0
        edge = 0.34 * trend + 0.34 * mom + 0.20 * score + 0.12 * vol_score
        return ok, edge

    if strategy == "BREAKOUT":
        ok = regime == "BULL" and trend >= 65 and br >= 70 and vol_ratio >= 1.0 and r24 > -1.5 and rv < 78
        edge = 0.42 * br + 0.22 * trend + 0.20 * vol_score + 0.16 * mom
        return ok, edge

    if strategy == "PULLBACK":
        ok = regime in {"BULL", "NEUTRAL"} and trend >= 65 and 38 <= rv <= 58 and r7 >= 0 and -6 <= r24 <= 3
        rsi_quality = max(0.0, 100.0 - abs(rv - 48.0) * 5.0)
        edge = 0.38 * trend + 0.28 * rsi_quality + 0.20 * score + 0.14 * mom
        return ok, edge

    if strategy == "MEAN_REVERSION":
        ok = regime == "NEUTRAL" and 28 <= rv <= 43 and r24 <= -0.8 and r7 > -12 and 0.4 <= atr <= 6
        oversold = max(0.0, min(100.0, (48.0 - rv) * 5.0))
        vol_quality = max(0.0, 100.0 - abs(atr - 2.5) * 18.0)
        edge = 0.50 * oversold + 0.25 * vol_quality + 0.15 * trend + 0.10 * score
        return ok, edge

    return False, -1e9


def strategy_candidates(scans: list[dict], regime: str, mode: str) -> list[tuple[dict, str, float]]:
    mode = str(mode or "AUTO_ROUTER")
    if mode == "CASH":
        return []

    if mode == "AUTO_ROUTER":
        if regime == "BULL":
            allowed = ["BREAKOUT", "MOMENTUM", "PULLBACK"]
        elif regime == "NEUTRAL":
            allowed = ["PULLBACK", "MEAN_REVERSION"]
        else:
            allowed = []  # bärisch = bewusst Cash
    else:
        allowed = [mode]

    out: list[tuple[dict, str, float]] = []
    for r in scans:
        for strategy in allowed:
            ok, edge = _eligible(r, strategy, regime)
            if ok:
                # Kleine Priorität für spezialisierte Setups, damit ein echter Breakout nicht
                # automatisch vom allgemeineren Momentum-Kriterium überlagert wird.
                bonus = {"BREAKOUT": 3.0, "PULLBACK": 1.5, "MEAN_REVERSION": 1.0, "MOMENTUM": 0.0}.get(strategy, 0.0)
                out.append((r, strategy, edge + bonus))
    out.sort(key=lambda x: x[2], reverse=True)

    # Je Coin nur das stärkste Setup behalten.
    dedup: list[tuple[dict, str, float]] = []
    seen: set[str] = set()
    for item in out:
        asset = str(item[0].get("asset"))
        if asset in seen:
            continue
        seen.add(asset)
        dedup.append(item)
    return dedup


def exit_reason(scan: dict | None, position: dict, regime: str, price: float) -> str | None:
    strategy = str(position.get("strategy", "MOMENTUM"))
    cfg = STRATEGY_RISK.get(strategy, STRATEGY_RISK["MOMENTUM"])
    entry = float(position["entry"])
    peak = max(float(position.get("peak_price", entry)), float(price))
    position["peak_price"] = peak

    sl = cfg["sl"] / 100.0
    tp = cfg["tp"] / 100.0
    trail = cfg["trail"] / 100.0
    act = cfg["trail_activate"] / 100.0
    trailing_level = peak * (1 - trail) if trail > 0 and peak >= entry * (1 + act) else None

    if price <= entry * (1 - sl):
        return "Stop-Loss"
    if price >= entry * (1 + tp):
        return "Take-Profit"
    if trailing_level is not None and price <= trailing_level:
        return "Trailing-Stop"

    if scan is None:
        return None
    trend = _f(scan, "trend_score")
    mom = _f(scan, "momentum_score", 50)
    rv = _f(scan, "rsi", 50)
    br = _f(scan, "breakout_score", 50)

    if strategy == "MOMENTUM" and (trend < 35 or mom < 40 or regime == "BEAR"):
        return "Momentum-Exit"
    if strategy == "BREAKOUT" and (br < 35 or trend < 35 or regime == "BEAR"):
        return "Breakout-Exit"
    if strategy == "PULLBACK" and (trend < 35 or rv > 72 or regime == "BEAR"):
        return "Pullback-Exit"
    if strategy == "MEAN_REVERSION" and (rv >= 58 or regime != "NEUTRAL"):
        return "MeanRev-Exit"
    return None


def _equity(cash: float, positions: dict, prices: dict[str, float]) -> float:
    out = float(cash)
    for asset, pos in positions.items():
        out += float(pos["qty"]) * float(prices.get(asset, pos.get("last_price", pos["entry"])))
    return out


def simulate_multi_strategy(
    snapshots: list[dict], *, mode: str = "AUTO_ROUTER", start_capital: float = 1000.0,
    allocation_pct: float = 20.0, fee_pct: float = 0.25, max_positions: int = 2,
    daily_loss_limit_pct: float = 3.0,
) -> MultiStrategyResult:
    if not snapshots:
        return MultiStrategyResult(start_capital, 0, 0, 0, 0, 0, 0, pd.Series(dtype=float), pd.DataFrame())

    cash = float(start_capital)
    fee = float(fee_pct) / 100.0
    alloc = float(allocation_pct) / 100.0
    positions: dict[str, dict] = {}
    last_prices: dict[str, float] = {}
    total_fees = 0.0
    logs: list[dict[str, Any]] = []
    pnl_list: list[float] = []
    eq_points: list[tuple[pd.Timestamp, float]] = []
    day_key = None
    day_start_equity = float(start_capital)
    blocked = False

    for snap in snapshots:
        t = pd.Timestamp(snap["timestamp"])
        scans = list(snap.get("scans", []))
        scan_map = {str(r["asset"]): r for r in scans}
        prices = {str(r["asset"]): _f(r, "price") for r in scans if _f(r, "price") > 0}
        last_prices.update(prices)
        regime = str(snap.get("regime", "NEUTRAL"))

        eq_before = _equity(cash, positions, last_prices)
        today = t.date().isoformat()
        if today != day_key:
            day_key = today
            day_start_equity = eq_before
            blocked = False
        if daily_loss_limit_pct > 0 and eq_before <= day_start_equity * (1 - daily_loss_limit_pct / 100.0):
            blocked = True

        # Exits zuerst.
        for asset, pos in list(positions.items()):
            price = float(prices.get(asset, pos.get("last_price", pos["entry"])))
            pos["last_price"] = price
            reason = exit_reason(scan_map.get(asset), pos, regime, price)
            if reason:
                qty = float(pos["qty"])
                gross = qty * price
                sell_fee = gross * fee
                proceeds = gross - sell_fee
                cash += proceeds
                total_fees += sell_fee
                pnl = proceeds - float(pos["entry_cost_total"])
                pnl_list.append(pnl)
                logs.append({
                    "timestamp": t, "asset": asset, "action": "SELL", "strategy": pos.get("strategy"),
                    "price": price, "pnl_eur": pnl, "reason": reason, "regime": regime,
                })
                del positions[asset]

        eq_mid = _equity(cash, positions, last_prices)
        if daily_loss_limit_pct > 0 and eq_mid <= day_start_equity * (1 - daily_loss_limit_pct / 100.0):
            blocked = True

        slots = max(0, int(max_positions) - len(positions))
        if not blocked and slots > 0:
            candidates = [x for x in strategy_candidates(scans, regime, mode) if str(x[0].get("asset")) not in positions]
            for scan, strategy, edge in candidates[:slots]:
                eq_now = _equity(cash, positions, last_prices)
                budget_total = min(cash, eq_now * alloc)
                if budget_total <= 10:
                    break
                buy_value = budget_total / (1 + fee)
                buy_fee = buy_value * fee
                price = _f(scan, "price")
                qty = buy_value / price
                total_cost = buy_value + buy_fee
                cash -= total_cost
                total_fees += buy_fee
                positions[str(scan["asset"])] = {
                    "qty": qty, "entry": price, "entry_cost_total": total_cost,
                    "last_price": price, "peak_price": price, "strategy": strategy,
                }
                logs.append({
                    "timestamp": t, "asset": scan["asset"], "action": "BUY", "strategy": strategy,
                    "price": price, "pnl_eur": np.nan, "reason": f"{strategy}-Einstieg",
                    "regime": regime, "edge_score": round(edge, 1),
                })

        eq_points.append((t, _equity(cash, positions, last_prices)))

    # Vergleichbar vollständig realisieren.
    end_t = pd.Timestamp(snapshots[-1]["timestamp"])
    for asset, pos in list(positions.items()):
        price = float(last_prices.get(asset, pos["entry"]))
        gross = float(pos["qty"]) * price
        sell_fee = gross * fee
        proceeds = gross - sell_fee
        cash += proceeds
        total_fees += sell_fee
        pnl = proceeds - float(pos["entry_cost_total"])
        pnl_list.append(pnl)
        logs.append({"timestamp": end_t, "asset": asset, "action": "SELL", "strategy": pos.get("strategy"),
                     "price": price, "pnl_eur": pnl, "reason": "Backtest-Ende", "regime": snapshots[-1].get("regime")})
        del positions[asset]
    eq_points.append((end_t, cash))

    equity = pd.Series([v for _, v in eq_points], index=[t for t, _ in eq_points], name="Kontowert", dtype=float)
    final_value = float(cash)
    ret = (final_value / start_capital - 1) * 100 if start_capital else 0.0
    if len(equity):
        peaks = equity.cummax()
        max_dd = abs(float(((equity / peaks - 1) * 100).min()))
    else:
        max_dd = 0.0
    pnl = pd.Series(pnl_list, dtype=float)
    completed = int(len(pnl))
    wins = int((pnl > 0).sum()) if completed else 0
    win_rate = wins / completed * 100 if completed else 0.0
    gp = float(pnl[pnl > 0].sum()) if completed else 0.0
    gl = abs(float(pnl[pnl < 0].sum())) if completed else 0.0
    pf = gp / gl if gl > 0 else (math.inf if gp > 0 else 0.0)
    return MultiStrategyResult(final_value, ret, max_dd, completed, win_rate, pf, total_fees, equity, pd.DataFrame(logs))


def strategy_comparison(snapshots: list[dict], **kwargs) -> pd.DataFrame:
    rows = []
    for mode in ["AUTO_ROUTER", "MOMENTUM", "BREAKOUT", "PULLBACK", "MEAN_REVERSION", "CASH"]:
        r = simulate_multi_strategy(snapshots, mode=mode, **kwargs)
        rows.append({
            "Strategie": STRATEGY_LABELS[mode], "Code": mode, "Rendite %": round(r.return_pct, 2),
            "Drawdown %": round(r.max_drawdown_pct, 2), "Profit Factor": 99.0 if math.isinf(r.profit_factor) else round(r.profit_factor, 2),
            "Trades": r.completed_trades, "Trefferquote %": round(r.win_rate_pct, 1), "Gebühren €": round(r.total_fees, 2),
        })
    return pd.DataFrame(rows).sort_values(["Rendite %", "Drawdown %"], ascending=[False, True]).reset_index(drop=True)


def benchmark_buy_hold_btc(snapshots: list[dict], start_capital: float = 1000.0, fee_pct: float = 0.25) -> dict:
    prices = []
    for snap in snapshots:
        btc = next((r for r in snap.get("scans", []) if r.get("asset") == "BTC" and _f(r, "price") > 0), None)
        if btc:
            prices.append((pd.Timestamp(snap["timestamp"]), _f(btc, "price")))
    if len(prices) < 2:
        return {"Strategie": "BTC Buy & Hold", "Rendite %": 0.0, "Drawdown %": 0.0, "Profit Factor": np.nan, "Trades": 1, "Gebühren €": 0.0}
    fee = fee_pct / 100.0
    buy_value = start_capital / (1 + fee)
    buy_fee = buy_value * fee
    qty = buy_value / prices[0][1]
    values = [qty * p for _, p in prices]
    final_gross = qty * prices[-1][1]
    sell_fee = final_gross * fee
    final = final_gross - sell_fee
    ser = pd.Series(values, index=[t for t, _ in prices], dtype=float)
    max_dd = abs(float(((ser / ser.cummax() - 1) * 100).min())) if len(ser) else 0.0
    return {"Strategie": "BTC Buy & Hold", "Rendite %": round((final/start_capital-1)*100, 2), "Drawdown %": round(max_dd, 2),
            "Profit Factor": np.nan, "Trades": 1, "Gebühren €": round(buy_fee + sell_fee, 2)}


def benchmark_btc_ema(snapshots: list[dict], start_capital: float = 1000.0, fee_pct: float = 0.25) -> dict:
    fee = fee_pct / 100.0
    cash = float(start_capital)
    qty = 0.0
    entry_cost = 0.0
    pnl_list = []
    eq = []
    fees = 0.0
    last_price = None
    for snap in snapshots:
        btc = next((r for r in snap.get("scans", []) if r.get("asset") == "BTC" and _f(r, "price") > 0), None)
        if not btc:
            continue
        price = _f(btc, "price")
        last_price = price
        trend_on = bool(btc.get("ema50_gt_ema200", False))
        if trend_on and qty <= 0:
            buy_value = cash / (1 + fee)
            buy_fee = buy_value * fee
            qty = buy_value / price
            entry_cost = buy_value + buy_fee
            cash -= entry_cost
            fees += buy_fee
        elif (not trend_on) and qty > 0:
            gross = qty * price
            sell_fee = gross * fee
            proceeds = gross - sell_fee
            pnl_list.append(proceeds - entry_cost)
            cash += proceeds
            fees += sell_fee
            qty = 0.0
            entry_cost = 0.0
        eq.append(cash + qty * price)
    if qty > 0 and last_price:
        gross = qty * last_price
        sell_fee = gross * fee
        proceeds = gross - sell_fee
        pnl_list.append(proceeds - entry_cost)
        cash += proceeds
        fees += sell_fee
    arr = np.asarray(eq + [cash], dtype=float)
    peaks = np.maximum.accumulate(arr) if len(arr) else np.array([start_capital])
    dd = abs(float(np.min((arr/peaks-1)*100))) if len(arr) else 0.0
    pnl = pd.Series(pnl_list, dtype=float)
    gp = float(pnl[pnl > 0].sum()) if len(pnl) else 0.0
    gl = abs(float(pnl[pnl < 0].sum())) if len(pnl) else 0.0
    pf = gp/gl if gl > 0 else (math.inf if gp > 0 else 0.0)
    return {"Strategie": "BTC EMA 50/200", "Rendite %": round((cash/start_capital-1)*100, 2), "Drawdown %": round(dd, 2),
            "Profit Factor": 99.0 if math.isinf(pf) else round(pf, 2), "Trades": int(len(pnl)), "Gebühren €": round(fees, 2)}


def fold_stability(snapshots: list[dict], *, n_blocks: int = 4, **kwargs) -> pd.DataFrame:
    if len(snapshots) < n_blocks * 10:
        raise RuntimeError("Zu wenige Scan-Punkte für den Strategie-Fold-Test.")
    parts = np.array_split(np.arange(len(snapshots)), n_blocks)
    rows = []
    modes = ["AUTO_ROUTER", "MOMENTUM", "BREAKOUT", "PULLBACK", "MEAN_REVERSION"]
    for mode in modes:
        returns = []
        pfs = []
        dds = []
        trades = 0
        for part in parts:
            block = [snapshots[int(i)] for i in part]
            r = simulate_multi_strategy(block, mode=mode, **kwargs)
            returns.append(r.return_pct)
            dds.append(r.max_drawdown_pct)
            pfs.append(3.0 if math.isinf(r.profit_factor) else min(3.0, r.profit_factor))
            trades += r.completed_trades
        profitable = sum(x > 0 for x in returns)
        combined = (float(np.prod(1 + np.asarray(returns)/100.0)) - 1) * 100
        status = "ROBUST" if profitable >= 3 and combined > 0 and min(returns) > -8 and np.mean(pfs) >= 1.05 else ("GEMISCHT" if profitable >= 2 and combined > 0 else "INSTABIL")
        rows.append({
            "Strategie": STRATEGY_LABELS[mode], "Status": status, "Profitable Folds": f"{profitable}/{n_blocks}",
            "Kombiniert %": round(combined, 2), "Ø Fold %": round(float(np.mean(returns)), 2),
            "Schlechtester Fold %": round(float(np.min(returns)), 2), "Max DD %": round(float(np.max(dds)), 2),
            "Ø PF": round(float(np.mean(pfs)), 2), "Trades": trades,
        })
    return pd.DataFrame(rows).sort_values(["Status", "Kombiniert %"], ascending=[True, False]).reset_index(drop=True)
