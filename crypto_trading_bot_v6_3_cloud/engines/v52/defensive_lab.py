from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math

import numpy as np
import pandas as pd


@dataclass
class DefensiveResult:
    final_value: float
    return_pct: float
    max_drawdown_pct: float
    profit_factor: float
    completed_trades: int
    total_fees: float
    time_in_market_pct: float
    cash_time_pct: float
    equity: pd.Series
    trade_log: pd.DataFrame


def _norm_index(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    idx = pd.to_datetime(x.index)
    try:
        if idx.tz is not None:
            idx = idx.tz_convert(None)
    except Exception:
        pass
    x.index = idx
    return x[~x.index.duplicated(keep="last")].sort_index()


def _compound(values: list[float]) -> float:
    if not values:
        return 0.0
    return (float(np.prod(1.0 + np.asarray(values, dtype=float) / 100.0)) - 1.0) * 100.0


def _pf_cap(v: float) -> float:
    if math.isinf(float(v)):
        return 3.0
    return max(0.0, min(3.0, float(v)))


def defensive_variants() -> list[dict[str, Any]]:
    # Wenige, bewusst vorab festgelegte Varianten. Kein Grid-Search.
    return [
        {
            "name": "BTC · EMA 50/200",
            "mode": "btc_only", "fast": 50, "slow": 200,
            "need_ret30": False, "need_ret7": False, "switch_margin": 0.0,
        },
        {
            "name": "BTC/ETH · Trend",
            "mode": "best", "fast": 50, "slow": 200,
            "need_ret30": False, "need_ret7": False, "switch_margin": 0.0,
        },
        {
            "name": "BTC/ETH · Trend + 30T positiv",
            "mode": "best", "fast": 50, "slow": 200,
            "need_ret30": True, "need_ret7": False, "switch_margin": 0.0,
        },
        {
            "name": "BTC/ETH · Trend + 7T/30T positiv",
            "mode": "best", "fast": 50, "slow": 200,
            "need_ret30": True, "need_ret7": True, "switch_margin": 0.0,
        },
        {
            "name": "BTC/ETH · defensiv + Wechselpuffer",
            "mode": "best", "fast": 50, "slow": 200,
            "need_ret30": True, "need_ret7": True, "switch_margin": 4.0,
        },
        {
            "name": "BTC/ETH · langsamer Trend",
            "mode": "best", "fast": 80, "slow": 240,
            "need_ret30": True, "need_ret7": False, "switch_margin": 4.0,
        },
    ]


def prepare_defensive_snapshots(
    data: dict[str, pd.DataFrame], *, test_days: int = 365, scan_every_hours: int = 6
) -> list[dict]:
    assets = [a for a in ("BTC", "ETH") if a in data]
    if len(assets) < 2:
        raise RuntimeError("Für den Defensiv-Test werden BTC- und ETH-Daten benötigt.")
    scan_h = int(scan_every_hours)
    if scan_h < 1:
        raise ValueError("scan_every_hours muss >= 1 sein")

    frames: dict[str, pd.DataFrame] = {}
    for asset in assets:
        df = _norm_index(data[asset])
        if df.empty or "Close" not in df.columns:
            raise RuntimeError(f"{asset}: keine brauchbaren Close-Daten.")
        close = df["Close"].astype(float).resample(f"{scan_h}h").last().dropna()
        # Warmup bis 240 Bars + 30 Tage Momentum.
        bars7 = max(2, int(round(24 * 7 / scan_h)))
        bars30 = max(bars7 + 2, int(round(24 * 30 / scan_h)))
        f = pd.DataFrame({"price": close})
        for span in (50, 80, 200, 240):
            f[f"ema{span}"] = close.ewm(span=span, adjust=False).mean()
        f["ret7"] = close.pct_change(bars7) * 100.0
        f["ret30"] = close.pct_change(bars30) * 100.0
        f["vol7"] = close.pct_change().rolling(bars7).std() * 100.0
        frames[asset] = f.dropna()

    common = frames["BTC"].index.intersection(frames["ETH"].index)
    if len(common) < 120:
        raise RuntimeError("Zu wenige gemeinsame BTC/ETH-Zeitpunkte.")
    end_ts = common.max()
    start_ts = end_ts - pd.Timedelta(days=int(test_days))
    common = common[common >= start_ts]

    snaps: list[dict] = []
    for ts in common:
        rows = {}
        for asset in assets:
            r = frames[asset].loc[ts]
            rows[asset] = {k: float(r[k]) for k in r.index}
        snaps.append({"timestamp": pd.Timestamp(ts), "rows": rows})
    if len(snaps) < 80:
        raise RuntimeError("Zu wenige Defensiv-Snapshots nach Warmup.")
    return snaps


def _eligible(r: dict[str, float], p: dict[str, Any]) -> bool:
    fast = int(p.get("fast", 50)); slow = int(p.get("slow", 200))
    price = float(r["price"])
    trend_ok = price > float(r[f"ema{slow}"]) and float(r[f"ema{fast}"]) > float(r[f"ema{slow}"])
    if not trend_ok:
        return False
    if bool(p.get("need_ret30")) and float(r["ret30"]) <= 0:
        return False
    if bool(p.get("need_ret7")) and float(r["ret7"]) <= 0:
        return False
    return True


def _strength(r: dict[str, float]) -> float:
    # Nur zur Wahl zwischen BTC und ETH, keine Kursprognose.
    return 0.65 * float(r["ret30"]) + 0.35 * float(r["ret7"])


def simulate_defensive(
    snapshots: list[dict], *, params: dict[str, Any], start_capital: float = 1000.0, fee_pct: float = 0.25
) -> DefensiveResult:
    if len(snapshots) < 3:
        raise RuntimeError("Zu wenige Snapshots für den Defensiv-Test.")
    fee_rate = float(fee_pct) / 100.0
    cash = float(start_capital)
    pos: dict[str, Any] | None = None
    total_fees = 0.0
    realized: list[float] = []
    trades: list[dict[str, Any]] = []
    eq_rows: list[tuple[pd.Timestamp, float]] = []
    market_points = 0

    def price_at(snap: dict, asset: str) -> float:
        return float(snap["rows"][asset]["price"])

    for snap in snapshots:
        ts = pd.Timestamp(snap["timestamp"])
        rows: dict[str, dict[str, float]] = snap["rows"]
        mode = str(params.get("mode", "best"))
        eligible = [a for a, r in rows.items() if _eligible(r, params)]
        if mode == "btc_only":
            desired = "BTC" if "BTC" in eligible else None
        else:
            desired = max(eligible, key=lambda a: _strength(rows[a])) if eligible else None

        # Wechselpuffer: bestehende Position darf bleiben, solange sie noch zulässig ist
        # und der Herausforderer nicht klar stärker ist.
        if pos is not None and desired is not None and desired != pos["asset"]:
            current = str(pos["asset"])
            if current in eligible:
                margin = float(params.get("switch_margin", 0.0))
                if _strength(rows[desired]) < _strength(rows[current]) + margin:
                    desired = current

        # Exit/Wechsel
        if pos is not None and desired != pos["asset"]:
            a = str(pos["asset"]); px = price_at(snap, a); qty = float(pos["qty"])
            gross = qty * px; fee = gross * fee_rate; proceeds = gross - fee
            cash += proceeds; total_fees += fee
            pnl = proceeds - float(pos["cost_basis"])
            realized.append(pnl)
            trades.append({
                "timestamp": ts, "asset": a, "action": "SELL", "price": px, "qty": qty,
                "fee": fee, "pnl_eur": pnl,
                "reason": "Trend beendet" if desired is None else f"Wechsel zu {desired}",
                "ret7": rows[a]["ret7"], "ret30": rows[a]["ret30"],
            })
            pos = None

        # Einstieg: nahezu gesamtes Kapital, absichtlich einfach.
        if pos is None and desired is not None and cash > 1.0:
            px = price_at(snap, desired)
            gross = cash / (1.0 + fee_rate)
            fee = gross * fee_rate
            qty = gross / px
            cash -= gross + fee
            total_fees += fee
            pos = {"asset": desired, "qty": qty, "cost_basis": gross + fee, "entry": px}
            trades.append({
                "timestamp": ts, "asset": desired, "action": "BUY", "price": px, "qty": qty,
                "fee": fee, "pnl_eur": np.nan, "reason": "Defensiver Trend-Einstieg",
                "ret7": rows[desired]["ret7"], "ret30": rows[desired]["ret30"],
            })

        if pos is not None:
            market_points += 1
            eq = cash + float(pos["qty"]) * price_at(snap, str(pos["asset"]))
        else:
            eq = cash
        eq_rows.append((ts, float(eq)))

    # Vergleichbare Endwerte: Position am Testende schließen.
    if pos is not None:
        snap = snapshots[-1]; ts = pd.Timestamp(snap["timestamp"]); a = str(pos["asset"])
        px = price_at(snap, a); qty = float(pos["qty"])
        gross = qty * px; fee = gross * fee_rate; proceeds = gross - fee
        cash += proceeds; total_fees += fee
        pnl = proceeds - float(pos["cost_basis"])
        realized.append(pnl)
        trades.append({"timestamp": ts, "asset": a, "action": "SELL", "price": px, "qty": qty,
                       "fee": fee, "pnl_eur": pnl, "reason": "Test-Ende"})
        pos = None
        eq_rows.append((ts, cash))

    equity = pd.Series([v for _, v in eq_rows], index=[t for t, _ in eq_rows], dtype=float)
    equity = equity[~equity.index.duplicated(keep="last")]
    peak = equity.cummax(); dd = ((equity / peak) - 1.0) * 100.0
    max_dd = abs(float(dd.min())) if not dd.empty else 0.0
    pos_sum = sum(x for x in realized if x > 0)
    neg_sum = -sum(x for x in realized if x < 0)
    pf = math.inf if neg_sum <= 1e-12 and pos_sum > 0 else (pos_sum / neg_sum if neg_sum > 0 else 0.0)
    time_in = market_points / max(1, len(snapshots)) * 100.0
    return DefensiveResult(
        final_value=float(cash), return_pct=(float(cash) / float(start_capital) - 1.0) * 100.0,
        max_drawdown_pct=max_dd, profit_factor=pf, completed_trades=len(realized), total_fees=float(total_fees),
        time_in_market_pct=float(time_in), cash_time_pct=float(100.0 - time_in), equity=equity,
        trade_log=pd.DataFrame(trades),
    )


def compare_defensive_strategies(
    snapshots: list[dict], *, start_capital: float = 1000.0, fee_pct: float = 0.25,
    dev_fraction: float = 0.80, progress_cb=None,
):
    if len(snapshots) < 100:
        raise RuntimeError("Zu wenige Snapshots für den Defensiv-Vergleich.")
    split = max(60, min(len(snapshots) - 30, int(len(snapshots) * float(dev_fraction))))
    dev = snapshots[:split]; confirmation = snapshots[split:]
    parts = [p for p in np.array_split(np.arange(len(dev)), 4) if len(p) >= 10]
    blocks = [[dev[int(i)] for i in p] for p in parts]
    if len(blocks) != 4:
        raise RuntimeError("Vier Entwicklungsblöcke konnten nicht gebildet werden.")

    variants = defensive_variants(); rows = []; detail = {}
    for vi, p in enumerate(variants):
        br = [simulate_defensive(b, params=p, start_capital=start_capital, fee_pct=fee_pct) for b in blocks]
        rets = [r.return_pct for r in br]; pfs = [_pf_cap(r.profit_factor) for r in br]
        dds = [r.max_drawdown_pct for r in br]
        profitable = sum(x > 0 for x in rets); combined = _compound(rets); worst = min(rets)
        mean_pf = float(np.mean(pfs)); max_dd = max(dds); trades = sum(r.completed_trades for r in br)
        avg_cash = float(np.mean([r.cash_time_pct for r in br]))
        score = profitable * 25 + combined * 1.2 + worst * 1.8 + mean_pf * 4 - max_dd * 0.9 - max(0, 4 - trades)
        if profitable == 4 and combined > 0 and worst > -3 and mean_pf >= 1.2 and max_dd <= 12:
            status = "STARK"
        elif profitable >= 3 and combined > -2 and mean_pf >= 1.0 and max_dd <= 15:
            status = "INTERESSANT"
        else:
            status = "SCHWACH"
        row = {
            "Variante": p["name"], "Status": status, "Positive Blöcke": f"{profitable}/4",
            "Entwicklung kombiniert %": round(combined, 2), "Schlechtester Block %": round(worst, 2),
            "Ø PF": round(mean_pf, 2), "Max DD %": round(max_dd, 2), "Trades": int(trades),
            "Ø Cash-Anteil %": round(avg_cash, 1), "Stabilitäts-Score": round(score, 2),
        }
        for i, x in enumerate(rets, 1): row[f"Block {i} %"] = round(x, 2)
        rows.append(row); detail[p["name"]] = {"params": p, "blocks": br}
        if progress_cb: progress_cb((vi + 1) / len(variants) * 0.75)

    dev_df = pd.DataFrame(rows).sort_values(["Stabilitäts-Score", "Entwicklung kombiniert %"], ascending=[False, False]).reset_index(drop=True)
    dev_df.insert(0, "Rang", range(1, len(dev_df) + 1))

    confirm_rows = []
    for ci, r in dev_df.iterrows():
        name = str(r["Variante"]); cr = simulate_defensive(confirmation, params=detail[name]["params"], start_capital=start_capital, fee_pct=fee_pct)
        detail[name]["confirmation"] = cr
        confirm_rows.append({
            "Rang Entwicklung": int(r["Rang"]), "Variante": name, "Bestätigung %": round(cr.return_pct, 2),
            "Bestätigung DD %": round(cr.max_drawdown_pct, 2), "Bestätigung PF": round(_pf_cap(cr.profit_factor), 2),
            "Trades": int(cr.completed_trades), "Cash-Anteil %": round(cr.cash_time_pct, 1), "Gebühren €": round(cr.total_fees, 2),
        })
        if progress_cb: progress_cb(0.75 + (ci + 1) / len(dev_df) * 0.25)
    confirm_df = pd.DataFrame(confirm_rows)

    winner = str(dev_df.iloc[0]["Variante"]); wr = dev_df.iloc[0]; wc = detail[winner]["confirmation"]
    overall = "SCHWACH"
    if str(wr["Status"]) == "STARK" and wc.return_pct > 0 and _pf_cap(wc.profit_factor) >= 1.1:
        overall = "PAPER-KANDIDAT"
    elif str(wr["Status"]) in {"STARK", "INTERESSANT"} and wc.return_pct > -2:
        overall = "WEITER TESTEN"
    summary = {
        "winner": winner, "overall": overall, "status": str(wr["Status"]),
        "positive_blocks": str(wr["Positive Blöcke"]), "dev_return": float(wr["Entwicklung kombiniert %"]),
        "worst": float(wr["Schlechtester Block %"]), "pf": float(wr["Ø PF"]), "max_dd": float(wr["Max DD %"]),
        "confirm_return": float(wc.return_pct), "confirm_pf": _pf_cap(wc.profit_factor),
        "confirm_dd": float(wc.max_drawdown_pct), "confirm_trades": int(wc.completed_trades),
        "confirm_cash_pct": float(wc.cash_time_pct), "winner_params": dict(detail[winner]["params"]),
    }
    return dev_df, confirm_df, summary, detail


def benchmark_buy_hold(snapshots: list[dict], asset: str, start_capital: float = 1000.0, fee_pct: float = 0.25) -> dict[str, float]:
    first = float(snapshots[0]["rows"][asset]["price"]); last = float(snapshots[-1]["rows"][asset]["price"])
    f = float(fee_pct) / 100.0; invest = float(start_capital) / (1.0 + f); qty = invest / first
    final = qty * last * (1.0 - f)
    return {"return_pct": (final / float(start_capital) - 1.0) * 100.0, "final_value": final}
