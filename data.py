"""Canlı mum (OHLCV) verisi: önce Binance, olmazsa Bybit."""
try:  # Kurumsal ağlarda (SSL denetimi) Windows sertifika deposunu kullan.
    import truststore  # requests'ten ÖNCE yüklenmeli
    truststore.inject_into_ssl()
except ImportError:
    pass

import pandas as pd
import requests

TIMEOUT = 15
HEADERS = {"User-Agent": "crypto-ta-bot/1.0"}


def _binance(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=TIMEOUT, headers=HEADERS,
    )
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["time", "open", "high", "low", "close", "volume"]
    return df


def _bybit(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    iv = {"1d": "D", "1w": "W", "4h": "240"}[interval]
    r = requests.get(
        "https://api.bybit.com/v5/market/kline",
        params={"category": "spot", "symbol": symbol, "interval": iv, "limit": min(limit, 1000)},
        timeout=TIMEOUT, headers=HEADERS,
    )
    r.raise_for_status()
    js = r.json()
    if js.get("retCode") != 0:
        raise RuntimeError(js.get("retMsg"))
    df = pd.DataFrame(js["result"]["list"]).iloc[:, :6]
    df.columns = ["time", "open", "high", "low", "close", "volume"]
    return df.iloc[::-1]  # Bybit yeniden eskiye sıralar


def get_klines(symbol: str, interval: str = "1d", limit: int = 500) -> tuple[pd.DataFrame, str]:
    errors = []
    for name, fn in (("Binance", _binance), ("Bybit", _bybit)):
        try:
            df = fn(symbol, interval, limit)
            df = df.astype(float)
            df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
            df = df.set_index("time").sort_index()
            if len(df) < 60:
                raise RuntimeError(f"yetersiz veri ({len(df)} mum)")
            return df, name
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise RuntimeError("Veri alınamadı -> " + " | ".join(errors))
