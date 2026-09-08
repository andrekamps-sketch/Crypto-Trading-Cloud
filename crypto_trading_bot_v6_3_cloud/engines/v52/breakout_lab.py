from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import math
import random
from typing import Any

import numpy as np
import pandas as pd


def _regime_allows_entries(policy: str, regime_code: str) -> bool:
    policy = str(policy or "all")
    regime_code = str(regime_code or "NEUTRAL").upper()
    if policy == "bull_only":
        return regime_code == "BULL"
    if policy == "no_bear":
        return regime_code != "BEAR"
    return True


@dataclass
class BreakoutResult:
    final_value: float
    return_pct: float
    max_drawdown_pct: float
    completed_trades: int
    win_rate_pct: float
    profit_factor: float
    total_fees: float
    equity: pd.Series
    trade_log: pd.DataFrame


def _f(d: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key, default) or default)
    except Exception:
        return float(default)


def baseline_breakout_params() -> dict[str, Any]:
    """Close to the V4.5 breakout rules, with wide/disabled extra filters."""
    return {
        "min_breakout_score": 70.0,
        "min_trend_score": 65.0,
        "min_volume_ratio": 1.0,
        "min_momentum_score": 0.0,
        "rsi_min": 0.0,
        "rsi_max": 78.0,
        "min_return_24h": -1.5,
        "high_distance_min": -8.0,
        "high_distance_max": 5.0,
        "regime_policy": "bull_only",
        "stop_loss_pct": 4.0,
        "take_profit_pct": 12.0,
        "trailing_stop_pct": 5.0,
        "trailing_activation_pct": 6.0,
        "exit_breakout_score": 35.0,
        "exit_trend_score": 35.0,
        "max_holding_hours": 0,
        "cooldown_loss_hours": 0,
        "cooldown_after_exit_hours": 0,
        "confirmation_scans": 1,
        "min_edge_score": 0.0,
        "signal_exit_min_hold_hours": 6,
    }


def _equity(cash: float, positions: dict, prices: dict[str, float]) -> float:
    value = float(cash)
    for asset, pos in positions.items():
        value += float(pos["qty"]) * float(prices.get(asset, pos.get("last_price", pos["entry"])))
    return value


def _proximity_quality(distance: float) -> float:
    # Prefer very close to the prior 30d high without rewarding extreme extensions.
    if -1.5 <= distance <= 1.5:
        return 100.0 - abs(distance) * 12.0
    if -4.0 <= distance < -1.5:
        return 75.0 + (distance + 4.0) * 8.0
    if 1.5 < distance <= 4.0:
        return 80.0 - (distance - 1.5) * 18.0
    return max(0.0, 45.0 - abs(distance) * 5.0)


def _candidate(scan: dict, regime: str, p: dict) -> tuple[bool, float]:
    if _f(scan, "price") <= 0 or str(scan.get("risk_level", "")) == "hoch":
        return False, -1e9
    if not _regime_allows_entries(str(p["regime_policy"]), regime):
        return False, -1e9

    breakout = _f(scan, "breakout_score")
    trend = _f(scan, "trend_score")
    volume_ratio = _f(scan, "volume_ratio", 1.0)
    momentum = _f(scan, "momentum_score", 50.0)
    rsi = _f(scan, "rsi", 50.0)
    r24 = _f(scan, "return_24h_pct")
    dist = _f(scan, "high_30d_distance_pct", 0.0)

    ok = (
        breakout >= float(p["min_breakout_score"])
        and trend >= float(p["min_trend_score"])
        and volume_ratio >= float(p["min_volume_ratio"])
        and momentum >= float(p["min_momentum_score"])
        and float(p["rsi_min"]) <= rsi <= float(p["rsi_max"])
        and r24 >= float(p["min_return_24h"])
        and float(p["high_distance_min"]) <= dist <= float(p["high_distance_max"])
    )
    if not ok:
        return False, -1e9

    volume_score = _f(scan, "volume_score", 50.0)
    prox = _proximity_quality(dist)
    edge = 0.34 * breakout + 0.22 * trend + 0.18 * volume_score + 0.14 * momentum + 0.12 * prox
    if edge < float(p.get("min_edge_score", 0.0)):
        return False, -1e9
    return True, float(edge)



def _entry_context(scan: dict, regime: str, edge: float) -> dict[str, Any]:
    """Snapshot the entry features so later loss analysis uses the actual entry state."""
    return {
        "entry_regime": str(regime or "NEUTRAL"),
        "entry_edge": round(float(edge), 1),
        "entry_breakout_score": _f(scan, "breakout_score"),
        "entry_trend_score": _f(scan, "trend_score"),
        "entry_volume_ratio": _f(scan, "volume_ratio", 1.0),
        "entry_volume_score": _f(scan, "volume_score", 50.0),
        "entry_momentum_score": _f(scan, "momentum_score", 50.0),
        "entry_rsi": _f(scan, "rsi", 50.0),
        "entry_return_24h_pct": _f(scan, "return_24h_pct"),
        "entry_return_7d_pct": _f(scan, "return_7d_pct"),
        "entry_high_30d_distance_pct": _f(scan, "high_30d_distance_pct", 0.0),
        "entry_atr_pct": _f(scan, "atr_pct", 0.0),
    }


def _sell_log_row(*, t: pd.Timestamp, asset: str, price: float, pnl: float,
                  gross_pnl_before_fees: float, sell_fee: float, reason: str,
                  age_h: float, regime: str, pos: dict) -> dict[str, Any]:
    row = {
        "timestamp": t, "asset": asset, "action": "SELL", "price": price,
        "pnl_eur": pnl, "gross_pnl_before_fees": gross_pnl_before_fees,
        "fee_eur": sell_fee, "reason": reason, "holding_hours": round(age_h, 1),
        "regime": regime, "entry_time": pos.get("entry_time"),
        "entry_price": pos.get("entry"),
    }
    for key in (
        "entry_regime", "entry_edge", "entry_breakout_score", "entry_trend_score",
        "entry_volume_ratio", "entry_volume_score", "entry_momentum_score", "entry_rsi",
        "entry_return_24h_pct", "entry_return_7d_pct", "entry_high_30d_distance_pct",
        "entry_atr_pct",
    ):
        row[key] = pos.get(key)
    return row


def simulate_breakout(
    snapshots: list[dict], *, params: dict,
    start_capital: float = 1000.0, allocation_pct: float = 20.0,
    fee_pct: float = 0.25, max_positions: int = 2,
    daily_loss_limit_pct: float = 3.0,
    collect_details: bool = True,
) -> BreakoutResult:
    if not snapshots:
        return BreakoutResult(start_capital, 0.0, 0.0, 0, 0.0, 0.0, 0.0, pd.Series(dtype=float), pd.DataFrame())

    p = baseline_breakout_params()
    p.update(params or {})

    cash = float(start_capital)
    fee = float(fee_pct) / 100.0
    alloc = float(allocation_pct) / 100.0
    positions: dict[str, dict] = {}
    last_prices: dict[str, float] = {}
    cooldown_until: dict[str, pd.Timestamp] = {}
    candidate_streak: dict[str, int] = {}
    total_fees = 0.0
    pnl_list: list[float] = []
    logs: list[dict[str, Any]] = []
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

        # Exits first. Stop/TP/trailing remain active regardless of holding time.
        for asset, pos in list(positions.items()):
            price = float(prices.get(asset, pos.get("last_price", pos["entry"])))
            if price <= 0:
                continue
            pos["last_price"] = price
            pos["peak_price"] = max(float(pos.get("peak_price", pos["entry"])), price)
            entry = float(pos["entry"])
            age_h = max(0.0, (t - pd.Timestamp(pos["entry_time"])).total_seconds() / 3600.0)
            trailing_level = None
            if float(p["trailing_stop_pct"]) > 0 and pos["peak_price"] >= entry * (1 + float(p["trailing_activation_pct"]) / 100.0):
                trailing_level = float(pos["peak_price"]) * (1 - float(p["trailing_stop_pct"]) / 100.0)

            reason = None
            if float(p["stop_loss_pct"]) > 0 and price <= entry * (1 - float(p["stop_loss_pct"]) / 100.0):
                reason = "Stop-Loss"
            elif float(p["take_profit_pct"]) > 0 and price >= entry * (1 + float(p["take_profit_pct"]) / 100.0):
                reason = "Take-Profit"
            elif trailing_level is not None and price <= trailing_level:
                reason = "Trailing-Stop"
            elif int(p["max_holding_hours"]) > 0 and age_h >= int(p["max_holding_hours"]):
                reason = "Max-Haltedauer"
            else:
                scan = scan_map.get(asset)
                if scan is not None and age_h >= float(p.get("signal_exit_min_hold_hours", 6)):
                    if _f(scan, "breakout_score") < float(p["exit_breakout_score"]):
                        reason = "Breakout-Exit"
                    elif _f(scan, "trend_score") < float(p["exit_trend_score"]):
                        reason = "Trend-Exit"

            if reason:
                qty = float(pos["qty"])
                gross = qty * price
                sell_fee = gross * fee
                proceeds = gross - sell_fee
                cash += proceeds
                total_fees += sell_fee
                pnl = proceeds - float(pos["entry_cost_total"])
                pnl_list.append(float(pnl))
                gross_pnl_before_fees = gross - float(pos.get("buy_value", pos["entry_cost_total"]))
                if collect_details:
                    logs.append(_sell_log_row(
                        t=t, asset=asset, price=price, pnl=pnl, gross_pnl_before_fees=gross_pnl_before_fees,
                        sell_fee=sell_fee, reason=reason, age_h=age_h, regime=regime, pos=pos
                    ))
                exit_cd = int(p.get("cooldown_after_exit_hours", 0))
                loss_cd = int(p.get("cooldown_loss_hours", 0)) if pnl < 0 else 0
                cd = max(exit_cd, loss_cd)
                if cd > 0:
                    cooldown_until[asset] = t + pd.Timedelta(hours=cd)
                candidate_streak[asset] = 0
                del positions[asset]

        eq_mid = _equity(cash, positions, last_prices)
        if daily_loss_limit_pct > 0 and eq_mid <= day_start_equity * (1 - daily_loss_limit_pct / 100.0):
            blocked = True

        # Track consecutive qualifying scans. This is intentionally independent of free slots
        # so a setup must stay valid over time before a later entry can occur.
        qualified_now: dict[str, tuple[bool, float]] = {}
        for scan in scans:
            asset = str(scan.get("asset"))
            ok, edge = _candidate(scan, regime, p)
            qualified_now[asset] = (ok, edge)
            candidate_streak[asset] = candidate_streak.get(asset, 0) + 1 if ok else 0

        slots = max(0, int(max_positions) - len(positions))
        if not blocked and slots > 0:
            candidates: list[tuple[dict, float]] = []
            for scan in scans:
                asset = str(scan.get("asset"))
                if asset in positions:
                    continue
                until = cooldown_until.get(asset)
                if until is not None and t < until:
                    continue
                ok, edge = qualified_now.get(asset, (False, -1e9))
                if ok and candidate_streak.get(asset, 0) >= int(p.get("confirmation_scans", 1)):
                    candidates.append((scan, edge))
            candidates.sort(key=lambda x: x[1], reverse=True)

            for scan, edge in candidates[:slots]:
                eq_now = _equity(cash, positions, last_prices)
                budget_total = min(cash, eq_now * alloc)
                if budget_total <= 10:
                    break
                price = _f(scan, "price")
                if price <= 0:
                    continue
                buy_value = budget_total / (1 + fee)
                buy_fee = buy_value * fee
                qty = buy_value / price
                total_cost = buy_value + buy_fee
                cash -= total_cost
                total_fees += buy_fee
                asset = str(scan["asset"])
                entry_ctx = _entry_context(scan, regime, edge)
                positions[asset] = {
                    "qty": qty, "entry": price, "entry_cost_total": total_cost,
                    "buy_value": buy_value, "buy_fee": buy_fee,
                    "last_price": price, "peak_price": price, "entry_time": t,
                    **entry_ctx,
                }
                if collect_details:
                    logs.append({
                        "timestamp": t, "asset": asset, "action": "BUY", "price": price,
                        "pnl_eur": np.nan, "gross_pnl_before_fees": np.nan, "fee_eur": buy_fee,
                        "reason": "Breakout-Einstieg", "holding_hours": np.nan,
                        "regime": regime, **entry_ctx,
                    })

        eq_points.append((t, _equity(cash, positions, last_prices)))

    # Force-close at end for comparable parameter sets.
    end_t = pd.Timestamp(snapshots[-1]["timestamp"])
    for asset, pos in list(positions.items()):
        price = float(last_prices.get(asset, pos["entry"]))
        gross = float(pos["qty"]) * price
        sell_fee = gross * fee
        proceeds = gross - sell_fee
        cash += proceeds
        total_fees += sell_fee
        pnl = proceeds - float(pos["entry_cost_total"])
        pnl_list.append(float(pnl))
        age_h = max(0.0, (end_t - pd.Timestamp(pos["entry_time"])).total_seconds() / 3600.0)
        gross_pnl_before_fees = gross - float(pos.get("buy_value", pos["entry_cost_total"]))
        if collect_details:
            logs.append(_sell_log_row(
                t=end_t, asset=asset, price=price, pnl=pnl, gross_pnl_before_fees=gross_pnl_before_fees,
                sell_fee=sell_fee, reason="Backtest-Ende", age_h=age_h,
                regime=str(snapshots[-1].get("regime", "NEUTRAL")), pos=pos
            ))
        del positions[asset]
    eq_points.append((end_t, cash))

    eq_values = np.asarray([v for _, v in eq_points], dtype=float)
    if collect_details:
        equity = pd.Series(eq_values, index=[t for t, _ in eq_points], name="Kontowert", dtype=float)
    else:
        equity = pd.Series(dtype=float)
    final_value = float(cash)
    ret = (final_value / float(start_capital) - 1) * 100 if start_capital else 0.0
    if len(eq_values):
        peaks = np.maximum.accumulate(eq_values)
        max_dd = abs(float(np.min((eq_values / peaks - 1.0) * 100.0)))
    else:
        max_dd = 0.0

    pnl = pd.Series(pnl_list, dtype=float)
    completed = int(len(pnl))
    wins = int((pnl > 0).sum()) if completed else 0
    win_rate = wins / completed * 100 if completed else 0.0
    gross_profit = float(pnl[pnl > 0].sum()) if completed else 0.0
    gross_loss = abs(float(pnl[pnl < 0].sum())) if completed else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    return BreakoutResult(final_value, ret, max_dd, completed, win_rate, pf, total_fees, equity, pd.DataFrame(logs))


def _pf_cap(v: float) -> float:
    if math.isinf(v):
        return 3.0
    return max(0.0, min(3.0, float(v)))


def _status_from_search(profitable: int, combined: float, worst: float, mean_pf: float, max_dd: float, trades: int) -> str:
    if profitable >= 3 and combined > 0 and worst > -3.0 and mean_pf >= 1.20 and max_dd <= 8.0 and trades >= 8:
        return "STABIL"
    if profitable >= 2 and combined > 0 and mean_pf >= 1.0:
        return "GEMISCHT"
    return "INSTABIL"


def generate_breakout_parameter_sets(n_trials: int, seed: int = 46) -> list[dict]:
    """Randomly sample the parameter space without materializing the enormous Cartesian grid."""
    rng = random.Random(int(seed))
    choices = {
        "min_breakout_score": [75.0, 80.0, 85.0, 90.0],
        "min_trend_score": [65.0, 80.0, 100.0],
        "min_volume_ratio": [1.0, 1.2, 1.5, 1.8, 2.0],
        "min_momentum_score": [55.0, 60.0, 65.0, 70.0, 75.0],
        "rsi_min": [45.0, 50.0, 55.0],
        "rsi_max": [68.0, 72.0, 75.0],
        "min_return_24h": [0.0, 1.0, 2.0, 3.0],
        "high_distance_min": [-2.0, -1.0, 0.0],
        "high_distance_max": [0.75, 1.5, 2.5],
        "regime_policy": ["bull_only", "no_bear"],
        "stop_loss_pct": [3.0, 4.0, 5.0],
        "take_profit_pct": [8.0, 10.0, 12.0, 15.0],
        "trailing_stop_pct": [3.0, 4.0, 5.0],
        "trailing_activation_pct": [4.0, 6.0, 8.0],
        "exit_breakout_score": [30.0, 40.0, 50.0],
        "exit_trend_score": [35.0, 65.0],
        "max_holding_hours": [48, 72, 120, 168],
        "cooldown_loss_hours": [24, 48, 72],
        "cooldown_after_exit_hours": [12, 24, 48],
        "confirmation_scans": [1, 2, 3],
        "min_edge_score": [75.0, 80.0, 85.0, 90.0],
        "signal_exit_min_hold_hours": [12, 24, 36],
    }
    out = [baseline_breakout_params()]
    seen = {tuple(sorted(out[0].items()))}
    attempts = 0
    max_attempts = max(5000, int(n_trials) * 30)
    while len(out) < int(n_trials) and attempts < max_attempts:
        attempts += 1
        p = {k: rng.choice(v) for k, v in choices.items()}
        if float(p["high_distance_min"]) >= float(p["high_distance_max"]):
            continue
        key = tuple(sorted(p.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out



def breakout_audit(result: BreakoutResult, start_capital: float) -> dict[str, float]:
    """Independent arithmetic check for a detailed simulation result."""
    trades = result.trade_log if isinstance(result.trade_log, pd.DataFrame) else pd.DataFrame()
    gross_pnl = 0.0
    logged_fees = 0.0
    if not trades.empty:
        if "gross_pnl_before_fees" in trades.columns:
            gross_pnl = float(pd.to_numeric(trades.loc[trades["action"].astype(str).str.upper()=="SELL", "gross_pnl_before_fees"], errors="coerce").fillna(0).sum())
        if "fee_eur" in trades.columns:
            logged_fees = float(pd.to_numeric(trades["fee_eur"], errors="coerce").fillna(0).sum())
    fees = float(result.total_fees)
    expected = float(start_capital) + gross_pnl - fees
    return {
        "start_capital": float(start_capital),
        "gross_pnl_before_fees": gross_pnl,
        "fees": fees,
        "logged_fees": logged_fees,
        "expected_final": expected,
        "final_value": float(result.final_value),
        "difference": float(result.final_value) - expected,
    }


def _group_trade_stats(sells: pd.DataFrame, by: str, label: str) -> pd.DataFrame:
    if sells.empty or by not in sells.columns:
        return pd.DataFrame()
    work = sells.copy()
    work[by] = work[by].astype("object").where(work[by].notna(), "–").astype(str)
    work["pnl_eur"] = pd.to_numeric(work.get("pnl_eur"), errors="coerce").fillna(0.0)
    g = work.groupby(by, dropna=False)["pnl_eur"].agg(["count", "sum", "mean"]).reset_index()
    wins = work.assign(_win=work["pnl_eur"] > 0).groupby(by)["_win"].mean().reset_index(name="Trefferquote %")
    g = g.merge(wins, on=by, how="left")
    g["Trefferquote %"] = (g["Trefferquote %"] * 100).round(1)
    g = g.rename(columns={by: label, "count": "Trades", "sum": "Netto P/L €", "mean": "Ø P/L €"})
    g["Netto P/L €"] = g["Netto P/L €"].round(2)
    g["Ø P/L €"] = g["Ø P/L €"].round(2)
    return g.sort_values("Netto P/L €", ascending=True).reset_index(drop=True)


def _binned_stats(sells: pd.DataFrame, source: str, bins, labels, label: str) -> pd.DataFrame:
    if sells.empty or source not in sells.columns:
        return pd.DataFrame()
    work = sells.copy()
    vals = pd.to_numeric(work[source], errors="coerce")
    work[label] = pd.cut(vals, bins=bins, labels=labels, include_lowest=True, right=False)
    work = work.dropna(subset=[label])
    return _group_trade_stats(work, label, label)


def _max_losing_streak(sells: pd.DataFrame) -> int:
    if sells.empty or "pnl_eur" not in sells.columns:
        return 0
    streak = best = 0
    for x in pd.to_numeric(sells.sort_values("timestamp")["pnl_eur"], errors="coerce").fillna(0.0):
        if x < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return int(best)


def _diagnostic_notes(sells: pd.DataFrame, tables: dict[str, pd.DataFrame], worst_return: float) -> list[str]:
    notes: list[str] = []
    if sells.empty:
        return ["Im schlechtesten Block wurden keine abgeschlossenen Trades gefunden; der Block ist deshalb für Ursachenanalyse zu dünn."]

    pnl = pd.to_numeric(sells.get("pnl_eur"), errors="coerce").fillna(0.0)
    total_losses = abs(float(pnl[pnl < 0].sum()))
    losses = int((pnl < 0).sum())
    wins = int((pnl > 0).sum())
    notes.append(f"Der schlechteste Block endete mit {worst_return:+.2f} %. Darin gab es {wins} Gewinner und {losses} Verlierer.")

    coin = tables.get("coin", pd.DataFrame())
    if not coin.empty:
        row = coin.iloc[0]
        loss = float(row["Netto P/L €"])
        if loss < 0:
            share = abs(loss) / total_losses * 100 if total_losses > 0 else 0.0
            notes.append(f"Größter Verlustbeitrag nach Coin: {row['Coin']} mit {loss:+.2f} € ({share:.0f} % der gesamten Verlustsumme vor Gegenrechnung der Gewinner).")

    for key, col, title in [
        ("regime", "Einstiegsphase", "Marktphase"),
        ("reason", "Exit-Grund", "Exit-Grund"),
        ("rsi", "RSI beim Einstieg", "RSI-Bereich"),
        ("volume", "Volumen beim Einstieg", "Volumen-Bereich"),
        ("breakout", "Breakout beim Einstieg", "Breakout-Bereich"),
        ("holding", "Haltedauer", "Haltedauer"),
    ]:
        t = tables.get(key, pd.DataFrame())
        if not t.empty:
            row = t.iloc[0]
            if float(row["Netto P/L €"]) < 0 and int(row["Trades"]) >= 2:
                prefix = "Schwächste" if title == "Marktphase" else "Schwächster"
                notes.append(f"{prefix} {title}: {row[col]} – {int(row['Trades'])} Trades, zusammen {float(row['Netto P/L €']):+.2f} €.")

    streak = _max_losing_streak(sells)
    if streak >= 2:
        notes.append(f"Längste Verlustserie im Block: {streak} Trades in Folge. Das spricht dafür, Cooldown/Marktphasenfilter gezielt gegen Serienverluste zu prüfen statt nur SL/TP enger zu stellen.")
    notes.append("Diese Hinweise sind Diagnose-Hypothesen. V4.8 ändert die Regeln nicht automatisch; ein Filter sollte erst übernommen werden, wenn er in getrennten Zeitblöcken erneut besser abschneidet.")
    return notes


def analyze_worst_search_block(
    snapshots: list[dict], *, params: dict,
    start_capital: float, allocation_pct: float, fee_pct: float,
    max_positions: int, daily_loss_limit_pct: float,
    n_search_blocks: int = 4, holdout_fraction: float = 0.20,
) -> dict[str, Any]:
    """Run the selected Quality-Breakout rules on each older search block with full logs.

    The newest holdout is deliberately excluded. This analysis explains the weakest search block;
    it does not use the holdout to invent a new rule.
    """
    if len(snapshots) < 100:
        raise RuntimeError("Zu wenige Snapshots für die Verlustanalyse.")
    split = max(60, min(len(snapshots) - 30, int(len(snapshots) * (1.0 - float(holdout_fraction)))))
    search = snapshots[:split]
    idx_parts = [part for part in np.array_split(np.arange(len(search)), int(n_search_blocks)) if len(part) >= 10]
    blocks = [[search[int(i)] for i in part] for part in idx_parts]
    common = dict(start_capital=float(start_capital), allocation_pct=float(allocation_pct), fee_pct=float(fee_pct),
                  max_positions=int(max_positions), daily_loss_limit_pct=float(daily_loss_limit_pct))
    detailed = [simulate_breakout(block, params=params, collect_details=True, **common) for block in blocks]

    overview_rows = []
    for i, (block, result) in enumerate(zip(blocks, detailed), start=1):
        overview_rows.append({
            "Block": i,
            "Von": pd.Timestamp(block[0]["timestamp"]).strftime("%Y-%m-%d"),
            "Bis": pd.Timestamp(block[-1]["timestamp"]).strftime("%Y-%m-%d"),
            "Rendite %": round(result.return_pct, 2),
            "Max DD %": round(result.max_drawdown_pct, 2),
            "Profit Factor": round(_pf_cap(result.profit_factor), 2),
            "Trades": int(result.completed_trades),
            "Trefferquote %": round(result.win_rate_pct, 1),
            "Gebühren €": round(result.total_fees, 2),
        })
    overview = pd.DataFrame(overview_rows)
    worst_idx = int(np.argmin([r.return_pct for r in detailed]))
    result = detailed[worst_idx]
    trades = result.trade_log.copy() if isinstance(result.trade_log, pd.DataFrame) else pd.DataFrame()
    sells = trades[trades.get("action", pd.Series(dtype=str)).astype(str).str.upper() == "SELL"].copy() if not trades.empty else pd.DataFrame()
    if not sells.empty:
        sells["timestamp"] = pd.to_datetime(sells["timestamp"], errors="coerce")
        sells["entry_time"] = pd.to_datetime(sells.get("entry_time"), errors="coerce")
        sells["entry_weekday"] = sells["entry_time"].dt.day_name().replace({
            "Monday":"Montag", "Tuesday":"Dienstag", "Wednesday":"Mittwoch", "Thursday":"Donnerstag",
            "Friday":"Freitag", "Saturday":"Samstag", "Sunday":"Sonntag"
        })
        sells["entry_hour"] = sells["entry_time"].dt.hour

    tables: dict[str, pd.DataFrame] = {}
    tables["coin"] = _group_trade_stats(sells, "asset", "Coin")
    tables["reason"] = _group_trade_stats(sells, "reason", "Exit-Grund")
    tables["regime"] = _group_trade_stats(sells, "entry_regime", "Einstiegsphase")
    tables["rsi"] = _binned_stats(sells, "entry_rsi", [-np.inf, 55, 62, 68, 72, 76, np.inf],
                                  ["<55", "55–62", "62–68", "68–72", "72–76", "≥76"], "RSI beim Einstieg")
    tables["volume"] = _binned_stats(sells, "entry_volume_ratio", [-np.inf, 1.2, 1.5, 2.0, 3.0, np.inf],
                                     ["<1,2×", "1,2–1,5×", "1,5–2,0×", "2–3×", "≥3×"], "Volumen beim Einstieg")
    tables["breakout"] = _binned_stats(sells, "entry_breakout_score", [-np.inf, 80, 85, 90, 95, np.inf],
                                       ["<80", "80–85", "85–90", "90–95", "≥95"], "Breakout beim Einstieg")
    tables["holding"] = _binned_stats(sells, "holding_hours", [-np.inf, 12, 24, 48, 72, 120, np.inf],
                                      ["<12 h", "12–24 h", "24–48 h", "48–72 h", "72–120 h", "≥120 h"], "Haltedauer")
    tables["weekday"] = _group_trade_stats(sells, "entry_weekday", "Wochentag")

    notes = _diagnostic_notes(sells, tables, result.return_pct)
    worst_block = blocks[worst_idx]
    return {
        "block_overview": overview,
        "worst_block_number": worst_idx + 1,
        "worst_from": pd.Timestamp(worst_block[0]["timestamp"]),
        "worst_to": pd.Timestamp(worst_block[-1]["timestamp"]),
        "worst_result": result,
        "trades": trades,
        "sells": sells,
        "coin_breakdown": tables["coin"],
        "reason_breakdown": tables["reason"],
        "regime_breakdown": tables["regime"],
        "rsi_breakdown": tables["rsi"],
        "volume_breakdown": tables["volume"],
        "breakout_breakdown": tables["breakout"],
        "holding_breakdown": tables["holding"],
        "weekday_breakdown": tables["weekday"],
        "max_losing_streak": _max_losing_streak(sells),
        "notes": notes,
    }


def optimize_breakout(
    snapshots: list[dict], *, n_trials: int,
    start_capital: float, allocation_pct: float, fee_pct: float,
    max_positions: int, daily_loss_limit_pct: float,
    n_search_blocks: int = 4, holdout_fraction: float = 0.20,
    progress_cb=None, seed: int = 46,
) -> tuple[pd.DataFrame, dict, BreakoutResult | None, pd.DataFrame]:
    """Rank only on the older search period, then evaluate top candidates on an untouched holdout.

    Holdout metrics never influence the ranking order. This is still a historical experiment,
    but it avoids selecting the winner directly on the newest slice.
    """
    if len(snapshots) < 100:
        raise RuntimeError("Zu wenige Snapshots für Breakout-Labor und Holdout.")

    split = max(60, min(len(snapshots) - 30, int(len(snapshots) * (1.0 - float(holdout_fraction)))))
    search = snapshots[:split]
    holdout = snapshots[split:]
    idx_parts = [p for p in np.array_split(np.arange(len(search)), int(n_search_blocks)) if len(p) >= 10]
    blocks = [[search[int(i)] for i in part] for part in idx_parts]
    if len(blocks) < 3:
        raise RuntimeError("Zu wenige Suchblöcke für die Stabilitätsprüfung.")

    params_list = generate_breakout_parameter_sets(n_trials, seed=seed)
    common = dict(start_capital=float(start_capital), allocation_pct=float(allocation_pct), fee_pct=float(fee_pct),
                  max_positions=int(max_positions), daily_loss_limit_pct=float(daily_loss_limit_pct))
    rows: list[dict] = []

    for i, p in enumerate(params_list):
        results = [simulate_breakout(block, params=p, collect_details=False, **common) for block in blocks]
        returns = [r.return_pct for r in results]
        pfs = [_pf_cap(r.profit_factor) for r in results]
        dds = [r.max_drawdown_pct for r in results]
        trades = sum(r.completed_trades for r in results)
        profitable = int(sum(x > 0 for x in returns))
        combined = (float(np.prod(1 + np.asarray(returns) / 100.0)) - 1.0) * 100.0
        worst = float(np.min(returns))
        avg_ret = float(np.mean(returns))
        std_ret = float(np.std(returns))
        max_dd = float(np.max(dds))
        mean_pf = float(np.mean(pfs))
        trade_penalty = max(0, 8 - trades) * 1.25
        churn_penalty = max(0, trades - 55) * 0.08
        # Quality-first rank: consistency dominates raw return. Holdout data never enters here.
        stability_score = (
            profitable * 12.0 + 1.10 * combined + 1.00 * worst + 0.20 * avg_ret
            - 0.55 * std_ret - 0.60 * max_dd + 2.0 * mean_pf - trade_penalty - churn_penalty
        )
        status = _status_from_search(profitable, combined, worst, mean_pf, max_dd, trades)
        rows.append({
            "Stabilitäts-Score": round(stability_score, 2), "Such-Status": status,
            "Positive Suchblöcke": f"{profitable}/{len(blocks)}", "Such-Kombiniert %": round(combined, 2),
            "Ø Suchblock %": round(avg_ret, 2), "Schlechtester Suchblock %": round(worst, 2),
            "Max Such-DD %": round(max_dd, 2), "Ø Such-PF": round(mean_pf, 2), "Such-Trades": int(trades),
            "Breakout ≥": p["min_breakout_score"], "Trend ≥": p["min_trend_score"],
            "Volumen x ≥": p["min_volume_ratio"], "Momentum ≥": p["min_momentum_score"],
            "Edge ≥": p["min_edge_score"], "Bestätigungen": p["confirmation_scans"],
            "RSI min": p["rsi_min"], "RSI max": p["rsi_max"], "24h % ≥": p["min_return_24h"],
            "Abstand Hoch min %": p["high_distance_min"], "Abstand Hoch max %": p["high_distance_max"],
            "Marktfilter": p["regime_policy"], "SL %": p["stop_loss_pct"], "TP %": p["take_profit_pct"],
            "Trail %": p["trailing_stop_pct"], "Trail ab %": p["trailing_activation_pct"],
            "Breakout Exit <": p["exit_breakout_score"], "Trend Exit <": p["exit_trend_score"],
            "Signal-Exit min h": p["signal_exit_min_hold_hours"], "Max Halt h": p["max_holding_hours"],
            "Cooldown Exit h": p["cooldown_after_exit_hours"], "Cooldown Verlust h": p["cooldown_loss_hours"],
            "_params": p,
        })
        if progress_cb is not None:
            progress_cb((i + 1) / max(1, len(params_list)) * 0.85)

    df = pd.DataFrame(rows).sort_values(["Stabilitäts-Score", "Such-Kombiniert %"], ascending=[False, False]).reset_index(drop=True)
    df.insert(0, "Rang", range(1, len(df) + 1))

    # Evaluate only the top candidates on holdout, but DO NOT reorder by holdout.
    top_n = min(12, len(df))
    holdout_rows = []
    best_holdout_result: BreakoutResult | None = None
    for j in range(top_n):
        p = df.loc[j, "_params"]
        hr = simulate_breakout(holdout, params=p, **common)
        pf = _pf_cap(hr.profit_factor)
        holdout_rows.append({
            "Rang": int(df.loc[j, "Rang"]), "Holdout %": round(hr.return_pct, 2),
            "Holdout DD %": round(hr.max_drawdown_pct, 2), "Holdout PF": round(pf, 2),
            "Holdout Trades": int(hr.completed_trades), "Holdout Treffer %": round(hr.win_rate_pct, 1),
        })
        df.loc[j, "Holdout %"] = round(hr.return_pct, 2)
        df.loc[j, "Holdout DD %"] = round(hr.max_drawdown_pct, 2)
        df.loc[j, "Holdout PF"] = round(pf, 2)
        df.loc[j, "Holdout Trades"] = int(hr.completed_trades)
        if j == 0:
            best_holdout_result = hr
        if progress_cb is not None:
            progress_cb(0.85 + (j + 1) / max(1, top_n) * 0.15)

    best = df.iloc[0]
    hret = float(best.get("Holdout %", np.nan))
    hpf = float(best.get("Holdout PF", np.nan))
    hdd = float(best.get("Holdout DD %", np.nan))
    htrades = int(best.get("Holdout Trades", 0) or 0)
    search_status = str(best["Such-Status"])
    if search_status == "STABIL" and hret > 0 and hpf >= 1.20 and hdd <= 8 and htrades >= 3:
        final_status = "ROBUST-KANDIDAT"
    elif search_status in {"STABIL", "GEMISCHT"} and hret > -3:
        final_status = "GEMISCHT"
    else:
        final_status = "INSTABIL"

    best_params = dict(best["_params"])
    summary = {
        "status": final_status,
        "search_status": search_status,
        "positive_search_blocks": str(best["Positive Suchblöcke"]),
        "search_combined_pct": float(best["Such-Kombiniert %"]),
        "search_worst_pct": float(best["Schlechtester Suchblock %"]),
        "search_max_dd_pct": float(best["Max Such-DD %"]),
        "search_mean_pf": float(best["Ø Such-PF"]),
        "holdout_return_pct": hret,
        "holdout_drawdown_pct": hdd,
        "holdout_pf": hpf,
        "holdout_trades": htrades,
        "best_params": best_params,
        "search_points": len(search),
        "holdout_points": len(holdout),
    }

    display_df = df.drop(columns=["_params"])
    return display_df, summary, best_holdout_result, pd.DataFrame(holdout_rows)
