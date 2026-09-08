from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List

import numpy as np
import pandas as pd

from data import CRYPTO_UNIVERSE, load_recent
from strategies import rsi


@dataclass
class ScanResult:
    asset: str
    symbol: str
    price: float
    score: float
    trend_score: float
    momentum_score: float
    rsi_score: float
    breakout_score: float
    volatility_score: float
    volume_score: float
    risk_level: str
    signal: str
    rsi: float
    return_24h_pct: float
    return_7d_pct: float
    atr_pct: float
    volume_ratio: float
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, v)))


def _atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high-low).abs(), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    price = float(close.iloc[-1])
    return float(atr / price * 100) if price > 0 and pd.notna(atr) else 0.0


def score_dataframe(asset: str, symbol: str, df: pd.DataFrame) -> ScanResult:
    if len(df) < 220:
        raise RuntimeError(f"Zu wenig Kursdaten für {asset} ({len(df)} Zeilen).")

    close = df["Close"].astype(float)
    price = float(close.iloc[-1])
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()

    # Trend 0..100
    trend = 0.0
    trend += 35 if price > float(ema20.iloc[-1]) else 0
    trend += 30 if float(ema20.iloc[-1]) > float(ema50.iloc[-1]) else 0
    trend += 35 if float(ema50.iloc[-1]) > float(ema200.iloc[-1]) else 0

    # Momentum: auf 1h-Kerzen ca. 24h und 7 Tage.
    r24 = (price / float(close.iloc[-25]) - 1) * 100 if len(close) >= 25 else 0.0
    r7 = (price / float(close.iloc[-169]) - 1) * 100 if len(close) >= 169 else 0.0
    mom = 50 + (r24 * 5.0) + (r7 * 1.8)
    momentum = _clamp(mom)

    # RSI: bevorzugt gesunden Aufwärtstrend, bestraft stark überkauft/überverkauft.
    rv = float(rsi(close, 14).iloc[-1])
    if 52 <= rv <= 68:
        rsi_s = 100 - abs(60 - rv) * 4
    elif 45 <= rv < 52:
        rsi_s = 65 + (rv - 45) * 5
    elif 68 < rv <= 75:
        rsi_s = 80 - (rv - 68) * 8
    elif rv > 75:
        rsi_s = max(5, 30 - (rv - 75) * 4)
    else:
        rsi_s = max(5, 55 - (45 - rv) * 4)
    rsi_s = _clamp(rsi_s)

    # Breakout: Nähe zum 30-Tage-Hoch (bei 1h ca. 720 Kerzen, soweit vorhanden).
    lookback = min(len(df)-1, 24*30)
    prior_high = float(df["High"].iloc[-lookback-1:-1].max()) if lookback > 10 else float(df["High"].iloc[:-1].max())
    if prior_high <= 0:
        breakout = 50.0
    else:
        distance_pct = (price / prior_high - 1) * 100
        # nahe Hoch bzw. leicht darüber ist interessant; weit darüber = Überdehnung.
        if -3 <= distance_pct <= 3:
            breakout = 90 - abs(distance_pct) * 8
        elif -8 <= distance_pct < -3:
            breakout = 65 + (distance_pct + 8) * 5
        elif distance_pct > 3:
            breakout = max(30, 75 - (distance_pct - 3) * 7)
        else:
            breakout = max(15, 40 + distance_pct * 2)
    breakout = _clamp(breakout)

    atrp = _atr_pct(df)
    # moderate Volatilität bevorzugen. Zu wenig Bewegung = wenig Chance, zu viel = höheres Risiko.
    if 0.5 <= atrp <= 3.5:
        vol_s = 90 - abs(2.0 - atrp) * 12
    elif 3.5 < atrp <= 6:
        vol_s = 70 - (atrp - 3.5) * 12
    elif atrp > 6:
        vol_s = max(5, 40 - (atrp - 6) * 8)
    else:
        vol_s = max(20, 55 + atrp * 30)
    volatility = _clamp(vol_s)

    volume = df["Volume"].astype(float).fillna(0)
    recent_vol = float(volume.tail(24).mean())
    base_vol = float(volume.tail(24*14).mean()) if len(volume) >= 24*14 else float(volume.mean())
    volume_ratio = recent_vol / base_vol if base_vol > 0 else 1.0
    volume_s = _clamp(40 + (volume_ratio - 0.7) * 55)

    # Gewichte: Trend + Momentum dominieren, Risiko/Volumen wirken als Filter.
    total = (
        trend * 0.28
        + momentum * 0.22
        + rsi_s * 0.16
        + breakout * 0.14
        + volatility * 0.12
        + volume_s * 0.08
    )

    # Harte Risikobremse bei überhitztem RSI oder extremer Volatilität.
    if rv > 80:
        total -= 15
    if atrp > 8:
        total -= 15
    total = _clamp(total)

    if atrp > 6 or rv > 78:
        risk = "hoch"
    elif atrp > 3.5 or rv > 72:
        risk = "mittel"
    else:
        risk = "normal"

    if total >= 75 and trend >= 65 and rv < 78:
        signal = "STARK"
    elif total >= 62 and trend >= 35:
        signal = "INTERESSANT"
    elif total >= 48:
        signal = "BEOBACHTEN"
    else:
        signal = "MEIDEN"

    notes = []
    if trend >= 100:
        notes.append("klarer Aufwärtstrend")
    elif trend >= 65:
        notes.append("positiver Trend")
    if r24 > 2:
        notes.append("gutes 24h-Momentum")
    if rv > 75:
        notes.append("RSI hoch")
    if atrp > 6:
        notes.append("sehr volatil")
    if volume_ratio > 1.3:
        notes.append("Volumen erhöht")
    if not notes:
        notes.append("kein außergewöhnlicher Vorteil")

    return ScanResult(
        asset=asset,
        symbol=symbol,
        price=price,
        score=round(total, 1),
        trend_score=round(trend, 1),
        momentum_score=round(momentum, 1),
        rsi_score=round(rsi_s, 1),
        breakout_score=round(breakout, 1),
        volatility_score=round(volatility, 1),
        volume_score=round(volume_s, 1),
        risk_level=risk,
        signal=signal,
        rsi=round(rv, 1),
        return_24h_pct=round(r24, 2),
        return_7d_pct=round(r7, 2),
        atr_pct=round(atrp, 2),
        volume_ratio=round(volume_ratio, 2),
        note=", ".join(notes),
    )


def scan_asset(asset: str, interval: str = "1h", period: str = "60d") -> ScanResult:
    symbol = CRYPTO_UNIVERSE[asset]
    df = load_recent(symbol, interval=interval, period=period)
    return score_dataframe(asset, symbol, df)


def scan_universe(assets: Iterable[str]) -> List[ScanResult]:
    results: List[ScanResult] = []
    for asset in assets:
        if asset not in CRYPTO_UNIVERSE:
            continue
        try:
            results.append(scan_asset(asset))
        except Exception as exc:
            results.append(ScanResult(
                asset=asset, symbol=CRYPTO_UNIVERSE[asset], price=0.0, score=0.0,
                trend_score=0.0, momentum_score=0.0, rsi_score=0.0, breakout_score=0.0,
                volatility_score=0.0, volume_score=0.0, risk_level="?", signal="FEHLER",
                rsi=0.0, return_24h_pct=0.0, return_7d_pct=0.0, atr_pct=0.0, volume_ratio=0.0,
                note=str(exc)[:120],
            ))
    return sorted(results, key=lambda r: r.score, reverse=True)


def market_regime_from_scan(scan) -> dict:
    """Classify the broad crypto market phase from a benchmark scan (prefer BTC).

    The classifier deliberately uses only information available at the scan time:
    trend_score plus 24h/7d returns. It is a coarse risk filter, not a forecast.
    """
    if scan is None:
        return {"code": "NEUTRAL", "label": "Neutral / Seitwärts", "emoji": "🟡", "reason": "Kein Benchmark verfügbar"}

    def get(name, default=0.0):
        try:
            if isinstance(scan, dict):
                return scan.get(name, default)
            return getattr(scan, name, default)
        except Exception:
            return default

    trend = float(get("trend_score", 0.0) or 0.0)
    r24 = float(get("return_24h_pct", 0.0) or 0.0)
    r7 = float(get("return_7d_pct", 0.0) or 0.0)

    if trend >= 65 and r7 > 0 and r24 > -2.0:
        code = "BULL"
        label = "Bullisch"
        emoji = "🟢"
        reason = f"Trend {trend:.0f}/100, 7T {r7:+.1f} %, 24h {r24:+.1f} %"
    elif trend <= 35 and r7 < 0 and r24 < 2.0:
        code = "BEAR"
        label = "Bärisch"
        emoji = "🔴"
        reason = f"Trend {trend:.0f}/100, 7T {r7:+.1f} %, 24h {r24:+.1f} %"
    else:
        code = "NEUTRAL"
        label = "Neutral / Seitwärts"
        emoji = "🟡"
        reason = f"Trend {trend:.0f}/100, 7T {r7:+.1f} %, 24h {r24:+.1f} %"
    return {"code": code, "label": label, "emoji": emoji, "reason": reason}


def regime_allows_entries(policy: str, regime_code: str) -> bool:
    policy = str(policy or "all")
    regime_code = str(regime_code or "NEUTRAL").upper()
    if policy == "bull_only":
        return regime_code == "BULL"
    if policy == "no_bear":
        return regime_code != "BEAR"
    return True


def regime_policy_label(policy: str) -> str:
    return {
        "all": "Alle Marktphasen",
        "no_bear": "Keine Neueinstiege im Bärenmarkt",
        "bull_only": "Nur bullische Marktphase",
    }.get(str(policy), str(policy))
