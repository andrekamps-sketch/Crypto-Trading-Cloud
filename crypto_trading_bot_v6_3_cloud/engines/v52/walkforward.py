from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np
import pandas as pd

from optimizer import SimResult, generate_parameter_sets, simulate_snapshots


@dataclass
class WalkForwardSummary:
    status: str
    profitable_folds: int
    folds: int
    profitable_share_pct: float
    compounded_return_pct: float
    mean_test_return_pct: float
    median_test_return_pct: float
    worst_test_return_pct: float
    max_test_drawdown_pct: float
    average_test_pf: float
    total_test_trades: int


def _pf_score(pf: float) -> float:
    if math.isinf(pf):
        return 3.0
    return max(0.0, min(3.0, float(pf)))


def _train_score(r: SimResult) -> float:
    trade_penalty = max(0, 10 - int(r.completed_trades)) * 0.7
    return float(r.return_pct) - 0.55 * float(r.max_drawdown_pct) + 1.1 * _pf_score(r.profit_factor) - trade_penalty


def _split_blocks(snapshots: list[dict], n_blocks: int = 6) -> list[list[dict]]:
    if len(snapshots) < n_blocks * 12:
        raise RuntimeError("Zu wenige historische Scan-Punkte für Walk-Forward. Längeren Zeitraum oder kürzeres Scan-Intervall wählen.")
    idx = np.array_split(np.arange(len(snapshots)), n_blocks)
    return [[snapshots[int(i)] for i in part] for part in idx if len(part)]


def walk_forward_optimize(
    snapshots: list[dict],
    *,
    n_trials: int,
    start_capital: float,
    allocation_pct: float,
    fee_pct: float,
    max_positions: int,
    daily_loss_limit_pct: float,
    current_params: dict,
    n_blocks: int = 6,
    progress_cb=None,
    seed: int = 42,
) -> tuple[pd.DataFrame, WalkForwardSummary]:
    """Anchored walk-forward optimization.

    History is split into blocks. The first two blocks train the first rule set,
    which is then tested only on the next unseen block. The training window then
    expands and the procedure repeats. This is closer to how periodic re-tuning
    would have behaved historically than one fixed 70/30 split.
    """
    blocks = _split_blocks(snapshots, int(n_blocks))
    if len(blocks) < 5:
        raise RuntimeError("Mindestens fünf Zeitblöcke werden benötigt.")

    params_list = generate_parameter_sets(int(n_trials), seed=int(seed), include=current_params)
    common = dict(
        start_capital=float(start_capital),
        allocation_pct=float(allocation_pct),
        fee_pct=float(fee_pct),
        max_positions=int(max_positions),
        daily_loss_limit_pct=float(daily_loss_limit_pct),
    )

    rows: list[dict] = []
    total_steps = max(1, (len(blocks) - 2) * len(params_list))
    step_no = 0

    for test_block_idx in range(2, len(blocks)):
        train = [snap for block in blocks[:test_block_idx] for snap in block]
        test = list(blocks[test_block_idx])
        ranked: list[tuple[float, dict, SimResult]] = []
        for p in params_list:
            tr = simulate_snapshots(train, **common, **p)
            ranked.append((_train_score(tr), p, tr))
            step_no += 1
            if progress_cb is not None and (step_no % max(1, total_steps // 100) == 0 or step_no == total_steps):
                progress_cb(step_no / total_steps)
        ranked.sort(key=lambda x: x[0], reverse=True)
        _, best_p, best_train = ranked[0]
        test_result = simulate_snapshots(test, **common, **best_p)
        start_ts = pd.Timestamp(test[0]["timestamp"])
        end_ts = pd.Timestamp(test[-1]["timestamp"])
        pf = 99.0 if math.isinf(test_result.profit_factor) else float(test_result.profit_factor)
        rows.append({
            "Fold": len(rows) + 1,
            "Test von": start_ts.strftime("%Y-%m-%d"),
            "Test bis": end_ts.strftime("%Y-%m-%d"),
            "Test %": round(test_result.return_pct, 2),
            "Test DD %": round(test_result.max_drawdown_pct, 2),
            "Test PF": round(pf, 2),
            "Test Trades": int(test_result.completed_trades),
            "Training %": round(best_train.return_pct, 2),
            "Training DD %": round(best_train.max_drawdown_pct, 2),
            "Entry Score": best_p["minimum_score"],
            "Exit Score": best_p["exit_score"],
            "SL %": best_p["stop_loss_pct"],
            "TP %": best_p["take_profit_pct"],
            "Trail %": best_p["trailing_stop_pct"],
            "Trail ab %": best_p["trailing_activation_pct"],
            "Regime-Filter": best_p.get("regime_policy", "all"),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("Walk-Forward konnte keine Test-Folds erzeugen.")

    returns = df["Test %"].astype(float).to_numpy()
    profitable = int((returns > 0).sum())
    share = profitable / len(returns) * 100.0
    compounded = (float(np.prod(1.0 + returns / 100.0)) - 1.0) * 100.0
    pfs = df["Test PF"].astype(float).replace(99.0, 3.0).clip(upper=3.0)
    mean_pf = float(pfs.mean()) if len(pfs) else 0.0
    worst = float(returns.min())
    max_dd = float(df["Test DD %"].astype(float).max())
    total_trades = int(df["Test Trades"].sum())

    if share >= 75 and compounded > 0 and worst > -8 and mean_pf >= 1.05 and total_trades >= 15:
        status = "ROBUST"
    elif share >= 50 and compounded > 0 and worst > -15:
        status = "GEMISCHT"
    else:
        status = "INSTABIL"

    summary = WalkForwardSummary(
        status=status,
        profitable_folds=profitable,
        folds=len(returns),
        profitable_share_pct=share,
        compounded_return_pct=compounded,
        mean_test_return_pct=float(np.mean(returns)),
        median_test_return_pct=float(np.median(returns)),
        worst_test_return_pct=worst,
        max_test_drawdown_pct=max_dd,
        average_test_pf=mean_pf,
        total_test_trades=total_trades,
    )
    return df, summary


def compare_regime_policies(
    snapshots: list[dict],
    *,
    params: dict,
    start_capital: float,
    allocation_pct: float,
    fee_pct: float,
    max_positions: int,
    daily_loss_limit_pct: float,
) -> pd.DataFrame:
    rows = []
    common = dict(
        start_capital=float(start_capital), allocation_pct=float(allocation_pct), fee_pct=float(fee_pct),
        max_positions=int(max_positions), daily_loss_limit_pct=float(daily_loss_limit_pct),
    )
    labels = {
        "all": "Alle Phasen",
        "no_bear": "Bärenmarkt meiden",
        "bull_only": "Nur bullisch",
    }
    base = {k: v for k, v in params.items() if k != "regime_policy"}
    for policy in ["all", "no_bear", "bull_only"]:
        r = simulate_snapshots(snapshots, **common, **base, regime_policy=policy)
        rows.append({
            "Marktphasen-Filter": labels[policy],
            "Rendite %": round(r.return_pct, 2),
            "Drawdown %": round(r.max_drawdown_pct, 2),
            "Profit Factor": 99.0 if math.isinf(r.profit_factor) else round(r.profit_factor, 2),
            "Trades": int(r.completed_trades),
            "Gebühren €": round(r.total_fees, 2),
        })
    return pd.DataFrame(rows).sort_values(["Rendite %", "Drawdown %"], ascending=[False, True]).reset_index(drop=True)


def regime_distribution(snapshots: Iterable[dict]) -> pd.DataFrame:
    vals = [str(s.get("regime", "NEUTRAL")) for s in snapshots]
    if not vals:
        return pd.DataFrame()
    ser = pd.Series(vals).value_counts()
    labels = {"BULL": "🟢 Bullisch", "NEUTRAL": "🟡 Neutral", "BEAR": "🔴 Bärisch"}
    total = float(ser.sum())
    rows = []
    for code in ["BULL", "NEUTRAL", "BEAR"]:
        n = int(ser.get(code, 0))
        rows.append({"Marktphase": labels[code], "Scan-Punkte": n, "Anteil %": round(n / total * 100.0, 1) if total else 0.0})
    return pd.DataFrame(rows)
