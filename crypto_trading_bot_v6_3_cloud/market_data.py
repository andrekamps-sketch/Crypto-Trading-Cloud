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


def load_fast_prices(assets: list[str], interval: str = "5m", period: str = "5d") -> dict[str, dict]:
    """Load a near-live price snapshot for multiple assets in one batch when possible.

    Uses the latest completed/available Yahoo candle. This is intended for paper
    mirroring and monitoring, not exchange-grade execution pricing.
    """
    assets = [str(a).upper() for a in assets if str(a).upper() in CRYPTO_UNIVERSE]
    if not assets:
        return {}
    symbols = {a: CRYPTO_UNIVERSE[a] for a in assets}
    out: dict[str, dict] = {}
    try:
        raw = yf.download(list(symbols.values()), period=period, interval=interval, auto_adjust=False,
                          progress=False, threads=True, group_by="ticker")
        if len(assets) == 1 and not isinstance(raw.columns, pd.MultiIndex):
            a = assets[0]
            x = normalize(raw)
            if not x.empty:
                out[a] = {"price": float(x["Close"].iloc[-1]), "timestamp": pd.Timestamp(x.index[-1]).isoformat(),
                          "source": f"Yahoo Finance {interval}"}
        elif isinstance(raw.columns, pd.MultiIndex):
            level0 = set(map(str, raw.columns.get_level_values(0)))
            for asset, symbol in symbols.items():
                try:
                    if symbol in level0:
                        sub = raw[symbol]
                    else:
                        # Fallback for the alternate yfinance MultiIndex orientation.
                        sub = raw.xs(symbol, axis=1, level=1)
                    x = normalize(sub)
                    if not x.empty:
                        out[asset] = {"price": float(x["Close"].iloc[-1]), "timestamp": pd.Timestamp(x.index[-1]).isoformat(),
                                      "source": f"Yahoo Finance {interval}"}
                except Exception:
                    continue
    except Exception:
        out = {}

    # Only retry missing assets individually. This keeps normal operation to one request
    # while remaining resilient to occasional yfinance batch-format changes.
    for asset in assets:
        if asset in out:
            continue
        try:
            symbol = symbols[asset]
            df = yf.download(symbol, period=period, interval=interval, auto_adjust=False,
                             progress=False, threads=False)
            x = normalize(df)
            if x.empty:
                continue
            out[asset] = {"price": float(x["Close"].iloc[-1]), "timestamp": pd.Timestamp(x.index[-1]).isoformat(),
                          "source": f"Yahoo Finance {interval}"}
        except Exception:
            continue
    return out
