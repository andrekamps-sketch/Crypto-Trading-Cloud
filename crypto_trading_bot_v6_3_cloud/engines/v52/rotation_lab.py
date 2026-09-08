from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math

import numpy as np
import pandas as pd


@dataclass
class RotationResult:
    final_value: float
    return_pct: float
    max_drawdown_pct: float
    profit_factor: float
    completed_trades: int
    total_fees: float
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
    x = x[~x.index.duplicated(keep="last")].sort_index()
    return x


def _compound(returns: list[float]) -> float:
    if not returns:
        return 0.0
    return (float(np.prod(1.0 + np.asarray(returns, dtype=float) / 100.0)) - 1.0) * 100.0


def _pf_cap(v: float) -> float:
    if math.isinf(float(v)):
        return 3.0
    return max(0.0, min(3.0, float(v)))


def rotation_variants() -> list[dict[str, Any]]:
    # Absichtlich nur wenige, vorab festgelegte Varianten. Kein Grid-Search.
    return [
        {"name": "Top 1 · nur bullisch", "top_n": 1, "rebalance_hours": 24, "min_score": 65.0, "policy": "bull_only", "rank_buffer": 2, "min_ret30": 0.0},
        {"name": "Top 2 · nur bullisch", "top_n": 2, "rebalance_hours": 24, "min_score": 60.0, "policy": "bull_only", "rank_buffer": 3, "min_ret30": 0.0},
        {"name": "Top 2 · kein Bärenmarkt", "top_n": 2, "rebalance_hours": 24, "min_score": 65.0, "policy": "no_bear", "rank_buffer": 3, "min_ret30": 0.0},
        {"name": "Top 2 · streng", "top_n": 2, "rebalance_hours": 24, "min_score": 70.0, "policy": "bull_only", "rank_buffer": 3, "min_ret30": 2.0},
        {"name": "Top 3 · nur bullisch", "top_n": 3, "rebalance_hours": 24, "min_score": 60.0, "policy": "bull_only", "rank_buffer": 4, "min_ret30": 0.0},
        {"name": "Top 2 · 12h Rotation", "top_n": 2, "rebalance_hours": 12, "min_score": 65.0, "policy": "bull_only", "rank_buffer": 3, "min_ret30": 0.0},
    ]


def prepare_rotation_snapshots(data: dict[str, pd.DataFrame], *, test_days: int = 365, scan_every_hours: int = 6) -> list[dict]:
    if not data:
        raise RuntimeError("Keine Kursdaten übergeben.")
    scan_h = int(scan_every_hours)
    if scan_h < 1:
        raise ValueError("scan_every_hours muss >= 1 sein")

    features: dict[str, pd.DataFrame] = {}
    for asset, raw in data.items():
        df = _norm_index(raw)
        if df.empty or "Close" not in df.columns:
            continue
        close_h = df["Close"].astype(float).resample(f"{scan_h}h").last().dropna()
        if len(close_h) < 220:
            continue
        ret1 = close_h.pct_change()
        bars_7d = max(2, int(round(24 * 7 / scan_h)))
        bars_30d = max(bars_7d + 2, int(round(24 * 30 / scan_h)))
        ema20 = close_h.ewm(span=20, adjust=False).mean()
        ema50 = close_h.ewm(span=50, adjust=False).mean()
        trend_pct = (ema20 / ema50 - 1.0) * 100.0
        vol = ret1.rolling(bars_7d).std() * 100.0
        f = pd.DataFrame({
            "price": close_h,
            "ret7": close_h.pct_change(bars_7d) * 100.0,
            "ret30": close_h.pct_change(bars_30d) * 100.0,
            "trend": trend_pct,
            "vol": vol,
            "ema20": ema20,
            "ema50": ema50,
        }).dropna()
        features[asset] = f
    if len(features) < 2:
        raise RuntimeError("Zu wenige Coins mit ausreichender Historie.")

    common = None
    for f in features.values():
        common = f.index if common is None else common.intersection(f.index)
    if common is None or len(common) < 100:
        raise RuntimeError("Zu wenige gemeinsame Scanner-Zeitpunkte.")
    end_ts = common.max()
    start_ts = end_ts - pd.Timedelta(days=int(test_days))
    common = common[common >= start_ts]

    snapshots: list[dict] = []
    for ts in common:
        rows = []
        for asset, f in features.items():
            if ts not in f.index:
                continue
            r = f.loc[ts]
            rows.append({
                "asset": asset, "price": float(r["price"]), "ret7": float(r["ret7"]),
                "ret30": float(r["ret30"]), "trend": float(r["trend"]), "vol": float(r["vol"]),
                "ema20": float(r["ema20"]), "ema50": float(r["ema50"]),
            })
        if len(rows) < 2:
            continue
        rdf = pd.DataFrame(rows)
        # Cross-sectional ranks: hoch ist gut, bei Volatilität niedrig ist gut.
        rdf["r7_rank"] = rdf["ret7"].rank(pct=True, method="average")
        rdf["r30_rank"] = rdf["ret30"].rank(pct=True, method="average")
        rdf["trend_rank"] = rdf["trend"].rank(pct=True, method="average")
        rdf["lowvol_rank"] = (-rdf["vol"]).rank(pct=True, method="average")
        rdf["score"] = 100.0 * (
            0.40 * rdf["r7_rank"] + 0.30 * rdf["r30_rank"] + 0.20 * rdf["trend_rank"] + 0.10 * rdf["lowvol_rank"]
        )
        rdf = rdf.sort_values(["score", "ret30"], ascending=False).reset_index(drop=True)
        rdf["rank"] = np.arange(1, len(rdf) + 1)

        # BTC als grober Risikofilter, sonst stärkster verfügbarer Coin als Benchmark.
        bench = rdf[rdf["asset"] == "BTC"]
        if bench.empty:
            bench = rdf.iloc[[0]]
        b = bench.iloc[0]
        if float(b["price"]) > float(b["ema50"]) and float(b["ret30"]) > 0:
            regime = "BULL"
        elif float(b["price"]) < float(b["ema50"]) and float(b["ret30"]) < 0:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"
        snapshots.append({"timestamp": pd.Timestamp(ts), "regime": regime, "rows": rdf.to_dict("records")})
    if len(snapshots) < 80:
        raise RuntimeError("Zu wenige Rotation-Snapshots nach Warmup.")
    return snapshots


def _allows(policy: str, regime: str) -> bool:
    if policy == "bull_only":
        return regime == "BULL"
    if policy == "no_bear":
        return regime != "BEAR"
    return True


def simulate_rotation(snapshots: list[dict], *, params: dict[str, Any], start_capital: float = 1000.0, fee_pct: float = 0.25) -> RotationResult:
    if len(snapshots) < 3:
        raise RuntimeError("Zu wenige Snapshots für Rotation.")
    cash = float(start_capital)
    positions: dict[str, dict[str, float]] = {}
    fee_rate = float(fee_pct) / 100.0
    last_rebalance: pd.Timestamp | None = None
    eq_rows: list[tuple[pd.Timestamp, float]] = []
    trades: list[dict[str, Any]] = []
    realized: list[float] = []
    total_fees = 0.0

    def current_equity(price_map: dict[str, float]) -> float:
        return cash + sum(float(p["qty"]) * float(price_map.get(a, p["last_price"])) for a, p in positions.items())

    for snap in snapshots:
        ts = pd.Timestamp(snap["timestamp"])
        rows = pd.DataFrame(snap["rows"])
        if rows.empty:
            continue
        price_map = {str(r["asset"]): float(r["price"]) for _, r in rows.iterrows()}
        for a, p in positions.items():
            if a in price_map:
                p["last_price"] = price_map[a]

        due = last_rebalance is None or (ts - last_rebalance).total_seconds() >= float(params.get("rebalance_hours", 24)) * 3600 - 1
        if due:
            last_rebalance = ts
            allowed = _allows(str(params.get("policy", "bull_only")), str(snap.get("regime", "NEUTRAL")))
            ranked = rows.sort_values("score", ascending=False).copy()
            ranked = ranked[(ranked["score"] >= float(params.get("min_score", 60.0))) & (ranked["ret30"] >= float(params.get("min_ret30", 0.0)))]
            top_n = int(params.get("top_n", 2))
            buffer_rank = int(params.get("rank_buffer", top_n + 1))
            desired = [] if not allowed else list(ranked.head(top_n)["asset"].astype(str))
            rank_map = {str(r["asset"]): int(r["rank"]) for _, r in rows.iterrows()}
            # Bestehende Position darf innerhalb des Puffers bleiben, wenn Marktfilter weiterhin erlaubt.
            if allowed:
                for a in list(positions):
                    if a not in desired and rank_map.get(a, 999) <= buffer_rank:
                        if len(desired) < top_n:
                            desired.append(a)
            desired = desired[:top_n]

            # Nicht mehr gewünschte Coins vollständig verkaufen.
            for a in list(positions):
                if a in desired:
                    continue
                px = float(price_map.get(a, positions[a]["last_price"]))
                qty = float(positions[a]["qty"])
                gross = qty * px
                fee = gross * fee_rate
                cash += gross - fee
                total_fees += fee
                pnl = gross - fee - float(positions[a]["cost_basis"])
                realized.append(pnl)
                trades.append({"timestamp": ts, "asset": a, "action": "SELL", "price": px, "qty": qty, "fee": fee, "pnl_eur": pnl, "reason": "Rotation/Filter", "regime": snap.get("regime"), "rank": rank_map.get(a)})
                del positions[a]

            # Neue Ziel-Coins mit verfügbarem Cash eröffnen. Kein tägliches unnötiges Rebalancing bestehender Positionen.
            new_assets = [a for a in desired if a not in positions]
            if new_assets and cash > 1.0:
                total_eq = current_equity(price_map)
                target_value = total_eq / max(1, top_n)
                for i, a in enumerate(new_assets):
                    px = float(price_map[a])
                    remaining_new = max(1, len(new_assets) - i)
                    budget = min(target_value, cash / remaining_new)
                    if budget <= 1.0 or px <= 0:
                        continue
                    # budget ist Gesamtbelastung inkl. Kaufgebühr.
                    gross = budget / (1.0 + fee_rate)
                    fee = gross * fee_rate
                    qty = gross / px
                    cash -= gross + fee
                    total_fees += fee
                    row = rows[rows["asset"] == a].iloc[0]
                    positions[a] = {"qty": qty, "cost_basis": gross + fee, "last_price": px}
                    trades.append({"timestamp": ts, "asset": a, "action": "BUY", "price": px, "qty": qty, "fee": fee, "pnl_eur": np.nan, "reason": "Top-Rotation", "regime": snap.get("regime"), "score": float(row["score"]), "rank": int(row["rank"]), "ret7": float(row["ret7"]), "ret30": float(row["ret30"])})

        eq_rows.append((ts, current_equity(price_map)))

    # Vergleichbare Endwerte: am Ende alles liquidieren inklusive Gebühr.
    last_snap = snapshots[-1]
    last_rows = pd.DataFrame(last_snap["rows"])
    last_prices = {str(r["asset"]): float(r["price"]) for _, r in last_rows.iterrows()}
    ts = pd.Timestamp(last_snap["timestamp"])
    for a in list(positions):
        px = float(last_prices.get(a, positions[a]["last_price"]))
        qty = float(positions[a]["qty"])
        gross = qty * px
        fee = gross * fee_rate
        cash += gross - fee
        total_fees += fee
        pnl = gross - fee - float(positions[a]["cost_basis"])
        realized.append(pnl)
        trades.append({"timestamp": ts, "asset": a, "action": "SELL", "price": px, "qty": qty, "fee": fee, "pnl_eur": pnl, "reason": "Test-Ende", "regime": last_snap.get("regime")})
        del positions[a]
    eq_rows.append((ts, cash))
    equity = pd.Series([v for _, v in eq_rows], index=[t for t, _ in eq_rows], dtype=float)
    equity = equity[~equity.index.duplicated(keep="last")]
    peak = equity.cummax()
    dd = ((equity / peak) - 1.0) * 100.0
    max_dd = abs(float(dd.min())) if not dd.empty else 0.0
    pos = sum(x for x in realized if x > 0)
    neg = -sum(x for x in realized if x < 0)
    pf = math.inf if neg <= 1e-12 and pos > 0 else (pos / neg if neg > 0 else 0.0)
    log = pd.DataFrame(trades)
    return RotationResult(
        final_value=float(cash), return_pct=(float(cash) / float(start_capital) - 1.0) * 100.0,
        max_drawdown_pct=max_dd, profit_factor=pf, completed_trades=len(realized), total_fees=float(total_fees),
        equity=equity, trade_log=log,
    )


def compare_rotation_strategies(snapshots: list[dict], *, start_capital: float = 1000.0, fee_pct: float = 0.25, dev_fraction: float = 0.80, progress_cb=None):
    if len(snapshots) < 100:
        raise RuntimeError("Zu wenige Snapshots für Rotationsvergleich.")
    split = max(60, min(len(snapshots) - 30, int(len(snapshots) * float(dev_fraction))))
    dev = snapshots[:split]
    confirmation = snapshots[split:]
    parts = [p for p in np.array_split(np.arange(len(dev)), 4) if len(p) >= 10]
    blocks = [[dev[int(i)] for i in p] for p in parts]
    if len(blocks) != 4:
        raise RuntimeError("Vier Entwicklungsblöcke konnten nicht gebildet werden.")

    rows = []
    detail = {}
    variants = rotation_variants()
    for vi, p in enumerate(variants):
        br = [simulate_rotation(b, params=p, start_capital=start_capital, fee_pct=fee_pct) for b in blocks]
        rets = [r.return_pct for r in br]
        pfs = [_pf_cap(r.profit_factor) for r in br]
        dds = [r.max_drawdown_pct for r in br]
        profitable = sum(x > 0 for x in rets)
        combined = _compound(rets)
        worst = min(rets)
        mean_pf = float(np.mean(pfs))
        max_dd = max(dds)
        trades = sum(r.completed_trades for r in br)
        score = profitable * 20 + combined * 1.5 + worst * 1.5 + mean_pf * 3 - max_dd * 0.8 - max(0, 8 - trades)
        if profitable >= 4 and combined > 0 and worst > -3 and mean_pf >= 1.2 and max_dd <= 10:
            status = "STARK"
        elif profitable >= 3 and mean_pf >= 1.1 and max_dd <= 12:
            status = "INTERESSANT"
        else:
            status = "SCHWACH"
        row = {"Variante": p["name"], "Status": status, "Positive Blöcke": f"{profitable}/4", "Entwicklung kombiniert %": round(combined, 2), "Schlechtester Block %": round(worst, 2), "Ø PF": round(mean_pf, 2), "Max DD %": round(max_dd, 2), "Trades": int(trades), "Stabilitäts-Score": round(score, 2)}
        for i, x in enumerate(rets, 1): row[f"Block {i} %"] = round(x, 2)
        rows.append(row)
        detail[p["name"]] = {"params": p, "blocks": br}
        if progress_cb: progress_cb((vi + 1) / len(variants) * 0.75)
    dev_df = pd.DataFrame(rows).sort_values(["Stabilitäts-Score", "Entwicklung kombiniert %"], ascending=[False, False]).reset_index(drop=True)
    dev_df.insert(0, "Rang", range(1, len(dev_df) + 1))

    confirm_rows = []
    for ci, r in dev_df.iterrows():
        name = str(r["Variante"])
        cr = simulate_rotation(confirmation, params=detail[name]["params"], start_capital=start_capital, fee_pct=fee_pct)
        detail[name]["confirmation"] = cr
        confirm_rows.append({"Rang Entwicklung": int(r["Rang"]), "Variante": name, "Bestätigung %": round(cr.return_pct, 2), "Bestätigung DD %": round(cr.max_drawdown_pct, 2), "Bestätigung PF": round(_pf_cap(cr.profit_factor), 2), "Trades": int(cr.completed_trades), "Gebühren €": round(cr.total_fees, 2)})
        if progress_cb: progress_cb(0.75 + (ci + 1) / len(dev_df) * 0.25)
    confirm_df = pd.DataFrame(confirm_rows)
    winner = str(dev_df.iloc[0]["Variante"])
    wr = dev_df.iloc[0]
    wc = detail[winner]["confirmation"]
    overall = "SCHWACH"
    if str(wr["Status"]) == "STARK" and wc.return_pct > 0 and _pf_cap(wc.profit_factor) >= 1.2:
        overall = "PAPER-KANDIDAT"
    elif str(wr["Status"]) in {"STARK", "INTERESSANT"} and wc.return_pct > -2:
        overall = "WEITER TESTEN"
    summary = {"winner": winner, "overall": overall, "status": str(wr["Status"]), "positive_blocks": str(wr["Positive Blöcke"]), "dev_return": float(wr["Entwicklung kombiniert %"]), "worst": float(wr["Schlechtester Block %"]), "pf": float(wr["Ø PF"]), "max_dd": float(wr["Max DD %"]), "confirm_return": float(wc.return_pct), "confirm_pf": _pf_cap(wc.profit_factor), "confirm_dd": float(wc.max_drawdown_pct), "confirm_trades": int(wc.completed_trades), "winner_params": dict(detail[winner]["params"]), "dev_from": pd.Timestamp(dev[0]["timestamp"]), "dev_to": pd.Timestamp(dev[-1]["timestamp"]), "confirmation_from": pd.Timestamp(confirmation[0]["timestamp"]), "confirmation_to": pd.Timestamp(confirmation[-1]["timestamp"])}
    return dev_df, confirm_df, summary, detail


def benchmark_rotation_buy_hold(snapshots: list[dict], asset: str = "BTC", start_capital: float = 1000.0, fee_pct: float = 0.25) -> dict[str, float]:
    first = None; last = None
    for s in snapshots:
        rows = {str(r["asset"]): r for r in s["rows"]}
        if asset in rows:
            if first is None: first = float(rows[asset]["price"])
            last = float(rows[asset]["price"])
    if not first or not last:
        return {"return_pct": 0.0, "final_value": start_capital}
    f = fee_pct / 100.0
    invest = start_capital / (1 + f)
    qty = invest / first
    final = qty * last * (1 - f)
    return {"return_pct": (final / start_capital - 1) * 100.0, "final_value": final}
