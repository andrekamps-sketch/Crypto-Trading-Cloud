from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import numpy as np
import pandas as pd

FROZEN_QUALITY = {
    "min_breakout_score": 75.0,
    "min_trend_score": 65.0,
    "min_volume_ratio": 1.0,
    "min_momentum_score": 55.0,
    "rsi_min": 0.0,
    "rsi_max": 75.0,
    "min_return_24h": 1.0,
    "high_distance_min": -1.0,
    "high_distance_max": 1.5,
    "regime_policy": "no_bear",
    "min_edge_score": 80.0,
}

@dataclass
class MonitorRow:
    asset: str
    price: float
    score: float
    signal: str
    risk: str
    trend: float
    momentum: float
    rsi: float
    breakout: float
    volume_ratio: float
    return_24h: float
    return_7d: float
    atr_pct: float
    high_distance: float
    quality_edge: float
    quality_ready: bool
    rules_ok: int
    rules_total: int
    missing: str
    note: str

    def to_dict(self):
        return asdict(self)


def _clamp(x, lo=0.0, hi=100.0):
    return float(max(lo, min(hi, x)))


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    down = -d.clip(upper=0.0)
    au = up.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    ad = down.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = au / ad.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def atr_pct(df: pd.DataFrame, period=14):
    h,l,c = df["High"].astype(float), df["Low"].astype(float), df["Close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h-l).abs(), (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    a = tr.rolling(period).mean().iloc[-1]
    p = float(c.iloc[-1])
    return float(a/p*100) if p > 0 and pd.notna(a) else 0.0


def proximity_quality(distance: float) -> float:
    if -1.5 <= distance <= 1.5:
        return 100.0 - abs(distance) * 12.0
    if -4.0 <= distance < -1.5:
        return 75.0 + (distance + 4.0) * 8.0
    if 1.5 < distance <= 4.0:
        return 80.0 - (distance - 1.5) * 18.0
    return max(0.0, 45.0 - abs(distance) * 5.0)


def score_frame(asset: str, df: pd.DataFrame, regime: str = "NEUTRAL") -> MonitorRow:
    if len(df) < 220:
        raise RuntimeError(f"Zu wenig 1h-Daten ({len(df)}).")
    c = df["Close"].astype(float)
    p = float(c.iloc[-1])
    e20 = c.ewm(span=20, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    e200 = c.ewm(span=200, adjust=False).mean()
    trend = (35 if p > e20.iloc[-1] else 0) + (30 if e20.iloc[-1] > e50.iloc[-1] else 0) + (35 if e50.iloc[-1] > e200.iloc[-1] else 0)
    r24 = (p / float(c.iloc[-25]) - 1) * 100 if len(c) >= 25 else 0.0
    r7 = (p / float(c.iloc[-169]) - 1) * 100 if len(c) >= 169 else 0.0
    momentum = _clamp(50 + r24*5.0 + r7*1.8)
    rv = float(rsi(c,14).iloc[-1])
    if 52 <= rv <= 68: rsi_s = 100 - abs(60-rv)*4
    elif 45 <= rv < 52: rsi_s = 65 + (rv-45)*5
    elif 68 < rv <= 75: rsi_s = 80 - (rv-68)*8
    elif rv > 75: rsi_s = max(5, 30-(rv-75)*4)
    else: rsi_s = max(5, 55-(45-rv)*4)
    rsi_s = _clamp(rsi_s)
    lookback = min(len(df)-1, 24*30)
    prior = df["High"].iloc[-lookback-1:-1] if lookback > 10 else df["High"].iloc[:-1]
    prior_high = float(prior.max()) if len(prior) else p
    dist = (p/prior_high - 1)*100 if prior_high > 0 else 0.0
    if -3 <= dist <= 3: breakout = 90-abs(dist)*8
    elif -8 <= dist < -3: breakout = 65+(dist+8)*5
    elif dist > 3: breakout = max(30,75-(dist-3)*7)
    else: breakout = max(15,40+dist*2)
    breakout = _clamp(breakout)
    atr = atr_pct(df)
    if .5 <= atr <= 3.5: vol_s = 90-abs(2.0-atr)*12
    elif 3.5 < atr <= 6: vol_s = 70-(atr-3.5)*12
    elif atr > 6: vol_s = max(5,40-(atr-6)*8)
    else: vol_s = max(20,55+atr*30)
    vol_s = _clamp(vol_s)
    v = df["Volume"].astype(float).fillna(0)
    recent = float(v.tail(24).mean())
    base = float(v.tail(24*14).mean()) if len(v)>=24*14 else float(v.mean())
    vr = recent/base if base>0 else 1.0
    volume_s = _clamp(40+(vr-.7)*55)
    total = _clamp(trend*.28 + momentum*.22 + rsi_s*.16 + breakout*.14 + vol_s*.12 + volume_s*.08)
    if rv > 80: total = _clamp(total-15)
    if atr > 8: total = _clamp(total-15)
    risk = "hoch" if atr>6 or rv>78 else ("mittel" if atr>3.5 or rv>72 else "normal")
    signal = "STARK" if total>=75 and trend>=65 and rv<78 else ("INTERESSANT" if total>=62 and trend>=35 else ("BEOBACHTEN" if total>=48 else "MEIDEN"))
    prox = proximity_quality(dist)
    edge = .34*breakout + .22*trend + .18*volume_s + .14*momentum + .12*prox

    q = FROZEN_QUALITY
    checks = [
        (risk != "hoch", f"Risiko {risk}"),
        (regime != "BEAR", f"Marktphase {regime}"),
        (breakout >= q["min_breakout_score"], f"Breakout {breakout:.0f}/{q['min_breakout_score']:.0f}"),
        (trend >= q["min_trend_score"], f"Trend {trend:.0f}/{q['min_trend_score']:.0f}"),
        (vr >= q["min_volume_ratio"], f"Volumen {vr:.2f}x/{q['min_volume_ratio']:.1f}x"),
        (momentum >= q["min_momentum_score"], f"Momentum {momentum:.0f}/{q['min_momentum_score']:.0f}"),
        (q["rsi_min"] <= rv <= q["rsi_max"], f"RSI {rv:.1f} ≤ {q['rsi_max']:.0f}"),
        (r24 >= q["min_return_24h"], f"24h {r24:+.2f}% ≥ {q['min_return_24h']:+.1f}%"),
        (q["high_distance_min"] <= dist <= q["high_distance_max"], f"30T-Hoch-Abstand {dist:+.2f}%"),
        (edge >= q["min_edge_score"], f"Edge {edge:.1f}/{q['min_edge_score']:.0f}"),
    ]
    missing = [txt for ok,txt in checks if not ok]
    notes=[]
    if trend>=65: notes.append("Trend positiv")
    if vr>=1.3: notes.append("Volumen erhöht")
    if rv>72: notes.append("RSI hoch")
    if abs(dist)<=1.5: notes.append("nahe 30T-Hoch")
    if not notes: notes.append("kein außergewöhnlicher Vorteil")
    return MonitorRow(asset,p,round(total,1),signal,risk,float(trend),round(momentum,1),round(rv,1),round(breakout,1),round(vr,2),round(r24,2),round(r7,2),round(atr,2),round(dist,2),round(edge,1),not missing,len(checks)-len(missing),len(checks),"; ".join(missing),", ".join(notes))


def classify_regime(btc: MonitorRow) -> dict:
    t,r24,r7 = btc.trend,btc.return_24h,btc.return_7d
    if t>=65 and r7>0 and r24>-3:
        return {"code":"BULL","label":"Bullisch","emoji":"🟢","reason":f"BTC-Trend {t:.0f}, 7T {r7:+.1f}%"}
    if t<35 and r7<0 and r24<1:
        return {"code":"BEAR","label":"Bärisch","emoji":"🔴","reason":f"BTC-Trend {t:.0f}, 7T {r7:+.1f}%"}
    return {"code":"NEUTRAL","label":"Neutral","emoji":"🟡","reason":f"BTC-Trend {t:.0f}, 7T {r7:+.1f}%"}


def rescore_quality(rows: list[MonitorRow], regime_code: str) -> list[MonitorRow]:
    # Re-score only quality readiness because regime is known after BTC classification.
    out=[]
    for r in rows:
        # easiest: reconstruct checks from existing row metrics.
        q=FROZEN_QUALITY
        checks=[
            (r.risk!="hoch", f"Risiko {r.risk}"),
            (regime_code!="BEAR", f"Marktphase {regime_code}"),
            (r.breakout>=q["min_breakout_score"], f"Breakout {r.breakout:.0f}/{q['min_breakout_score']:.0f}"),
            (r.trend>=q["min_trend_score"], f"Trend {r.trend:.0f}/{q['min_trend_score']:.0f}"),
            (r.volume_ratio>=q["min_volume_ratio"], f"Volumen {r.volume_ratio:.2f}x/{q['min_volume_ratio']:.1f}x"),
            (r.momentum>=q["min_momentum_score"], f"Momentum {r.momentum:.0f}/{q['min_momentum_score']:.0f}"),
            (q["rsi_min"]<=r.rsi<=q["rsi_max"], f"RSI {r.rsi:.1f} ≤ {q['rsi_max']:.0f}"),
            (r.return_24h>=q["min_return_24h"], f"24h {r.return_24h:+.2f}% ≥ {q['min_return_24h']:+.1f}%"),
            (q["high_distance_min"]<=r.high_distance<=q["high_distance_max"], f"30T-Hoch-Abstand {r.high_distance:+.2f}%"),
            (r.quality_edge>=q["min_edge_score"], f"Edge {r.quality_edge:.1f}/{q['min_edge_score']:.0f}"),
        ]
        missing=[txt for ok,txt in checks if not ok]
        r.quality_ready=not missing; r.rules_ok=len(checks)-len(missing); r.rules_total=len(checks); r.missing="; ".join(missing)
        out.append(r)
    return out
