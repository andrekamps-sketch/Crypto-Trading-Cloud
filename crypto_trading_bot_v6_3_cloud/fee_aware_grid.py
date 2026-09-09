from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from experiments import Portfolio, _complete_hourly, _grid_targets, _metrics, run_grid
from market_data import load_v6_bundle

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "fee_aware_grid_state.json"
HISTORY_FILE = DATA_DIR / "fee_aware_grid_history.csv"
LATEST_FILE = DATA_DIR / "fee_aware_grid_latest.json"

DEFAULTS = {
    "start_capital": 1000.0,
    "fee_pct": 0.25,
    # Slippage is not charged in the legacy V6 baseline, but is included in the
    # entry hurdle so the challenger must have enough expected room to cover it.
    "slippage_pct": 0.10,
    "safety_buffer_pct": 0.35,
    "confirm_hours": 2,
    "min_hours_between_rebalances": 3,
    "min_price_move_pct": 0.70,
}


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def state() -> dict[str, Any] | None:
    x = _read_json(STATE_FILE, None)
    return x if isinstance(x, dict) else None


def start_fee_grid(start_capital: float = 1000.0, fee_pct: float = 0.25, **params) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in params.items() if k in cfg})
    cfg["start_capital"] = float(start_capital)
    cfg["fee_pct"] = float(fee_pct)
    if cfg["start_capital"] <= 0:
        raise ValueError("Startkapital muss > 0 sein.")
    if cfg["fee_pct"] < 0:
        raise ValueError("Gebühr darf nicht negativ sein.")
    now = pd.Timestamp.now(tz="UTC").floor("h")
    payload = {
        "version": "6.10",
        "mode": "FEE_AWARE_GRID_SHADOW",
        "active": True,
        "started_at": now.isoformat(),
        "assets": ["BTC", "ETH"],
        "params": cfg,
        "last_update_at": None,
        "last_summary": {},
    }
    _write_json(STATE_FILE, payload)
    return payload


def stop_fee_grid() -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Fee-Aware-Grid wurde noch nicht gestartet.")
    s["active"] = False
    _write_json(STATE_FILE, s)
    return s


def resume_fee_grid() -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Fee-Aware-Grid wurde noch nicht gestartet.")
    s["active"] = True
    _write_json(STATE_FILE, s)
    return s


def _fee_aware_run(
    asset: str,
    bundle: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    initial: float,
    fee_rate: float,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Same adaptive-grid idea as V6, but with explicit anti-churn gates.

    The challenger keeps the original z-score target ladder. It only increases
    exposure when the distance back to the 72h EMA exceeds an estimated
    round-trip cost hurdle, requires the target for multiple completed hours,
    and blocks small/rapid rebalances. Risk-off exits always remain allowed.
    """
    now = pd.Timestamp.now(tz="UTC")
    df = _complete_hourly(bundle[asset], now)
    if df.empty:
        return {"status": "wartet", "hint": f"Noch keine abgeschlossene 1h-Kerze für {asset}."}

    close = df["Close"]
    ema = close.ewm(span=72, adjust=False).mean()
    std = close.rolling(72).std(ddof=0).replace(0, np.nan)
    z = (close - ema) / std
    slow_ema = close.ewm(span=480, adjust=False).mean()
    mom7 = close.pct_change(24 * 7)

    pf = Portfolio(initial, fee_rate)
    eq_points: list[tuple[pd.Timestamp, float]] = []
    current_target = 0.0
    last_price = None
    last_rebalance_price: float | None = None
    last_rebalance_ts: pd.Timestamp | None = None
    desired_target: float | None = None
    desired_streak = 0
    blocked_cost = 0
    blocked_time = 0
    blocked_move = 0
    rebalances = 0

    hurdle_pct = (
        2.0 * float(cfg.get("fee_pct", fee_rate * 100.0))
        + 2.0 * float(cfg.get("slippage_pct", 0.10))
        + float(cfg.get("safety_buffer_pct", 0.35))
    )
    confirm_hours = max(1, int(cfg.get("confirm_hours", 2)))
    min_hours = max(0.0, float(cfg.get("min_hours_between_rebalances", 3)))
    min_move = max(0.0, float(cfg.get("min_price_move_pct", 0.70)))

    for ts, price_raw in close.items():
        if ts < start.floor("h"):
            continue
        price = float(price_raw)
        last_price = price
        other = "ETH" if asset == "BTC" else "BTC"
        prices = {asset: price, other: 1.0}
        zv = z.loc[ts]
        risk_off = bool(
            pd.notna(slow_ema.loc[ts]) and pd.notna(mom7.loc[ts])
            and price < slow_ema.loc[ts]
            and mom7.loc[ts] < -0.08
        )
        raw_target = _grid_targets(float(zv) if pd.notna(zv) else np.nan, risk_off)

        if desired_target == raw_target:
            desired_streak += 1
        else:
            desired_target = raw_target
            desired_streak = 1

        should_rebalance = abs(raw_target - current_target) >= 0.24
        if should_rebalance:
            allow = True
            reason_bits = []

            # Emergency / broad risk-off de-risking is never delayed.
            emergency_exit = bool(risk_off and raw_target == 0.0 and current_target > 0.0)

            if not emergency_exit:
                if desired_streak < confirm_hours:
                    allow = False
                    blocked_time += 1
                    reason_bits.append(f"Bestätigung {desired_streak}/{confirm_hours}")

                if last_rebalance_ts is not None:
                    hours_since = (ts - last_rebalance_ts).total_seconds() / 3600.0
                    if hours_since < min_hours:
                        allow = False
                        blocked_time += 1
                        reason_bits.append(f"Cooldown {hours_since:.1f}/{min_hours:.0f}h")

                if last_rebalance_price is not None:
                    move_pct = abs(price / last_rebalance_price - 1.0) * 100.0
                    if move_pct < min_move:
                        allow = False
                        blocked_move += 1
                        reason_bits.append(f"Kursweg {move_pct:.2f}%<{min_move:.2f}%")

                # New/increased exposure must have enough room back to the EMA
                # to cover two fees, two assumed slippage legs and a buffer.
                if raw_target > current_target:
                    if pd.isna(ema.loc[ts]) or price <= 0:
                        edge_pct = 0.0
                    else:
                        edge_pct = max(0.0, (float(ema.loc[ts]) - price) / price * 100.0)
                    if edge_pct < hurdle_pct:
                        allow = False
                        blocked_cost += 1
                        reason_bits.append(f"Edge {edge_pct:.2f}%<{hurdle_pct:.2f}% Kostenhürde")

            if allow:
                why = f"FeeGrid z={float(zv):.2f} Ziel={raw_target:.0%} Hürde={hurdle_pct:.2f}%"
                pf.rebalance({asset: raw_target, other: 0.0}, prices, ts, why)
                current_target = raw_target
                last_rebalance_price = price
                last_rebalance_ts = ts
                rebalances += 1

        eq_points.append((ts, pf.equity(prices)))

    if not eq_points or last_price is None:
        return {"status": "wartet", "hint": f"Noch zu wenige neue 1h-Daten für {asset}."}

    curve = pd.Series({t: v for t, v in eq_points}).sort_index()
    m = _metrics(curve, pf)
    other = "ETH" if asset == "BTC" else "BTC"
    equity = pf.equity({asset: last_price, other: 1.0})
    pos_value = pf.qty[asset] * last_price
    alloc = pos_value / equity if equity > 0 else 0.0
    return {
        "status": "läuft",
        "name": f"{asset} Fee-Aware Grid",
        "curve": curve,
        "trades": pf.trades,
        "fees": pf.fees_paid,
        "realized_pnl": pf.realized_pnl,
        "position": f"{asset} {alloc:.0%} / Cash {1-alloc:.0%}",
        "sample_points": len(curve),
        "rebalances": rebalances,
        "blocked_cost": blocked_cost,
        "blocked_time": blocked_time,
        "blocked_move": blocked_move,
        "hurdle_pct": hurdle_pct,
        **m,
    }


def _serialize_result(asset: str, variant: str, r: dict[str, Any], baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    if r.get("status") != "läuft":
        return {"Coin": asset, "Variante": variant, "Status": "wartet", "Hinweis": r.get("hint", "")}
    base_end = float((baseline or {}).get("end", r.get("end", 0)) or 0)
    base_fees = float((baseline or {}).get("fees", r.get("fees", 0)) or 0)
    pf = r.get("profit_factor", 0)
    return {
        "Coin": asset,
        "Variante": variant,
        "Status": "läuft",
        "Kontowert €": round(float(r.get("end", 0)), 2),
        "Rendite %": round(float(r.get("return_pct", 0)), 3),
        "Drawdown %": round(abs(float(r.get("drawdown_pct", 0))), 3),
        "PF": "∞" if pf == float("inf") else round(float(pf), 2),
        "Trades": len(r.get("trades", []) or []),
        "Gebühren €": round(float(r.get("fees", 0)), 2),
        "vs Standard €": round(float(r.get("end", 0)) - base_end, 2) if baseline is not None else 0.0,
        "Gebühren gespart €": round(base_fees - float(r.get("fees", 0)), 2) if baseline is not None else 0.0,
        "Position": r.get("position", ""),
        "Rebalances": int(r.get("rebalances", 0) or 0),
        "Kosten-Blocker": int(r.get("blocked_cost", 0) or 0),
    }


def process_fee_grid() -> dict[str, Any]:
    s = state()
    if not s:
        return {"active": False, "message": "Fee-Aware-Grid noch nicht gestartet", "rows": []}
    if not s.get("active", True):
        return {"active": False, "message": "Fee-Aware-Grid gestoppt", "rows": []}

    start = pd.Timestamp(s["started_at"])
    start = start.tz_convert("UTC") if start.tzinfo else start.tz_localize("UTC")
    cfg = dict(DEFAULTS)
    cfg.update(s.get("params") or {})
    initial = float(cfg.get("start_capital", 1000.0))
    fee_rate = float(cfg.get("fee_pct", 0.25)) / 100.0
    bundle = load_v6_bundle(start, warmup_days=75)

    rows = []
    curves = {}
    summary = {}
    for asset in ["BTC", "ETH"]:
        standard = run_grid(asset, bundle, start, initial, fee_rate)
        aware = _fee_aware_run(asset, bundle, start, initial, fee_rate, cfg)
        rows.append(_serialize_result(asset, "Standard Grid", standard))
        rows.append(_serialize_result(asset, "Fee-Aware Grid", aware, standard))
        summary[asset] = {
            "standard_end": float(standard.get("end", initial) or initial),
            "aware_end": float(aware.get("end", initial) or initial),
            "standard_fees": float(standard.get("fees", 0) or 0),
            "aware_fees": float(aware.get("fees", 0) or 0),
            "net_advantage_eur": float(aware.get("end", initial) or initial) - float(standard.get("end", initial) or initial),
            "fees_saved_eur": float(standard.get("fees", 0) or 0) - float(aware.get("fees", 0) or 0),
            "hurdle_pct": float(aware.get("hurdle_pct", 0) or 0),
        }
        if standard.get("status") == "läuft":
            curves[f"{asset} Standard"] = standard.get("curve")
        if aware.get("status") == "läuft":
            curves[f"{asset} Fee-Aware"] = aware.get("curve")

    now = pd.Timestamp.now(tz="UTC").isoformat()
    payload = {"ok": True, "active": True, "evaluated_at": now, "started_at": s["started_at"], "params": cfg, "rows": rows, "summary": summary}
    _write_json(LATEST_FILE, payload)
    s["last_update_at"] = now
    s["last_summary"] = summary
    _write_json(STATE_FILE, s)

    hist_rows = []
    for row in rows:
        if row.get("Status") != "läuft":
            continue
        hist_rows.append({"timestamp": now, **row})
    if hist_rows:
        frame = pd.DataFrame(hist_rows)
        if HISTORY_FILE.exists():
            try:
                old = pd.read_csv(HISTORY_FILE)
                frame = pd.concat([old, frame], ignore_index=True)
            except Exception:
                pass
        if len(frame) > 20000:
            frame = frame.tail(20000)
        frame.to_csv(HISTORY_FILE, index=False)

    msg_parts = []
    for asset in ["BTC", "ETH"]:
        x = summary.get(asset, {})
        msg_parts.append(f"{asset}: Fee-Aware vs Standard {x.get('net_advantage_eur',0):+.2f} € · Gebühren gespart {x.get('fees_saved_eur',0):+.2f} €")
    return {"active": True, "message": " · ".join(msg_parts), "rows": rows, "summary": summary, "payload": payload}


def latest() -> dict[str, Any]:
    x = _read_json(LATEST_FILE, {}) or {}
    return x if isinstance(x, dict) else {}


def result_dataframe() -> pd.DataFrame:
    return pd.DataFrame((latest() or {}).get("rows", []) or [])


def history_dataframe(limit: int = 2000) -> pd.DataFrame:
    if not HISTORY_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(HISTORY_FILE).tail(limit)
    except Exception:
        return pd.DataFrame()
