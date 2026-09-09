from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from market_data import CRYPTO_UNIVERSE, load_fast_prices

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "live_paper_long_short_state.json"
HISTORY_FILE = DATA_DIR / "live_paper_long_short_history.csv"
TRADES_FILE = DATA_DIR / "live_paper_long_short_trades.csv"

DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "LINK", "LTC", "DOT", "BCH"]
DEFAULTS = {
    "start_capital": 1000.0,
    "position_pct": 20.0,
    "max_positions": 3,
    "max_exposure_pct": 60.0,
    "fee_pct": 0.25,
    "slippage_pct": 0.10,
    "stop_loss_pct": 4.0,
    "take_profit_pct": 8.0,
    "trailing_activation_pct": 4.0,
    "trailing_stop_pct": 3.0,
    "long_min_rules": 9,
    "long_min_edge": 85.0,
    "short_min_rules": 7,
    "short_min_edge": 72.0,
    "confirm_scans": 1,
    "cooldown_hours": 6,
    # Conservative paper cost for a hypothetical 1x short/margin position.
    "short_funding_pct_per_8h": 0.01,
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


def _append_csv(path: Path, row: dict[str, Any], limit: int = 30000) -> None:
    frame = pd.DataFrame([row])
    if path.exists():
        try:
            old = pd.read_csv(path)
            frame = pd.concat([old, frame], ignore_index=True)
        except Exception:
            pass
    if len(frame) > limit:
        frame = frame.tail(limit)
    frame.to_csv(path, index=False)


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def state() -> dict[str, Any] | None:
    x = _read_json(STATE_FILE, None)
    return x if isinstance(x, dict) else None


def start_long_short_paper(assets: list[str] | None = None, start_capital: float = 1000.0, **params) -> dict[str, Any]:
    assets = [str(a).upper() for a in (assets or DEFAULT_ASSETS)]
    assets = [a for a in assets if a in CRYPTO_UNIVERSE]
    if not assets:
        raise ValueError("Mindestens ein unterstützter Coin muss gewählt sein.")
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in params.items() if k in cfg})
    cfg["start_capital"] = float(start_capital)
    if cfg["start_capital"] <= 0:
        raise ValueError("Startkapital muss > 0 sein.")
    if int(cfg["max_positions"]) < 1:
        raise ValueError("Maximale Positionen muss mindestens 1 sein.")
    now = _utc_now().isoformat()
    payload = {
        "version": "6.9",
        "mode": "LIVE_PAPER_LONG_SHORT_ONLY",
        "started_at": now,
        "active": True,
        "entry_paused": False,
        "assets": assets,
        "params": cfg,
        "cash": float(cfg["start_capital"]),
        "equity": float(cfg["start_capital"]),
        "peak_equity": float(cfg["start_capital"]),
        "max_drawdown_pct": 0.0,
        "fees_total": 0.0,
        "funding_total": 0.0,
        "realized_pnl": 0.0,
        "positions": {},
        "long_confirmations": {},
        "short_confirmations": {},
        "cooldown_until": {},
        "last_confirmation_scan": None,
        "last_update_at": None,
        "last_prices": {},
        "trade_count": 0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "long_entries": 0,
        "short_entries": 0,
    }
    _write_json(STATE_FILE, payload)
    return payload


def set_entry_paused(paused: bool) -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Long+Short Live-Paper wurde noch nicht gestartet.")
    s["entry_paused"] = bool(paused)
    _write_json(STATE_FILE, s)
    return s


def stop_long_short_paper() -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Long+Short Live-Paper wurde noch nicht gestartet.")
    s["active"] = False
    _write_json(STATE_FILE, s)
    return s


def resume_long_short_paper() -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Long+Short Live-Paper wurde noch nicht gestartet.")
    s["active"] = True
    _write_json(STATE_FILE, s)
    return s


def _monitor_rows(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {str(r.get("Coin", "")).upper(): r for r in ((payload or {}).get("rows", []) or []) if r.get("Coin")}


def _price_dict(snapshot: dict[str, Any]) -> dict[str, float]:
    out = {}
    for a, x in (snapshot or {}).items():
        try:
            p = float(x.get("price") if isinstance(x, dict) else x)
            if p > 0:
                out[str(a).upper()] = p
        except Exception:
            pass
    return out


def _long_signal(row: dict[str, Any] | None, regime: str, cfg: dict[str, Any]) -> tuple[bool, int, float, list[str]]:
    if not row:
        return False, 0, 0.0, ["keine Monitordaten"]
    rules = int(row.get("rules_ok", 0) or 0)
    edge = float(row.get("Edge", 0) or 0)
    missing = []
    if rules < int(cfg["long_min_rules"]): missing.append(f"Quality {rules}/{cfg['long_min_rules']}")
    if edge < float(cfg["long_min_edge"]): missing.append(f"Edge {edge:.1f}/{cfg['long_min_edge']:.0f}")
    if str(row.get("Risiko", "")) == "hoch": missing.append("Risiko hoch")
    if regime == "BEAR": missing.append("Bärenmarkt")
    return not missing, rules, edge, missing


def _short_metrics(row: dict[str, Any] | None, regime: str) -> tuple[int, float, list[str]]:
    """Independent bearish checklist built from the frozen monitor metrics.

    This deliberately does not invert the long quality score 1:1. It asks whether
    trend, momentum and returns are weak enough for a paper-only short candidate.
    """
    if not row:
        return 0, 0.0, ["keine Monitordaten"]
    trend = float(row.get("Trend", 50) or 50)
    momentum = float(row.get("Momentum", 50) or 50)
    rsi = float(row.get("RSI", 50) or 50)
    r24 = float(row.get("24h %", row.get("Return 24h %", row.get("24h", 0))) or 0)
    r7 = float(row.get("7T %", row.get("Return 7d %", row.get("7d", 0))) or 0)
    score = float(row.get("Score", 50) or 50)
    risk = str(row.get("Risiko", ""))

    checks = [
        (risk != "hoch", f"Risiko {risk or '–'}"),
        (regime != "BULL", f"Marktphase {regime}"),
        (trend <= 35.0, f"Trend {trend:.0f} ≤ 35"),
        (momentum <= 45.0, f"Momentum {momentum:.0f} ≤ 45"),
        (30.0 <= rsi <= 52.0, f"RSI {rsi:.1f} in 30–52"),
        (r24 <= -0.8, f"24h {r24:+.2f}% ≤ -0.8%"),
        (r7 <= 0.0, f"7T {r7:+.2f}% ≤ 0%"),
        (score <= 48.0, f"Long-Score {score:.0f} ≤ 48"),
    ]
    ok_count = sum(1 for ok, _ in checks if ok)
    missing = [txt for ok, txt in checks if not ok]

    weak_trend = max(0.0, min(100.0, 100.0 - trend))
    weak_momentum = max(0.0, min(100.0, 100.0 - momentum))
    score_weak = max(0.0, min(100.0, 100.0 - score))
    rsi_bear = max(0.0, min(100.0, 100.0 - abs(rsi - 40.0) * 5.0))
    r24_bear = max(0.0, min(100.0, 50.0 + (-r24) * 10.0))
    r7_bear = max(0.0, min(100.0, 50.0 + (-r7) * 3.0))
    edge = 0.24 * weak_trend + 0.22 * weak_momentum + 0.18 * score_weak + 0.14 * rsi_bear + 0.12 * r24_bear + 0.10 * r7_bear
    return ok_count, round(edge, 1), missing


def _short_signal(row: dict[str, Any] | None, regime: str, cfg: dict[str, Any]) -> tuple[bool, int, float, list[str]]:
    rules, edge, missing = _short_metrics(row, regime)
    extra = []
    if rules < int(cfg["short_min_rules"]): extra.append(f"Short-Regeln {rules}/{cfg['short_min_rules']}")
    if edge < float(cfg["short_min_edge"]): extra.append(f"Short-Edge {edge:.1f}/{cfg['short_min_edge']:.0f}")
    return not extra, rules, edge, missing + extra


def _funding_cost(pos: dict[str, Any], now: pd.Timestamp, cfg: dict[str, Any]) -> float:
    if str(pos.get("side")) != "SHORT":
        return 0.0
    try:
        opened = pd.Timestamp(pos.get("opened_at"))
        if opened.tzinfo is None:
            opened = opened.tz_localize("UTC")
        hours = max(0.0, (now - opened).total_seconds() / 3600.0)
    except Exception:
        hours = 0.0
    rate_8h = float(cfg.get("short_funding_pct_per_8h", 0.01)) / 100.0
    return float(pos.get("notional", 0.0)) * rate_8h * (hours / 8.0)


def _equity(s: dict[str, Any], prices: dict[str, float], now: pd.Timestamp | None = None) -> tuple[float, float, float]:
    now = now or _utc_now()
    cfg = s.get("params") or DEFAULTS
    fee = float(cfg.get("fee_pct", 0.25)) / 100.0
    slip = float(cfg.get("slippage_pct", 0.10)) / 100.0
    exposure = 0.0
    marked_positions = 0.0
    funding_accrued = 0.0
    for asset, pos in (s.get("positions") or {}).items():
        p = prices.get(asset, float(pos.get("last_price", pos.get("entry_price", 0))))
        side = str(pos.get("side", "LONG"))
        qty = float(pos.get("qty", 0))
        if side == "SHORT":
            cover = p * (1.0 + slip)
            close_fee = qty * cover * fee
            funding = _funding_cost(pos, now, cfg)
            gross_pnl = qty * (float(pos.get("entry_price", 0)) - cover)
            marked_positions += float(pos.get("collateral", pos.get("notional", 0))) + gross_pnl - close_fee - funding
            funding_accrued += funding
            exposure += float(pos.get("notional", 0))
        else:
            liquidation = qty * p * (1.0 - slip) * (1.0 - fee)
            marked_positions += liquidation
            exposure += qty * p
    return float(s.get("cash", 0)) + marked_positions, exposure, funding_accrued


def _event(action: str, side: str, asset: str, price: float, fee: float, pnl: float, reason: str, equity: float, funding: float = 0.0) -> dict[str, Any]:
    ts = _utc_now().isoformat()
    if action == "OPEN":
        icon = "🟢" if side == "LONG" else "🔻"
        label = "LONG" if side == "LONG" else "SHORT"
        text = f"↕️ {icon} LIVE-PAPER {label} · {asset} @ {price:,.4f} €\nGebühr {fee:.2f} € · {reason} · Konto {equity:,.2f} €"
    else:
        icon = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
        label = "LONG SCHLIESSEN" if side == "LONG" else "SHORT SCHLIESSEN"
        extra = f" · Funding {funding:.2f} €" if side == "SHORT" and funding > 0 else ""
        text = f"↕️ {icon} {label} · {asset} @ {price:,.4f} € · G/V {pnl:+.2f} €\nGebühr {fee:.2f} €{extra} · {reason} · Konto {equity:,.2f} €"
    return {"timestamp": ts, "action": action, "side": side, "asset": asset, "price": price, "fee": fee, "funding": funding, "pnl_eur": pnl, "reason": reason, "telegram": text}


def process_long_short_paper(monitor_payload: dict[str, Any] | None = None, price_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    s = state()
    if not s:
        return {"active": False, "message": "Long+Short Live-Paper noch nicht gestartet", "events": []}
    if not s.get("active", True):
        return {"active": False, "message": "Long+Short Live-Paper gestoppt", "events": []}

    cfg = s["params"]
    assets = list(s.get("assets") or [])
    monitor_payload = monitor_payload or {}
    rows = _monitor_rows(monitor_payload)
    regime = str(((monitor_payload.get("regime") or {}).get("code") or "NEUTRAL")).upper()
    if price_snapshot is None:
        price_snapshot = load_fast_prices(assets, interval="5m", period="5d")
    prices = _price_dict(price_snapshot)
    if not prices:
        return {"active": True, "message": "Keine aktuellen 5-Minuten-Kurse verfügbar", "events": []}

    now = _utc_now()
    fee_rate = float(cfg["fee_pct"]) / 100.0
    slip_rate = float(cfg["slippage_pct"]) / 100.0
    events: list[dict[str, Any]] = []

    # One confirmation step per new monitor scan, not every 5-minute mark.
    monitor_at = str(monitor_payload.get("evaluated_at") or "")
    if monitor_at and monitor_at != s.get("last_confirmation_scan"):
        for asset in assets:
            long_ok, _, _, _ = _long_signal(rows.get(asset), regime, cfg)
            short_ok, _, _, _ = _short_signal(rows.get(asset), regime, cfg)
            s.setdefault("long_confirmations", {})[asset] = (int(s.get("long_confirmations", {}).get(asset, 0)) + 1) if long_ok else 0
            s.setdefault("short_confirmations", {})[asset] = (int(s.get("short_confirmations", {}).get(asset, 0)) + 1) if short_ok else 0
        s["last_confirmation_scan"] = monitor_at

    # Manage open positions using near-live prices.
    for asset in list((s.get("positions") or {}).keys()):
        pos = s["positions"][asset]
        p = prices.get(asset)
        if not p:
            continue
        pos["last_price"] = p
        side = str(pos.get("side", "LONG"))
        entry = float(pos.get("entry_price", 0))
        reason = None
        if side == "SHORT":
            pos["trough_price"] = min(float(pos.get("trough_price", p)), p)
            stop = entry * (1.0 + float(cfg["stop_loss_pct"]) / 100.0)
            take = entry * (1.0 - float(cfg["take_profit_pct"]) / 100.0)
            trail_active = float(pos["trough_price"]) <= entry * (1.0 - float(cfg["trailing_activation_pct"]) / 100.0)
            trail = float(pos["trough_price"]) * (1.0 + float(cfg["trailing_stop_pct"]) / 100.0) if trail_active else None
            if p >= stop: reason = "Short Stop-Loss"
            elif p <= take: reason = "Short Take-Profit"
            elif trail is not None and p >= trail: reason = "Short Trailing-Stop"
            if reason:
                cover = p * (1.0 + slip_rate)
                qty = float(pos["qty"])
                close_fee = qty * cover * fee_rate
                funding = _funding_cost(pos, now, cfg)
                gross_pnl = qty * (entry - cover)
                settlement = float(pos.get("collateral", pos.get("notional", 0))) + gross_pnl - close_fee - funding
                pnl = gross_pnl - float(pos.get("open_fee", 0)) - close_fee - funding
                s["cash"] = float(s.get("cash", 0)) + settlement
                s["fees_total"] = float(s.get("fees_total", 0)) + close_fee
                s["funding_total"] = float(s.get("funding_total", 0)) + funding
                s["realized_pnl"] = float(s.get("realized_pnl", 0)) + pnl
                s["closed_trades"] = int(s.get("closed_trades", 0)) + 1
                s["wins"] = int(s.get("wins", 0)) + (1 if pnl > 0 else 0)
                s["losses"] = int(s.get("losses", 0)) + (1 if pnl < 0 else 0)
                del s["positions"][asset]
                s.setdefault("cooldown_until", {})[asset] = (now + pd.Timedelta(hours=float(cfg["cooldown_hours"]))).isoformat()
                eq, _, _ = _equity(s, prices, now)
                e = _event("CLOSE", "SHORT", asset, cover, close_fee, pnl, reason, eq, funding)
                events.append(e); _append_csv(TRADES_FILE, e)
        else:
            pos["peak_price"] = max(float(pos.get("peak_price", p)), p)
            stop = entry * (1.0 - float(cfg["stop_loss_pct"]) / 100.0)
            take = entry * (1.0 + float(cfg["take_profit_pct"]) / 100.0)
            trail_active = float(pos["peak_price"]) >= entry * (1.0 + float(cfg["trailing_activation_pct"]) / 100.0)
            trail = float(pos["peak_price"]) * (1.0 - float(cfg["trailing_stop_pct"]) / 100.0) if trail_active else None
            if p <= stop: reason = "Long Stop-Loss"
            elif p >= take: reason = "Long Take-Profit"
            elif trail is not None and p <= trail: reason = "Long Trailing-Stop"
            if reason:
                sell = p * (1.0 - slip_rate)
                qty = float(pos["qty"])
                gross = qty * sell
                close_fee = gross * fee_rate
                net = gross - close_fee
                cost = qty * entry + float(pos.get("open_fee", 0))
                pnl = net - cost
                s["cash"] = float(s.get("cash", 0)) + net
                s["fees_total"] = float(s.get("fees_total", 0)) + close_fee
                s["realized_pnl"] = float(s.get("realized_pnl", 0)) + pnl
                s["closed_trades"] = int(s.get("closed_trades", 0)) + 1
                s["wins"] = int(s.get("wins", 0)) + (1 if pnl > 0 else 0)
                s["losses"] = int(s.get("losses", 0)) + (1 if pnl < 0 else 0)
                del s["positions"][asset]
                s.setdefault("cooldown_until", {})[asset] = (now + pd.Timedelta(hours=float(cfg["cooldown_hours"]))).isoformat()
                eq, _, _ = _equity(s, prices, now)
                e = _event("CLOSE", "LONG", asset, sell, close_fee, pnl, reason, eq)
                events.append(e); _append_csv(TRADES_FILE, e)

    # Rank new long and short candidates together by their own edge.
    if not s.get("entry_paused", False) and len(s.get("positions", {})) < int(cfg["max_positions"]):
        candidates = []
        for asset in assets:
            if asset in s.get("positions", {}) or asset not in prices:
                continue
            cd = (s.get("cooldown_until") or {}).get(asset)
            if cd:
                try:
                    if pd.Timestamp(cd) > now:
                        continue
                except Exception:
                    pass
            row = rows.get(asset)
            long_ok, long_rules, long_edge, _ = _long_signal(row, regime, cfg)
            short_ok, short_rules, short_edge, _ = _short_signal(row, regime, cfg)
            if long_ok and int((s.get("long_confirmations") or {}).get(asset, 0)) >= int(cfg["confirm_scans"]):
                candidates.append((long_edge, long_rules, "LONG", asset, row))
            if short_ok and int((s.get("short_confirmations") or {}).get(asset, 0)) >= int(cfg["confirm_scans"]):
                candidates.append((short_edge, short_rules, "SHORT", asset, row))
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

        for edge, rules, side, asset, row in candidates:
            if len(s.get("positions", {})) >= int(cfg["max_positions"]):
                break
            eq_now, exposure_now, _ = _equity(s, prices, now)
            if eq_now <= 0:
                break
            max_exposure = eq_now * float(cfg["max_exposure_pct"]) / 100.0
            room = max(0.0, max_exposure - exposure_now)
            target = min(eq_now * float(cfg["position_pct"]) / 100.0, room)
            raw = prices[asset]
            if target < 10.0:
                break

            if side == "SHORT":
                exec_price = raw * (1.0 - slip_rate)  # adverse sell slippage
                qty = target / exec_price
                notional = qty * exec_price
                open_fee = notional * fee_rate
                required = notional + open_fee  # 1x collateral, no leverage
                if required > float(s.get("cash", 0)) + 1e-8:
                    continue
                s["cash"] = float(s.get("cash", 0)) - required
                s["fees_total"] = float(s.get("fees_total", 0)) + open_fee
                s["positions"][asset] = {
                    "side": "SHORT", "qty": qty, "entry_price": exec_price, "signal_price": raw,
                    "open_fee": open_fee, "collateral": notional, "notional": notional,
                    "opened_at": now.isoformat(), "trough_price": raw, "last_price": raw,
                    "entry_rules": int(rules), "entry_edge": float(edge), "entry_score": float((row or {}).get("Score", 0)),
                }
                s["short_entries"] = int(s.get("short_entries", 0)) + 1
                s["trade_count"] = int(s.get("trade_count", 0)) + 1
                eq_after, _, _ = _equity(s, prices, now)
                e = _event("OPEN", "SHORT", asset, exec_price, open_fee, 0.0, f"Short {rules}/8 · Edge {edge:.1f} · Regime {regime}", eq_after)
                events.append(e); _append_csv(TRADES_FILE, e)
            else:
                exec_price = raw * (1.0 + slip_rate)
                qty = target / (exec_price * (1.0 + fee_rate))
                gross = qty * exec_price
                open_fee = gross * fee_rate
                total = gross + open_fee
                if total > float(s.get("cash", 0)) + 1e-8:
                    continue
                s["cash"] = float(s.get("cash", 0)) - total
                s["fees_total"] = float(s.get("fees_total", 0)) + open_fee
                s["positions"][asset] = {
                    "side": "LONG", "qty": qty, "entry_price": exec_price, "signal_price": raw,
                    "open_fee": open_fee, "notional": gross, "opened_at": now.isoformat(),
                    "peak_price": raw, "last_price": raw, "entry_rules": int(rules),
                    "entry_edge": float(edge), "entry_score": float((row or {}).get("Score", 0)),
                }
                s["long_entries"] = int(s.get("long_entries", 0)) + 1
                s["trade_count"] = int(s.get("trade_count", 0)) + 1
                eq_after, _, _ = _equity(s, prices, now)
                e = _event("OPEN", "LONG", asset, exec_price, open_fee, 0.0, f"Quality {rules}/10 · Edge {edge:.1f} · Regime {regime}", eq_after)
                events.append(e); _append_csv(TRADES_FILE, e)

    equity, exposure, funding_accrued = _equity(s, prices, now)
    s["equity"] = equity
    s["peak_equity"] = max(float(s.get("peak_equity", equity)), equity)
    if s["peak_equity"] > 0:
        dd = max(0.0, (float(s["peak_equity"]) - equity) / float(s["peak_equity"]) * 100.0)
        s["max_drawdown_pct"] = max(float(s.get("max_drawdown_pct", 0)), dd)
    s["last_update_at"] = now.isoformat()
    s["last_prices"] = price_snapshot
    _write_json(STATE_FILE, s)

    _append_csv(HISTORY_FILE, {
        "timestamp": now.isoformat(), "equity_eur": round(equity, 6), "cash_eur": round(float(s.get("cash", 0)), 6),
        "exposure_eur": round(exposure, 6), "positions": len(s.get("positions", {})),
        "long_positions": sum(1 for p in (s.get("positions") or {}).values() if p.get("side") == "LONG"),
        "short_positions": sum(1 for p in (s.get("positions") or {}).values() if p.get("side") == "SHORT"),
        "fees_eur": round(float(s.get("fees_total", 0)), 6), "funding_accrued_eur": round(funding_accrued, 6),
        "realized_pnl_eur": round(float(s.get("realized_pnl", 0)), 6), "drawdown_pct": round(float(s.get("max_drawdown_pct", 0)), 6),
    })
    return {"active": True, "message": f"{len(prices)} Kurse · {len(s.get('positions',{}))} Positionen · Konto {equity:.2f} €", "events": events, "state": s}


def positions_dataframe(s: dict[str, Any] | None = None) -> pd.DataFrame:
    s = s or state() or {}
    prices = _price_dict(s.get("last_prices") or {})
    cfg = s.get("params") or DEFAULTS
    now = _utc_now()
    out = []
    for asset, pos in (s.get("positions") or {}).items():
        side = str(pos.get("side", "LONG"))
        cur = prices.get(asset, float(pos.get("last_price", pos.get("entry_price", 0))))
        entry = float(pos.get("entry_price", 0)); qty = float(pos.get("qty", 0))
        if side == "SHORT":
            pnl_pct = (entry / cur - 1) * 100 if cur > 0 else 0.0
            stop = entry * (1 + float(cfg.get("stop_loss_pct", 4))/100)
            take = entry * (1 - float(cfg.get("take_profit_pct", 8))/100)
            mark = "🔻 SHORT"
            funding = _funding_cost(pos, now, cfg)
        else:
            pnl_pct = (cur / entry - 1) * 100 if entry > 0 else 0.0
            stop = entry * (1 - float(cfg.get("stop_loss_pct", 4))/100)
            take = entry * (1 + float(cfg.get("take_profit_pct", 8))/100)
            mark = "🟢 LONG"
            funding = 0.0
        out.append({
            "Coin": asset, "Richtung": mark, "Einstieg €": round(entry, 6), "Aktuell €": round(cur, 6),
            "Menge": qty, "Kurs-G/V %": round(pnl_pct, 2), "Stop €": round(stop, 6), "Ziel €": round(take, 6),
            "Funding bisher €": round(funding, 4), "Entry-Regeln": int(pos.get("entry_rules", 0)),
            "Entry-Edge": round(float(pos.get("entry_edge", 0)), 1), "Seit": str(pos.get("opened_at", "")),
        })
    return pd.DataFrame(out)


def market_dataframe(monitor_payload: dict[str, Any] | None = None, s: dict[str, Any] | None = None) -> pd.DataFrame:
    s = s or state() or {}
    monitor_payload = monitor_payload or {}
    rows = _monitor_rows(monitor_payload)
    prices = _price_dict(s.get("last_prices") or {})
    regime = str(((monitor_payload.get("regime") or {}).get("code") or "NEUTRAL")).upper()
    cfg = s.get("params") or DEFAULTS
    out = []
    for asset in s.get("assets", DEFAULT_ASSETS):
        r = rows.get(asset, {})
        l_ok, l_rules, l_edge, _ = _long_signal(r, regime, cfg)
        s_ok, s_rules, s_edge, _ = _short_signal(r, regime, cfg)
        if asset in (s.get("positions") or {}):
            decision = str(s["positions"][asset].get("side", "LONG"))
        elif l_ok and (not s_ok or l_edge >= s_edge):
            decision = "LONG bereit"
        elif s_ok:
            decision = "SHORT bereit"
        else:
            decision = "WARTEN"
        out.append({
            "Coin": asset, "Live €": prices.get(asset), "Regime": regime, "Entscheidung": decision,
            "Long Regeln": l_rules, "Long Edge": round(l_edge, 1), "Short Regeln": s_rules, "Short Edge": round(s_edge, 1),
            "Trend": r.get("Trend"), "Momentum": r.get("Momentum"), "RSI": r.get("RSI"), "24h %": r.get("24h %"), "7T %": r.get("7T %"),
        })
    return pd.DataFrame(out)


def history_dataframe(limit: int = 1500) -> pd.DataFrame:
    if not HISTORY_FILE.exists(): return pd.DataFrame()
    try: return pd.read_csv(HISTORY_FILE).tail(limit)
    except Exception: return pd.DataFrame()


def trades_dataframe(limit: int = 1000) -> pd.DataFrame:
    if not TRADES_FILE.exists(): return pd.DataFrame()
    try: return pd.read_csv(TRADES_FILE).tail(limit)
    except Exception: return pd.DataFrame()
