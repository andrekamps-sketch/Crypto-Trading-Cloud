from __future__ import annotations

import pandas as pd


def ema_cross(df: pd.DataFrame) -> pd.Series:
    close = df["Close"]
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    return (fast > slow).astype(int).rename("EMA 12/26")


def sma_cross(df: pd.DataFrame) -> pd.Series:
    close = df["Close"]
    fast = close.rolling(20).mean()
    slow = close.rolling(50).mean()
    return (fast > slow).fillna(False).astype(int).rename("SMA 20/50")


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def rsi_pullback(df: pd.DataFrame) -> pd.Series:
    close = df["Close"]
    r = rsi(close)
    trend = close > close.rolling(100).mean()
    # Zustandssignal: im Aufwaertstrend halten, sobald RSI nach einem Ruecksetzer > 40 liegt.
    condition = trend & (r > 40) & (r < 72)
    return condition.fillna(False).astype(int).rename("RSI Pullback")


def breakout(df: pd.DataFrame) -> pd.Series:
    close = df["Close"]
    prior_high = df["High"].rolling(30).max().shift(1)
    trailing = close.rolling(12).min()
    entered = (close > prior_high).fillna(False)
    signal = pd.Series(0, index=df.index, dtype=int)
    in_pos = False
    entry_floor = None
    for i, ts in enumerate(df.index):
        if not in_pos and bool(entered.iloc[i]):
            in_pos = True
            entry_floor = trailing.iloc[i]
        elif in_pos:
            floor = trailing.iloc[i]
            if pd.notna(floor):
                entry_floor = floor
            if entry_floor is not None and close.iloc[i] < entry_floor:
                in_pos = False
        signal.loc[ts] = 1 if in_pos else 0
    return signal.rename("30D Breakout")


def trend_rsi(df: pd.DataFrame) -> pd.Series:
    close = df["Close"]
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    r = rsi(close)
    cond = (ema50 > ema200) & (r > 45) & (r < 75)
    return cond.fillna(False).astype(int).rename("Trend + RSI")


STRATEGIES = {
    "EMA 12/26": ema_cross,
    "SMA 20/50": sma_cross,
    "RSI Pullback": rsi_pullback,
    "30D Breakout": breakout,
    "Trend + RSI": trend_rsi,
}
