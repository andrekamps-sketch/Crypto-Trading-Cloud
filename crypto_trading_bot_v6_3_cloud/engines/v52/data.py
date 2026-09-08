from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

import pandas as pd
import yfinance as yf

# Bewusst auf große/liquide Coins begrenzt. Die Liste kann später erweitert werden.
CRYPTO_UNIVERSE: Dict[str, str] = {
    "BTC": "BTC-EUR",
    "ETH": "ETH-EUR",
    "SOL": "SOL-EUR",
    "XRP": "XRP-EUR",
    "BNB": "BNB-EUR",
    "ADA": "ADA-EUR",
    "DOGE": "DOGE-EUR",
    "AVAX": "AVAX-EUR",
    "LINK": "LINK-EUR",
    "LTC": "LTC-EUR",
    "DOT": "DOT-EUR",
    "BCH": "BCH-EUR",
}

SYMBOLS = {f"{a} ({s})": s for a, s in CRYPTO_UNIVERSE.items()}


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise RuntimeError("Keine Kursdaten empfangen.")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).title() for c in df.columns})
    needed = ["Open", "High", "Low", "Close"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise RuntimeError(f"Fehlende Kurs-Spalten: {missing}")
    df = df.dropna(subset=["Close"]).copy()
    if "Volume" not in df.columns:
        df["Volume"] = 0.0
    return df


def load_history(symbol: str, start, end) -> pd.DataFrame:
    df = yf.download(
        symbol,
        start=start,
        end=end,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    return _normalize(df)



def load_hourly_history(symbol: str, start, end) -> pd.DataFrame:
    """1h-Historie fuer den Scanner-Backtest. Yahoo begrenzt Intraday-Historien."""
    df = yf.download(
        symbol,
        start=start,
        end=end,
        interval="1h",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    return _normalize(df)


def load_recent(symbol: str, interval: str = "1h", period: str = "60d") -> pd.DataFrame:
    df = yf.download(
        symbol,
        interval=interval,
        period=period,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    return _normalize(df)


def latest_price(symbol: str) -> float:
    try:
        df = load_recent(symbol, interval="5m", period="5d")
    except Exception:
        df = load_recent(symbol, interval="1h", period="5d")
    return float(df["Close"].iloc[-1])


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
