from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from cloud_core import DATA_DIR, read_json, write_json

API = "https://api.binance.com"
STATE_FILE = DATA_DIR / "candlestick_v2_state.json"
TRADES_FILE = DATA_DIR / "candlestick_v2_trades.json"
LATEST_FILE = DATA_DIR / "candlestick_v2_latest.json"

DEFAULTS = {
    "start_capital": 1000.0,
    "fee_pct": 0.10,
    "risk_budget_eur": 2.50,
    "reward_risk": 2.0,
    "cooldown_hours": 6.0,
    "max_positions": 3,
    "max_notional_pct": 20.0,
    "min_quote_volume_24h": 10_000_000.0,
    "universe_size": 50,
    "min_quality_score": 6.0,
    "volume_ratio_min": 1.15,
    "timeframes": ["15m", "1h"],
}

STABLE_BASES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "EUR", "AEUR", "TRY", "BRL", "BIDR"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _http_json(path: str, params: dict[str, Any] | None = None, timeout: int = 12) -> Any:
    qs = urllib.parse.urlencode(params or {})
    url = API + path + (("?" + qs) if qs else "")
    req = urllib.request.Request(url, headers={"User-Agent": "CryptoTradingZentrale-CandlestickV2/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _eligible_symbols() -> dict[str, str]:
    info = _http_json("/api/v3/exchangeInfo")
    out: dict[str, str] = {}
    for x in info.get("symbols", []) if isinstance(info, dict) else []:
        if str(x.get("status")) != "TRADING" or str(x.get("quoteAsset")) != "USDT":
            continue
        if x.get("isSpotTradingAllowed") is False:
            continue
        base = str(x.get("baseAsset", "")).upper()
        symbol = str(x.get("symbol", "")).upper()
        if not base or not symbol or base in STABLE_BASES or base.endswith(LEVERAGED_SUFFIXES):
            continue
        out[symbol] = base
    return out


def _market_snapshot(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    eligible = _eligible_symbols()
    raw = _http_json("/api/v3/ticker/24hr")
    rows: list[dict[str, Any]] = []
    for t in raw if isinstance(raw, list) else []:
        symbol = str(t.get("symbol", "")).upper()
        coin = eligible.get(symbol)
        if not coin:
            continue
        qv = _f(t.get("quoteVolume"))
        price = _f(t.get("lastPrice"))
        if qv < float(cfg["min_quote_volume_24h"]) or price <= 0:
            continue
        rows.append({
            "symbol": symbol,
            "coin": coin,
            "price": price,
            "quote_volume": qv,
            "change_24h": _f(t.get("priceChangePercent")),
        })
    rows.sort(key=lambda x: x["quote_volume"], reverse=True)
    keep = rows[: max(10, int(cfg["universe_size"]))]
    # Majors are always included if tradable/liquid enough.
    majors = {"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE"}
    present = {r["coin"] for r in keep}
    for r in rows:
        if r["coin"] in majors and r["coin"] not in present:
            keep.append(r)
            present.add(r["coin"])
    return keep


def _klines(symbol: str, interval: str, limit: int = 120) -> pd.DataFrame:
    raw = _http_json("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    rows = []
    now_ms = int(_now().timestamp() * 1000)
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, list) or len(x) < 11:
            continue
        close_ms = int(_f(x[6]))
        if close_ms > now_ms:
            continue
        rows.append({
            "timestamp": pd.to_datetime(int(x[0]), unit="ms", utc=True),
            "close_time": pd.to_datetime(close_ms, unit="ms", utc=True),
            "open": _f(x[1]), "high": _f(x[2]), "low": _f(x[3]), "close": _f(x[4]),
            "volume": _f(x[5]), "quote_volume": _f(x[7]),
        })
    return pd.DataFrame(rows).set_index("timestamp") if rows else pd.DataFrame()


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _pattern(df: pd.DataFrame) -> tuple[str, str, float]:
    if len(df) < 3:
        return "", "", 0.0
    a = df.iloc[-2]
    b = df.iloc[-1]
    ao, ac = float(a.open), float(a.close)
    bo, bc, bh, bl = map(float, [b.open, b.close, b.high, b.low])
    body = abs(bc - bo)
    rng = max(1e-12, bh - bl)
    upper = bh - max(bo, bc)
    lower = min(bo, bc) - bl

    bull_engulf = ac < ao and bc > bo and bo <= ac and bc >= ao
    bear_engulf = ac > ao and bc < bo and bo >= ac and bc <= ao
    hammer = bc > bo and lower >= 2.2 * max(body, rng * 0.08) and upper <= body * 0.8
    shooting = bc < bo and upper >= 2.2 * max(body, rng * 0.08) and lower <= body * 0.8

    avg_body = (df["close"] - df["open"]).abs().iloc[-21:-1].mean() if len(df) >= 22 else (df["close"] - df["open"]).abs().iloc[:-1].mean()
    avg_body = float(avg_body) if pd.notna(avg_body) else 0.0
    bull_momentum = bc > bo and body >= 1.5 * max(avg_body, 1e-12) and (bh - bc) <= 0.18 * rng
    bear_momentum = bc < bo and body >= 1.5 * max(avg_body, 1e-12) and (bc - bl) <= 0.18 * rng

    if bull_engulf:
        return "LONG", "Bullish Engulfing", 2.0
    if bear_engulf:
        return "SHORT", "Bearish Engulfing", 2.0
    if hammer:
        return "LONG", "Hammer", 1.7
    if shooting:
        return "SHORT", "Shooting Star", 1.7
    if bull_momentum:
        return "LONG", "Momentum Candle", 1.5
    if bear_momentum:
        return "SHORT", "Momentum Candle", 1.5
    return "", "", 0.0


def _trend(df: pd.DataFrame) -> tuple[str, float, float, float]:
    c = df["close"].astype(float)
    e20 = _ema(c, 20)
    e50 = _ema(c, 50)
    price = float(c.iloc[-1])
    if price > float(e20.iloc[-1]) > float(e50.iloc[-1]):
        return "LONG", price, float(e20.iloc[-1]), float(e50.iloc[-1])
    if price < float(e20.iloc[-1]) < float(e50.iloc[-1]):
        return "SHORT", price, float(e20.iloc[-1]), float(e50.iloc[-1])
    return "NEUTRAL", price, float(e20.iloc[-1]), float(e50.iloc[-1])


def _higher_tf(tf: str) -> str:
    return "1h" if tf == "15m" else "4h"


def _stop_bounds(tf: str) -> tuple[float, float]:
    return (0.008, 0.025) if tf == "15m" else (0.010, 0.035)


def _risk_factor(quote_volume: float, atr_pct: float) -> tuple[float, str]:
    if quote_volume >= 500_000_000 and atr_pct <= 2.0:
        return 1.0, "Core"
    if quote_volume >= 75_000_000 and atr_pct <= 4.0:
        return 0.75, "Normal"
    return 0.50, "Volatil"


def _evaluate_symbol(row: dict[str, Any], tf: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(row["symbol"])
    df = _klines(symbol, tf, 130)
    if len(df) < 60:
        return None
    side, pattern, pattern_score = _pattern(df)
    if not side:
        return None

    cur_trend, price, e20, e50 = _trend(df)
    if cur_trend != side:
        return None

    htf = _higher_tf(tf)
    hdf = _klines(symbol, htf, 100)
    if len(hdf) < 55:
        return None
    htrend, _, _, _ = _trend(hdf)
    if htrend != side:
        return None

    close = df["close"].astype(float)
    rsi = float(_rsi(close).iloc[-1])
    vol_avg = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 22 else float(df["volume"].iloc[:-1].mean())
    vol_ratio = float(df["volume"].iloc[-1]) / max(vol_avg, 1e-12)
    atr = float(_atr(df).iloc[-1])
    atr_pct = atr / max(price, 1e-12) * 100.0

    rsi_ok = (42 <= rsi <= 65) if side == "LONG" else (35 <= rsi <= 58)
    vol_ok = vol_ratio >= float(cfg["volume_ratio_min"])

    # Score: pattern 1.5-2, current trend 1.5, HTF alignment 2, RSI 1, volume 1, EMA separation 0.5.
    ema_sep = abs(e20 - e50) / max(price, 1e-12) * 100.0
    score = pattern_score + 1.5 + 2.0 + (1.0 if rsi_ok else 0.0) + (1.0 if vol_ok else 0.0) + (0.5 if ema_sep >= 0.20 else 0.0)
    # We deliberately require at least one of volume/RSI and the overall score threshold.
    if score < float(cfg["min_quality_score"]) or not (rsi_ok or vol_ok):
        return None

    lo, hi = _stop_bounds(tf)
    stop_pct = min(hi, max(lo, 1.35 * atr / max(price, 1e-12)))
    rr = float(cfg["reward_risk"])
    if side == "LONG":
        stop = price * (1 - stop_pct)
        take = price * (1 + rr * stop_pct)
    else:
        stop = price * (1 + stop_pct)
        take = price * (1 - rr * stop_pct)

    factor, risk_tier = _risk_factor(float(row["quote_volume"]), atr_pct)
    return {
        "symbol": symbol, "coin": str(row["coin"]), "timeframe": tf, "side": side,
        "pattern": pattern, "score": round(score, 2), "price": price, "stop": stop, "take": take,
        "stop_pct": stop_pct, "rsi": rsi, "volume_ratio": vol_ratio, "atr_pct": atr_pct,
        "quote_volume": float(row["quote_volume"]), "risk_factor": factor, "risk_tier": risk_tier,
        "bar_close": str(df["close_time"].iloc[-1]), "htf": htf,
    }


def _default_state(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    c = {**DEFAULTS, **(cfg or {})}
    return {
        "version": "2.0", "active": True, "started_at": _iso(), "config": c,
        "balance": float(c["start_capital"]), "positions": {}, "cooldowns": {},
        "last_scan_bar": {}, "stats": {"entries": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "fees": 0.0},
    }


def state() -> dict[str, Any] | None:
    return read_json(STATE_FILE, None)


def start_candlestick_v2(config: dict[str, Any] | None = None, reset: bool = False) -> dict[str, Any]:
    old = state()
    if old and not reset:
        old["active"] = True
        if config:
            old["config"] = {**DEFAULTS, **old.get("config", {}), **config}
        write_json(STATE_FILE, old)
        return old
    s = _default_state(config)
    write_json(STATE_FILE, s)
    if reset:
        write_json(TRADES_FILE, [])
    return s


def stop_candlestick_v2() -> dict[str, Any]:
    s = state() or _default_state()
    s["active"] = False
    write_json(STATE_FILE, s)
    return s


def resume_candlestick_v2() -> dict[str, Any]:
    return start_candlestick_v2(reset=False)


def _cooldown_active(s: dict[str, Any], coin: str) -> bool:
    raw = str((s.get("cooldowns") or {}).get(coin, "") or "")
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > _now()
    except Exception:
        return False


def _record_trade(t: dict[str, Any]) -> None:
    trades = read_json(TRADES_FILE, []) or []
    trades.append(t)
    if len(trades) > 3000:
        trades = trades[-3000:]
    write_json(TRADES_FILE, trades)


def _close_position(s: dict[str, Any], coin: str, pos: dict[str, Any], exit_price: float, reason: str) -> dict[str, Any]:
    cfg = s["config"]
    entry = float(pos["entry"])
    notional = float(pos["notional"])
    side = str(pos["side"])
    fee_rate = float(cfg["fee_pct"]) / 100.0
    direction = 1.0 if side == "LONG" else -1.0
    gross = notional * direction * ((exit_price / entry) - 1.0)
    fees = notional * fee_rate + (notional * (exit_price / entry)) * fee_rate
    net = gross - fees
    s["balance"] = float(s.get("balance", cfg["start_capital"])) + net
    stats = s.setdefault("stats", {})
    stats["net_pnl"] = float(stats.get("net_pnl", 0.0)) + net
    stats["fees"] = float(stats.get("fees", 0.0)) + fees
    if net >= 0:
        stats["wins"] = int(stats.get("wins", 0)) + 1
    else:
        stats["losses"] = int(stats.get("losses", 0)) + 1
    if "Stop-Loss" in reason:
        until = _now() + timedelta(hours=float(cfg["cooldown_hours"]))
        s.setdefault("cooldowns", {})[coin] = _iso(until)

    trade = {
        "timestamp": _iso(), "coin": coin, "symbol": pos["symbol"], "timeframe": pos["timeframe"],
        "side": side, "entry": entry, "exit": exit_price, "notional": notional,
        "gross_pnl": gross, "fees": fees, "net_pnl": net, "reason": reason,
        "pattern": pos.get("pattern", ""), "quality_score": pos.get("score", 0),
    }
    trade["telegram"] = (
        f"🕯️ Candlestick V2 {side} geschlossen · {coin}/{pos['timeframe']} · {reason}\n"
        f"Entry {entry:.8g} → Exit {exit_price:.8g} USDT · Netto {net:+.2f} €"
    )
    _record_trade(trade)
    s.get("positions", {}).pop(coin, None)
    return trade


def _process_exits(s: dict[str, Any], market_by_coin: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for coin, pos in list((s.get("positions") or {}).items()):
        m = market_by_coin.get(coin)
        if not m:
            continue
        p = float(m["price"])
        side = str(pos["side"])
        stop = float(pos["stop"])
        take = float(pos["take"])
        entry = float(pos["entry"])
        one_r = abs(entry - stop)

        # Break-even protection after +1R: cover expected round-trip fee plus a tiny buffer.
        be = float(pos.get("break_even_stop", 0.0) or 0.0)
        if side == "LONG" and p >= entry + one_r and be <= 0:
            fee_buf = 2.2 * float(s["config"]["fee_pct"]) / 100.0
            pos["break_even_stop"] = entry * (1 + fee_buf)
            stop = max(stop, float(pos["break_even_stop"]))
            pos["stop"] = stop
        elif side == "SHORT" and p <= entry - one_r and be <= 0:
            fee_buf = 2.2 * float(s["config"]["fee_pct"]) / 100.0
            pos["break_even_stop"] = entry * (1 - fee_buf)
            stop = min(stop, float(pos["break_even_stop"]))
            pos["stop"] = stop

        reason = ""
        if side == "LONG":
            if p <= stop:
                reason = "Break-even" if pos.get("break_even_stop") else "Stop-Loss"
            elif p >= take:
                reason = "Take-Profit 2R"
        else:
            if p >= stop:
                reason = "Break-even" if pos.get("break_even_stop") else "Stop-Loss"
            elif p <= take:
                reason = "Take-Profit 2R"
        if reason:
            events.append(_close_position(s, coin, pos, p, reason))
    return events


def _scan_due(s: dict[str, Any], tf: str) -> bool:
    now = _now()
    if tf == "15m":
        bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        # latest completed bucket starts 15m before current boundary
        completed = bucket - timedelta(minutes=15)
    else:
        bucket = now.replace(minute=0, second=0, microsecond=0)
        completed = bucket - timedelta(hours=1)
    key = completed.isoformat()
    return str((s.get("last_scan_bar") or {}).get(tf, "")) != key


def _mark_scan(s: dict[str, Any], tf: str) -> None:
    now = _now()
    if tf == "15m":
        bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) - timedelta(minutes=15)
    else:
        bucket = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    s.setdefault("last_scan_bar", {})[tf] = bucket.isoformat()


def _open_signal(s: dict[str, Any], sig: dict[str, Any]) -> dict[str, Any] | None:
    cfg = s["config"]
    coin = sig["coin"]
    if coin in (s.get("positions") or {}) or _cooldown_active(s, coin):
        return None
    if len(s.get("positions") or {}) >= int(cfg["max_positions"]):
        return None

    capital = float(s.get("balance", cfg["start_capital"]))
    fee_roundtrip = 2.0 * float(cfg["fee_pct"]) / 100.0
    risk_budget = float(cfg["risk_budget_eur"]) * float(sig["risk_factor"])
    loss_fraction = float(sig["stop_pct"]) + fee_roundtrip
    notional = risk_budget / max(loss_fraction, 1e-9)
    notional = min(notional, capital * float(cfg["max_notional_pct"]) / 100.0)
    if notional < 20.0:
        return None

    pos = {
        "opened_at": _iso(), "symbol": sig["symbol"], "coin": coin, "timeframe": sig["timeframe"],
        "side": sig["side"], "entry": sig["price"], "stop": sig["stop"], "take": sig["take"],
        "notional": notional, "pattern": sig["pattern"], "score": sig["score"],
        "risk_tier": sig["risk_tier"], "atr_pct": sig["atr_pct"], "rsi": sig["rsi"],
        "volume_ratio": sig["volume_ratio"], "bar_close": sig["bar_close"], "break_even_stop": 0.0,
    }
    s.setdefault("positions", {})[coin] = pos
    s.setdefault("stats", {})["entries"] = int(s.get("stats", {}).get("entries", 0)) + 1
    event = {
        "timestamp": _iso(), "action": "OPEN", "coin": coin, "symbol": sig["symbol"],
        "timeframe": sig["timeframe"], "side": sig["side"], "price": sig["price"], "notional": notional,
        "quality_score": sig["score"], "pattern": sig["pattern"], "risk_tier": sig["risk_tier"],
        "telegram": (
            f"🕯️ Candlestick V2 {sig['side']} eröffnet · {coin}/{sig['timeframe']}\n"
            f"{sig['pattern']} · Quality {sig['score']:.1f}/8 · Risiko {sig['risk_tier']}\n"
            f"Entry {sig['price']:.8g} · SL {sig['stop']:.8g} · TP {sig['take']:.8g} USDT\n"
            f"Positionswert {notional:.2f} € · max. Risiko ca. {risk_budget:.2f} €"
        ),
    }
    return event


def process_candlestick_v2() -> dict[str, Any]:
    s = state()
    if not s or not s.get("active", False):
        return {"active": False, "message": "Candlestick V2 nicht aktiv", "events": []}
    cfg = {**DEFAULTS, **(s.get("config") or {})}
    s["config"] = cfg

    market = _market_snapshot(cfg)
    market_by_coin = {r["coin"]: r for r in market}
    events = _process_exits(s, market_by_coin)
    signals: list[dict[str, Any]] = []

    for tf in cfg.get("timeframes", ["15m", "1h"]):
        if not _scan_due(s, tf):
            continue
        candidates = [r for r in market if r["coin"] not in (s.get("positions") or {}) and not _cooldown_active(s, r["coin"])]
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_evaluate_symbol, r, tf, cfg) for r in candidates]
            for f in as_completed(futs):
                try:
                    sig = f.result()
                    if sig:
                        signals.append(sig)
                except Exception:
                    pass
        _mark_scan(s, tf)

    # Highest quality first. A coin can still only receive one position across all TFs.
    signals.sort(key=lambda x: (float(x["score"]), float(x["quote_volume"])), reverse=True)
    for sig in signals:
        ev = _open_signal(s, sig)
        if ev:
            events.append(ev)

    write_json(STATE_FILE, s)
    latest = {
        "updated_at": _iso(), "active": True, "balance": round(float(s["balance"]), 2),
        "positions": list((s.get("positions") or {}).values()), "signals_considered": len(signals),
        "stats": s.get("stats", {}), "universe": len(market),
    }
    write_json(LATEST_FILE, latest)
    return {
        "active": True, "events": events, "signals": signals,
        "message": f"{len(market)} Coins · {len(signals)} Quality-Signale · {len(s.get('positions', {}))} offen · Konto {float(s['balance']):.2f} €",
    }


def positions_dataframe() -> pd.DataFrame:
    s = state() or {}
    rows = list((s.get("positions") or {}).values())
    return pd.DataFrame(rows)


def trades_dataframe() -> pd.DataFrame:
    return pd.DataFrame(read_json(TRADES_FILE, []) or [])


def latest() -> dict[str, Any]:
    return read_json(LATEST_FILE, {}) or {}
