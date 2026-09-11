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

# V7.1 defaults are intentionally conservative. The purpose of the challenger is
# to prove that less churn can beat the legacy grid after costs, not to maximize
# the number of trades.
DEFAULTS = {
    "start_capital": 1000.0,
    "fee_pct": 0.25,
    "slippage_pct": 0.10,
    "safety_buffer_pct": 0.35,
    "fee_multiple": 3.0,
    "confirm_hours": 2,
    "min_hours_between_rebalances": 4,
    "min_price_move_pct": 0.80,
    "max_price_move_pct": 1.50,
    "volatility_multiplier": 1.80,
    "max_exposure_pct": 60.0,
    "bear_max_exposure_pct": 40.0,
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




def _archive_previous_season() -> None:
    """Archive a prior fee-grid season before a clean restart."""
    if not any(path.exists() for path in (STATE_FILE, HISTORY_FILE, LATEST_FILE)):
        return
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    archive = DATA_DIR / "fee_grid_archive" / stamp
    archive.mkdir(parents=True, exist_ok=True)
    for path in (STATE_FILE, HISTORY_FILE, LATEST_FILE):
        if path.exists():
            path.replace(archive / path.name)


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
    _archive_previous_season()
    now = pd.Timestamp.now(tz="UTC").floor("h")
    payload = {
        "version": "7.1",
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


def _dynamic_grid_step_pct(close: pd.Series, cfg: dict[str, Any]) -> pd.Series:
    """Return a volatility-aware minimum course move in percent.

    The 24h rolling hourly-return volatility is scaled and clamped to the user
    controlled corridor (default 0.80% .. 1.50%).
    """
    floor = max(0.0, float(cfg.get("min_price_move_pct", 0.80)))
    ceiling = max(floor, float(cfg.get("max_price_move_pct", 1.50)))
    mult = max(0.0, float(cfg.get("volatility_multiplier", 1.80)))
    hourly_vol_pct = close.pct_change().rolling(24, min_periods=8).std(ddof=0) * 100.0
    dynamic = (hourly_vol_pct * mult).clip(lower=floor, upper=ceiling)
    return dynamic.fillna(floor)


def _regime_at(price: float, slow_ema: float | np.floating | None, mom7: float | np.floating | None) -> str:
    if slow_ema is None or mom7 is None or pd.isna(slow_ema) or pd.isna(mom7):
        return "neutral"
    if price < float(slow_ema) and float(mom7) < 0.0:
        return "bear"
    if price > float(slow_ema) and float(mom7) > 0.0:
        return "bull"
    return "neutral"


def _fee_hurdle_pct(fee_pct: float, cfg: dict[str, Any]) -> float:
    """Gross mean-reversion edge required before increasing exposure.

    Requirement A: at least `fee_multiple` times the pure round-trip fee.
    Requirement B: at least estimated round-trip fee + slippage + safety buffer.
    We use the stricter of both.
    """
    slippage = max(0.0, float(cfg.get("slippage_pct", 0.10)))
    buffer_pct = max(0.0, float(cfg.get("safety_buffer_pct", 0.35)))
    fee_multiple = max(1.0, float(cfg.get("fee_multiple", 3.0)))
    pure_fee_hurdle = 2.0 * fee_pct * fee_multiple
    all_in_hurdle = 2.0 * fee_pct + 2.0 * slippage + buffer_pct
    return max(pure_fee_hurdle, all_in_hurdle)


def _fee_aware_run(
    asset: str,
    bundle: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    initial: float,
    fee_rate: float,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Adaptive grid with anti-churn, fee hurdle and bear-market exposure brake."""
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
    dynamic_step = _dynamic_grid_step_pct(close, cfg)

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
    blocked_exposure = 0
    rebalances = 0
    bear_hours = 0

    fee_pct = float(cfg.get("fee_pct", fee_rate * 100.0))
    hurdle_pct = _fee_hurdle_pct(fee_pct, cfg)
    confirm_hours = max(1, int(cfg.get("confirm_hours", 2)))
    min_hours = max(0.0, float(cfg.get("min_hours_between_rebalances", 4)))
    max_exposure = min(1.0, max(0.0, float(cfg.get("max_exposure_pct", 60.0)) / 100.0))
    bear_max_exposure = min(max_exposure, max(0.0, float(cfg.get("bear_max_exposure_pct", 40.0)) / 100.0))

    for ts, price_raw in close.items():
        if ts < start.floor("h"):
            continue
        price = float(price_raw)
        last_price = price
        other = "ETH" if asset == "BTC" else "BTC"
        prices = {asset: price, other: 1.0}
        zv = z.loc[ts]
        mom7_v = mom7.loc[ts]
        slow_v = slow_ema.loc[ts]
        regime = _regime_at(price, slow_v, mom7_v)
        bear_hours += int(regime == "bear")

        severe_risk_off = bool(
            pd.notna(slow_v) and pd.notna(mom7_v)
            and price < float(slow_v)
            and float(mom7_v) < -0.08
        )
        raw_target = _grid_targets(float(zv) if pd.notna(zv) else np.nan, severe_risk_off)

        cap = bear_max_exposure if regime == "bear" else max_exposure
        capped_target = min(float(raw_target), cap)
        if capped_target < float(raw_target) - 1e-12:
            blocked_exposure += 1
        raw_target = capped_target

        if desired_target == raw_target:
            desired_streak += 1
        else:
            desired_target = raw_target
            desired_streak = 1

        should_rebalance = abs(raw_target - current_target) >= 0.19
        if should_rebalance:
            allow = True

            # Strong risk-off liquidation is always immediate.
            emergency_exit = bool(severe_risk_off and raw_target == 0.0 and current_target > 0.0)

            if not emergency_exit:
                if desired_streak < confirm_hours:
                    allow = False
                    blocked_time += 1

                if last_rebalance_ts is not None:
                    hours_since = (ts - last_rebalance_ts).total_seconds() / 3600.0
                    if hours_since < min_hours:
                        allow = False
                        blocked_time += 1

                required_move = float(dynamic_step.loc[ts])
                if last_rebalance_price is not None:
                    move_pct = abs(price / last_rebalance_price - 1.0) * 100.0
                    if move_pct < required_move:
                        allow = False
                        blocked_move += 1

                # Only increases need a profit-room test. Reductions remain possible
                # after confirmation/cooldown/move gates and risk-off can exit instantly.
                if raw_target > current_target:
                    if pd.isna(ema.loc[ts]) or price <= 0:
                        edge_pct = 0.0
                    else:
                        edge_pct = max(0.0, (float(ema.loc[ts]) - price) / price * 100.0)
                    if edge_pct < hurdle_pct:
                        allow = False
                        blocked_cost += 1

            if allow:
                step_pct = float(dynamic_step.loc[ts])
                why = (
                    f"FeeGrid V7.1 z={float(zv):.2f} Ziel={raw_target:.0%} "
                    f"Hürde={hurdle_pct:.2f}% Abstand={step_pct:.2f}% Regime={regime}"
                )
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
    last_ts = curve.index[-1]
    last_regime = _regime_at(float(last_price), slow_ema.loc[last_ts], mom7.loc[last_ts])
    last_step = float(dynamic_step.loc[last_ts])

    return {
        "status": "läuft",
        "name": f"{asset} Fee-Aware Grid V7.1",
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
        "blocked_exposure": blocked_exposure,
        "bear_hours": bear_hours,
        "hurdle_pct": hurdle_pct,
        "dynamic_step_pct": last_step,
        "regime": last_regime,
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
        "Abstands-Blocker": int(r.get("blocked_move", 0) or 0),
        "Exposure-Blocker": int(r.get("blocked_exposure", 0) or 0),
        "Dyn. Abstand %": round(float(r.get("dynamic_step_pct", 0) or 0), 2) if variant != "Standard Grid" else None,
        "Regime": r.get("regime", "") if variant != "Standard Grid" else "",
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
    summary = {}
    for asset in ["BTC", "ETH"]:
        standard = run_grid(asset, bundle, start, initial, fee_rate)
        aware = _fee_aware_run(asset, bundle, start, initial, fee_rate, cfg)
        rows.append(_serialize_result(asset, "Standard Grid", standard))
        rows.append(_serialize_result(asset, "Fee-Aware V7.1", aware, standard))
        summary[asset] = {
            "standard_end": float(standard.get("end", initial) or initial),
            "aware_end": float(aware.get("end", initial) or initial),
            "standard_fees": float(standard.get("fees", 0) or 0),
            "aware_fees": float(aware.get("fees", 0) or 0),
            "net_advantage_eur": float(aware.get("end", initial) or initial) - float(standard.get("end", initial) or initial),
            "fees_saved_eur": float(standard.get("fees", 0) or 0) - float(aware.get("fees", 0) or 0),
            "hurdle_pct": float(aware.get("hurdle_pct", 0) or 0),
            "dynamic_step_pct": float(aware.get("dynamic_step_pct", 0) or 0),
            "regime": str(aware.get("regime", "")),
            "blocked_cost": int(aware.get("blocked_cost", 0) or 0),
            "blocked_move": int(aware.get("blocked_move", 0) or 0),
            "blocked_exposure": int(aware.get("blocked_exposure", 0) or 0),
        }

    now = pd.Timestamp.now(tz="UTC").isoformat()
    payload = {
        "ok": True,
        "active": True,
        "algorithm_version": "7.1",
        "evaluated_at": now,
        "started_at": s["started_at"],
        "params": cfg,
        "rows": rows,
        "summary": summary,
    }
    _write_json(LATEST_FILE, payload)
    s["last_update_at"] = now
    s["last_summary"] = summary
    s["algorithm_version"] = "7.1"
    _write_json(STATE_FILE, s)

    hist_rows = []
    for row in rows:
        if row.get("Status") != "läuft":
            continue
        hist_rows.append({"timestamp": now, "algorithm_version": "7.1", **row})
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
        msg_parts.append(
            f"{asset}: V7.1 vs Standard {x.get('net_advantage_eur',0):+.2f} € · "
            f"Gebühren gespart {x.get('fees_saved_eur',0):+.2f} € · "
            f"Abstand {x.get('dynamic_step_pct',0):.2f}%"
        )
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
