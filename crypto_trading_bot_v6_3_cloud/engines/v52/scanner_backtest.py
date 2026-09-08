from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np
import pandas as pd

from scanner import score_dataframe, market_regime_from_scan, regime_allows_entries


@dataclass
class ScannerBacktestResult:
    final_value: float
    return_pct: float
    max_drawdown_pct: float
    completed_trades: int
    win_rate_pct: float
    profit_factor: float
    total_fees: float
    equity: pd.Series
    trade_log: pd.DataFrame


def _equity(cash: float, positions: dict, prices: dict[str, float]) -> float:
    value = float(cash)
    for asset, pos in positions.items():
        value += float(pos["qty"]) * float(prices.get(asset, pos.get("last_price", pos["entry"])))
    return value


def run_scanner_backtest(
    history_by_asset: Dict[str, pd.DataFrame],
    *,
    start_capital: float = 1000.0,
    allocation_pct: float = 20.0,
    fee_pct: float = 0.25,
    stop_loss_pct: float = 4.0,
    take_profit_pct: float = 8.0,
    minimum_score: float = 70.0,
    exit_score: float = 45.0,
    max_positions: int = 2,
    daily_loss_limit_pct: float = 3.0,
    trailing_stop_pct: float = 3.0,
    trailing_activation_pct: float = 4.0,
    scan_every_hours: int = 6,
    test_days: int = 90,
    regime_policy: str = "all",
) -> ScannerBacktestResult:
    clean = {a: df.sort_index().dropna(subset=["Close"]).copy() for a, df in history_by_asset.items() if df is not None and len(df) >= 300}
    if len(clean) < 2:
        raise RuntimeError("Für den Scanner-Backtest werden mindestens zwei Coins mit genügend 1h-Daten benötigt.")

    common = None
    for df in clean.values():
        idx = pd.DatetimeIndex(df.index)
        common = idx if common is None else common.intersection(idx)
    common = common.sort_values()
    if len(common) < 300:
        raise RuntimeError("Zu wenige gemeinsame Zeitpunkte in den Kursdaten.")

    sim_start = common[-1] - pd.Timedelta(days=int(test_days))
    eligible = common[common >= sim_start]
    if len(eligible) < 24:
        raise RuntimeError("Der gewählte Testzeitraum enthält zu wenige Daten.")
    step = max(1, int(scan_every_hours))
    timestamps = eligible[::step]

    cash = float(start_capital)
    positions: dict[str, dict] = {}
    fee = float(fee_pct) / 100.0
    alloc = float(allocation_pct) / 100.0
    sl = float(stop_loss_pct) / 100.0
    tp = float(take_profit_pct) / 100.0
    trail = float(trailing_stop_pct) / 100.0
    trail_activation = float(trailing_activation_pct) / 100.0
    realized = 0.0
    total_fees = 0.0
    log: list[dict] = []
    eq_points: list[tuple[pd.Timestamp, float]] = []
    day_key = None
    day_start_equity = float(start_capital)
    blocked_today = False
    last_prices: dict[str, float] = {}

    for t in timestamps:
        scans = []
        prices: dict[str, float] = {}
        for asset, df in clean.items():
            hist = df.loc[:t].tail(900)
            if len(hist) < 220:
                continue
            try:
                result = score_dataframe(asset, asset, hist)
            except Exception:
                continue
            scans.append(result)
            prices[asset] = float(result.price)
        scans.sort(key=lambda r: r.score, reverse=True)
        scan_map = {r.asset: r for r in scans}
        benchmark = scan_map.get("BTC") or (scans[0] if scans else None)
        regime = market_regime_from_scan(benchmark)
        regime_ok = regime_allows_entries(regime_policy, regime["code"])
        last_prices.update(prices)

        eq_before = _equity(cash, positions, last_prices)
        this_day = pd.Timestamp(t).date().isoformat()
        if this_day != day_key:
            day_key = this_day
            day_start_equity = eq_before
            blocked_today = False
        if daily_loss_limit_pct > 0 and eq_before <= day_start_equity * (1 - daily_loss_limit_pct / 100.0):
            blocked_today = True

        # Exits first.
        for asset, pos in list(positions.items()):
            price = float(prices.get(asset, pos.get("last_price", pos["entry"])))
            if price <= 0:
                continue
            pos["last_price"] = price
            pos["peak_price"] = max(float(pos.get("peak_price", pos["entry"])), price)
            entry = float(pos["entry"])
            scan = scan_map.get(asset)
            score = float(scan.score) if scan else 0.0
            reason = None
            trailing_level = None
            if trail > 0 and pos["peak_price"] >= entry * (1 + trail_activation):
                trailing_level = float(pos["peak_price"]) * (1 - trail)
            if sl > 0 and price <= entry * (1 - sl):
                reason = "Stop-Loss"
            elif tp > 0 and price >= entry * (1 + tp):
                reason = "Take-Profit"
            elif trailing_level is not None and price <= trailing_level:
                reason = "Trailing-Stop"
            elif scan and scan.score < exit_score:
                reason = "Score-Exit"

            if reason:
                qty = float(pos["qty"])
                gross = qty * price
                sell_fee = gross * fee
                proceeds = gross - sell_fee
                cash += proceeds
                total_fees += sell_fee
                pnl = proceeds - float(pos["entry_cost_total"])
                realized += pnl
                holding_hours = (pd.Timestamp(t) - pd.Timestamp(pos["opened_at"])).total_seconds() / 3600.0
                log.append({
                    "timestamp": t, "asset": asset, "action": "SELL", "price": price, "qty": qty,
                    "fee_eur": sell_fee, "pnl_eur": pnl, "reason": reason, "score": score,
                    "entry_score": pos.get("entry_score"), "holding_hours": holding_hours, "market_regime": regime["code"],
                })
                del positions[asset]

        eq_mid = _equity(cash, positions, last_prices)
        if daily_loss_limit_pct > 0 and eq_mid <= day_start_equity * (1 - daily_loss_limit_pct / 100.0):
            blocked_today = True

        slots = max(0, int(max_positions) - len(positions))
        if not blocked_today and regime_ok and slots > 0:
            candidates = [
                r for r in scans
                if r.price > 0 and r.score >= minimum_score and r.signal in {"STARK", "INTERESSANT"}
                and r.asset not in positions and r.risk_level != "hoch"
            ]
            for scan in candidates[:slots]:
                eq_now = _equity(cash, positions, last_prices)
                budget_total = min(cash, eq_now * alloc)
                if budget_total <= 10:
                    break
                buy_value = budget_total / (1 + fee)
                buy_fee = buy_value * fee
                qty = buy_value / float(scan.price)
                total_cost = buy_value + buy_fee
                cash -= total_cost
                total_fees += buy_fee
                positions[scan.asset] = {
                    "qty": qty, "entry": float(scan.price), "entry_cost_total": total_cost,
                    "opened_at": t, "entry_score": float(scan.score), "last_price": float(scan.price),
                    "peak_price": float(scan.price),
                }
                log.append({
                    "timestamp": t, "asset": scan.asset, "action": "BUY", "price": float(scan.price), "qty": qty,
                    "fee_eur": buy_fee, "pnl_eur": np.nan, "reason": "Scanner-Einstieg", "score": float(scan.score),
                    "entry_score": float(scan.score), "holding_hours": np.nan, "market_regime": regime["code"],
                })

        eq_points.append((pd.Timestamp(t), _equity(cash, positions, last_prices)))

    # Close remaining positions at the last known scan so final value is fully realized/comparable.
    if timestamps.size:
        t = pd.Timestamp(timestamps[-1])
        for asset, pos in list(positions.items()):
            price = float(last_prices.get(asset, pos["entry"]))
            qty = float(pos["qty"])
            gross = qty * price
            sell_fee = gross * fee
            proceeds = gross - sell_fee
            cash += proceeds
            total_fees += sell_fee
            pnl = proceeds - float(pos["entry_cost_total"])
            realized += pnl
            holding_hours = (t - pd.Timestamp(pos["opened_at"])).total_seconds() / 3600.0
            log.append({
                "timestamp": t, "asset": asset, "action": "SELL", "price": price, "qty": qty,
                "fee_eur": sell_fee, "pnl_eur": pnl, "reason": "Backtest-Ende", "score": np.nan,
                "entry_score": pos.get("entry_score"), "holding_hours": holding_hours,
            })
            del positions[asset]
        eq_points.append((t, cash))

    equity = pd.Series([v for _, v in eq_points], index=[t for t, _ in eq_points], name="Kontowert", dtype=float)
    final_value = float(cash)
    ret = (final_value / start_capital - 1) * 100 if start_capital else 0.0
    if not equity.empty:
        peaks = equity.cummax()
        drawdown = (equity / peaks - 1.0) * 100.0
        max_dd = abs(float(drawdown.min()))
    else:
        max_dd = 0.0

    trade_df = pd.DataFrame(log)
    sells = trade_df[trade_df["action"] == "SELL"] if not trade_df.empty else pd.DataFrame()
    pnl = pd.to_numeric(sells.get("pnl_eur", pd.Series(dtype=float)), errors="coerce").dropna()
    wins = int((pnl > 0).sum())
    completed = int(len(pnl))
    win_rate = wins / completed * 100 if completed else 0.0
    gross_profit = float(pnl[pnl > 0].sum()) if completed else 0.0
    gross_loss = abs(float(pnl[pnl < 0].sum())) if completed else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    return ScannerBacktestResult(
        final_value=final_value,
        return_pct=ret,
        max_drawdown_pct=max_dd,
        completed_trades=completed,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        total_fees=float(total_fees),
        equity=equity,
        trade_log=trade_df,
    )
