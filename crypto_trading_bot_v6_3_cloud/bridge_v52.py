from __future__ import annotations
import json, sys
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: bridge_v52.py <engine_dir> <state_json> <output_json>")
    engine = Path(sys.argv[1]).resolve()
    state_path = Path(sys.argv[2]).resolve()
    out = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(engine))
    from data import CRYPTO_UNIVERSE, load_hourly_history
    from forward_competition import evaluate_forward_competition, quality_breakout_frozen_params

    comp = json.loads(state_path.read_text(encoding="utf-8"))
    start_dt = pd.Timestamp(comp["started_at"]).to_pydatetime()
    now_dt = datetime.now().astimezone()
    warmup = start_dt - timedelta(days=90)
    assets = list(dict.fromkeys(list(comp.get("assets") or ["BTC", "ETH", "SOL", "LINK", "AVAX"]) + ["BTC"]))
    data = {a: load_hourly_history(CRYPTO_UNIVERSE[a], warmup, now_dt) for a in assets}
    table, results, eq = evaluate_forward_competition(
        data, started_at=comp["started_at"], scan_hours=int(comp.get("scan_hours", 6)),
        start_capital=float(comp.get("start_capital", 1000)), fee_pct=float(comp.get("fee_pct", 0.25)),
        quality_params=dict(comp.get("quality_params") or quality_breakout_frozen_params()),
    )
    trade_events = []
    for strategy, result in results.items():
        log = getattr(result, "trade_log", None)
        if log is None or log.empty:
            continue
        for row in log.where(pd.notna(log), None).to_dict(orient="records"):
            trade_events.append({
                "system": "V5.2",
                "strategy": strategy,
                "timestamp": str(row.get("timestamp", "")),
                "asset": str(row.get("asset", "")),
                "action": str(row.get("action", "")),
                "price": row.get("price"),
                "fee": row.get("fee_eur"),
                "pnl_eur": row.get("pnl_eur"),
                "reason": str(row.get("reason", "")),
            })
    trade_events.sort(key=lambda x: str(x.get("timestamp", "")))

    payload = {
        "ok": True, "started_at": comp["started_at"],
        "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rows": table.where(pd.notna(table), None).to_dict(orient="records"),
        "trade_events": trade_events,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            Path(sys.argv[3]).write_text(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        raise
