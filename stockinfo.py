"""Borsa İstanbul hisseleri: arama ve şirket bilgi kartı (Yahoo Finance, ücretsiz)."""
import data  # noqa: F401  (SSL ayarları; diğer importlardan önce)

import html
import logging

from analysis import fmt_num
from coininfo import clean_desc, fmt_big
from translate import to_turkish

log = logging.getLogger("stockinfo")

SECTORS = {
    "Industrials": "Sanayi", "Financial Services": "Finans", "Basic Materials": "Temel Malzemeler",
    "Consumer Cyclical": "Döngüsel Tüketim", "Consumer Defensive": "Temel Tüketim",
    "Technology": "Teknoloji", "Energy": "Enerji", "Utilities": "Kamu Hizmetleri (Elektrik/Gaz/Su)",
    "Real Estate": "Gayrimenkul", "Healthcare": "Sağlık", "Communication Services": "İletişim",
}
INDEX_ALIASES = {"BIST": "XU100", "BIST100": "XU100", "XU100": "XU100", "BIST30": "XU030", "XU030": "XU030"}


def find_stock(query: str) -> str | None:
    """'thyao', 'aselsan', 'bist100' -> Yahoo sembolü ('THYAO.IS'). Bulunamazsa None."""
    import yfinance as yf
    q = query.strip().upper().removesuffix(".IS")
    q = INDEX_ALIASES.get(q, q)
    try:
        if not yf.Ticker(q + ".IS").history(period="5d").empty:
            return q + ".IS"
    except Exception as e:  # noqa: BLE001
        log.warning("Ticker denemesi başarısız (%s): %s", q, e)
    try:  # şirket adıyla arama (ör. "aselsan", "turk hava")
        for item in yf.Search(query, max_results=10).quotes:
            sym = item.get("symbol", "")
            if sym.endswith(".IS"):
                return sym
    except Exception as e:  # noqa: BLE001
        log.warning("Yahoo arama hatası: %s", e)
    return None


def get_stock_info(ticker: str) -> dict:
    import yfinance as yf
    return yf.Ticker(ticker).info or {}


def build_stock_info_message(ticker: str, info: dict) -> str:
    e = html.escape
    code = ticker.removesuffix(".IS")
    name = info.get("longName") or info.get("shortName") or code
    tl = lambda x: "—" if x is None else f"₺{fmt_num(x)}"  # noqa: E731

    L = [f"<b>🏢 {e(name)} ({e(code)}) Hakkında</b>"]
    if info.get("sector"):
        sector = SECTORS.get(info["sector"], info["sector"])
        industry = info.get("industry") or ""
        industry_tr = (to_turkish(industry) or industry) if industry else ""
        L.append(f"🏷 Sektör: {e(sector)}" + (f" · {e(industry_tr)}" if industry_tr else ""))
    if info.get("fullTimeEmployees"):
        L.append(f"👥 Çalışan sayısı: {info['fullTimeEmployees']:,}".replace(",", "."))
    if info.get("city"):
        L.append(f"📍 Merkez: {e(info['city'])}")
    if info.get("website"):
        L.append(f"🌐 {e(info['website'])}")

    L.append("")
    L.append("<b>— Piyasa Verileri —</b>")
    price = info.get("regularMarketPrice") or info.get("currentPrice")
    prev = info.get("regularMarketPreviousClose") or info.get("previousClose")
    chg = f" ({(price / prev - 1) * 100:+.2f}% günlük)" if price and prev else ""
    L.append(f"💰 Fiyat: {tl(price)}{chg}")
    if info.get("marketCap"):
        L.append(f"🏦 Piyasa değeri: ₺{fmt_big(info['marketCap'])}")
    vol, avg = info.get("regularMarketVolume") or info.get("volume"), info.get("averageVolume")
    if vol:
        line = f"🔄 Günlük hacim: {fmt_big(vol)} lot"
        if avg:
            line += f" (ort. {fmt_big(avg)}, %{vol / avg * 100:.0f})"
        L.append(line)
    if info.get("fiftyTwoWeekHigh"):
        L.append(f"🔝 52 hafta zirve: {tl(info['fiftyTwoWeekHigh'])} · 🔻 dip: {tl(info.get('fiftyTwoWeekLow'))}")
        if price:
            L.append(f"   Zirveden uzaklık: {(price / info['fiftyTwoWeekHigh'] - 1) * 100:+.1f}%")
    dy = info.get("dividendYield")
    if dy:
        dy = dy if dy > 1 else dy * 100  # yfinance sürümüne göre oran ya da yüzde döner
        L.append(f"💵 Temettü verimi: %{dy:.2f}")

    desc = clean_desc(info.get("longBusinessSummary") or "")
    if desc:
        tr = to_turkish(desc)
        note = " <i>(otomatik çeviri)</i>" if tr else " <i>(çeviri yapılamadı)</i>"
        L.append("")
        L.append(f"<b>📖 Şirket ne iş yapar?</b>{note}")
        L.append(e(tr or desc))
    return "\n".join(L)
