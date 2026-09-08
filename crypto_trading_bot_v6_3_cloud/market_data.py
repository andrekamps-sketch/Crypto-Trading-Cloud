from __future__ import annotations

from typing import Dict
import pandas as pd
import yfinance as yf

CRYPTO_UNIVERSE: Dict[str, str] = {
    "BTC": "BTC-EUR", "ETH": "ETH-EUR", "SOL": "SOL-EUR", "XRP": "XRP-EUR",
    "BNB": "BNB-EUR", "ADA": "ADA-EUR", "DOGE": "DOGE-EUR", "AVAX": "AVAX-EUR",
    "LINK": "LINK-EUR", "LTC": "LTC-EUR", "DOT": "DOT-EUR", "BCH": "BCH-EUR",
}


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise RuntimeError("Keine Kursdaten empfangen.")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).title() for c in df.columns})
    if "Close" not in df.columns:
        raise RuntimeError("Close-Spalte fehlt.")
    for c in ["Open", "High", "Low"]:
        if c not in df.columns:
            df[c] = df["Close"]
    if "Volume" not in df.columns:
        df["Volume"] = 0.0
    out = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"]).copy()
    out.index = pd.to_datetime(out.index, utc=True)
    return out[~out.index.duplicated(keep="last")].sort_index()


def load_recent(asset: str, period: str = "60d", interval: str = "1h") -> pd.DataFrame:
    symbol = CRYPTO_UNIVERSE[asset]
    df = yf.download(symbol, period=period, interval=interval, auto_adjust=False,
                     progress=False, threads=False)
    return normalize(df)


def load_hourly(asset: str, start_utc: pd.Timestamp, end_utc: pd.Timestamp) -> pd.DataFrame:
    symbol = CRYPTO_UNIVERSE[asset]
    df = yf.download(symbol, start=start_utc.to_pydatetime(), end=end_utc.to_pydatetime(),
                     interval="1h", auto_adjust=False, progress=False, threads=False)
    return normalize(df)


def load_v6_bundle(start_utc: pd.Timestamp, warmup_days: int = 75):
    now = pd.Timestamp.now(tz="UTC")
    hist_start = start_utc - pd.Timedelta(days=warmup_days)
    end = now + pd.Timedelta(hours=2)
    return {a: load_hourly(a, hist_start, end) for a in ["BTC", "ETH"]}
