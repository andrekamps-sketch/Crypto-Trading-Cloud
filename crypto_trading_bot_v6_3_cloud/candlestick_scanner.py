from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CANDLE_STATE = DATA_DIR / "candlestick_paper_state.json"
CANDLE_LATEST = DATA_DIR / "candlestick_scan_latest.json"
CANDLE_HISTORY = DATA_DIR / "candlestick_paper_history.csv"
CANDLE_TRADES = DATA_DIR / "candlestick_paper_trades.csv"
CANDLE_SIGNALS = DATA_DIR / "candlestick_signal_history.csv"

BASE_ENDPOINTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]

STABLE_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "USDP", "USDS", "DAI", "EUR", "AEUR", "EURI",
    "BUSD", "UST", "USTC", "PYUSD", "PAXG", "XAUT",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

DEFAULTS = {
    "start_capital": 1000.0,
    "universe_size": 50,
    "min_quote_volume_usd": 5_000_000.0,
    "max_spread_bps": 25.0,
    "timeframes": ["15m", "1h"],
    "min_score": 75.0,
    "max_positions": 3,
    "risk_per_trade_pct": 0.75,
    "max_position_pct": 30.0,
    "max_total_exposure_pct": 75.0,
    "fee_pct": 0.10,
    "slippage_pct": 0.05,
    "atr_period": 14,
    "atr_stop_mult": 1.50,
    "reward_risk": 2.0,
    "cooldown_hours": 6.0,
    "scan_interval_minutes": 15,
    "allow_short": True,
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


def _append_csv(path: Path, row: dict[str, Any], limit: int = 25000) -> None:
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


def _f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _http_json(path: str, params: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    qs = urllib.parse.urlencode(params or {})
    suffix = path + (("?" + qs) if qs else "")
    last: Exception | None = None
    for base in BASE_ENDPOINTS:
        url = base.rstrip("/") + suffix
        req = urllib.request.Request(url, headers={"User-Agent": "CryptoTradingCandles/7.2"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Binance-Marktdaten nicht erreichbar: {last}")


def state() -> dict[str, Any] | None:
    x = _read_json(CANDLE_STATE, None)
    return x if isinstance(x, dict) else None


def latest() -> dict[str, Any]:
    x = _read_json(CANDLE_LATEST, {})
    return x if isinstance(x, dict) else {}


def start_candlestick_paper(start_capital: float = 1000.0, **params) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    for k, v in params.items():
        if k in cfg:
            cfg[k] = v
    cfg["start_capital"] = float(start_capital)
    cfg["universe_size"] = max(10, min(100, int(cfg["universe_size"])))
    cfg["max_positions"] = max(1, min(10, int(cfg["max_positions"])))
    cfg["min_score"] = max(50.0, min(100.0, float(cfg["min_score"])))
    cfg["risk_per_trade_pct"] = max(0.1, min(5.0, float(cfg["risk_per_trade_pct"])))
    cfg["reward_risk"] = max(1.0, min(5.0, float(cfg["reward_risk"])))
    cfg["timeframes"] = [x for x in cfg.get("timeframes", ["15m", "1h"]) if x in {"15m", "1h"}] or ["15m", "1h"]
    if cfg["start_capital"] <= 0:
        raise ValueError("Startkapital muss größer als 0 sein.")

    now = _utc_now().isoformat()
    s = {
        "version": "7.2",
        "mode": "CANDLESTICK_PAPER_ONLY",
        "started_at": now,
        "active": True,
        "entry_paused": False,
        "params": cfg,
        "cash": float(cfg["start_capital"]),
        "equity": float(cfg["start_capital"]),
        "peak_equity": float(cfg["start_capital"]),
        "max_drawdown_pct": 0.0,
        "fees_total": 0.0,
        "realized_pnl": 0.0,
        "positions": {},
        "cooldown_until": {},
        "seen_signals": [],
        "trade_count": 0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "last_scan_at": None,
        "last_update_at": None,
        "last_prices": {},
        "last_universe": [],
    }
    _write_json(CANDLE_STATE, s)
    return s


def stop_candlestick_paper() -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Candlestick-Paper wurde noch nicht gestartet.")
    s["active"] = False
    _write_json(CANDLE_STATE, s)
    return s


def resume_candlestick_paper() -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Candlestick-Paper wurde noch nicht gestartet.")
    s["active"] = True
    _write_json(CANDLE_STATE, s)
    return s


def set_entry_paused(paused: bool) -> dict[str, Any]:
    s = state()
    if not s:
        raise RuntimeError("Candlestick-Paper wurde noch nicht gestartet.")
    s["entry_paused"] = bool(paused)
    _write_json(CANDLE_STATE, s)
    return s


def _eligible_markets() -> dict[str, dict[str, Any]]:
    info = _http_json("/api/v3/exchangeInfo")
    out: dict[str, dict[str, Any]] = {}
    for x in info.get("symbols", []) if isinstance(info, dict) else []:
        if str(x.get("status")) != "TRADING" or str(x.get("quoteAsset")) != "USDT":
            continue
        if x.get("isSpotTradingAllowed") is False:
            continue
        base = str(x.get("baseAsset", "")).upper()
        symbol = str(x.get("symbol", "")).upper()
        if not base or not symbol or base in STABLE_BASES or base.endswith(LEVERAGED_SUFFIXES):
            continue
        out[symbol] = {"coin": base, "symbol": symbol}
    return out


def _market_universe(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, float], int]:
    eligible = _eligible_markets()
    tickers = _http_json("/api/v3/ticker/24hr")
    books = _http_json("/api/v3/ticker/bookTicker")
    book_map = {str(x.get("symbol", "")): x for x in books if isinstance(x, dict)} if isinstance(books, list) else {}
    prices: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    min_qv = float(cfg["min_quote_volume_usd"])
    max_spread = float(cfg["max_spread_bps"])
    for t in tickers if isinstance(tickers, list) else []:
        symbol = str(t.get("symbol", "")).upper()
        meta = eligible.get(symbol)
        if not meta:
            continue
        last = _f(t.get("lastPrice"))
        if last > 0:
            prices[symbol] = last
        qv = _f(t.get("quoteVolume"))
        if qv < min_qv or last <= 0:
            continue
        b = book_map.get(symbol, {})
        bid, ask = _f(b.get("bidPrice")), _f(b.get("askPrice"))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        spread_bps = ((ask - bid) / mid * 10000.0) if bid > 0 and ask > 0 and mid > 0 else 999.0
        if spread_bps > max_spread:
            continue
        rows.append({
            "Coin": meta["coin"], "Symbol": symbol, "Preis $": last,
            "24h Volumen $": qv, "24h %": _f(t.get("priceChangePercent")),
            "Spread bps": spread_bps,
        })
    rows.sort(key=lambda x: float(x["24h Volumen $"]), reverse=True)
    return rows[: int(cfg["universe_size"])], prices, len(eligible)


def _all_prices() -> dict[str, float]:
    raw = _http_json("/api/v3/ticker/price")
    out: dict[str, float] = {}
    for x in raw if isinstance(raw, list) else []:
        symbol = str(x.get("symbol", "")).upper()
        p = _f(x.get("price"))
        if symbol.endswith("USDT") and p > 0:
            out[symbol] = p
    return out


def _kline_df(symbol: str, interval: str, limit: int = 220) -> pd.DataFrame:
    raw = _http_json("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = []
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, list) or len(x) < 8:
            continue
        close_time = int(_f(x[6]))
        if close_time >= now_ms:  # only completed candles
            continue
        rows.append({
            "timestamp": pd.to_datetime(int(x[0]), unit="ms", utc=True),
            "open": _f(x[1]), "high": _f(x[2]), "low": _f(x[3]), "close": _f(x[4]),
            "volume": _f(x[5]), "quote_volume": _f(x[7]),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("timestamp").sort_index()


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0).clip(0, 100)


def _atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _shape(row: pd.Series) -> dict[str, float]:
    o, h, l, c = map(float, [row["open"], row["high"], row["low"], row["close"]])
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    return {
        "o": o, "h": h, "l": l, "c": c, "range": rng, "body": body,
        "upper": h - max(o, c), "lower": min(o, c) - l,
        "body_ratio": body / rng, "close_loc": (c - l) / rng,
    }


def _detect_pattern(df: pd.DataFrame, i: int) -> dict[str, Any] | None:
    if len(df) < 60 or i < 4:
        return None
    cur, prev, prev2 = _shape(df.iloc[i]), _shape(df.iloc[i - 1]), _shape(df.iloc[i - 2])
    close = df["close"].astype(float)
    recent5 = float(close.iloc[i - 1] / close.iloc[max(0, i - 6)] - 1.0) if i >= 6 else 0.0
    found: list[tuple[int, str, str]] = []

    if cur["c"] > cur["o"] and prev["c"] < prev["o"] and cur["o"] <= prev["c"] and cur["c"] >= prev["o"] and cur["body"] >= 0.80 * max(prev["body"], 1e-12):
        found.append((20, "Bullish Engulfing", "LONG"))
    if cur["c"] < cur["o"] and prev["c"] > prev["o"] and cur["o"] >= prev["c"] and cur["c"] <= prev["o"] and cur["body"] >= 0.80 * max(prev["body"], 1e-12):
        found.append((20, "Bearish Engulfing", "SHORT"))

    if cur["lower"] >= 2.0 * max(cur["body"], cur["range"] * 0.06) and cur["upper"] <= 0.75 * max(cur["body"], cur["range"] * 0.06) and cur["close_loc"] >= 0.60 and recent5 < 0:
        found.append((18, "Hammer", "LONG"))
    if cur["upper"] >= 2.0 * max(cur["body"], cur["range"] * 0.06) and cur["lower"] <= 0.75 * max(cur["body"], cur["range"] * 0.06) and cur["close_loc"] <= 0.40 and recent5 > 0:
        found.append((18, "Shooting Star", "SHORT"))

    if cur["lower"] >= 2.5 * max(cur["body"], cur["range"] * 0.05) and cur["close_loc"] >= 0.72:
        found.append((17, "Bullish Pin Bar", "LONG"))
    if cur["upper"] >= 2.5 * max(cur["body"], cur["range"] * 0.05) and cur["close_loc"] <= 0.28:
        found.append((17, "Bearish Pin Bar", "SHORT"))

    # Three-candle star patterns ending on the signal candle.
    a, b, c = prev2, prev, cur
    if a["c"] < a["o"] and a["body_ratio"] >= 0.50 and b["body"] <= a["body"] * 0.55 and c["c"] > c["o"] and c["c"] >= (a["o"] + a["c"]) / 2:
        found.append((20, "Morning Star", "LONG"))
    if a["c"] > a["o"] and a["body_ratio"] >= 0.50 and b["body"] <= a["body"] * 0.55 and c["c"] < c["o"] and c["c"] <= (a["o"] + a["c"]) / 2:
        found.append((20, "Evening Star", "SHORT"))

    if not found:
        return None
    found.sort(reverse=True, key=lambda x: x[0])
    strength, name, direction = found[0]
    return {"pattern": name, "direction": direction, "pattern_points": strength}


def _score_signal(symbol: str, coin: str, timeframe: str, df: pd.DataFrame, liquidity: float, spread_bps: float, cfg: dict[str, Any]) -> dict[str, Any] | None:
    if len(df) < 80:
        return None
    i = len(df) - 2  # signal candle; final candle is confirmation
    pat = _detect_pattern(df, i)
    if not pat:
        return None
    direction = str(pat["direction"])
    if direction == "SHORT" and not bool(cfg.get("allow_short", True)):
        return None

    close = df["close"].astype(float)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    rsi = _rsi_series(close, 14)
    atr = _atr_series(df, int(cfg["atr_period"]))
    sig = _shape(df.iloc[i])
    conf = _shape(df.iloc[-1])
    price = float(conf["c"])
    atr_i = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else sig["range"]
    atr_i = max(atr_i, sig["range"] * 0.25, price * 0.001)

    # 20 trend points
    if direction == "LONG":
        trend_checks = [float(close.iloc[i]) > float(ema20.iloc[i]), float(ema20.iloc[i]) > float(ema50.iloc[i])]
    else:
        trend_checks = [float(close.iloc[i]) < float(ema20.iloc[i]), float(ema20.iloc[i]) < float(ema50.iloc[i])]
    trend_points = 20 if all(trend_checks) else 10 if any(trend_checks) else 0

    # 20 support/resistance points based only on candles before the signal.
    prior = df.iloc[max(0, i - 24):i]
    if direction == "LONG":
        level = float(prior["low"].min()) if not prior.empty else sig["l"]
        dist = abs(sig["l"] - level)
    else:
        level = float(prior["high"].max()) if not prior.empty else sig["h"]
        dist = abs(sig["h"] - level)
    tol = max(atr_i, price * 0.006)
    sr_points = 20 if dist <= tol else 10 if dist <= 2.0 * tol else 0

    # 15 volume points
    vol_prior = df["quote_volume"].iloc[max(0, i - 20):i]
    med_vol = float(vol_prior.median()) if len(vol_prior) else 0.0
    vol_ratio = float(df["quote_volume"].iloc[i] / med_vol) if med_vol > 0 else 1.0
    volume_points = 15 if vol_ratio >= 1.50 else 10 if vol_ratio >= 1.10 else 5 if vol_ratio >= 0.80 else 0

    # 10 RSI/momentum points
    rsi_i = float(rsi.iloc[i])
    if direction == "LONG":
        momentum_points = 10 if 35 <= rsi_i <= 65 else 7 if 28 <= rsi_i <= 72 else 2 if rsi_i < 78 else 0
    else:
        momentum_points = 10 if 35 <= rsi_i <= 65 else 7 if 28 <= rsi_i <= 72 else 2 if rsi_i > 22 else 0

    # 10 confirmation points. A trade requires full confirmation beyond signal high/low.
    if direction == "LONG":
        confirmed = conf["c"] > sig["h"] and conf["c"] > conf["o"]
        confirmation_points = 10 if confirmed else 5 if conf["c"] > sig["c"] and conf["c"] > conf["o"] else 0
    else:
        confirmed = conf["c"] < sig["l"] and conf["c"] < conf["o"]
        confirmation_points = 10 if confirmed else 5 if conf["c"] < sig["c"] and conf["c"] < conf["o"] else 0

    # Stop and 2R target, providing the final 5 CRV points when valid.
    slip = float(cfg["slippage_pct"]) / 100.0
    entry = price * (1.0 + slip) if direction == "LONG" else price * (1.0 - slip)
    if direction == "LONG":
        stop = min(sig["l"], entry - float(cfg["atr_stop_mult"]) * atr_i)
        risk_abs = entry - stop
        target = entry + float(cfg["reward_risk"]) * risk_abs
    else:
        stop = max(sig["h"], entry + float(cfg["atr_stop_mult"]) * atr_i)
        risk_abs = stop - entry
        target = entry - float(cfg["reward_risk"]) * risk_abs
    risk_pct = risk_abs / entry * 100.0 if entry > 0 else 999.0
    crv_points = 5 if 0.15 <= risk_pct <= 12.0 else 0

    total = int(pat["pattern_points"] + trend_points + sr_points + volume_points + momentum_points + confirmation_points + crv_points)
    candle_time = pd.Timestamp(df.index[i]).isoformat()
    signal_id = f"{symbol}|{timeframe}|{candle_time}|{pat['pattern']}|{direction}"
    reasons = [
        f"{pat['pattern']} {pat['pattern_points']}/20",
        f"Trend {trend_points}/20",
        f"S/R {sr_points}/20",
        f"Volumen {volume_points}/15 ({vol_ratio:.2f}×)",
        f"RSI {momentum_points}/10 ({rsi_i:.1f})",
        f"Bestätigung {confirmation_points}/10",
        f"CRV {crv_points}/5",
    ]
    return {
        "signal_id": signal_id,
        "Coin": coin,
        "Symbol": symbol,
        "Zeitrahmen": timeframe,
        "Richtung": direction,
        "Muster": str(pat["pattern"]),
        "Score": total,
        "Bestätigt": bool(confirmed),
        "Signalzeit": candle_time,
        "Bestätigungszeit": pd.Timestamp(df.index[-1]).isoformat(),
        "Preis $": round(price, 10),
        "Entry $": round(entry, 10),
        "Stop $": round(stop, 10),
        "Ziel $": round(target, 10),
        "Risiko %": round(risk_pct, 3),
        "RSI": round(rsi_i, 1),
        "Volumen x": round(vol_ratio, 2),
        "24h Volumen $": round(float(liquidity), 0),
        "Spread bps": round(float(spread_bps), 2),
        "Pattern-Punkte": int(pat["pattern_points"]),
        "Trend-Punkte": trend_points,
        "S/R-Punkte": sr_points,
        "Volumen-Punkte": volume_points,
        "Momentum-Punkte": momentum_points,
        "Bestätigung-Punkte": confirmation_points,
        "CRV-Punkte": crv_points,
        "Details": " · ".join(reasons),
    }


def _scan_one(row: dict[str, Any], timeframe: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    df = _kline_df(str(row["Symbol"]), timeframe)
    return _score_signal(
        str(row["Symbol"]), str(row["Coin"]), timeframe, df,
        float(row["24h Volumen $"]), float(row["Spread bps"]), cfg,
    )


def _equity(s: dict[str, Any], prices: dict[str, float]) -> tuple[float, float]:
    cash = float(s.get("cash", 0.0))
    total = cash
    exposure = 0.0
    for pos in (s.get("positions") or {}).values():
        entry = float(pos.get("entry_price", 0.0))
        current = float(prices.get(str(pos.get("symbol", "")), pos.get("last_price", entry)))
        notional = float(pos.get("notional_eur", 0.0))
        side = 1.0 if str(pos.get("direction")) == "LONG" else -1.0
        ret = side * (current / entry - 1.0) if entry > 0 else 0.0
        total += notional + notional * ret
        exposure += notional
    return total, exposure


def _close_position(s: dict[str, Any], key: str, market_price: float, reason: str, now: pd.Timestamp) -> dict[str, Any]:
    pos = s["positions"][key]
    direction = str(pos["direction"])
    slip = float((s.get("params") or DEFAULTS)["slippage_pct"]) / 100.0
    fee_rate = float((s.get("params") or DEFAULTS)["fee_pct"]) / 100.0
    exit_price = market_price * (1.0 - slip) if direction == "LONG" else market_price * (1.0 + slip)
    entry = float(pos["entry_price"])
    notional = float(pos["notional_eur"])
    side = 1.0 if direction == "LONG" else -1.0
    gross_pnl = notional * side * (exit_price / entry - 1.0) if entry > 0 else 0.0
    exit_fee = notional * (exit_price / entry if entry > 0 else 1.0) * fee_rate
    entry_fee = float(pos.get("entry_fee_eur", 0.0))
    net_pnl = gross_pnl - entry_fee - exit_fee
    s["cash"] = float(s.get("cash", 0.0)) + notional + gross_pnl - exit_fee
    s["fees_total"] = float(s.get("fees_total", 0.0)) + exit_fee
    s["realized_pnl"] = float(s.get("realized_pnl", 0.0)) + net_pnl
    s["closed_trades"] = int(s.get("closed_trades", 0)) + 1
    if net_pnl > 0:
        s["wins"] = int(s.get("wins", 0)) + 1
    elif net_pnl < 0:
        s["losses"] = int(s.get("losses", 0)) + 1
    del s["positions"][key]
    cooldown_key = f"{pos['symbol']}|{pos['timeframe']}|{direction}"
    s.setdefault("cooldown_until", {})[cooldown_key] = (now + pd.Timedelta(hours=float((s.get("params") or DEFAULTS)["cooldown_hours"]))).isoformat()
    event = {
        "timestamp": now.isoformat(), "action": "CLOSE", "Coin": pos["coin"], "Symbol": pos["symbol"],
        "Zeitrahmen": pos["timeframe"], "Richtung": direction, "Muster": pos["pattern"],
        "Score": pos["score"], "entry_price": round(entry, 10), "exit_price": round(exit_price, 10),
        "notional_eur": round(notional, 6), "entry_fee_eur": round(entry_fee, 6), "exit_fee_eur": round(exit_fee, 6),
        "gross_pnl_eur": round(gross_pnl, 6), "net_pnl_eur": round(net_pnl, 6), "reason": reason,
        "signal_id": pos.get("signal_id", ""),
    }
    _append_csv(CANDLE_TRADES, event)
    event["telegram"] = f"🕯️ Candlestick {direction} geschlossen · {pos['coin']}/{pos['timeframe']} · {reason} · Netto {net_pnl:+.2f} €"
    return event


def _open_position(s: dict[str, Any], sig: dict[str, Any], prices: dict[str, float], now: pd.Timestamp) -> dict[str, Any] | None:
    cfg = s.get("params") or DEFAULTS
    symbol = str(sig["Symbol"])
    direction = str(sig["Richtung"])
    current = float(prices.get(symbol, sig["Preis $"]))
    slip = float(cfg["slippage_pct"]) / 100.0
    entry = current * (1.0 + slip) if direction == "LONG" else current * (1.0 - slip)
    stop = float(sig["Stop $"])
    # Re-anchor ATR-derived risk to the actual current entry if market moved slightly.
    risk_pct = abs(entry - stop) / entry if entry > 0 else 0.0
    if risk_pct <= 0.001 or risk_pct > 0.15:
        return None
    equity, exposure = _equity(s, prices)
    risk_budget = equity * float(cfg["risk_per_trade_pct"]) / 100.0
    by_risk = risk_budget / risk_pct
    by_position = equity * float(cfg["max_position_pct"]) / 100.0
    room = max(0.0, equity * float(cfg["max_total_exposure_pct"]) / 100.0 - exposure)
    fee_rate = float(cfg["fee_pct"]) / 100.0
    by_cash = float(s.get("cash", 0.0)) / (1.0 + fee_rate)
    notional = min(by_risk, by_position, room, by_cash)
    if notional < 25.0:
        return None
    entry_fee = notional * fee_rate
    risk_abs = abs(entry - stop)
    target = entry + float(cfg["reward_risk"]) * risk_abs if direction == "LONG" else entry - float(cfg["reward_risk"]) * risk_abs
    key = f"{symbol}|{sig['Zeitrahmen']}|{direction}"
    s["cash"] = float(s.get("cash", 0.0)) - notional - entry_fee
    s["fees_total"] = float(s.get("fees_total", 0.0)) + entry_fee
    s["trade_count"] = int(s.get("trade_count", 0)) + 1
    s.setdefault("positions", {})[key] = {
        "coin": sig["Coin"], "symbol": symbol, "timeframe": sig["Zeitrahmen"], "direction": direction,
        "pattern": sig["Muster"], "score": int(sig["Score"]), "signal_id": sig["signal_id"],
        "entry_price": entry, "signal_price": float(sig["Preis $"]), "last_price": current,
        "stop_price": stop, "target_price": target, "notional_eur": notional,
        "entry_fee_eur": entry_fee, "opened_at": now.isoformat(), "risk_pct": risk_pct * 100.0,
        "rsi": sig.get("RSI"), "volume_ratio": sig.get("Volumen x"),
    }
    event = {
        "timestamp": now.isoformat(), "action": "OPEN", "Coin": sig["Coin"], "Symbol": symbol,
        "Zeitrahmen": sig["Zeitrahmen"], "Richtung": direction, "Muster": sig["Muster"], "Score": sig["Score"],
        "entry_price": round(entry, 10), "exit_price": None, "notional_eur": round(notional, 6),
        "entry_fee_eur": round(entry_fee, 6), "exit_fee_eur": 0.0, "gross_pnl_eur": 0.0, "net_pnl_eur": -round(entry_fee, 6),
        "reason": f"Score {sig['Score']}/100 · bestätigt", "signal_id": sig["signal_id"],
    }
    _append_csv(CANDLE_TRADES, event)
    event["telegram"] = f"🕯️ Candlestick {direction} · {sig['Coin']}/{sig['Zeitrahmen']} · {sig['Muster']} · Score {sig['Score']}/100 · Paper {notional:.0f} €"
    return event


def _manage_positions(s: dict[str, Any], prices: dict[str, float], now: pd.Timestamp) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for key in list((s.get("positions") or {}).keys()):
        pos = s["positions"][key]
        symbol = str(pos["symbol"])
        current = float(prices.get(symbol, pos.get("last_price", pos["entry_price"])))
        if current <= 0:
            continue
        pos["last_price"] = current
        direction = str(pos["direction"])
        stop, target = float(pos["stop_price"]), float(pos["target_price"])
        reason = None
        if direction == "LONG":
            if current <= stop:
                reason = "Stop-Loss"
            elif current >= target:
                reason = f"Take-Profit {float((s.get('params') or DEFAULTS)['reward_risk']):.1f}R"
        else:
            if current >= stop:
                reason = "Stop-Loss"
            elif current <= target:
                reason = f"Take-Profit {float((s.get('params') or DEFAULTS)['reward_risk']):.1f}R"
        if reason:
            events.append(_close_position(s, key, current, reason, now))
    return events


def _scan_due(s: dict[str, Any], now: pd.Timestamp, force: bool) -> bool:
    if force or not s.get("last_scan_at"):
        return True
    try:
        last = pd.Timestamp(s["last_scan_at"])
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        return (now - last).total_seconds() >= float((s.get("params") or DEFAULTS)["scan_interval_minutes"]) * 60.0
    except Exception:
        return True


def process_candlestick_paper(force_scan: bool = False) -> dict[str, Any]:
    s = state()
    if not s or not s.get("active", True):
        return {"active": False, "message": "Candlestick-Paper nicht aktiv", "events": [], "state": s}
    cfg = s.get("params") or dict(DEFAULTS)
    now = _utc_now()
    events: list[dict[str, Any]] = []
    scan_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    eligible_total = 0

    if _scan_due(s, now, force_scan):
        universe, prices, eligible_total = _market_universe(cfg)
        s["last_universe"] = universe
        s["last_scan_at"] = now.isoformat()
        jobs = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for row in universe:
                for tf in cfg.get("timeframes", ["15m", "1h"]):
                    jobs.append((pool.submit(_scan_one, row, str(tf), cfg), row, tf))
            for future, row, tf in jobs:
                try:
                    sig = future.result()
                    if sig:
                        scan_rows.append(sig)
                except Exception as exc:
                    errors.append(f"{row.get('Symbol')}/{tf}: {exc}")
        scan_rows.sort(key=lambda x: (bool(x.get("Bestätigt")), float(x.get("Score", 0)), float(x.get("24h Volumen $", 0))), reverse=True)
        _write_json(CANDLE_LATEST, {
            "ok": True, "evaluated_at": now.isoformat(), "eligible_markets": eligible_total,
            "universe_size": len(universe), "timeframes": cfg.get("timeframes"),
            "qualified": sum(1 for x in scan_rows if bool(x.get("Bestätigt")) and float(x.get("Score", 0)) >= float(cfg["min_score"])),
            "rows": scan_rows[:150], "errors": errors[:30],
        })
        for sig in scan_rows:
            _append_csv(CANDLE_SIGNALS, {k: v for k, v in sig.items() if k != "Details"})
    else:
        prices = _all_prices()
        scan_rows = list((latest() or {}).get("rows", []) or [])

    # Stops/targets are checked before opening anything new.
    events.extend(_manage_positions(s, prices, now))

    if not s.get("entry_paused", False) and scan_rows and len(s.get("positions", {})) < int(cfg["max_positions"]):
        seen = set(map(str, s.get("seen_signals", []) or []))
        candidates = [
            x for x in scan_rows
            if bool(x.get("Bestätigt")) and float(x.get("Score", 0)) >= float(cfg["min_score"])
            and str(x.get("signal_id")) not in seen
        ]
        candidates.sort(key=lambda x: (float(x.get("Score", 0)), float(x.get("24h Volumen $", 0))), reverse=True)
        for sig in candidates:
            if len(s.get("positions", {})) >= int(cfg["max_positions"]):
                break
            cooldown_key = f"{sig['Symbol']}|{sig['Zeitrahmen']}|{sig['Richtung']}"
            cd = (s.get("cooldown_until") or {}).get(cooldown_key)
            if cd:
                try:
                    if pd.Timestamp(cd) > now:
                        continue
                except Exception:
                    pass
            pos_key = cooldown_key
            if pos_key in (s.get("positions") or {}):
                continue
            e = _open_position(s, sig, prices, now)
            seen.add(str(sig["signal_id"]))
            if e:
                events.append(e)
        s["seen_signals"] = list(seen)[-3000:]

    equity, exposure = _equity(s, prices)
    s["equity"] = equity
    s["peak_equity"] = max(float(s.get("peak_equity", equity)), equity)
    if float(s["peak_equity"]) > 0:
        dd = max(0.0, (float(s["peak_equity"]) - equity) / float(s["peak_equity"]) * 100.0)
        s["max_drawdown_pct"] = max(float(s.get("max_drawdown_pct", 0.0)), dd)
    s["last_update_at"] = now.isoformat()
    # Keep only held-symbol prices to avoid bloating the state file.
    held = {str(p.get("symbol")) for p in (s.get("positions") or {}).values()}
    s["last_prices"] = {sym: float(prices[sym]) for sym in held if sym in prices}
    _write_json(CANDLE_STATE, s)
    _append_csv(CANDLE_HISTORY, {
        "timestamp": now.isoformat(), "equity_eur": round(equity, 6), "cash_eur": round(float(s.get("cash", 0.0)), 6),
        "exposure_eur": round(exposure, 6), "positions": len(s.get("positions", {})),
        "fees_eur": round(float(s.get("fees_total", 0.0)), 6), "realized_pnl_eur": round(float(s.get("realized_pnl", 0.0)), 6),
        "max_drawdown_pct": round(float(s.get("max_drawdown_pct", 0.0)), 6),
    })
    qualified = sum(1 for x in scan_rows if bool(x.get("Bestätigt")) and float(x.get("Score", 0)) >= float(cfg["min_score"]))
    return {
        "active": True,
        "message": f"{len(s.get('last_universe', []))} Coins · {qualified} bestätigte Setups · {len(s.get('positions', {}))} Positionen · Konto {equity:.2f} €",
        "events": events, "state": s, "signals": scan_rows,
    }


def positions_dataframe(s: dict[str, Any] | None = None) -> pd.DataFrame:
    s = s or state() or {}
    rows = []
    for pos in (s.get("positions") or {}).values():
        entry = float(pos.get("entry_price", 0.0))
        cur = float(pos.get("last_price", entry))
        side = 1.0 if str(pos.get("direction")) == "LONG" else -1.0
        pnl_pct = side * (cur / entry - 1.0) * 100.0 if entry > 0 else 0.0
        rows.append({
            "Coin": pos.get("coin"), "TF": pos.get("timeframe"), "Richtung": pos.get("direction"), "Muster": pos.get("pattern"),
            "Score": pos.get("score"), "Paper €": round(float(pos.get("notional_eur", 0.0)), 2),
            "Entry $": round(entry, 10), "Aktuell $": round(cur, 10), "G/V %": round(pnl_pct, 2),
            "Stop $": round(float(pos.get("stop_price", 0.0)), 10), "Ziel $": round(float(pos.get("target_price", 0.0)), 10),
            "Risiko %": round(float(pos.get("risk_pct", 0.0)), 2), "Seit": pos.get("opened_at"),
        })
    return pd.DataFrame(rows)


def signals_dataframe(limit: int = 100) -> pd.DataFrame:
    rows = list((latest() or {}).get("rows", []) or [])
    if not rows:
        return pd.DataFrame()
    cols = ["Coin", "Zeitrahmen", "Richtung", "Muster", "Score", "Bestätigt", "Preis $", "Stop $", "Ziel $", "Risiko %", "RSI", "Volumen x", "24h Volumen $", "Spread bps", "Details", "Signalzeit"]
    df = pd.DataFrame(rows[:limit])
    return df[[c for c in cols if c in df.columns]]


def history_dataframe(limit: int = 3000) -> pd.DataFrame:
    if not CANDLE_HISTORY.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(CANDLE_HISTORY).tail(limit)
    except Exception:
        return pd.DataFrame()


def trades_dataframe(limit: int = 1000) -> pd.DataFrame:
    if not CANDLE_TRADES.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(CANDLE_TRADES).tail(limit)
    except Exception:
        return pd.DataFrame()


def pattern_stats() -> pd.DataFrame:
    df = trades_dataframe(5000)
    if df.empty or "action" not in df.columns:
        return pd.DataFrame()
    closed = df[df["action"].astype(str) == "CLOSE"].copy()
    if closed.empty:
        return pd.DataFrame()
    closed["net_pnl_eur"] = pd.to_numeric(closed["net_pnl_eur"], errors="coerce").fillna(0.0)
    closed["Gewinn"] = closed["net_pnl_eur"] > 0
    keys = [c for c in ["Muster", "Zeitrahmen", "Richtung"] if c in closed.columns]
    out = closed.groupby(keys, dropna=False).agg(
        Trades=("net_pnl_eur", "size"),
        Gewinner=("Gewinn", "sum"),
        Netto_EUR=("net_pnl_eur", "sum"),
        Ø_EUR=("net_pnl_eur", "mean"),
    ).reset_index()
    out["Trefferquote %"] = out["Gewinner"] / out["Trades"].clip(lower=1) * 100.0
    out = out.rename(columns={"Netto_EUR": "Netto €", "Ø_EUR": "Ø €"})
    for c in ["Netto €", "Ø €", "Trefferquote %"]:
        out[c] = out[c].round(2)
    return out.sort_values(["Netto €", "Trefferquote %"], ascending=False).reset_index(drop=True)


def leaderboard_row() -> dict[str, Any] | None:
    s = state()
    if not s:
        return None
    start = float((s.get("params") or DEFAULTS).get("start_capital", 1000.0))
    equity = float(s.get("equity", start))
    ret = (equity / start - 1.0) * 100.0 if start > 0 else 0.0
    return {
        "System": "V7.2", "Strategie": "Coin Candlestick Scanner", "Status": "läuft" if s.get("active", True) else "pausiert",
        "Kontowert €": round(equity, 2), "Rendite %": round(ret, 2), "Drawdown %": round(float(s.get("max_drawdown_pct", 0.0)), 2),
        "PF": None, "Trades": int(s.get("closed_trades", 0)), "Gebühren €": round(float(s.get("fees_total", 0.0)), 2),
        "Hinweis": f"{len(s.get('positions', {}))} offen · Score ≥ {float((s.get('params') or DEFAULTS).get('min_score',75)):.0f}",
    }
