from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_EVENTS = DATA_DIR / "signal_lab_events.json"
SIGNAL_STATE = DATA_DIR / "signal_lab_state.json"

# Checkpoints are intentionally coarse because the cloud worker normally scans hourly.
HORIZONS: dict[str, pd.Timedelta] = {
    "1h": pd.Timedelta(hours=1),
    "6h": pd.Timedelta(hours=6),
    "24h": pd.Timedelta(hours=24),
    "3d": pd.Timedelta(days=3),
    "7d": pd.Timedelta(days=7),
}
MAX_EVENTS = 5000


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _ts(value: Any) -> pd.Timestamp:
    t = pd.Timestamp(value if value is not None else datetime.now().astimezone())
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t


def _float(value: Any) -> float | None:
    try:
        x = float(value)
        return x if pd.notna(x) else None
    except Exception:
        return None


def _stage(row: dict[str, Any]) -> int:
    rules = int(row.get("rules_ok", 0) or 0)
    total = int(row.get("rules_total", 10) or 10)
    if bool(row.get("quality_ready")) or rules >= total or rules >= 10:
        return 10
    if rules >= 9:
        return 9
    return 0


def _ret(price: float, start: float) -> float:
    return (price / start - 1.0) * 100.0 if start > 0 else 0.0


def _new_event(row: dict[str, Any], stage: int, scan_time: pd.Timestamp, regime: dict[str, Any], trigger: str) -> dict[str, Any]:
    price = _float(row.get("Preis €")) or 0.0
    stamp = scan_time.isoformat()
    return {
        "signal_id": f"{row.get('Coin','?')}-{stage}-{stamp}",
        "asset": str(row.get("Coin", "")),
        "stage": int(stage),
        "trigger": trigger,
        "signal_time": stamp,
        "signal_price": price,
        "market_regime": str((regime or {}).get("code") or (regime or {}).get("label") or ""),
        "market_label": str((regime or {}).get("label") or ""),
        "edge": _float(row.get("Edge")),
        "rsi": _float(row.get("RSI")),
        "score": _float(row.get("Score")),
        "rules_ok": int(row.get("rules_ok", 0) or 0),
        "rules_total": int(row.get("rules_total", 10) or 10),
        "missing": str(row.get("Fehlt", "") or ""),
        "max_price_seen": price,
        "min_price_seen": price,
        "max_up_pct": 0.0,
        "max_down_pct": 0.0,
        "last_seen_time": stamp,
        "last_seen_price": price,
        **{f"price_{h}": None for h in HORIZONS},
        **{f"ret_{h}_pct": None for h in HORIZONS},
        **{f"checkpoint_{h}": None for h in HORIZONS},
    }


def _update_event(event: dict[str, Any], row: dict[str, Any], scan_time: pd.Timestamp) -> int:
    price = _float(row.get("Preis €"))
    start = _float(event.get("signal_price"))
    if price is None or start is None or start <= 0:
        return 0
    event["last_seen_time"] = scan_time.isoformat()
    event["last_seen_price"] = price
    max_p = max(_float(event.get("max_price_seen")) or start, price)
    min_p = min(_float(event.get("min_price_seen")) or start, price)
    event["max_price_seen"] = max_p
    event["min_price_seen"] = min_p
    event["max_up_pct"] = round(_ret(max_p, start), 4)
    event["max_down_pct"] = round(_ret(min_p, start), 4)

    signal_time = _ts(event.get("signal_time"))
    elapsed = scan_time - signal_time
    checkpoints = 0
    for label, horizon in HORIZONS.items():
        key = f"price_{label}"
        if event.get(key) is None and elapsed >= horizon:
            event[key] = price
            event[f"ret_{label}_pct"] = round(_ret(price, start), 4)
            event[f"checkpoint_{label}"] = scan_time.isoformat()
            checkpoints += 1
    return checkpoints


def process_signal_lab(monitor_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Persist 9/10 and 10/10 Quality signals and update forward outcomes.

    This is observational only. It never changes V5.2/V6 rules, positions or paper trades.
    On the very first V6.6 run, already-active 9/10 or 10/10 candidates are stored as
    `baseline` events so their future development can still be measured without pretending
    they were fresh transitions.
    """
    payload = monitor_payload or {}
    rows = payload.get("rows", []) or []
    if not rows:
        return {"ok": False, "message": "Kein Markt-Scan vorhanden", "new_signals": 0, "checkpoints": 0, "total": len(_read_json(SIGNAL_EVENTS, []) or [])}

    scan_time = _ts(payload.get("evaluated_at") or datetime.now().astimezone())
    regime = payload.get("regime") or {}
    events: list[dict[str, Any]] = _read_json(SIGNAL_EVENTS, []) or []
    state: dict[str, Any] = _read_json(SIGNAL_STATE, {}) or {}
    previous = {str(k): int(v or 0) for k, v in (state.get("active_stages") or {}).items()}
    initialized = bool(state.get("initialized"))
    current: dict[str, int] = {}
    row_map: dict[str, dict[str, Any]] = {}

    for row in rows:
        coin = str(row.get("Coin", "") or "")
        if not coin:
            continue
        current[coin] = _stage(row)
        row_map[coin] = row

    checkpoints = 0
    # Update every existing signal with the latest observed price of its asset.
    for event in events:
        coin = str(event.get("asset", ""))
        if coin in row_map:
            checkpoints += _update_event(event, row_map[coin], scan_time)

    new_events: list[dict[str, Any]] = []
    for coin, stage in current.items():
        if stage < 9:
            continue
        old = previous.get(coin, 0)
        if not initialized:
            # Capture the V6.6 starting situation, clearly marked as baseline.
            new_events.append(_new_event(row_map[coin], stage, scan_time, regime, "baseline"))
        elif stage > old:
            # Fresh 9/10 or upgrade 9 -> 10. Falling below 9 re-arms future signals.
            new_events.append(_new_event(row_map[coin], stage, scan_time, regime, "transition"))

    if new_events:
        events.extend(new_events)
    if len(events) > MAX_EVENTS:
        events = events[-MAX_EVENTS:]

    _write_json(SIGNAL_EVENTS, events)
    _write_json(SIGNAL_STATE, {
        "initialized": True,
        "active_stages": current,
        "last_scan_at": scan_time.isoformat(),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    return {
        "ok": True,
        "message": "Signal-Labor aktualisiert",
        "new_signals": len(new_events),
        "checkpoints": checkpoints,
        "total": len(events),
        "new": new_events,
    }


def events_dataframe() -> pd.DataFrame:
    rows = _read_json(SIGNAL_EVENTS, []) or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "signal_time" in df.columns:
        df["signal_time"] = pd.to_datetime(df["signal_time"], errors="coerce", utc=True)
        df = df.sort_values("signal_time", ascending=False)
    return df


def horizon_stats(include_baseline: bool = True) -> pd.DataFrame:
    df = events_dataframe()
    if df.empty:
        return pd.DataFrame(columns=["Stage", "Horizont", "Signale", "Ø Rendite %", "Median %", "Positiv %"])
    if not include_baseline and "trigger" in df.columns:
        df = df[df["trigger"] != "baseline"]
    out: list[dict[str, Any]] = []
    for stage in (9, 10):
        sdf = df[pd.to_numeric(df.get("stage"), errors="coerce") == stage]
        for h in HORIZONS:
            col = f"ret_{h}_pct"
            if col not in sdf.columns:
                vals = pd.Series(dtype=float)
            else:
                vals = pd.to_numeric(sdf[col], errors="coerce").dropna()
            if vals.empty:
                continue
            out.append({
                "Stage": f"{stage}/10",
                "Horizont": h,
                "Signale": int(len(vals)),
                "Ø Rendite %": round(float(vals.mean()), 2),
                "Median %": round(float(vals.median()), 2),
                "Positiv %": round(float((vals > 0).mean() * 100), 1),
                "Bester %": round(float(vals.max()), 2),
                "Schlechtester %": round(float(vals.min()), 2),
            })
    return pd.DataFrame(out)


def coin_stats(horizon: str = "24h", include_baseline: bool = True) -> pd.DataFrame:
    df = events_dataframe()
    col = f"ret_{horizon}_pct"
    if df.empty or col not in df.columns:
        return pd.DataFrame()
    if not include_baseline and "trigger" in df.columns:
        df = df[df["trigger"] != "baseline"]
    df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df[col].notna()]
    if df.empty:
        return pd.DataFrame()
    rows = []
    for (asset, stage), g in df.groupby(["asset", "stage"], dropna=False):
        vals = g[col].dropna()
        rows.append({
            "Coin": asset,
            "Stage": f"{int(stage)}/10",
            "Signale": int(len(vals)),
            f"Ø {horizon} %": round(float(vals.mean()), 2),
            "Positiv %": round(float((vals > 0).mean() * 100), 1),
        })
    return pd.DataFrame(rows).sort_values(["Signale", f"Ø {horizon} %"], ascending=[False, False])


def lab_status() -> dict[str, Any]:
    events = _read_json(SIGNAL_EVENTS, []) or []
    state = _read_json(SIGNAL_STATE, {}) or {}
    stages = [int(e.get("stage", 0) or 0) for e in events]
    complete_24h = sum(1 for e in events if e.get("ret_24h_pct") is not None)
    complete_7d = sum(1 for e in events if e.get("ret_7d_pct") is not None)
    return {
        "total": len(events),
        "signals_9": sum(1 for x in stages if x == 9),
        "signals_10": sum(1 for x in stages if x >= 10),
        "complete_24h": complete_24h,
        "complete_7d": complete_7d,
        "last_scan_at": state.get("last_scan_at"),
        "initialized": bool(state.get("initialized")),
        "events_path": str(SIGNAL_EVENTS),
    }
