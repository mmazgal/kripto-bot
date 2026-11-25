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
VOLUME_X = 4.0      # son 15 dk hacmi, "normal"in kaç katı. Normal = o saat diliminin son ~10 günlük
                    # medyanı ile son 24 saatin medyanından büyüğü (ABD açılışı gibi her gün tekrarlanan
                    # hacim artışları alarm sayılmaz)
VOLUME_MIN_MOVE = 0.7  # hacim alarmı için fiyat 15 dk'da en az ±% bu kadar oynamış olmalı
                       # (30 günlük testte alarm sayısı yarıya indi, sonraki hareket normalin ~2.7 katı)
BASELINE_TTL = 3600    # saat dilimi hacim profili önbelleği (sn)
COOLDOWN_FAST = 2 * 3600   # fiyat/hacim alarmı tekrar süresi (sn)
COOLDOWN_DAILY = 20 * 3600  # günlük teknik sinyal tekrar süresi (sn)

# Yukarıdaki dört eşik learn.py tarafından haftalık ayarlanır; veritabanındaki ayar bunların üzerine yazılır.
DEFAULTS = {"DROP_15M": DROP_15M, "DROP_1H": DROP_1H, "VOLUME_X": VOLUME_X, "VOLUME_MIN_MOVE": VOLUME_MIN_MOVE}
CFG_VERSION = 0

_last_sent: dict[tuple[str, str], float] = {}
_profiles: dict[str, tuple[float, object]] = {}


class AlertMsg(str):
    """Alarm metni + kayıt numarası (👍/👎 butonları için)."""
    event_id: int | None = None


def _alert(text: str, item: str, kind: str, m: dict, day_ch: float) -> AlertMsg:
    import learn
    msg = AlertMsg(text)
    msg.event_id = learn.log_event(item, kind, True, m, day_ch)
    return msg


def _slot(ts) -> int:
    """Zaman damgasının TSİ'deki 15 dakikalık dilimi (gün içindeki dakika, 15'e yuvarlanmış)."""
    t = ts.astimezone(TR)
    return t.hour * 60 + t.minute // 15 * 15


def _volume_profile(item: str, fetch):
    """Her 15 dk'lık saat dilimi için son günlerin medyan hacmi (önbellekli). Hata olursa None."""
    hit = _profiles.get(item)
    if hit and time.time() - hit[0] < BASELINE_TTL:
        return hit[1]
    try:
        v = fetch()["volume"].iloc[:-1]  # son (açık) mum hariç
        prof = v.groupby([_slot(t) for t in v.index]).median()
    except Exception as ex:  # noqa: BLE001
        log.warning("Hacim profili alınamadı (%s): %s", item, ex)
        prof = None
    _profiles[item] = (time.time(), prof)
    return prof


def _due(symbol: str, kind: str, cooldown: int) -> bool:
    key = (symbol, kind)
    now = time.time()
    if now - _last_sent.get(key, 0) < cooldown:
        return False
    _last_sent[key] = now
    if cooldown >= COOLDOWN_DAILY:  # günlük sinyaller: sunucu yeniden başlasa da aynı gün tekrar gönderme
        import events
        pkey = f"alert:{symbol}:{kind}:{datetime.now(TR).date().isoformat()}"
        if events.already_notified(pkey):
            return False
        events.mark_notified(pkey)
    return True


def bist_open() -> bool:
    now = datetime.now(TR)
    return now.weekday() < 5 and (10, 0) <= (now.hour, now.minute) <= (18, 10)


def _intraday_metrics(df5, session_only: bool = False, profile=None) -> dict:
    """5 dakikalık mumlardan 15 dk / 1 saat değişim ve hacim katı.
    session_only (BIST): yalnız bugünün seans mumları sayılır; açılışta önceki günün kapanışı ve
    kapanış seansı hacmi "ani hareket"/"hacim patlaması" sanılmasın."""
    c, v = df5["close"], df5["volume"]
    price = float(c.iloc[-1])
    n = len(c)
    if session_only:
        today = datetime.now(TR).date()
        n = int(sum(1 for t in df5.index if t.astimezone(TR).date() == today))
    ch15 = (price / float(c.iloc[-4]) - 1) * 100 if n > 4 else 0.0
    ch1h = (price / float(c.iloc[-13]) - 1) * 100 if n > 13 else 0.0
    vol15 = v.rolling(3).sum()
    # son tamamlanmış 15 dk (açık mum hariç) vs "normal": son 24 saatin medyanı ile
    # o saat diliminin geçmiş günlerdeki medyanından büyüğü
    recent = float(v.iloc[-4:-1].sum())
    base = float(vol15.iloc[-292:-4].median()) if len(vol15) > 20 else np.nan
    if profile is not None and n > 4:
        slot_med = profile.get(_slot(df5.index[-4]))
        if slot_med is not None and slot_med > 0 and not (base >= slot_med):
            base = float(slot_med)
    vol_x = recent / base if n > 4 and base and base > 0 else 0.0
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
    # Pivot 3 mum sonra doğrulanır; tam doğrulandığı gün (bars_ago == 3) bir kez bildir
    for f in ta.divergences(daily, recent=3):
        if f["bars_ago"] == 3:
            out.append((f"div_{f['type']}", ta.divergence_text([f])[0]))
    return out


def _levels(daily, price: float) -> tuple[float | None, float | None]:
    """Fiyatın üstündeki en yakın direnç ve altındaki en yakın destek
    (5/20/50 günlük uçlar, Bollinger bantları ve 20 günlük ortalama)."""
    up, mid, lo, _, _ = ta.bollinger(daily["close"])
    cands = [float(daily["high"].iloc[-6:-1].max()), float(daily["low"].iloc[-6:-1].min()), float(mid.iloc[-1]),
             float(daily["high"].iloc[-20:].max()), float(daily["low"].iloc[-20:].min()),
             float(daily["high"].iloc[-50:].max()), float(daily["low"].iloc[-50:].min()),
             float(up.iloc[-1]), float(lo.iloc[-1])]
    above = [x for x in cands if x > price]
    below = [x for x in cands if x < price]
    return (min(above) if above else None), (max(below) if below else None)


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
        profile = _volume_profile(item, lambda: get_stock_klines(code + ".IS", "15m", "1mo")[0])
    else:
        df5, _ = get_klines(code + "USDT", "5m", 300)
        daily, _ = get_klines(code + "USDT", "1d", 200)
        profile = _volume_profile(item, lambda: get_klines(code + "USDT", "15m", 1000)[0])

    m = _intraday_metrics(df5, session_only=is_bist, profile=profile)
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
                f"gün {day_ch:+.2f}% · hacim {m['vol_x']:.1f}x (saat normaline göre)"]

    msgs = []
    head = f"<b>{label}</b> {fmt_price(m['price'])}"
    fast = abs(m["ch15"]) >= DROP_15M or abs(m["ch1h"]) >= DROP_1H
    if fast or (is_bist and abs(day_ch) >= BIST_DAY):
        if fast:
            down = (m["ch1h"] if abs(m["ch1h"]) >= abs(m["ch15"]) else m["ch15"]) < 0
            kind, cooldown = ("drop" if down else "pump"), COOLDOWN_FAST
        else:  # BIST gün içi toplam değişim: gün boyu iki saatte bir tekrar etmesin, günde bir kez
            down = day_ch < 0
            kind, cooldown = ("day_drop" if down else "day_pump"), COOLDOWN_DAILY
        if _due(code, kind, cooldown):
            title = "🚨🔻 ANİ DÜŞÜŞ" if down else "🚀🟢 ANİ YÜKSELİŞ"
            msgs.append(_alert(f"{title} — {head}\n15 dk: {m['ch15']:+.2f}% · 1 saat: {m['ch1h']:+.2f}% · "
                               f"gün: {day_ch:+.2f}%\nHacim: normalin {m['vol_x']:.1f} katı\n👉 {cmd}",
                               item, kind, m, day_ch))
    # Hacim alarmı: fiyat da oynamış olmalı; ani hareket alarmı zaten hacmi yazıyorsa ayrıca gönderme.
    # Testte hacim yönü tahmin etmedi (~%50) ama sonrasında hareketin sertleştiğini gösterdi.
    if (m["vol_x"] >= VOLUME_X and abs(m["ch15"]) >= VOLUME_MIN_MOVE and not fast
            and _due(code, "volume", COOLDOWN_FAST)):
        res, sup = _levels(daily, m["price"])
        lv = " · ".join(x for x in (f"direnç {fmt_price(res)}" if res else "",
                                    f"destek {fmt_price(sup)}" if sup else "") if x)
        msgs.append(_alert(f"📊🔥 YÜKSEK İŞLEM HACMİ — {head}\n"
                           f"Son 15 dk hacmi bu saatin normalinin <b>{m['vol_x']:.1f} katı</b> · "
                           f"fiyat 15 dk: {m['ch15']:+.2f}%\n"
                           "⚡ Sert hareket ihtimali yüksek; yön hacimden tek başına okunamaz."
                           + (f"\nYakın seviyeler: {lv}" if lv else "") + f"\n👉 {cmd}",
                           item, "volume", m, day_ch))
    if not msgs:  # eşiğe yaklaşan ama alarm olmayan olay: öğrenme için kaydet
        import learn
        learn.maybe_log_candidate(item, m, day_ch)
    if not is_bist:
        try:
            import futures
            fa = futures.funding_alert(code)
            if fa and _due(code, "funding", COOLDOWN_DAILY):
                msgs.append(_alert(f"🔔 VADELİ İŞLEM UYARISI — {head}\n{fa}\n👉 {cmd}", item, "funding", m, day_ch))
        except Exception as ex:  # noqa: BLE001
            log.warning("Fonlama kontrolü hatası (%s): %s", code, ex)
    for kind, text in _daily_signals(daily):
        if _due(code, kind, COOLDOWN_DAILY):
            msgs.append(_alert(f"🔔 TEKNİK SİNYAL — {head}\n{text}\n👉 {cmd}", item, kind, m, day_ch))
    return msgs


def check_all(report: bool = False, items: list[str] | None = None) -> list[str]:
    import learn
    learn.apply_config()
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
    import learn
    learn.apply_config()  # haftalık ayarla güncellenmiş eşikler
    out = {}
    for item in items:
        try:
            out[item] = check_symbol(item)
        except Exception as e:  # noqa: BLE001
            log.warning("Alarm kontrolü hatası (%s): %s", item, e)
            out[item] = []
    return out
