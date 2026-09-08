from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class PerformanceSummary:
    completed_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    net_pnl: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    max_drawdown_pct: float
    total_fees: float
    avg_holding_hours: float
    best_coin: str
    best_coin_pnl: float
    worst_coin: str
    worst_coin_pnl: float


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def analyze_trade_log(trades: pd.DataFrame, start_capital: float = 1000.0) -> PerformanceSummary:
    if trades is None or trades.empty:
        return PerformanceSummary(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "–", 0.0, "–", 0.0)

    df = trades.copy()
    if "action" not in df.columns:
        return PerformanceSummary(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "–", 0.0, "–", 0.0)

    sells = df[df["action"].astype(str).str.upper() == "SELL"].copy()
    pnl = _num(sells["pnl_eur"]) if "pnl_eur" in sells.columns else pd.Series(dtype=float)
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    completed = int(len(sells))
    win_rate = wins / completed * 100 if completed else 0.0
    avg_win = float(pnl[pnl > 0].mean()) if wins else 0.0
    avg_loss = float(pnl[pnl < 0].mean()) if losses else 0.0
    gross_profit = float(pnl[pnl > 0].sum()) if wins else 0.0
    gross_loss = abs(float(pnl[pnl < 0].sum())) if losses else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)

    equity = [float(start_capital)]
    if "equity_after" in df.columns:
        vals = pd.to_numeric(df["equity_after"], errors="coerce").dropna().tolist()
        equity.extend(float(x) for x in vals if float(x) > 0)
    max_dd = 0.0
    if equity:
        arr = np.asarray(equity, dtype=float)
        peaks = np.maximum.accumulate(arr)
        dd = np.where(peaks > 0, (arr / peaks - 1.0) * 100.0, 0.0)
        max_dd = abs(float(dd.min()))

    total_fees = float(_num(df["fee_eur"]).sum()) if "fee_eur" in df.columns else 0.0
    holding = pd.to_numeric(sells.get("holding_hours", pd.Series(dtype=float)), errors="coerce").dropna()
    avg_holding = float(holding.mean()) if not holding.empty else 0.0

    best_coin, best_pnl, worst_coin, worst_pnl = "–", 0.0, "–", 0.0
    if completed and "asset" in sells.columns:
        temp = sells.assign(_pnl=pnl.values).groupby("asset", dropna=False)["_pnl"].sum().sort_values(ascending=False)
        if not temp.empty:
            best_coin, best_pnl = str(temp.index[0]), float(temp.iloc[0])
            worst_coin, worst_pnl = str(temp.index[-1]), float(temp.iloc[-1])

    return PerformanceSummary(
        completed_trades=completed,
        wins=wins,
        losses=losses,
        win_rate_pct=win_rate,
        net_pnl=float(pnl.sum()) if completed else 0.0,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=float(profit_factor),
        max_drawdown_pct=max_dd,
        total_fees=total_fees,
        avg_holding_hours=avg_holding,
        best_coin=best_coin,
        best_coin_pnl=best_pnl,
        worst_coin=worst_coin,
        worst_coin_pnl=worst_pnl,
    )


def coin_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or trades.empty or "action" not in trades.columns or "asset" not in trades.columns:
        return pd.DataFrame()
    sells = trades[trades["action"].astype(str).str.upper() == "SELL"].copy()
    if sells.empty:
        return pd.DataFrame()
    sells["pnl_eur"] = pd.to_numeric(sells.get("pnl_eur"), errors="coerce").fillna(0.0)
    out = sells.groupby("asset").agg(
        Trades=("pnl_eur", "size"),
        Gewinn_EUR=("pnl_eur", "sum"),
        Ø_Trade_EUR=("pnl_eur", "mean"),
        Trefferquote=("pnl_eur", lambda s: (s > 0).mean() * 100),
    ).reset_index().rename(columns={"asset": "Coin"})
    return out.sort_values("Gewinn_EUR", ascending=False)
