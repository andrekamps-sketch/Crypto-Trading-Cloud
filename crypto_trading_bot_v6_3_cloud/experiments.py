from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


@dataclass
class Trade:
    timestamp: pd.Timestamp
    asset: str
    action: str
    price: float
    qty: float
    fee: float
    realized_pnl: float
    reason: str


@dataclass
class Portfolio:
    initial_cash: float
    fee_rate: float = 0.0025
    cash: float = field(init=False)
    qty: Dict[str, float] = field(default_factory=lambda: {"BTC": 0.0, "ETH": 0.0})
    avg_cost: Dict[str, float] = field(default_factory=lambda: {"BTC": 0.0, "ETH": 0.0})
    fees_paid: float = 0.0
    realized_pnl: float = 0.0
    trades: List[Trade] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = float(self.initial_cash)

    def equity(self, prices: Dict[str, float]) -> float:
        return float(self.cash + sum(self.qty[a] * prices[a] for a in self.qty))

    def _sell(self, asset: str, qty_to_sell: float, price: float, ts: pd.Timestamp, reason: str) -> None:
        qty_to_sell = min(max(float(qty_to_sell), 0.0), self.qty[asset])
        if qty_to_sell <= 1e-12:
            return
        gross = qty_to_sell * price
        fee = gross * self.fee_rate
        pnl = qty_to_sell * (price - self.avg_cost[asset]) - fee
        self.cash += gross - fee
        self.qty[asset] -= qty_to_sell
        self.fees_paid += fee
        self.realized_pnl += pnl
        self.trades.append(Trade(ts, asset, "SELL", price, qty_to_sell, fee, pnl, reason))
        if self.qty[asset] <= 1e-10:
            self.qty[asset] = 0.0
            self.avg_cost[asset] = 0.0

    def _buy_value(self, asset: str, value: float, price: float, ts: pd.Timestamp, reason: str) -> None:
        value = max(float(value), 0.0)
        if value < 1.0 or self.cash < 1.0:
            return
        max_value = self.cash / (1.0 + self.fee_rate)
        value = min(value, max_value)
        if value < 1.0:
            return
        fee = value * self.fee_rate
        q = value / price
        old_q = self.qty[asset]
        old_basis = old_q * self.avg_cost[asset]
        self.cash -= value + fee
        self.qty[asset] += q
        # Kaufgebühr ist Teil der Einstandskosten.
        self.avg_cost[asset] = (old_basis + value + fee) / self.qty[asset]
        self.fees_paid += fee
        self.trades.append(Trade(ts, asset, "BUY", price, q, fee, 0.0, reason))

    def rebalance(self, targets: Dict[str, float], prices: Dict[str, float], ts: pd.Timestamp, reason: str) -> None:
        # Zielgewichte auf 0..1 begrenzen; Rest bleibt Cash.
        clean = {a: max(0.0, min(1.0, float(targets.get(a, 0.0)))) for a in self.qty}
        total_target = sum(clean.values())
        if total_target > 1.0:
            clean = {a: w / total_target for a, w in clean.items()}

        eq = self.equity(prices)
        # Zuerst verkaufen, damit Käufe genug Cash haben.
        for asset in self.qty:
            cur_val = self.qty[asset] * prices[asset]
            tgt_val = clean[asset] * eq
            if cur_val - tgt_val > 2.0:
                self._sell(asset, (cur_val - tgt_val) / prices[asset], prices[asset], ts, reason)

        eq = self.equity(prices)
        for asset in self.qty:
            cur_val = self.qty[asset] * prices[asset]
            tgt_val = clean[asset] * eq
            if tgt_val - cur_val > 2.0:
                self._buy_value(asset, tgt_val - cur_val, prices[asset], ts, reason)


def _metrics(curve: pd.Series, pf: Portfolio) -> Dict[str, float]:
    if curve.empty:
        return {"end": pf.initial_cash, "return_pct": 0.0, "drawdown_pct": 0.0, "profit_factor": 0.0}
    peak = curve.cummax()
    dd = ((curve / peak) - 1.0) * 100.0
    sells = [t.realized_pnl for t in pf.trades if t.action == "SELL"]
    gains = sum(x for x in sells if x > 0)
    losses = -sum(x for x in sells if x < 0)
    if losses > 1e-12:
        profit_factor = gains / losses
    elif gains > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0
    end = float(curve.iloc[-1])
    return {
        "end": end,
        "return_pct": (end / pf.initial_cash - 1.0) * 100.0,
        "drawdown_pct": float(dd.min()),
        "profit_factor": profit_factor,
    }


def _complete_hourly(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    cutoff = now.floor("h")
    return df[df.index < cutoff].copy()


def _pair_frame(bundle: Dict[str, pd.DataFrame], now: pd.Timestamp) -> pd.DataFrame:
    b = _complete_hourly(bundle["BTC"], now)["Close"].rename("BTC")
    e = _complete_hourly(bundle["ETH"], now)["Close"].rename("ETH")
    x = pd.concat([b, e], axis=1).dropna()
    # Nur abgeschlossene 6h-Buckets verwenden.
    x = x.resample("6h").last().dropna()
    x = x[x.index < now.floor("6h")]
    return x


def run_pairs(bundle: Dict[str, pd.DataFrame], start: pd.Timestamp, initial: float, fee_rate: float) -> Dict:
    now = pd.Timestamp.now(tz="UTC")
    x = _pair_frame(bundle, now)
    if x.empty:
        return {"status": "wartet", "hint": "Noch keine abgeschlossene 6h-Kerze nach dem Start."}

    ratio = x["ETH"] / x["BTC"]
    roll = 120  # ca. 30 Tage bei 6h
    mean = ratio.rolling(roll).mean()
    std = ratio.rolling(roll).std(ddof=0).replace(0, np.nan)
    z = (ratio - mean) / std
    btc_mom30 = x["BTC"].pct_change(120)
    eth_mom30 = x["ETH"].pct_change(120)

    pf = Portfolio(initial, fee_rate)
    eq_points: List[Tuple[pd.Timestamp, float]] = []
    state = "CASH"
    last_prices = None

    for ts, row in x.iterrows():
        if ts < start.floor("6h"):
            continue
        prices = {"BTC": float(row["BTC"]), "ETH": float(row["ETH"])}
        last_prices = prices
        zv = z.loc[ts]
        if pd.isna(zv):
            eq_points.append((ts, pf.equity(prices)))
            continue
        # Spot-only: kein Shorting. Bei breitem Krypto-Abverkauf lieber Cash.
        risk_off = bool(
            (pd.notna(btc_mom30.loc[ts]) and pd.notna(eth_mom30.loc[ts]))
            and ((btc_mom30.loc[ts] + eth_mom30.loc[ts]) / 2.0 < -0.12)
        )
        desired = state
        if risk_off:
            desired = "CASH"
        elif zv <= -1.35:
            desired = "ETH"
        elif zv >= 1.35:
            desired = "BTC"
        elif abs(zv) <= 0.25:
            desired = "CASH"

        if desired != state:
            targets = {"BTC": 1.0 if desired == "BTC" else 0.0, "ETH": 1.0 if desired == "ETH" else 0.0}
            pf.rebalance(targets, prices, ts, f"Pairs z={zv:.2f} -> {desired}")
            state = desired
        eq_points.append((ts, pf.equity(prices)))

    if not eq_points or last_prices is None:
        return {"status": "wartet", "hint": "Noch zu wenige neue 6h-Daten seit dem Einfrieren."}
    curve = pd.Series({t: v for t, v in eq_points}).sort_index()
    m = _metrics(curve, pf)
    return {
        "status": "läuft",
        "name": "BTC/ETH Pairs",
        "curve": curve,
        "trades": pf.trades,
        "fees": pf.fees_paid,
        "position": state,
        "sample_points": len(curve),
        **m,
    }


def _grid_targets(zv: float, risk_off: bool) -> float:
    if risk_off or pd.isna(zv):
        return 0.0
    if zv <= -2.0:
        return 1.00
    if zv <= -1.5:
        return 0.75
    if zv <= -1.0:
        return 0.50
    if zv <= -0.5:
        return 0.25
    if zv >= 0.0:
        return 0.0
    return 0.25


def run_grid(asset: str, bundle: Dict[str, pd.DataFrame], start: pd.Timestamp, initial: float, fee_rate: float) -> Dict:
    now = pd.Timestamp.now(tz="UTC")
    df = _complete_hourly(bundle[asset], now)
    if df.empty:
        return {"status": "wartet", "hint": f"Noch keine abgeschlossene 1h-Kerze für {asset}."}
    close = df["Close"]
    ema = close.ewm(span=72, adjust=False).mean()  # ca. 3 Tage
    std = close.rolling(72).std(ddof=0).replace(0, np.nan)
    z = (close - ema) / std
    slow_ema = close.ewm(span=480, adjust=False).mean()  # ca. 20 Tage
    mom7 = close.pct_change(24 * 7)

    pf = Portfolio(initial, fee_rate)
    eq_points: List[Tuple[pd.Timestamp, float]] = []
    current_target = 0.0
    last_price = None

    for ts, price_raw in close.items():
        if ts < start.floor("h"):
            continue
        price = float(price_raw)
        last_price = price
        prices = {"BTC": price if asset == "BTC" else 0.0, "ETH": price if asset == "ETH" else 0.0}
        # Für das nicht gehandelte Asset einen unschädlichen Preis setzen.
        other = "ETH" if asset == "BTC" else "BTC"
        prices[other] = 1.0
        zv = z.loc[ts]
        risk_off = bool(
            pd.notna(slow_ema.loc[ts]) and pd.notna(mom7.loc[ts])
            and price < slow_ema.loc[ts]
            and mom7.loc[ts] < -0.08
        )
        target = _grid_targets(float(zv) if pd.notna(zv) else np.nan, risk_off)
        if abs(target - current_target) >= 0.24:
            pf.rebalance({asset: target, other: 0.0}, prices, ts, f"Grid z={zv:.2f} Ziel={target:.0%}")
            current_target = target
        eq_points.append((ts, pf.equity(prices)))

    if not eq_points or last_price is None:
        return {"status": "wartet", "hint": f"Noch zu wenige neue 1h-Daten für {asset}."}
    curve = pd.Series({t: v for t, v in eq_points}).sort_index()
    m = _metrics(curve, pf)
    pos_value = pf.qty[asset] * last_price
    equity = pf.equity({asset: last_price, ("ETH" if asset == "BTC" else "BTC"): 1.0})
    alloc = (pos_value / equity) if equity > 0 else 0.0
    return {
        "status": "läuft",
        "name": f"{asset} Adaptive Grid",
        "curve": curve,
        "trades": pf.trades,
        "fees": pf.fees_paid,
        "position": f"{asset} {alloc:.0%} / Cash {1-alloc:.0%}",
        "sample_points": len(curve),
        **m,
    }


def trades_to_frame(trades: List[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["Zeit", "Asset", "Aktion", "Preis", "Menge", "Gebühr", "Realisierter G/V", "Grund"])
    rows = []
    for t in trades:
        rows.append({
            "Zeit": t.timestamp,
            "Asset": t.asset,
            "Aktion": t.action,
            "Preis": round(t.price, 6),
            "Menge": round(t.qty, 8),
            "Gebühr": round(t.fee, 4),
            "Realisierter G/V": round(t.realized_pnl, 4),
            "Grund": t.reason,
        })
    return pd.DataFrame(rows)
