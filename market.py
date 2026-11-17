"""Piyasa duyarlılığı, haberler ve anlık fiyat (hepsi ücretsiz kaynaklar)."""
import data  # noqa: F401  (SSL ayarları; requests'ten önce)

import html
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger("market")
HEADERS = {"User-Agent": "Mozilla/5.0 (crypto-ta-bot)"}
_cache: dict[str, tuple[float, object]] = {}

FNG_TR = {"Extreme Fear": "Aşırı Korku", "Fear": "Korku", "Neutral": "Nötr",
          "Greed": "Açgözlülük", "Extreme Greed": "Aşırı Açgözlülük"}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


# ---------------- Anlık fiyat ----------------
def current_price(item: str) -> float:
    if item.upper().startswith("BIST:"):
        import yfinance as yf
        return float(yf.Ticker(item.split(":", 1)[1].upper() + ".IS").fast_info["lastPrice"])
    sym = item.upper() + "USDT"
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": sym},
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:  # noqa: BLE001
        r = requests.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot", "symbol": sym},
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        return float(r.json()["result"]["list"][0]["lastPrice"])


def usd_try() -> float | None:
    def f():
        import yfinance as yf
        return float(yf.Ticker("TRY=X").fast_info["lastPrice"])
    try:
        return _cached("usdtry", 600, f)
    except Exception as e:  # noqa: BLE001
        log.warning("USD/TRY alınamadı: %s", e)
        return None


# ---------------- Duyarlılık ----------------
def fear_greed() -> dict | None:
    def f():
        r = requests.get("https://api.alternative.me/fng/", params={"limit": 2}, headers=HEADERS, timeout=10)
        r.raise_for_status()
        d = r.json()["data"]
        return {"value": int(d[0]["value"]), "label": FNG_TR.get(d[0]["value_classification"], d[0]["value_classification"]),
                "prev": int(d[1]["value"]) if len(d) > 1 else None}
    try:
        return _cached("fng", 600, f)
    except Exception as e:  # noqa: BLE001
        log.warning("Fear&Greed alınamadı: %s", e)
        return None


def btc_dominance() -> float | None:
    def f():
        r = requests.get("https://api.coingecko.com/api/v3/global", headers=HEADERS, timeout=10)
        r.raise_for_status()
        return float(r.json()["data"]["market_cap_percentage"]["btc"])
    try:
        return _cached("dom", 600, f)
    except Exception as e:  # noqa: BLE001
        log.warning("BTC dominansı alınamadı: %s", e)
        return None


def bist100_change() -> float | None:
    def f():
        import yfinance as yf
        fi = yf.Ticker("XU100.IS").fast_info
        return (float(fi["lastPrice"]) / float(fi["previousClose"]) - 1) * 100
    try:
        return _cached("xu100", 600, f)
    except Exception as e:  # noqa: BLE001
        log.warning("BIST100 değişimi alınamadı: %s", e)
        return None


def fng_emoji(v: int) -> str:
    return "😱" if v < 25 else "😟" if v < 45 else "😐" if v < 56 else "😊" if v < 75 else "🤑"


def sentiment_line(market: str) -> str:
    """Rapor başlığına eklenen tek satırlık piyasa havası."""
    parts = []
    if market == "bist":
        x = bist100_change()
        if x is not None:
            parts.append(f"BIST100 {x:+.2f}%")
        u = usd_try()
        if u:
            parts.append(f"USD/TRY {u:.2f}")
    else:
        fg = fear_greed()
        if fg:
            parts.append(f"{fng_emoji(fg['value'])} Korku/Açgözlülük: {fg['value']} ({fg['label']})")
        dom = btc_dominance()
        if dom:
            parts.append(f"BTC dominansı %{dom:.1f}")
    return ("🌡 Piyasa havası: " + " · ".join(parts)) if parts else ""


def market_overview() -> str:
    """/piyasa komutu ve sabah özeti için ayrıntılı piyasa durumu."""
    L = ["<b>🌡 Piyasa Havası</b>"]
    fg = fear_greed()
    if fg:
        trend = ""
        if fg["prev"] is not None:
            trend = f" (dün {fg['prev']})"
        L.append(f"{fng_emoji(fg['value'])} Kripto Korku/Açgözlülük: <b>{fg['value']}</b> – {fg['label']}{trend}")
        if fg["value"] < 25:
            L.append("   ↳ Piyasada panik var; tarihsel olarak uzun vadeli alım fırsatı bölgeleri.")
        elif fg["value"] > 75:
            L.append("   ↳ Aşırı iyimserlik; düzeltme riski artıyor.")
    dom = btc_dominance()
    if dom:
        L.append(f"👑 BTC dominansı: %{dom:.1f}" +
                 ("  (yüksek: para BTC'de, altcoinler zayıf)" if dom > 55 else
                  "  (düşük: altcoin sezonu belirtisi)" if dom < 45 else ""))
    x = bist100_change()
    if x is not None:
        L.append(f"🇹🇷 BIST 100 günlük: {x:+.2f}%")
    u = usd_try()
    if u:
        L.append(f"💵 USD/TRY: {u:.2f}")
    return "\n".join(L)


# ---------------- Haberler ----------------
def _ago(dt: datetime) -> str:
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 3600:
        return f"{int(s // 60)} dk önce"
    if s < 86400:
        return f"{int(s // 3600)} saat önce"
    return f"{int(s // 86400)} gün önce"


def news(query: str, n: int = 3) -> list[dict]:
    def f():
        r = requests.get("https://news.google.com/rss/search",
                         params={"q": query, "hl": "tr", "gl": "TR", "ceid": "TR:tr"},
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        out = []
        for it in ET.fromstring(r.content).iter("item"):
            title = it.findtext("title") or ""
            src = it.findtext("source") or ""
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate"))
            except Exception:  # noqa: BLE001
                dt = None
            out.append({"title": title, "link": it.findtext("link"), "source": src, "date": dt})
        out.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out
    try:
        return _cached("news:" + query, 900, f)[:n]
    except Exception as e:  # noqa: BLE001
        log.warning("Haber alınamadı (%s): %s", query, e)
        return []


def news_block(query: str, n: int = 3) -> str:
    items = news(query, n)
    if not items:
        return ""
    e = html.escape
    L = ["<b>📰 Son Haberler</b>"]
    for it in items:
        when = f" · {_ago(it['date'])}" if it["date"] else ""
        L.append(f"• <a href=\"{e(it['link'] or '')}\">{e(it['title'])}</a> <i>({e(it['source'])}{when})</i>")
    return "\n".join(L)
