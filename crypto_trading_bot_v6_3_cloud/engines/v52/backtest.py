from __future__ import annotations

from dataclasses import dataclass
import pandas as pd


@dataclass
class BacktestResult:
    strategy: str
    final_value: float
    return_pct: float
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float
    equity: pd.Series
    trade_log: pd.DataFrame


def backtest(
    df: pd.DataFrame,
    signal: pd.Series,
    strategy_name: str,
    start_capital: float = 1000.0,
    allocation_pct: float = 30.0,
    fee_pct: float = 0.25,
    stop_loss_pct: float = 5.0,
    take_profit_pct: float = 10.0,
) -> BacktestResult:
    cash = float(start_capital)
    qty = 0.0
    entry_price = None
    entry_cost_total = 0.0
    entry_time = None
    trade_rows = []
    equity_vals = []
    idx = []
    reentry_blocked = False

    alloc = max(0.01, min(allocation_pct / 100.0, 1.0))
    fee = max(0.0, fee_pct / 100.0)
    sl = max(0.0, stop_loss_pct / 100.0)
    tp = max(0.0, take_profit_pct / 100.0)

    for ts, row in df.iterrows():
        close = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])
        sig = int(signal.loc[ts]) if ts in signal.index and pd.notna(signal.loc[ts]) else 0
        exited_this_bar = False

        if sig == 0:
            reentry_blocked = False

        if qty > 0 and entry_price is not None:
            stop_price = entry_price * (1 - sl) if sl > 0 else None
            target_price = entry_price * (1 + tp) if tp > 0 else None
            exit_price = None
            reason = None

            # Konservativ: Wenn SL und TP in derselben Kerze beruehrt wurden, zaehlt SL zuerst.
            if stop_price is not None and low <= stop_price:
                exit_price, reason = stop_price, "Stop-Loss"
            elif target_price is not None and high >= target_price:
                exit_price, reason = target_price, "Take-Profit"
            elif sig == 0:
                exit_price, reason = close, "Strategie-Exit"

            if exit_price is not None:
                gross = qty * exit_price
                sell_fee = gross * fee
                proceeds = gross - sell_fee
                cash += proceeds
                pnl = proceeds - entry_cost_total
                trade_rows.append({
                    "Einstieg": entry_time,
                    "Ausstieg": ts,
                    "Einstiegspreis": round(entry_price, 2),
                    "Ausstiegspreis": round(exit_price, 2),
                    "Ergebnis EUR": round(pnl, 2),
                    "Ergebnis %": round((pnl / entry_cost_total * 100) if entry_cost_total else 0, 2),
                    "Grund": reason,
                })
                qty = 0.0
                entry_price = None
                entry_cost_total = 0.0
                entry_time = None
                exited_this_bar = True
                if reason in {"Stop-Loss", "Take-Profit"}:
                    reentry_blocked = True

        # Kein Sofort-Wiedereinstieg in derselben Kerze nach einem Exit.
        if qty == 0 and sig == 1 and not exited_this_bar and not reentry_blocked:
            budget_total = cash * alloc
            if budget_total > 5:
                buy_value = budget_total / (1 + fee)
                buy_fee = buy_value * fee
                qty = buy_value / close
                cash -= (buy_value + buy_fee)
                entry_price = close
                entry_cost_total = buy_value + buy_fee
                entry_time = ts

        equity_vals.append(cash + qty * close)
        idx.append(ts)

    equity = pd.Series(equity_vals, index=idx, name=strategy_name)
    final = float(equity.iloc[-1]) if not equity.empty else start_capital
    ret = (final / start_capital - 1) * 100
    peak = equity.cummax()
    drawdown = (equity / peak - 1) * 100
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0

    tdf = pd.DataFrame(trade_rows)
    wins = int((tdf["Ergebnis EUR"] > 0).sum()) if not tdf.empty else 0
    trades = len(tdf)
    win_rate = (wins / trades * 100) if trades else 0.0

    return BacktestResult(
        strategy=strategy_name,
        final_value=final,
        return_pct=ret,
        max_drawdown_pct=max_dd,
        trades=trades,
        win_rate_pct=win_rate,
        equity=equity,
        trade_log=tdf,
    )
