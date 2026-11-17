"""Fiyat/hacim alarmları: ani düşüş-yükseliş, hacim patlaması, günlük teknik sinyaller.

Takip listesi ve eşikler aşağıda. Değiştirmek için bu dosyayı düzenlemek yeterli.
"""
import data  # noqa: F401  (SSL ayarları; diğer importlardan önce)

import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

import indicators as ta
from analysis import CURRENCY, fmt_price
from data import get_klines, get_stock_klines

log = logging.getLogger("alerts")
TR = timezone(timedelta(hours=3))

# ---- Takip listesi ----  (kripto: sembol, BIST: "BIST:KOD")
WATCHLIST = ["BTC", "SOL"]

# ---- Eşikler ----
DROP_15M = 2.5      # 15 dakikada ±% değişim
DROP_1H = 4.0       # 1 saatte ±% değişim
BIST_DAY = 4.0      # BIST: önceki kapanışa göre ±% gün içi değişim
VOLUME_X = 4.0      # son 15 dk hacmi, son 24 saatin 15 dk'lık medyanının kaç katı
COOLDOWN_FAST = 2 * 3600   # fiyat/hacim alarmı tekrar süresi (sn)
COOLDOWN_DAILY = 20 * 3600  # günlük teknik sinyal tekrar süresi (sn)

_last_sent: dict[tuple[str, str], float] = {}


def _due(symbol: str, kind: str, cooldown: int) -> bool:
    key = (symbol, kind)
    now = time.time()
    if now - _last_sent.get(key, 0) < cooldown:
        return False
    _last_sent[key] = now
    return True


def bist_open() -> bool:
    now = datetime.now(TR)
    return now.weekday() < 5 and (10, 0) <= (now.hour, now.minute) <= (18, 10)


def _intraday_metrics(df5) -> dict:
    """5 dakikalık mumlardan 15 dk / 1 saat değişim ve hacim katı."""
    c, v = df5["close"], df5["volume"]
    price = float(c.iloc[-1])
    ch15 = (price / float(c.iloc[-4]) - 1) * 100 if len(c) > 4 else 0.0
    ch1h = (price / float(c.iloc[-13]) - 1) * 100 if len(c) > 13 else 0.0
    vol15 = v.rolling(3).sum()
    # son tamamlanmış 15 dk (açık mum hariç) vs önceki 24 saatin medyanı
    recent = float(v.iloc[-4:-1].sum())
    base = float(vol15.iloc[-292:-4].median()) if len(vol15) > 20 else np.nan
    vol_x = recent / base if base and base > 0 else 0.0
    return {"price": price, "ch15": ch15, "ch1h": ch1h, "vol_x": vol_x}


def _daily_signals(daily) -> list[tuple[str, str]]:
    """Bugün oluşan günlük teknik sinyaller: (tür, açıklama)."""
    out = []
    _, trend = ta.parabolic_sar(daily["high"], daily["low"])
    if trend.iloc[-1] != trend.iloc[-2]:
        out.append(("sar", "🔄 Günlük Parabolic SAR " +
                    ("YÜKSELİŞ trendine döndü." if trend.iloc[-1] == 1 else "DÜŞÜŞ trendine döndü.")))
    r = ta.rsi(daily["close"])
    now_r, prev_r = float(r.iloc[-1]), float(r.iloc[-2])
    if now_r < 30 <= prev_r:
        out.append(("rsi30", f"📉 Günlük RSI aşırı SATIM bölgesine girdi ({now_r:.0f})."))
    if now_r > 70 >= prev_r:
        out.append(("rsi70", f"📈 Günlük RSI aşırı ALIM bölgesine girdi ({now_r:.0f})."))
    return out


def check_symbol(item: str, report: bool = False) -> list[str]:
    """Tek bir varlığı kontrol eder. report=True ise eşiğe bakmadan durum özeti döner."""
    is_bist = item.upper().startswith("BIST:")
    code = item.split(":", 1)[1].upper() if is_bist else item.upper()
    CURRENCY.set(("" if code.startswith("XU") else "₺") if is_bist else "$")
    label = f"{code} (BIST)" if is_bist else code
    cmd = f"/hisse {code.lower()}" if is_bist else f"/analiz {code.lower()}"

    if is_bist:
        if not report and not bist_open():
            return []
        df5, _ = get_stock_klines(code + ".IS", "5m", "5d")
        daily, _ = get_stock_klines(code + ".IS", "1d", "1y")
    else:
        df5, _ = get_klines(code + "USDT", "5m", 300)
        daily, _ = get_klines(code + "USDT", "1d", 200)

    m = _intraday_metrics(df5)
    prev_close = float(daily["close"].iloc[-2])
    if is_bist:  # Yahoo geçmişinde gün eksik olabiliyor; resmi önceki kapanışı kullan
        try:
            import yfinance as yf
            prev_close = float(yf.Ticker(code + ".IS").fast_info["previousClose"]) or prev_close
        except Exception:  # noqa: BLE001
            pass
    day_ch = (m["price"] / prev_close - 1) * 100

    if report:
        return [f"{label}: {fmt_price(m['price'])} · 15dk {m['ch15']:+.2f}% · 1s {m['ch1h']:+.2f}% · "
                f"gün {day_ch:+.2f}% · hacim {m['vol_x']:.1f}x"]

    msgs = []
    head = f"<b>{label}</b> {fmt_price(m['price'])}"
    if abs(m["ch15"]) >= DROP_15M or abs(m["ch1h"]) >= DROP_1H or (is_bist and abs(day_ch) >= BIST_DAY):
        down = (m["ch1h"] if abs(m["ch1h"]) >= abs(m["ch15"]) else m["ch15"]) < 0
        if is_bist and abs(m["ch15"]) < DROP_15M and abs(m["ch1h"]) < DROP_1H:
            down = day_ch < 0
        kind = "drop" if down else "pump"
        if _due(code, kind, COOLDOWN_FAST):
            title = "🚨🔻 ANİ DÜŞÜŞ" if down else "🚀🟢 ANİ YÜKSELİŞ"
            msgs.append(f"{title} — {head}\n15 dk: {m['ch15']:+.2f}% · 1 saat: {m['ch1h']:+.2f}% · "
                        f"gün: {day_ch:+.2f}%\nHacim: normalin {m['vol_x']:.1f} katı\n👉 {cmd}")
    if m["vol_x"] >= VOLUME_X and _due(code, "volume", COOLDOWN_FAST):
        yon = "alış" if m["ch15"] >= 0 else "satış"
        msgs.append(f"📊🔥 YÜKSEK İŞLEM HACMİ — {head}\nSon 15 dk hacmi normalin <b>{m['vol_x']:.1f} katı</b> "
                    f"(fiyat 15 dk: {m['ch15']:+.2f}%, ağırlık {yon} yönlü)\n👉 {cmd}")
    for kind, text in _daily_signals(daily):
        if _due(code, kind, COOLDOWN_DAILY):
            msgs.append(f"🔔 TEKNİK SİNYAL — {head}\n{text}\n👉 {cmd}")
    return msgs


def check_all(report: bool = False, items: list[str] | None = None) -> list[str]:
    msgs = []
    for item in items or WATCHLIST:
        try:
            msgs += check_symbol(item, report=report)
        except Exception as e:  # noqa: BLE001
            log.warning("Alarm kontrolü hatası (%s): %s", item, e)
            if report:
                msgs.append(f"{item}: veri alınamadı ({e})")
    return msgs


def check_items(items: list[str]) -> dict[str, list[str]]:
    """Her varlık için alarm mesajlarını ayrı ayrı döndürür (sohbet bazlı dağıtım için)."""
    out = {}
    for item in items:
        try:
            out[item] = check_symbol(item)
        except Exception as e:  # noqa: BLE001
            log.warning("Alarm kontrolü hatası (%s): %s", item, e)
            out[item] = []
    return out
