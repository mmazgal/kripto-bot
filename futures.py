"""Binance vadeli işlem verileri (ücretsiz, anahtarsız): fonlama oranı, açık pozisyon, long/short oranı."""
import data  # noqa: F401  (SSL ayarları)

import logging
import time

import requests

log = logging.getLogger("futures")
F = "https://fapi.binance.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (crypto-ta-bot)"}
_cache: dict[str, tuple[float, object]] = {}

FUNDING_HOT = 0.0005    # 8 saatlik %0.05 üstü: long'lar kalabalık
FUNDING_ALERT = 0.001   # %0.1: alarm


def _get(path, **params):
    r = requests.get(F + path, params=params, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def funding(symbol: str) -> float | None:
    key = "f:" + symbol
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 240:
        return hit[1]
    try:
        v = float(_get("/fapi/v1/premiumIndex", symbol=symbol)["lastFundingRate"])
    except Exception:  # noqa: BLE001
        v = None
    _cache[key] = (time.time(), v)
    return v


def snapshot(coin: str) -> dict | None:
    sym = coin.upper() + "USDT"
    fr = funding(sym)
    if fr is None:
        return None  # vadeli işlemde yok
    out = {"funding": fr}
    try:
        oi = _get("/futures/data/openInterestHist", symbol=sym, period="1d", limit=8)
        if len(oi) >= 2:
            out["oi"] = float(oi[-1]["sumOpenInterestValue"])
            out["oi_chg"] = (float(oi[-1]["sumOpenInterestValue"]) / float(oi[0]["sumOpenInterestValue"]) - 1) * 100
    except Exception as ex:  # noqa: BLE001
        log.warning("OI alınamadı (%s): %s", sym, ex)
    try:
        ls = _get("/futures/data/globalLongShortAccountRatio", symbol=sym, period="1d", limit=1)
        if ls:
            out["long_pct"] = float(ls[-1]["longAccount"]) * 100
            out["ls"] = float(ls[-1]["longShortRatio"])
    except Exception as ex:  # noqa: BLE001
        log.warning("L/S alınamadı (%s): %s", sym, ex)
    return out


def section(coin: str, price_chg_7d: float | None = None) -> list[str]:
    """Analiz raporuna eklenecek satırlar (HTML güvenli)."""
    s = snapshot(coin)
    if not s:
        return []
    from coininfo import fmt_big
    L = ["", "<b>— Vadeli İşlem Verileri (Binance) —</b>"]
    fr = s["funding"]
    note = ("long'lar kalabalık, ani düşüşte zincirleme tasfiye riski" if fr >= FUNDING_HOT else
            "short'lar kalabalık, ani yükselişte 'short squeeze' olabilir" if fr <= -0.0003 else "dengeli")
    L.append(f"💸 Fonlama oranı: {fr * 100:+.4f}% (8 saatlik) — {note}")
    if "oi" in s:
        oi_note = ""
        if price_chg_7d is not None and "oi_chg" in s:
            if s["oi_chg"] > 5 and price_chg_7d < 0:
                oi_note = " — fiyat düşerken pozisyon artıyor: yeni short'lar açılıyor"
            elif s["oi_chg"] > 5 and price_chg_7d > 0:
                oi_note = " — fiyat ve pozisyon birlikte artıyor: yükseliş destekleniyor"
            elif s["oi_chg"] < -5:
                oi_note = " — pozisyonlar kapanıyor: hareket zayıflayabilir"
        L.append(f"📦 Açık pozisyon: ${fmt_big(s['oi'])} (7g {s.get('oi_chg', 0):+.1f}%){oi_note}")
    if "long_pct" in s:
        crowd = " — kalabalık long tarafında" if s["ls"] > 2.5 else " — kalabalık short tarafında" if s["ls"] < 0.8 else ""
        L.append(f"⚖️ Hesapların %{s['long_pct']:.0f}'i long (L/S {s['ls']:.2f}){crowd}")
    return L


def funding_alert(coin: str) -> str | None:
    fr = funding(coin.upper() + "USDT")
    if fr is None or abs(fr) < FUNDING_ALERT:
        return None
    side = "LONG" if fr > 0 else "SHORT"
    return (f"💸 Aşırı fonlama oranı: {fr * 100:+.3f}% (8 saatlik). {side} tarafı çok kalabalık; "
            "ters yönde sert bir tasfiye hareketi riski yüksek.")
