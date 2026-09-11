from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
RADAR_LATEST = DATA_DIR / "crypto_radar_latest.json"
RADAR_HISTORY = DATA_DIR / "crypto_radar_history.csv"
RADAR_ALERT_STATE = DATA_DIR / "crypto_radar_alert_state.json"

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
    "deep_scan_n": 40,
    "min_quote_volume_usd": 2_000_000.0,
    "hot_score": 80.0,
    "watch_score": 70.0,
    "alert_cooldown_hours": 6,
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


def _http_json(path: str, params: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    qs = urllib.parse.urlencode(params or {})
    suffix = path + (("?" + qs) if qs else "")
    last: Exception | None = None
    for base in BASE_ENDPOINTS:
        url = base.rstrip("/") + suffix
        req = urllib.request.Request(url, headers={"User-Agent": "CryptoTradingRadar/7.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            continue
    raise RuntimeError(f"Binance-Marktdaten nicht erreichbar: {last}")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _rsi(close: pd.Series, period: int = 14) -> float:
    if len(close) < period + 2:
        return 50.0
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    r = 100 - (100 / (1 + rs))
    val = float(r.iloc[-1]) if pd.notna(r.iloc[-1]) else 50.0
    return max(0.0, min(100.0, val))


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100 if b else 0.0


def _kline_df(symbol: str, limit: int = 170) -> pd.DataFrame:
    raw = _http_json("/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": limit})
    if not isinstance(raw, list) or not raw:
        return pd.DataFrame()
    rows = []
    for x in raw:
        if not isinstance(x, list) or len(x) < 11:
            continue
        rows.append({
            "timestamp": pd.to_datetime(int(x[0]), unit="ms", utc=True),
            "open": _f(x[1]), "high": _f(x[2]), "low": _f(x[3]), "close": _f(x[4]),
            "volume": _f(x[5]), "quote_volume": _f(x[7]), "trades": int(_f(x[8])),
            "taker_buy_quote": _f(x[10]),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("timestamp").sort_index()


def _eligible_markets() -> dict[str, dict[str, Any]]:
    info = _http_json("/api/v3/exchangeInfo")
    out: dict[str, dict[str, Any]] = {}
    for x in info.get("symbols", []) if isinstance(info, dict) else []:
        if str(x.get("status")) != "TRADING":
            continue
        if str(x.get("quoteAsset")) != "USDT":
            continue
        if x.get("isSpotTradingAllowed") is False:
            continue
        base = str(x.get("baseAsset", "")).upper()
        symbol = str(x.get("symbol", "")).upper()
        if not base or not symbol or base in STABLE_BASES:
            continue
        if base.endswith(LEVERAGED_SUFFIXES):
            continue
        out[symbol] = {"coin": base, "symbol": symbol}
    return out


def _broad_market(min_quote_volume: float) -> tuple[pd.DataFrame, int]:
    markets = _eligible_markets()
    tickers = _http_json("/api/v3/ticker/24hr")
    rows = []
    if not isinstance(tickers, list):
        tickers = []
    for t in tickers:
        sym = str(t.get("symbol", "")).upper()
        meta = markets.get(sym)
        if not meta:
            continue
        qv = _f(t.get("quoteVolume"))
        if qv < min_quote_volume:
            continue
        last = _f(t.get("lastPrice"))
        if last <= 0:
            continue
        rows.append({
            "Coin": meta["coin"], "Symbol": sym, "Preis $": last,
            "24h %": _f(t.get("priceChangePercent")), "24h Volumen $": qv,
            "24h Trades": int(_f(t.get("count"))),
            "24h Hoch": _f(t.get("highPrice")), "24h Tief": _f(t.get("lowPrice")),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df, len(markets)
    # Pre-score is deliberately cheap: it only selects markets for the deeper 1h analysis.
    vol_rank = df["24h Volumen $"].rank(pct=True).fillna(0) * 100
    move = df["24h %"].abs().clip(0, 30)
    activity = np.log10(df["24h Trades"].clip(lower=1))
    activity = (activity - activity.min()) / max(1e-9, activity.max() - activity.min()) * 100
    df["PreScore"] = (0.50 * vol_rank + 0.30 * (move / 30 * 100) + 0.20 * activity).round(1)
    return df.sort_values("PreScore", ascending=False).reset_index(drop=True), len(markets)


def _deep_metrics(row: pd.Series) -> dict[str, Any] | None:
    symbol = str(row["Symbol"])
    df = _kline_df(symbol)
    if len(df) < 60:
        return None
    c = df["close"].astype(float)
    qv = df["quote_volume"].astype(float)
    price = float(c.iloc[-1])
    mom1 = _pct(price, float(c.iloc[-2])) if len(c) >= 2 else 0.0
    mom6 = _pct(price, float(c.iloc[-7])) if len(c) >= 7 else 0.0
    mom24 = _pct(price, float(c.iloc[-25])) if len(c) >= 25 else 0.0
    mom7d = _pct(price, float(c.iloc[0])) if len(c) >= 2 else 0.0
    ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
    rsi = _rsi(c)
    prev_24 = qv.iloc[-25:-1] if len(qv) >= 25 else qv.iloc[:-1]
    med_qv = float(prev_24.median()) if len(prev_24) else 0.0
    vol_spike = float(qv.iloc[-1] / med_qv) if med_qv > 0 else 1.0
    ret = c.pct_change().dropna()
    vol24 = float(ret.tail(24).std() * math.sqrt(24) * 100) if len(ret) >= 8 else 0.0
    high7 = float(df["high"].tail(min(168, len(df))).max())
    low7 = float(df["low"].tail(min(168, len(df))).min())
    dist_high = _pct(price, high7) if high7 else 0.0
    dist_low = _pct(price, low7) if low7 else 0.0
    trend_pct = _pct(ema20, ema50) if ema50 else 0.0
    liquidity = float(row.get("24h Volumen $", 0.0))

    # Opportunity score: discovery/ranking, not a promised expected return.
    liq_score = min(100.0, max(0.0, (math.log10(max(liquidity, 1)) - 6.0) / 4.0 * 100))
    trend_score = min(100.0, abs(trend_pct) / 5.0 * 100)
    momentum_score = min(100.0, (abs(mom6) / 10.0 * 45) + (abs(mom24) / 18.0 * 55))
    volume_score = min(100.0, max(0.0, (vol_spike - 0.8) / 3.2 * 100))
    breakout_score = min(100.0, max(0.0, (4.0 - min(abs(dist_high), abs(dist_low))) / 4.0 * 100))
    anomaly_score = min(100.0, 35 * min(abs(mom1) / 4.0, 1) + 35 * min(vol_spike / 4.0, 1) + 30 * min(abs(mom6) / 10.0, 1))

    # Penalize the classic "already exploded" setup; radar still shows it as an anomaly.
    extreme_penalty = 0.0
    if rsi >= 82 or rsi <= 18:
        extreme_penalty += 10
    if abs(mom24) >= 25:
        extreme_penalty += 15
    if liquidity < 5_000_000:
        extreme_penalty += 8

    opportunity = 0.24 * liq_score + 0.22 * trend_score + 0.22 * momentum_score + 0.18 * volume_score + 0.14 * breakout_score - extreme_penalty
    opportunity = float(max(0.0, min(100.0, opportunity)))

    direction = "NEUTRAL"
    if mom6 > 1 and mom24 > 2 and ema20 > ema50:
        direction = "AUFWÄRTS"
    elif mom6 < -1 and mom24 < -2 and ema20 < ema50:
        direction = "ABWÄRTS"

    reasons = []
    if vol_spike >= 2.5:
        reasons.append(f"Volumen {vol_spike:.1f}× normal")
    if abs(mom1) >= 3.0:
        reasons.append(f"1h {mom1:+.1f}%")
    if abs(mom6) >= 6.0:
        reasons.append(f"6h {mom6:+.1f}%")
    if dist_high >= -0.35:
        reasons.append("nahe/über 7T-Hoch")
    if dist_low <= 0.35:
        reasons.append("nahe/unter 7T-Tief")

    risk = []
    if liquidity < 5_000_000:
        risk.append("geringe Liquidität")
    if abs(mom24) >= 25:
        risk.append("extreme 24h-Bewegung")
    if rsi >= 82:
        risk.append("RSI extrem hoch")
    elif rsi <= 18:
        risk.append("RSI extrem niedrig")
    if vol24 >= 18:
        risk.append("sehr hohe Volatilität")

    return {
        "Coin": str(row["Coin"]), "Symbol": symbol, "Preis $": price,
        "Radar-Score": round(opportunity, 1), "Anomalie": round(anomaly_score, 1), "Richtung": direction,
        "1h %": round(mom1, 2), "6h %": round(mom6, 2), "24h %": round(mom24, 2), "7T %": round(mom7d, 2),
        "Volumen-Spike": round(vol_spike, 2), "24h Volumen $": round(liquidity, 0), "RSI": round(rsi, 1),
        "Trend EMA20/50 %": round(trend_pct, 2), "Volatilität 24h %": round(vol24, 2),
        "Abstand 7T-Hoch %": round(dist_high, 2), "Abstand 7T-Tief %": round(dist_low, 2),
        "Gründe": "; ".join(reasons) if reasons else "–", "Risiko": "; ".join(risk) if risk else "–",
    }


def scan_crypto_radar(deep_scan_n: int | None = None, min_quote_volume_usd: float | None = None) -> dict[str, Any]:
    deep_n = int(deep_scan_n or DEFAULTS["deep_scan_n"])
    deep_n = max(10, min(80, deep_n))
    min_qv = float(min_quote_volume_usd or DEFAULTS["min_quote_volume_usd"])
    broad, listed_count = _broad_market(min_qv)
    if broad.empty:
        raise RuntimeError("Keine geeigneten USDT-Spotmärkte gefunden.")

    # Deep-scan a diverse mix: high pre-score plus biggest up/down movers among liquid markets.
    liquid = broad.copy()
    picks = list(liquid.head(max(10, int(deep_n * 0.6)))["Symbol"])
    remaining = deep_n - len(picks)
    if remaining > 0:
        movers = pd.concat([
            liquid.nlargest(max(1, remaining // 2), "24h %"),
            liquid.nsmallest(max(1, remaining - remaining // 2), "24h %"),
        ])
        picks += [s for s in movers["Symbol"].tolist() if s not in picks]
    picks = picks[:deep_n]

    rows = []
    errors = []
    by_symbol = broad.set_index("Symbol")
    for i, sym in enumerate(picks):
        try:
            candidate = by_symbol.loc[sym].copy()
            candidate["Symbol"] = sym
            m = _deep_metrics(candidate)
            if m:
                rows.append(m)
        except Exception as exc:
            errors.append(f"{sym}: {exc}")
        # Tiny pacing avoids bursts against public endpoints.
        if i and i % 15 == 0:
            time.sleep(0.12)

    deep = pd.DataFrame(rows)
    if deep.empty:
        raise RuntimeError("Detailanalyse konnte keine Märkte auswerten. " + (errors[0] if errors else ""))
    deep = deep.sort_values(["Radar-Score", "Anomalie", "24h Volumen $"], ascending=[False, False, False]).reset_index(drop=True)

    hot = deep[deep["Radar-Score"] >= DEFAULTS["hot_score"]]
    anomalies = deep[deep["Anomalie"] >= 75]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Binance public Spot market data (USDT pairs)",
        "listed_spot_usdt": int(listed_count),
        "liquid_universe": int(len(broad)),
        "deep_scanned": int(len(deep)),
        "min_quote_volume_usd": min_qv,
        "rows": deep.to_dict(orient="records"),
        "broad_top": broad.head(100).to_dict(orient="records"),
        "hot_count": int(len(hot)),
        "anomaly_count": int(len(anomalies)),
        "errors": errors[:10],
    }
    _write_json(RADAR_LATEST, payload)
    _append_history(payload)
    return payload


def _append_history(payload: dict[str, Any]) -> None:
    rows = []
    for r in payload.get("rows", [])[:20]:
        rows.append({"timestamp": payload.get("generated_at"), **r})
    if not rows:
        return
    df = pd.DataFrame(rows)
    if RADAR_HISTORY.exists():
        try:
            old = pd.read_csv(RADAR_HISTORY)
            df = pd.concat([old, df], ignore_index=True)
        except Exception:
            pass
    if len(df) > 20000:
        df = df.tail(20000)
    df.to_csv(RADAR_HISTORY, index=False)


def latest() -> dict[str, Any]:
    return _read_json(RADAR_LATEST, {}) or {}


def rows_dataframe(payload: dict[str, Any] | None = None) -> pd.DataFrame:
    p = payload or latest()
    return pd.DataFrame(p.get("rows", []) or [])


def history_dataframe(limit: int = 3000) -> pd.DataFrame:
    if not RADAR_HISTORY.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(RADAR_HISTORY).tail(limit)
    except Exception:
        return pd.DataFrame()


def _alert_key(coin: str, kind: str) -> str:
    return f"{coin}|{kind}"


def build_new_alerts(payload: dict[str, Any] | None = None) -> list[str]:
    """Return Telegram alerts only when a coin newly crosses a radar threshold.

    The first scan is a silent baseline. A coin can alert again after it has fallen
    back below the threshold and later crosses it again.
    """
    p = payload or latest()
    now = pd.Timestamp.now(tz="UTC")
    state = _read_json(RADAR_ALERT_STATE, {}) or {}
    first_baseline = not bool(state.get("__initialized__"))
    messages: list[str] = []
    active_now: set[str] = set()

    for r in p.get("rows", []) or []:
        coin = str(r.get("Coin", "?"))
        score = _f(r.get("Radar-Score"))
        anomaly = _f(r.get("Anomalie"))
        kinds: list[tuple[str, float, str]] = []
        if score >= DEFAULTS["hot_score"]:
            kinds.append(("hot", score, f"🔥 CRYPTO-RADAR · {coin}\nRadar-Score {score:.1f}/100 · {r.get('Richtung','')}\n1h {r.get('1h %',0):+.2f}% · 6h {r.get('6h %',0):+.2f}% · 24h {r.get('24h %',0):+.2f}%\nVolumen {r.get('Volumen-Spike',0):.1f}× · RSI {r.get('RSI',0):.1f}\nGrund: {r.get('Gründe','–')}\nRisiko: {r.get('Risiko','–')}\nℹ️ Radar-Hinweis, keine Kaufempfehlung."))
        if anomaly >= 85:
            kinds.append(("anomaly", anomaly, f"🚨 MARKT-ANOMALIE · {coin}\nAnomalie {anomaly:.1f}/100 · Radar {score:.1f}\n1h {r.get('1h %',0):+.2f}% · 6h {r.get('6h %',0):+.2f}% · Volumen {r.get('Volumen-Spike',0):.1f}×\n{r.get('Gründe','–')}\nℹ️ Auffällige Bewegung – nicht automatisch ein Einstieg."))

        for kind, metric, msg in kinds:
            key = _alert_key(coin, kind)
            active_now.add(key)
            prev = state.get(key, {})
            if isinstance(prev, str):
                prev = {"active": True, "last_sent": prev}
            was_active = bool(prev.get("active")) if isinstance(prev, dict) else False
            if not first_baseline and not was_active:
                messages.append(msg)
            state[key] = {
                "active": True,
                "last_seen": now.isoformat(),
                "last_sent": now.isoformat() if (not first_baseline and not was_active) else (prev.get("last_sent") if isinstance(prev, dict) else None),
                "metric": round(metric, 2),
            }

    # Reset thresholds once a coin is no longer qualifying, so a later re-entry alerts again.
    for key, prev in list(state.items()):
        if key.startswith("__") or key in active_now or not isinstance(prev, dict):
            continue
        if prev.get("active"):
            prev["active"] = False
            prev["last_seen"] = now.isoformat()
            state[key] = prev

    state["__initialized__"] = True
    _write_json(RADAR_ALERT_STATE, state)
    return messages[:6]


def status() -> dict[str, Any]:
    p = latest()
    if not p:
        return {"available": False}
    rows = p.get("rows", []) or []
    return {
        "available": True,
        "generated_at": p.get("generated_at"),
        "universe": p.get("liquid_universe", 0),
        "deep": p.get("deep_scanned", 0),
        "top": rows[0].get("Coin") if rows else None,
        "top_score": rows[0].get("Radar-Score") if rows else None,
    }
