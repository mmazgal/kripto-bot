"""Canlı mum (OHLCV) verisi: önce Binance, olmazsa Bybit."""
try:  # Kurumsal ağlarda (SSL denetimi) Windows sertifika deposunu kullan.
    import truststore  # requests'ten ÖNCE yüklenmeli
    truststore.inject_into_ssl()
except ImportError:
    pass

import os
import ssl
import tempfile
from pathlib import Path

import pandas as pd
import requests


def _setup_curl_ca():
    """yfinance (curl_cffi) truststore'u kullanmaz. Windows'ta kurumsal SSL denetimi varsa
    sistem kök sertifikalarını bir dosyaya çıkarıp ona yönlendir. Linux/Render'da gereksiz."""
    if os.name != "nt" or os.environ.get("CURL_CA_BUNDLE"):
        return
    try:
        pems = [ssl.DER_cert_to_PEM_cert(c) for store in ("ROOT", "CA")
                for c, enc, _ in ssl.enum_certificates(store) if enc == "x509_asn"]
        path = Path(tempfile.gettempdir()) / "bta_winroots.pem"
        path.write_text("".join(pems), encoding="ascii")
        os.environ["CURL_CA_BUNDLE"] = str(path)
    except Exception:  # noqa: BLE001
        pass


_setup_curl_ca()

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
    iv = {"1d": "D", "1w": "W", "4h": "240", "1h": "60", "15m": "15", "5m": "5"}[interval]
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


def get_klines_since(symbol: str, interval: str, start_ms: int, limit: int = 300) -> pd.DataFrame:
    """Belirli bir andan itibaren mumlar (alarm sonuçlarını ölçmek için). Önce Binance, olmazsa Bybit."""
    errors = []
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": symbol, "interval": interval, "startTime": start_ms, "limit": limit},
                         timeout=TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        df = pd.DataFrame(r.json()).iloc[:, :6]
    except Exception as e:  # noqa: BLE001
        errors.append(f"Binance: {e}")
        try:
            iv = {"1h": "60", "15m": "15", "5m": "5"}[interval]
            r = requests.get("https://api.bybit.com/v5/market/kline",
                             params={"category": "spot", "symbol": symbol, "interval": iv,
                                     "start": start_ms, "limit": min(limit, 1000)},
                             timeout=TIMEOUT, headers=HEADERS)
            r.raise_for_status()
            df = pd.DataFrame(r.json()["result"]["list"]).iloc[:, :6]
        except Exception as e2:  # noqa: BLE001
            errors.append(f"Bybit: {e2}")
            raise RuntimeError("Veri alınamadı -> " + " | ".join(errors)) from e2
    if df.empty:
        return df
    df.columns = ["time", "open", "high", "low", "close", "volume"]
    df = df.astype(float)
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    return df.set_index("time").sort_index()


def get_stock_klines(ticker: str, interval: str = "1d", period: str = "2y") -> tuple[pd.DataFrame, str]:
    """Borsa İstanbul hissesi (ör. THYAO.IS) için Yahoo Finance mumları."""
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError(f"{ticker} için veri bulunamadı")
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = df[df["close"].notna() & (df["close"] > 0)]
    if len(df) < 60:
        raise RuntimeError(f"yetersiz veri ({len(df)} mum)")
    return df, "Yahoo Finance"
