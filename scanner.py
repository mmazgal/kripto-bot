"""Günün fırsatları: ilk 100 coin ve BIST 100'ü tarayıp dikkat çeken teknik sinyalleri listeler.

Bu bir sinyal listesidir, al/sat önerisi DEĞİLDİR (backtest göstergelerin tek başına tahmin gücünün zayıf
olduğunu gösterdi). Amaç, bakmaya değer varlıkları öne çıkarmak.
"""
import data  # noqa: F401  (SSL ayarları)

import html
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

import indicators as ta
from analysis import score_series
from data import get_klines

log = logging.getLogger("scanner")
TR = timezone(timedelta(hours=3))
HEADERS = {"User-Agent": "Mozilla/5.0 (crypto-ta-bot)"}
e = html.escape

# BIST 100 bileşimi çeyreklik değişir; listede olmayan/işlem görmeyen kodlar sessizce atlanır.
BIST100 = """AEFES AGHOL AKBNK AKSA AKSEN ALARK ALFAS ALTNY ANSGR ARCLK ASELS ASTOR BERA BIMAS BRSAN BRYAT BTCIM
CANTE CCOLA CIMSA CLEBI CWENE DOAS DOHOL ECILC EGEEN EKGYO ENERY ENJSA ENKAI EREGL EUPWR FROTO GARAN GESAN GLYHO
GUBRF HALKB HEKTS IPEKE ISCTR ISMEN KARSN KCAER KCHOL KONTR KOZAA KOZAL KRDMD KTLEV LMKDK MAVI MGROS MIATK MPARK
OBAMS ODAS OTKAR OYAKC PASEU PETKM PGSUS QUAGR REEDR SAHOL SASA SDTTR SISE SKBNK SMRTG SOKM TABGD TAVHL TCELL
THYAO TKFEN TOASO TRALT TSKB TTKOM TTRAK TUKAS TUPRS TURSG ULKER VAKBN VESBE VESTL YEOTK YKBNK ZOREN""".split()

STABLE = {"usdt", "usdc", "dai", "fdusd", "tusd", "usde", "usds", "pyusd", "busd", "usdd", "frax", "usd1", "rlusd",
          "gusd", "usdp", "lusd", "eurc", "eurs", "susde", "usdb", "usdx", "xaut", "paxg", "bfusd", "usdtb", "usdf"}

_cache: dict[str, tuple[float, object]] = {}


# ---------------- Evrenler ----------------
def crypto_universe(n: int = 100) -> list[str]:
    r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                     params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 200, "page": 1},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    t = requests.get("https://api.binance.com/api/v3/ticker/price", headers=HEADERS, timeout=20)
    t.raise_for_status()
    pairs = {x["symbol"] for x in t.json()}
    out = []
    for c in r.json():
        s = c["symbol"].lower()
        name = c["name"].lower()
        if s in STABLE or any(w in name for w in ("wrapped", "staked", "bridged", "restaked")):
            continue
        if abs((c.get("price_change_percentage_24h") or 0)) < 0.3 and 0.97 < (c.get("current_price") or 0) < 1.03:
            continue  # stablecoin gibi davranan
        if s.upper() + "USDT" in pairs and s.upper() not in out:
            out.append(s.upper())
        if len(out) >= n:
            break
    return out


def _crypto_frame(sym: str):
    try:
        df, _ = get_klines(sym + "USDT", "1d", 200)
        return sym, df
    except Exception:  # noqa: BLE001
        return sym, None


def load_crypto(n: int = 100) -> dict[str, pd.DataFrame]:
    syms = crypto_universe(n)
    with ThreadPoolExecutor(8) as ex:
        return {s: df for s, df in ex.map(_crypto_frame, syms) if df is not None}


def load_bist() -> dict[str, pd.DataFrame]:
    import yfinance as yf
    tickers = [c + ".IS" for c in BIST100]
    raw = yf.download(tickers, period="1y", interval="1d", group_by="ticker", auto_adjust=True,
                      threads=True, progress=False)
    out = {}
    for c in BIST100:
        try:
            df = raw[c + ".IS"].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) >= 80:
                out[c] = df
        except Exception:  # noqa: BLE001
            continue
    return out


# ---------------- Sinyaller ----------------
def signals_for(df: pd.DataFrame) -> dict:
    sc = score_series(df)
    c, v = df["close"], df["volume"]
    r = ta.rsi(c)
    line, sig, _ = ta.macd(c)
    _, trend = ta.parabolic_sar(df["high"], df["low"])
    _, _, _, _, bw = ta.bollinger(c)
    sigs = []  # (yön, ağırlık, açıklama)
    if trend.iloc[-1] != trend.iloc[-3] or trend.iloc[-1] != trend.iloc[-2]:
        d = int(trend.iloc[-1])
        sigs.append((d, 1.5, "SAR " + ("yükselişe" if d > 0 else "düşüşe") + " döndü"))
    cross = np.sign(line - sig)
    if cross.iloc[-1] != cross.iloc[-3]:
        d = int(cross.iloc[-1])
        sigs.append((d, 1.0, "MACD " + ("yukarı" if d > 0 else "aşağı") + " kesti"))
    rn = float(r.iloc[-1])
    if rn > 30 and float(r.iloc[-4:-1].min()) < 30:
        sigs.append((1, 1.5, "RSI aşırı satımdan çıktı"))
    elif rn < 70 and float(r.iloc[-4:-1].max()) > 70:
        sigs.append((-1, 1.5, "RSI aşırı alımdan düştü"))
    elif rn <= 30:
        sigs.append((1, 0.75, f"RSI aşırı satımda ({rn:.0f})"))
    elif rn >= 70:
        sigs.append((-1, 0.75, f"RSI aşırı alımda ({rn:.0f})"))
    s = sc["total"]
    if s.iloc[-1] >= 0.2 > s.iloc[-3]:
        sigs.append((1, 1.0, "skor yükseliş bölgesine geçti"))
    elif s.iloc[-1] <= -0.2 < s.iloc[-3]:
        sigs.append((-1, 1.0, "skor düşüş bölgesine geçti"))
    vavg = float(v.iloc[-21:-1].mean())
    if vavg > 0 and float(v.iloc[-1]) > 2.5 * vavg:
        d = 1 if c.iloc[-1] >= c.iloc[-2] else -1
        sigs.append((d, 1.0, f"hacim {float(v.iloc[-1]) / vavg:.1f}x"))
    for f in ta.divergences(df):
        d = 1 if f["type"] == "bull" else -1
        sigs.append((d, 0.75, ("pozitif" if d > 0 else "negatif") + f" uyumsuzluk ({f['osc']})"))
    squeeze = float((bw.iloc[-120:] < bw.iloc[-1]).mean()) < 0.1
    rank = sum(d * w for d, w, _ in sigs) + 0.5 * float(s.iloc[-1])
    chg = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
    return {"signals": sigs, "rank": rank, "score": float(s.iloc[-1]), "squeeze": squeeze, "chg": chg,
            "price": float(c.iloc[-1]), "rsi": rn}


def scan(frames: dict[str, pd.DataFrame]) -> dict[str, dict]:
    out = {}
    for k, df in frames.items():
        try:
            out[k] = signals_for(df)
        except Exception as ex:  # noqa: BLE001
            log.warning("Tarama hatası (%s): %s", k, ex)
    return out


# ---------------- Rapor ----------------
def _fmt(name: str, r: dict, want: int) -> str:
    reasons = [t for d, _, t in r["signals"] if d == want] or ["genel skor"]
    return f"• <b>{e(name)}</b> ({r['chg']:+.1f}%) — {e(', '.join(reasons[:3]))}"


def render(results: dict[str, dict], title: str, cmd: str, top: int = 5) -> str:
    bulls = sorted([(k, r) for k, r in results.items() if r["rank"] > 0.5 and any(d > 0 for d, _, _ in r["signals"])],
                   key=lambda x: -x[1]["rank"])[:top]
    bears = sorted([(k, r) for k, r in results.items() if r["rank"] < -0.5 and any(d < 0 for d, _, _ in r["signals"])],
                   key=lambda x: x[1]["rank"])[:top]
    squeeze = sorted([(k, r) for k, r in results.items() if r["squeeze"]], key=lambda x: -abs(x[1]["score"]))[:4]
    L = [f"<b>{title}</b> <i>({len(results)} varlık tarandı)</i>", ""]
    L.append("<b>🟢 Yükseliş sinyali verenler</b>")
    L.extend(_fmt(k, r, 1) for k, r in bulls) if bulls else L.append("• Belirgin sinyal yok")
    L.append("")
    L.append("<b>🔴 Düşüş sinyali verenler</b>")
    L.extend(_fmt(k, r, -1) for k, r in bears) if bears else L.append("• Belirgin sinyal yok")
    if squeeze:
        L.append("")
        L.append("<b>⚡ Bantları sıkışanlar</b> (sert hareket yakın olabilir)")
        L.append("• " + e(", ".join(k for k, _ in squeeze)))
    L.append(f"\nDetay için: {cmd}")
    return "\n".join(L)


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def crypto_report() -> str:
    res = _cached("crypto", 1800, lambda: scan(load_crypto()))
    return render(res, "🔎 Günün Fırsatları — Kripto (ilk 100)", "/analiz &lt;coin&gt;")


def bist_report() -> str:
    res = _cached("bist", 1800, lambda: scan(load_bist()))
    return render(res, "🔎 Günün Fırsatları — BIST 100", "/hisse &lt;kod&gt;")


FOOTER = ("<i>⚠️ Bu liste teknik sinyallerin dökümüdür, al/sat önerisi değildir. Backtest, göstergelerin tek başına "
          "tahmin gücünün zayıf olduğunu gösterdi; bakmaya değer adayları öne çıkarmak için kullanın.</i>")
