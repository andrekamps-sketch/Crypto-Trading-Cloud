from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math

import numpy as np
import pandas as pd

from breakout_lab import simulate_breakout
from scanner import score_dataframe, market_regime_from_scan


@dataclass
class ForwardResult:
    name: str
    final_value: float
    return_pct: float
    max_drawdown_pct: float
    profit_factor: float
    completed_trades: int
    total_fees: float
    equity: pd.Series
    trade_log: pd.DataFrame
    note: str = ""


def quality_breakout_frozen_params() -> dict[str, Any]:
    """Rules frozen from the V4.7/V4.8 quality-breakout candidate.

    These are intentionally not optimized in V5.2. The forward competition is useful only
    if the rules are fixed before future prices arrive.
    """
    return {
        "min_breakout_score": 75.0,
        "min_trend_score": 65.0,
        "min_volume_ratio": 1.0,
        "min_momentum_score": 55.0,
        "rsi_min": 0.0,
        "rsi_max": 75.0,
        "min_return_24h": 1.0,
        "high_distance_min": -1.0,
        "high_distance_max": 1.5,
        "regime_policy": "no_bear",
        "stop_loss_pct": 3.0,
        "take_profit_pct": 8.0,
        "trailing_stop_pct": 4.0,
        "trailing_activation_pct": 8.0,
        "exit_breakout_score": 35.0,
        "exit_trend_score": 35.0,
        "max_holding_hours": 120,
        "cooldown_loss_hours": 72,
        "cooldown_after_exit_hours": 12,
        "confirmation_scans": 1,
        "min_edge_score": 80.0,
        "signal_exit_min_hold_hours": 12,
    }


def _naive_index(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    idx = pd.to_datetime(x.index)
    try:
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
    except Exception:
        try:
            idx = idx.tz_localize(None)
        except Exception:
            pass
    x.index = idx
    return x[~x.index.duplicated(keep="last")].sort_index()


def _naive_ts(ts) -> pd.Timestamp:
    x = pd.Timestamp(ts)
    try:
        if x.tzinfo is not None:
            x = x.tz_convert("UTC").tz_localize(None)
    except Exception:
        try:
            x = x.tz_localize(None)
        except Exception:
            pass
    return x


def _pf(values: list[float]) -> float:
    if not values:
        return 0.0
    gp = sum(v for v in values if v > 0)
    gl = abs(sum(v for v in values if v < 0))
    if gl <= 1e-12:
        return math.inf if gp > 0 else 0.0
    return gp / gl


def _max_dd(equity: pd.Series) -> float:
    if equity is None or equity.empty:
        return 0.0
    vals = equity.astype(float)
    peaks = vals.cummax()
    dd = (vals / peaks - 1.0) * 100.0
    return abs(float(dd.min()))


def prepare_forward_breakout_snapshots(
    history_by_asset: dict[str, pd.DataFrame], *, started_at, scan_every_hours: int = 6
) -> list[dict]:
    """Build only post-freeze scanner snapshots while using pre-freeze data for indicators."""
    clean = {}
    for asset, df in history_by_asset.items():
        if df is None or df.empty:
            continue
        x = _naive_index(df).dropna(subset=["Close"])
        if len(x) >= 240:
            clean[asset] = x
    if len(clean) < 2:
        raise RuntimeError("Zu wenige Coins mit genügend Warmup-Daten für Quality Breakout.")

    common = None
    for df in clean.values():
        common = df.index if common is None else common.intersection(df.index)
    common = pd.DatetimeIndex(common).sort_values()
    start = _naive_ts(started_at)
    eligible = common[common >= start]
    if len(eligible) == 0:
        return []

    # yfinance 1h data: scan every N hourly observations starting with first post-freeze point.
    step = max(1, int(scan_every_hours))
    timestamps = eligible[::step]
    snapshots: list[dict] = []
    for t in timestamps:
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
            prior_slice = hist["High"].iloc[-lookback-1:-1] if lookback > 10 else hist["High"].iloc[:-1]
            prior_high = float(prior_slice.max()) if len(prior_slice) else float(hist["High"].iloc[-1])
            dist = ((float(r.price) / prior_high) - 1.0) * 100.0 if prior_high > 0 else 0.0
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
                "high_30d_distance_pct": float(dist),
            })
        scans.sort(key=lambda z: z["score"], reverse=True)
        if scans:
            benchmark = next((x for x in scans if x.get("asset") == "BTC"), scans[0])
            regime = market_regime_from_scan(benchmark)
            snapshots.append({"timestamp": pd.Timestamp(t), "scans": scans, "regime": regime["code"]})
    return snapshots


def _quality_result(
    history_by_asset: dict[str, pd.DataFrame], *, started_at, scan_hours: int,
    start_capital: float, fee_pct: float, quality_params: dict[str, Any] | None = None,
) -> ForwardResult:
    snaps = prepare_forward_breakout_snapshots(history_by_asset, started_at=started_at, scan_every_hours=scan_hours)
    if len(snaps) < 2:
        raise RuntimeError("Quality Breakout: noch zu wenige neue Scanpunkte seit dem Einfrieren.")
    br = simulate_breakout(
        snaps,
        params=dict(quality_params or quality_breakout_frozen_params()),
        start_capital=float(start_capital), allocation_pct=20.0, fee_pct=float(fee_pct),
        max_positions=2, daily_loss_limit_pct=3.0, collect_details=True,
    )
    log = br.trade_log.copy()
    real_sells = pd.DataFrame()
    if not log.empty and "action" in log.columns:
        real_sells = log[(log["action"].astype(str).str.upper() == "SELL") & (log["reason"].astype(str) != "Backtest-Ende")]
    pnl = pd.to_numeric(real_sells.get("pnl_eur", pd.Series(dtype=float)), errors="coerce").dropna().tolist()
    return ForwardResult(
        name="Quality Breakout", final_value=float(br.final_value), return_pct=float(br.return_pct),
        max_drawdown_pct=float(br.max_drawdown_pct), profit_factor=_pf(pnl),
        completed_trades=int(len(real_sells)), total_fees=float(br.total_fees),
        equity=br.equity, trade_log=log,
        note="Zwischenstand als Liquidationswert; eine offene Position wird für die Vergleichszahl zum letzten Kurs inkl. Gebühr bewertet.",
    )


def _resampled_btc(df: pd.DataFrame, scan_hours: int) -> pd.DataFrame:
    x = _naive_index(df).dropna(subset=["Close"])
    close = x["Close"].astype(float).resample(f"{int(scan_hours)}h").last().dropna()
    out = pd.DataFrame({"price": close})
    out["ema50"] = close.ewm(span=50, adjust=False).mean()
    out["ema200"] = close.ewm(span=200, adjust=False).mean()
    return out


def _ema_result(
    btc_df: pd.DataFrame, *, started_at, scan_hours: int, start_capital: float, fee_pct: float
) -> ForwardResult:
    f = _resampled_btc(btc_df, scan_hours)
    start = _naive_ts(started_at)
    post = f[f.index >= start].copy()
    if post.empty:
        raise RuntimeError("EMA: noch keine neue 6h-Kerze seit dem Start.")
    fee_rate = float(fee_pct) / 100.0
    cash = float(start_capital)
    qty = 0.0
    cost_basis = 0.0
    total_fees = 0.0
    realized: list[float] = []
    logs: list[dict[str, Any]] = []
    eq_rows: list[tuple[pd.Timestamp, float]] = []

    for ts, r in post.iterrows():
        px = float(r["price"])
        eligible = px > float(r["ema200"]) and float(r["ema50"]) > float(r["ema200"])
        if eligible and qty <= 0 and cash > 1.0:
            gross = cash / (1.0 + fee_rate)
            buy_fee = gross * fee_rate
            qty = gross / px
            cost_basis = gross + buy_fee
            cash -= cost_basis
            total_fees += buy_fee
            logs.append({"timestamp": ts, "asset": "BTC", "action": "BUY", "price": px, "fee_eur": buy_fee, "pnl_eur": np.nan, "reason": "EMA50>EMA200 + Kurs>EMA200"})
        elif not eligible and qty > 0:
            gross = qty * px
            sell_fee = gross * fee_rate
            proceeds = gross - sell_fee
            pnl = proceeds - cost_basis
            cash += proceeds
            total_fees += sell_fee
            realized.append(pnl)
            logs.append({"timestamp": ts, "asset": "BTC", "action": "SELL", "price": px, "fee_eur": sell_fee, "pnl_eur": pnl, "reason": "EMA-Trend beendet"})
            qty = 0.0; cost_basis = 0.0
        eq_rows.append((ts, cash + qty * px))

    last_px = float(post["price"].iloc[-1])
    # Liquidation value only for fair current comparison; no synthetic SELL in trade count/log.
    if qty > 0:
        gross = qty * last_px
        hypothetical_sell_fee = gross * fee_rate
        final_value = cash + gross - hypothetical_sell_fee
        comparison_fees = total_fees + hypothetical_sell_fee
    else:
        final_value = cash
        comparison_fees = total_fees
    equity = pd.Series([v for _, v in eq_rows], index=[t for t, _ in eq_rows], dtype=float, name="Kontowert")
    if not equity.empty:
        equity.iloc[-1] = final_value
    return ForwardResult(
        name="BTC EMA 50/200", final_value=float(final_value),
        return_pct=(float(final_value) / float(start_capital) - 1.0) * 100.0,
        max_drawdown_pct=_max_dd(equity), profit_factor=_pf(realized),
        completed_trades=len(realized), total_fees=float(comparison_fees),
        equity=equity, trade_log=pd.DataFrame(logs),
        note="EMA50/200 auf 6h-Kursen. Offene BTC-Position wird im Zwischenstand als Liquidationswert inkl. hypothetischer Verkaufsgebühr bewertet.",
    )


def _buy_hold_result(
    btc_df: pd.DataFrame, *, started_at, start_capital: float, fee_pct: float
) -> ForwardResult:
    x = _naive_index(btc_df).dropna(subset=["Close"])
    start = _naive_ts(started_at)
    post = x[x.index >= start]
    if post.empty:
        raise RuntimeError("Buy & Hold: noch kein Kurs nach dem Startzeitpunkt verfügbar.")
    fee_rate = float(fee_pct) / 100.0
    first_px = float(post["Close"].iloc[0]); last_px = float(post["Close"].iloc[-1])
    buy_value = float(start_capital) / (1.0 + fee_rate)
    buy_fee = buy_value * fee_rate
    qty = buy_value / first_px
    gross_last = qty * last_px
    sell_fee = gross_last * fee_rate
    final_value = gross_last - sell_fee
    eq = qty * post["Close"].astype(float)
    if not eq.empty:
        eq.iloc[-1] = final_value
    log = pd.DataFrame([{
        "timestamp": post.index[0], "asset": "BTC", "action": "BUY", "price": first_px,
        "fee_eur": buy_fee, "pnl_eur": np.nan, "reason": "Buy & Hold Start"
    }])
    return ForwardResult(
        name="BTC Buy & Hold", final_value=float(final_value),
        return_pct=(float(final_value) / float(start_capital) - 1.0) * 100.0,
        max_drawdown_pct=_max_dd(eq), profit_factor=0.0, completed_trades=0,
        total_fees=float(buy_fee + sell_fee), equity=eq.rename("Kontowert"), trade_log=log,
        note="Buy & Hold wird als aktueller Liquidationswert inklusive Kauf- und hypothetischer Verkaufsgebühr gezeigt.",
    )


def evaluate_forward_competition(
    history_by_asset: dict[str, pd.DataFrame], *, started_at, scan_hours: int = 6,
    start_capital: float = 1000.0, fee_pct: float = 0.25,
    quality_params: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, ForwardResult], pd.DataFrame]:
    if "BTC" not in history_by_asset:
        raise RuntimeError("BTC-Daten fehlen.")
    results: dict[str, ForwardResult] = {}
    errors: dict[str, str] = {}
    try:
        results["Quality Breakout"] = _quality_result(history_by_asset, started_at=started_at, scan_hours=scan_hours, start_capital=start_capital, fee_pct=fee_pct, quality_params=quality_params)
    except Exception as exc:
        errors["Quality Breakout"] = str(exc)
    try:
        results["BTC EMA 50/200"] = _ema_result(history_by_asset["BTC"], started_at=started_at, scan_hours=scan_hours, start_capital=start_capital, fee_pct=fee_pct)
    except Exception as exc:
        errors["BTC EMA 50/200"] = str(exc)
    try:
        results["BTC Buy & Hold"] = _buy_hold_result(history_by_asset["BTC"], started_at=started_at, start_capital=start_capital, fee_pct=fee_pct)
    except Exception as exc:
        errors["BTC Buy & Hold"] = str(exc)

    rows = []
    for name in ("Quality Breakout", "BTC EMA 50/200", "BTC Buy & Hold"):
        r = results.get(name)
        if r is None:
            rows.append({"Strategie": name, "Status": "wartet", "Kontowert €": np.nan, "Rendite %": np.nan, "Drawdown %": np.nan, "Profit Factor": np.nan, "Abgeschl. Trades": 0, "Gebühren €": np.nan, "Hinweis": errors.get(name, "")})
        else:
            rows.append({
                "Strategie": name, "Status": "läuft", "Kontowert €": round(r.final_value, 2),
                "Rendite %": round(r.return_pct, 2), "Drawdown %": round(r.max_drawdown_pct, 2),
                "Profit Factor": ("∞" if math.isinf(r.profit_factor) else round(r.profit_factor, 2)),
                "Abgeschl. Trades": int(r.completed_trades), "Gebühren €": round(r.total_fees, 2), "Hinweis": r.note,
            })
    table = pd.DataFrame(rows)
    available = table[pd.to_numeric(table["Kontowert €"], errors="coerce").notna()].copy()
    if not available.empty:
        available = available.sort_values("Kontowert €", ascending=False)
        rank_map = {name: i + 1 for i, name in enumerate(available["Strategie"].tolist())}
        table.insert(0, "Rang", [rank_map.get(n, "–") for n in table["Strategie"]])
    else:
        table.insert(0, "Rang", "–")
    if not table.empty:
        table["_rank_sort"] = pd.to_numeric(table["Rang"], errors="coerce").fillna(999)
        table = table.sort_values(["_rank_sort", "Strategie"]).drop(columns=["_rank_sort"]).reset_index(drop=True)

    eq = pd.DataFrame()
    for name, r in results.items():
        if r.equity is not None and not r.equity.empty:
            s = r.equity.copy()
            s = s[~s.index.duplicated(keep="last")]
            s.name = name
            eq = pd.concat([eq, s], axis=1)
    if not eq.empty:
        eq = eq.sort_index().ffill()
    return table, results, eq
