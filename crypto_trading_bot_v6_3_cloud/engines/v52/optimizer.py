from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import math
import random
from typing import Dict, Iterable

import numpy as np
import pandas as pd

from scanner import score_dataframe, market_regime_from_scan, regime_allows_entries


@dataclass
class SimResult:
    final_value: float
    return_pct: float
    max_drawdown_pct: float
    completed_trades: int
    win_rate_pct: float
    profit_factor: float
    total_fees: float


def _equity(cash: float, positions: dict, prices: dict[str, float]) -> float:
    value = float(cash)
    for asset, pos in positions.items():
        value += float(pos["qty"]) * float(prices.get(asset, pos.get("last_price", pos["entry"])))
    return value


def prepare_snapshots(
    history_by_asset: Dict[str, pd.DataFrame],
    *,
    test_days: int,
    scan_every_hours: int,
    progress_cb=None,
) -> list[dict]:
    """Precompute historical scanner scores once so hundreds of parameter sets can be tested quickly."""
    clean = {
        a: df.sort_index().dropna(subset=["Close"]).copy()
        for a, df in history_by_asset.items()
        if df is not None and len(df) >= 300
    }
    if len(clean) < 2:
        raise RuntimeError("Für den Optimierer werden mindestens zwei Coins mit genügend 1h-Daten benötigt.")

    common = None
    for df in clean.values():
        idx = pd.DatetimeIndex(df.index)
        common = idx if common is None else common.intersection(idx)
    common = common.sort_values()
    if len(common) < 300:
        raise RuntimeError("Zu wenige gemeinsame Zeitpunkte in den Kursdaten.")

    sim_start = common[-1] - pd.Timedelta(days=int(test_days))
    eligible = common[common >= sim_start]
    step = max(1, int(scan_every_hours))
    timestamps = eligible[::step]
    if len(timestamps) < 20:
        raise RuntimeError("Zu wenige historische Scan-Zeitpunkte für die Optimierung.")

    snapshots: list[dict] = []
    total = len(timestamps)
    for i, t in enumerate(timestamps):
        scans = []
        for asset, df in clean.items():
            hist = df.loc[:t].tail(900)
            if len(hist) < 220:
                continue
            try:
                r = score_dataframe(asset, asset, hist)
            except Exception:
                continue
            close_hist = hist["Close"].astype(float)
            ema50 = close_hist.ewm(span=50, adjust=False).mean()
            ema200 = close_hist.ewm(span=200, adjust=False).mean()
            lookback = min(len(hist) - 1, 24 * 30)
            prior_high = float(hist["High"].iloc[-lookback-1:-1].max()) if lookback > 10 else float(hist["High"].iloc[:-1].max())
            high_30d_distance_pct = ((float(r.price) / prior_high) - 1.0) * 100.0 if prior_high > 0 else 0.0
            scans.append({
                "asset": r.asset,
                "price": float(r.price),
                "ema50_gt_ema200": bool(float(ema50.iloc[-1]) > float(ema200.iloc[-1])),
                "score": float(r.score),
                "signal": str(r.signal),
                "risk_level": str(r.risk_level),
                "trend_score": float(r.trend_score),
                "momentum_score": float(r.momentum_score),
                "rsi_score": float(r.rsi_score),
                "breakout_score": float(r.breakout_score),
                "volatility_score": float(r.volatility_score),
                "volume_score": float(r.volume_score),
                "rsi": float(r.rsi),
                "atr_pct": float(r.atr_pct),
                "volume_ratio": float(r.volume_ratio),
                "return_24h_pct": float(r.return_24h_pct),
                "return_7d_pct": float(r.return_7d_pct),
                "high_30d_distance_pct": float(high_30d_distance_pct),
            })
        scans.sort(key=lambda r: r["score"], reverse=True)
        if scans:
            benchmark = next((x for x in scans if x.get("asset") == "BTC"), scans[0])
            regime = market_regime_from_scan(benchmark)
            snapshots.append({"timestamp": pd.Timestamp(t), "scans": scans, "regime": regime["code"]})
        if progress_cb is not None and (i % max(1, total // 100) == 0 or i == total - 1):
            progress_cb((i + 1) / total)

    if len(snapshots) < 20:
        raise RuntimeError("Es konnten zu wenige historische Scanner-Snapshots erzeugt werden.")
    return snapshots


def simulate_snapshots(
    snapshots: list[dict],
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
    regime_policy: str = "all",
) -> SimResult:
    if not snapshots:
        return SimResult(start_capital, 0.0, 0.0, 0, 0.0, 0.0, 0.0)

    cash = float(start_capital)
    positions: dict[str, dict] = {}
    fee = float(fee_pct) / 100.0
    alloc = float(allocation_pct) / 100.0
    sl = float(stop_loss_pct) / 100.0
    tp = float(take_profit_pct) / 100.0
    trail = float(trailing_stop_pct) / 100.0
    trail_activation = float(trailing_activation_pct) / 100.0
    total_fees = 0.0
    pnl_list: list[float] = []
    equity_points: list[float] = []
    day_key = None
    day_start_equity = float(start_capital)
    blocked_today = False
    last_prices: dict[str, float] = {}

    for snap in snapshots:
        t = pd.Timestamp(snap["timestamp"])
        scans = snap["scans"]
        scan_map = {r["asset"]: r for r in scans}
        prices = {r["asset"]: float(r["price"]) for r in scans if float(r["price"]) > 0}
        last_prices.update(prices)

        eq_before = _equity(cash, positions, last_prices)
        this_day = t.date().isoformat()
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
            elif scan is not None and float(scan["score"]) < float(exit_score):
                reason = "Score-Exit"

            if reason:
                qty = float(pos["qty"])
                gross = qty * price
                sell_fee = gross * fee
                proceeds = gross - sell_fee
                cash += proceeds
                total_fees += sell_fee
                pnl = proceeds - float(pos["entry_cost_total"])
                pnl_list.append(float(pnl))
                del positions[asset]

        eq_mid = _equity(cash, positions, last_prices)
        if daily_loss_limit_pct > 0 and eq_mid <= day_start_equity * (1 - daily_loss_limit_pct / 100.0):
            blocked_today = True

        slots = max(0, int(max_positions) - len(positions))
        current_regime = str(snap.get("regime", "NEUTRAL"))
        regime_ok = regime_allows_entries(regime_policy, current_regime)
        if not blocked_today and regime_ok and slots > 0:
            candidates = [
                r for r in scans
                if float(r["price"]) > 0
                and float(r["score"]) >= float(minimum_score)
                and r["signal"] in {"STARK", "INTERESSANT"}
                and r["asset"] not in positions
                and r["risk_level"] != "hoch"
            ]
            for scan in candidates[:slots]:
                eq_now = _equity(cash, positions, last_prices)
                budget_total = min(cash, eq_now * alloc)
                if budget_total <= 10:
                    break
                buy_value = budget_total / (1 + fee)
                buy_fee = buy_value * fee
                qty = buy_value / float(scan["price"])
                total_cost = buy_value + buy_fee
                cash -= total_cost
                total_fees += buy_fee
                positions[scan["asset"]] = {
                    "qty": qty,
                    "entry": float(scan["price"]),
                    "entry_cost_total": total_cost,
                    "last_price": float(scan["price"]),
                    "peak_price": float(scan["price"]),
                }

        equity_points.append(_equity(cash, positions, last_prices))

    # Force-close at end for comparable parameter sets.
    for asset, pos in list(positions.items()):
        price = float(last_prices.get(asset, pos["entry"]))
        qty = float(pos["qty"])
        gross = qty * price
        sell_fee = gross * fee
        proceeds = gross - sell_fee
        cash += proceeds
        total_fees += sell_fee
        pnl_list.append(proceeds - float(pos["entry_cost_total"]))
        del positions[asset]
    equity_points.append(float(cash))

    final_value = float(cash)
    ret = (final_value / float(start_capital) - 1) * 100 if start_capital else 0.0
    if equity_points:
        arr = np.asarray(equity_points, dtype=float)
        peaks = np.maximum.accumulate(arr)
        dd = np.where(peaks > 0, (arr / peaks - 1.0) * 100.0, 0.0)
        max_dd = abs(float(dd.min()))
    else:
        max_dd = 0.0

    pnl = pd.Series(pnl_list, dtype=float)
    completed = int(len(pnl))
    wins = int((pnl > 0).sum()) if completed else 0
    win_rate = wins / completed * 100 if completed else 0.0
    gross_profit = float(pnl[pnl > 0].sum()) if completed else 0.0
    gross_loss = abs(float(pnl[pnl < 0].sum())) if completed else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)

    return SimResult(
        final_value=final_value,
        return_pct=float(ret),
        max_drawdown_pct=float(max_dd),
        completed_trades=completed,
        win_rate_pct=float(win_rate),
        profit_factor=float(pf),
        total_fees=float(total_fees),
    )


def _split_snapshots(snapshots: list[dict], train_fraction: float = 0.7):
    split = max(10, min(len(snapshots) - 10, int(len(snapshots) * train_fraction)))
    train = snapshots[:split]
    validation = snapshots[split:]
    thirds = []
    n = len(snapshots)
    cuts = [0, n // 3, 2 * n // 3, n]
    for i in range(3):
        part = snapshots[cuts[i]:cuts[i+1]]
        if len(part) >= 10:
            thirds.append(part)
    return train, validation, thirds


def _pf_for_score(pf: float) -> float:
    if math.isinf(pf):
        return 3.0
    return max(0.0, min(3.0, float(pf)))


def _robust_score(train: SimResult, val: SimResult, segment_returns: list[float]) -> float:
    min_seg = min(segment_returns) if segment_returns else val.return_pct
    dispersion = float(np.std(segment_returns)) if segment_returns else 0.0
    trade_penalty = max(0, 8 - val.completed_trades) * 0.8
    pf_bonus = _pf_for_score(val.profit_factor) * 1.1
    # Validation dominates. Drawdown, instability and too few trades are penalized.
    return (
        val.return_pct
        - 0.60 * val.max_drawdown_pct
        + 0.22 * train.return_pct
        - 0.18 * train.max_drawdown_pct
        + 0.28 * min_seg
        - 0.18 * dispersion
        - 0.10 * abs(train.return_pct - val.return_pct)
        + pf_bonus
        - trade_penalty
    )


def generate_parameter_sets(n_trials: int, seed: int = 42, include: dict | None = None) -> list[dict]:
    grid = list(product(
        [62.0, 65.0, 68.0, 70.0, 72.0, 75.0, 78.0],  # minimum_score
        [35.0, 40.0, 45.0, 50.0, 55.0, 60.0],        # exit_score
        [2.5, 3.0, 4.0, 5.0, 6.0],                    # SL
        [5.0, 6.0, 8.0, 10.0, 12.0],                  # TP
        [0.0, 2.0, 3.0, 4.0, 5.0],                    # trailing
        [2.0, 3.0, 4.0, 5.0, 6.0],                    # trailing activation
        ["all", "no_bear", "bull_only"],               # market regime policy
    ))
    rng = random.Random(int(seed))
    rng.shuffle(grid)
    out = []
    if include:
        out.append({
            "minimum_score": float(include["minimum_score"]),
            "exit_score": float(include["exit_score"]),
            "stop_loss_pct": float(include["stop_loss_pct"]),
            "take_profit_pct": float(include["take_profit_pct"]),
            "trailing_stop_pct": float(include["trailing_stop_pct"]),
            "trailing_activation_pct": float(include["trailing_activation_pct"]),
            "regime_policy": str(include.get("regime_policy", "all")),
        })
    seen = {tuple(out[0].values())} if out else set()
    for combo in grid:
        p = {
            "minimum_score": combo[0], "exit_score": combo[1], "stop_loss_pct": combo[2],
            "take_profit_pct": combo[3], "trailing_stop_pct": combo[4], "trailing_activation_pct": combo[5],
            "regime_policy": combo[6],
        }
        key = tuple(p.values())
        if key in seen:
            continue
        out.append(p); seen.add(key)
        if len(out) >= int(n_trials):
            break
    return out


def optimize_parameters(
    snapshots: list[dict],
    *,
    n_trials: int,
    start_capital: float,
    allocation_pct: float,
    fee_pct: float,
    max_positions: int,
    daily_loss_limit_pct: float,
    current_params: dict,
    progress_cb=None,
    seed: int = 42,
) -> pd.DataFrame:
    train, validation, thirds = _split_snapshots(snapshots, 0.70)
    params_list = generate_parameter_sets(n_trials, seed=seed, include=current_params)
    rows = []

    common = dict(
        start_capital=float(start_capital), allocation_pct=float(allocation_pct), fee_pct=float(fee_pct),
        max_positions=int(max_positions), daily_loss_limit_pct=float(daily_loss_limit_pct),
    )

    for i, p in enumerate(params_list):
        tr = simulate_snapshots(train, **common, **p)
        va = simulate_snapshots(validation, **common, **p)
        seg_results = [simulate_snapshots(seg, **common, **p) for seg in thirds]
        seg_returns = [x.return_pct for x in seg_results]
        score = _robust_score(tr, va, seg_returns)
        pf_val = va.profit_factor
        status = "stabil" if (
            tr.return_pct > 0 and va.return_pct > 0 and min(seg_returns or [0]) > -5
            and (math.isinf(pf_val) or pf_val >= 1.05) and va.completed_trades >= 5
        ) else "gemischt"
        rows.append({
            "Stabilitäts-Score": round(score, 2),
            "Status": status,
            "Validierung %": round(va.return_pct, 2),
            "Validierung DD %": round(va.max_drawdown_pct, 2),
            "Validierung PF": (99.0 if math.isinf(pf_val) else round(pf_val, 2)),
            "Validierung Trades": va.completed_trades,
            "Training %": round(tr.return_pct, 2),
            "Training DD %": round(tr.max_drawdown_pct, 2),
            "Training PF": (99.0 if math.isinf(tr.profit_factor) else round(tr.profit_factor, 2)),
            "Schlechtestes Drittel %": round(min(seg_returns) if seg_returns else va.return_pct, 2),
            "Ø Drittel %": round(float(np.mean(seg_returns)) if seg_returns else va.return_pct, 2),
            "Entry Score": p["minimum_score"],
            "Exit Score": p["exit_score"],
            "SL %": p["stop_loss_pct"],
            "TP %": p["take_profit_pct"],
            "Trail %": p["trailing_stop_pct"],
            "Trail ab %": p["trailing_activation_pct"],
            "Regime-Filter": p.get("regime_policy", "all"),
        })
        if progress_cb is not None:
            progress_cb((i + 1) / len(params_list))

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values(["Stabilitäts-Score", "Validierung %"], ascending=[False, False]).reset_index(drop=True)
    df.insert(0, "Rang", range(1, len(df) + 1))
    return df
