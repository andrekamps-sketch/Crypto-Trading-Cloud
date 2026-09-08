from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from market_data import CRYPTO_UNIVERSE, load_recent, load_v6_bundle
from monitor import score_frame, classify_regime, rescore_quality
from experiments import run_pairs, run_grid

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
ENGINE_V52 = ROOT / "engines" / "v52"

V52_STATE = DATA_DIR / "forward_competition.json"
V6_STATE = DATA_DIR / "v6_forward_state.json"
V52_LATEST = DATA_DIR / "v52_latest.json"
V6_LATEST = DATA_DIR / "v6_latest.json"
MONITOR_LATEST = DATA_DIR / "monitor_latest.json"
CENTRAL_HISTORY = DATA_DIR / "central_snapshots.csv"
WORKER_STATUS = DATA_DIR / "worker_status.json"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _required(payload: dict, keys: list[str], label: str) -> None:
    missing = [k for k in keys if k not in payload]
    if missing:
        raise ValueError(f"{label}: Pflichtfelder fehlen: {', '.join(missing)}")


def validate_v52_state(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("V5.2-State ist kein JSON-Objekt.")
    _required(payload, ["started_at", "start_capital", "fee_pct", "scan_hours", "assets"], "V5.2")
    pd.Timestamp(payload["started_at"])
    assets = [str(x).upper() for x in payload.get("assets", [])]
    if "BTC" not in assets or len(assets) < 2:
        raise ValueError("V5.2-State muss BTC und mindestens einen weiteren Coin enthalten.")
    unknown = [a for a in assets if a not in CRYPTO_UNIVERSE]
    if unknown:
        raise ValueError("Unbekannte Coins im V5.2-State: " + ", ".join(unknown))
    payload = dict(payload)
    payload["assets"] = assets
    payload.setdefault("rules_locked", True)
    return payload


def validate_v6_state(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("V6-State ist kein JSON-Objekt.")
    _required(payload, ["start_utc", "capital", "fee_pct"], "V6")
    pd.Timestamp(payload["start_utc"])
    return dict(payload)


def save_state(kind: str, payload: dict) -> Path:
    if kind == "v52":
        payload = validate_v52_state(payload)
        write_json(V52_STATE, payload)
        return V52_STATE
    if kind == "v6":
        payload = validate_v6_state(payload)
        write_json(V6_STATE, payload)
        return V6_STATE
    raise ValueError("Unbekannter State-Typ")


def state_status() -> dict[str, Any]:
    return {
        "v52": read_json(V52_STATE, None),
        "v6": read_json(V6_STATE, None),
        "data_dir": str(DATA_DIR),
        "worker": read_json(WORKER_STATUS, {}),
    }


def evaluate_v52() -> dict:
    if not V52_STATE.exists():
        raise RuntimeError("V5.2-State wurde noch nicht in die Cloud importiert.")
    out = DATA_DIR / "_v52_bridge_result.json"
    cmd = [sys.executable, str(ROOT / "bridge_v52.py"), str(ENGINE_V52), str(V52_STATE), str(out)]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    payload = read_json(out, {})
    if not payload.get("ok"):
        msg = payload.get("error") or cp.stderr[-1500:] or "V5.2-Auswertung fehlgeschlagen"
        raise RuntimeError(msg)
    write_json(V52_LATEST, payload)
    return payload


def evaluate_v6() -> dict[str, dict]:
    state = read_json(V6_STATE, None)
    if not state:
        raise RuntimeError("V6-State wurde noch nicht in die Cloud importiert.")
    start = pd.Timestamp(state["start_utc"])
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    else:
        start = start.tz_convert("UTC")
    bundle = load_v6_bundle(start, warmup_days=75)
    fee_rate = float(state.get("fee_pct", 0.25)) / 100.0
    cap = float(state.get("capital", 1000))
    results = {
        "BTC/ETH Pairs": run_pairs(bundle, start, cap, fee_rate),
        "BTC Grid": run_grid("BTC", bundle, start, cap, fee_rate),
        "ETH Grid": run_grid("ETH", bundle, start, cap, fee_rate),
    }
    trade_events = []
    for strategy, result in results.items():
        for t in result.get("trades", []) or []:
            trade_events.append({
                "system": "V6",
                "strategy": strategy,
                "timestamp": str(getattr(t, "timestamp", "")),
                "asset": str(getattr(t, "asset", "")),
                "action": str(getattr(t, "action", "")),
                "price": float(getattr(t, "price", 0.0)),
                "fee": float(getattr(t, "fee", 0.0)),
                "pnl_eur": float(getattr(t, "realized_pnl", 0.0)),
                "reason": str(getattr(t, "reason", "")),
            })
    trade_events.sort(key=lambda x: str(x.get("timestamp", "")))
    table = v6_table(results)
    write_json(V6_LATEST, {
        "ok": True,
        "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rows": table.where(pd.notna(table), None).to_dict(orient="records"),
        "trade_events": trade_events,
    })
    return results


def v52_table(payload: dict | None = None) -> pd.DataFrame:
    payload = payload or read_json(V52_LATEST, {})
    rows = payload.get("rows", []) if payload else []
    out = []
    for r in rows:
        out.append({
            "System": "V5.2", "Strategie": r.get("Strategie"), "Status": r.get("Status"),
            "Kontowert €": r.get("Kontowert €"), "Rendite %": r.get("Rendite %"),
            "Drawdown %": r.get("Drawdown %"), "PF": r.get("Profit Factor"),
            "Trades": r.get("Abgeschl. Trades", 0), "Gebühren €": r.get("Gebühren €"),
            "Hinweis": r.get("Hinweis", ""),
        })
    return pd.DataFrame(out)


def v6_table(results: dict[str, dict] | None = None) -> pd.DataFrame:
    if results is None:
        payload = read_json(V6_LATEST, {})
        return pd.DataFrame(payload.get("rows", [])) if payload else pd.DataFrame()
    rows = []
    for name, r in results.items():
        if r.get("status") != "läuft":
            rows.append({"System": "V6", "Strategie": name, "Status": "wartet", "Kontowert €": None,
                         "Rendite %": None, "Drawdown %": None, "PF": None, "Trades": 0,
                         "Gebühren €": None, "Hinweis": r.get("hint", "")})
        else:
            pf = r.get("profit_factor", 0)
            pf_val = "∞" if pf == float("inf") else round(float(pf), 2)
            rows.append({"System": "V6", "Strategie": name, "Status": "läuft",
                         "Kontowert €": round(float(r["end"]), 2), "Rendite %": round(float(r["return_pct"]), 2),
                         "Drawdown %": round(float(r["drawdown_pct"]), 2), "PF": pf_val,
                         "Trades": len(r.get("trades", [])), "Gebühren €": round(float(r.get("fees", 0)), 2),
                         "Hinweis": r.get("position", "")})
    return pd.DataFrame(rows)


def save_snapshot(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    x = df.copy()
    x.insert(0, "timestamp", datetime.now().astimezone().isoformat(timespec="seconds"))
    if CENTRAL_HISTORY.exists():
        try:
            old = pd.read_csv(CENTRAL_HISTORY)
            x = pd.concat([old, x], ignore_index=True)
        except Exception:
            pass
    # keep history bounded for a small cloud disk
    if len(x) > 20000:
        x = x.tail(20000)
    x.to_csv(CENTRAL_HISTORY, index=False)


def evaluate_all() -> pd.DataFrame:
    frames = []
    if V52_STATE.exists():
        frames.append(v52_table(evaluate_v52()))
    if V6_STATE.exists():
        frames.append(v6_table(evaluate_v6()))
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    save_snapshot(combined)
    return combined


def scan_market(assets: list[str] | None = None) -> dict:
    assets = assets or list(CRYPTO_UNIVERSE)
    rows = []
    errors = []
    for asset in assets:
        try:
            df = load_recent(asset, "60d", "1h")
            rows.append(score_frame(asset, df, "NEUTRAL"))
        except Exception as exc:
            errors.append(f"{asset}: {exc}")
    btc = next((r for r in rows if r.asset == "BTC"), None)
    regime = classify_regime(btc) if btc else None
    if regime:
        rows = rescore_quality(rows, regime["code"])
    rows = sorted(rows, key=lambda r: (r.quality_ready, r.rules_ok, r.quality_edge, r.score), reverse=True)
    serial = []
    for r in rows:
        serial.append({
            "Coin": r.asset, "Quality": "READY" if r.quality_ready else f"{r.rules_ok}/{r.rules_total}",
            "quality_ready": bool(r.quality_ready), "rules_ok": int(r.rules_ok), "rules_total": int(r.rules_total),
            "Score": float(r.score), "Edge": float(r.quality_edge), "Signal": str(r.signal), "Risiko": str(r.risk),
            "Preis €": float(r.price), "Trend": float(r.trend), "Momentum": float(r.momentum), "RSI": float(r.rsi),
            "Breakout": float(r.breakout), "Volumen x": float(r.volume_ratio), "24h %": float(r.return_24h),
            "7T %": float(r.return_7d), "Abstand 30T-Hoch %": float(r.high_distance), "Fehlt": str(r.missing),
            "Notiz": str(r.note),
        })
    payload = {
        "ok": True, "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "regime": regime, "rows": serial, "errors": errors,
    }
    write_json(MONITOR_LATEST, payload)
    return payload


def latest_combined() -> pd.DataFrame:
    frames = []
    a = v52_table()
    b = v6_table()
    if not a.empty:
        frames.append(a)
    if not b.empty:
        frames.append(b)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
