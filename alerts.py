"""Fiyat/hacim alarmları: ani hareket, trend başlangıcı, hacim patlaması, günlük teknik sinyaller.

Eşikler her varlığın KENDİ oynaklığına göredir: "15 dakikada %2.5" yerine "15 dakikada normal hareketinin
10 katı". Sabit yüzde BTC'de neredeyse hiç çalmıyordu (30 günde günde 0.25 alarm), PEPE'de ise 4 kat sık
çalıyordu; oynaklığa göre eşikte coinler birbirine yaklaşır.

metrics_frame() ve classify() hem canlı kontrolde hem de learn.py'deki geçmişi yeniden oynatmada aynen
kullanılır; böylece öğrenme döngüsü canlıda olanın aynısını simüle eder.
"""
import data  # noqa: F401  (SSL ayarları; diğer importlardan önce)

import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import indicators as ta
from analysis import CURRENCY, fmt_price
from data import get_klines, get_stock_klines

log = logging.getLogger("alerts")
TR = timezone(timedelta(hours=3))

# ---- Takip listesi ----  (kripto: sembol, BIST: "BIST:KOD")
WATCHLIST = ["BTC", "SOL"]

# ---- Eşikler (normal hareketin katı) ----  learn.py haftalık ayarlar; veritabanındaki ayar bunları ezer.
K15 = 10.0          # ani hareket: 15 dk değişim, varlığın tipik 15 dk hareketinin kaç katı
K1H = 10.0          # ani hareket: 1 saatlik değişim, tipik 1 saatlik hareketin kaç katı
VOLUME_X = 4.0      # hacim: son 15 dk hacmi, o saatin normalinin kaç katı (normal = o 15 dk diliminin
                    # son ~10 günlük medyanı ile son 24 saatin medyanından büyüğü)
VOLUME_K = 4.0      # hacim alarmı için 15 dk fiyat değişimi tipik hareketin en az kaç katı olmalı
TREND_K = 4.0       # trend başlangıcı: 2 saatlik değişim, tipik 2 saatlik hareketin kaç katı
REV_K = 4.0         # dip/tepe dönüşü: son 4 saatin zirvesinden düşüş (dibinden yükseliş), tipik 2 saatlik
                    # hareketin kaç katı
# ---- Sabit koşullar (ayarlanmaz) ----
FLOOR_15M = 0.8     # çok sakin varlıklarda anlamsız küçük hareketler alarm olmasın: en az ±% (15 dk)
FLOOR_1H = 1.2      # en az ±% (1 saat)
TREND_VOL = 1.3     # trend: son 2 saatin hacmi normalin en az bu katı
TREND_ADX = 20.0    # trend: 15 dakikalık ADX en az bu ve yükseliyor
REV_RSI = 25.0      # dönüş: 15 dk RSI ≤ 25 (dip) / ≥ 75 (tepe)
REV_VOL = 3.0       # dönüş: son 1 saatte hacim normalin en az 3 katına çıkmış (satış/alış zirvesi)...
REV_FADE = 0.6      # ...ve şimdi o zirvenin %60'ının altına inmiş (baskı sönüyor)
REV_BOUNCE = 1.0    # dönüş: son 1 saatin dibinden (tepesinden) en az 1 tipik 15 dk hareket kadar dönmüş
BIST_DAY = 4.0      # BIST: önceki kapanışa göre ±% gün içi değişim (günde bir)
# Dönüş alarmı mesajındaki geçmiş test özeti (BTC/ETH/SOL/DOGE/PEPE, 26.10–25.11.2026; "belirgin" = tipik 4s hareket kadar)
REV_HIST_DIP = ("Geçmiş test (30 gün, 5 coin): 100 dip alarmından 43'ünde fiyat 4 saatte belirgin yükseldi "
                "(rastgele anlarda 23), 35'inde düşüş sürdü. Riskli; geçersizlik seviyesine dikkat.")
REV_HIST_TOP = ("Geçmiş test (30 gün, 5 coin): 100 tepe alarmından 70'inde fiyat 4 saatte belirgin düştü "
                "(rastgele anlarda 29). Örneklem küçük (27 alarm) ve düşen piyasada ölçüldü.")
PROFILE_TTL = 3600  # hacim profili ve tipik hareket önbelleği (sn)
COOLDOWN_FAST = 2 * 3600    # ani hareket / hacim alarmı tekrar süresi (sn)
COOLDOWN_TREND = 4 * 3600   # trend alarmı tekrar süresi
COOLDOWN_DAILY = 20 * 3600  # günlük teknik sinyal tekrar süresi

TUNABLE = ("K15", "K1H", "VOLUME_X", "VOLUME_K", "TREND_K", "REV_K")
DEFAULTS = {k: globals()[k] for k in TUNABLE}
CFG_VERSION = 0
COOLDOWNS = {"fast": COOLDOWN_FAST, "volume": COOLDOWN_FAST, "trend": COOLDOWN_TREND, "rev": COOLDOWN_TREND}

_last_sent: dict[tuple[str, str], float] = {}
_profiles: dict[str, tuple[float, object]] = {}


def params() -> dict:
    """Karar için kullanılan tüm eşikler (ayarlananlar + sabitler)."""
    names = TUNABLE + ("FLOOR_15M", "FLOOR_1H", "TREND_VOL", "TREND_ADX", "REV_RSI", "REV_VOL", "REV_FADE",
                       "REV_BOUNCE")
    return {k: float(globals()[k]) for k in names}


class AlertMsg(str):
    """Alarm metni + kayıt numarası (👍/👎 butonları için) + tür (sessize alma için)."""
    event_id: int | None = None
    kind: str = ""
    item: str = ""


def _alert(text: str, item: str, kind: str, m: dict, day_ch: float) -> AlertMsg:
    import learn
    if learn.group_of(kind) in learn.reversal_groups():  # 👎 "ters yöne gitti" geri bildirimi + ölçüm
        text = text.replace("\n👉", "\n↩️ <i>Dikkat: bu tür alarmlardan sonra fiyat çoğunlukla ters döndü "
                                    "(senin geri bildirimin ve ölçüm). Dip/tepe olarak da düşün.</i>\n👉", 1)
    msg = AlertMsg(text)
    msg.event_id = learn.log_event(item, kind, True, m, day_ch)
    msg.kind, msg.item = kind, item
    return msg


def _slot(ts) -> int:
    """Zaman damgasının TSİ'deki 15 dakikalık dilimi (gün içindeki dakika, 15'e yuvarlanmış)."""
    t = ts.astimezone(TR)
    return t.hour * 60 + t.minute // 15 * 15


# ---------------- Ölçüm (canlı ve geri oynatmada ortak) ----------------
def typical_from(close: pd.Series, bars: tuple[int, int, int]) -> dict:
    """Tipik mutlak hareket (%): 15 dk / 1 saat / 2 saat (medyan). bars: o sürelerin mum sayısı."""
    lc = np.log(close)
    return {f"t{n}": float((lc - lc.shift(b)).abs().median() * 100) for n, b in zip(("15", "1h", "2h"), bars)}


def _profile(item: str, fetch):
    """15 dakikalık mumlardan: saat dilimi başına medyan hacim + tipik hareketler. Önbellekli; hata → None."""
    hit = _profiles.get(item)
    if hit and time.time() - hit[0] < PROFILE_TTL:
        return hit[1]
    try:
        df = fetch().iloc[:-1]  # son (açık) mum hariç
        prof = {"vol": df["volume"].groupby([_slot(t) for t in df.index]).median(),
                "typ": typical_from(df["close"], (1, 4, 8))}
    except Exception as ex:  # noqa: BLE001
        log.warning("Hacim profili alınamadı (%s): %s", item, ex)
        prof = None
    _profiles[item] = (time.time(), prof)
    return prof


def metrics_frame(df5: pd.DataFrame, typ, tod=None, session_only: bool = False) -> pd.DataFrame:
    """5 dakikalık mumların her satırı için ölçümler. Son satır = şu an (açık mum).
    typ: {'t15','t1h','t2h'} sabit ya da satır bazında seri. tod: satır bazında o saat diliminin normal hacmi.
    session_only (BIST): her gün seans başında önceki günün kapanışı "ani hareket" sanılmasın."""
    c, v = df5["close"], df5["volume"]
    f = pd.DataFrame(index=df5.index)
    f["price"] = c
    for name, k in (("15", 3), ("1h", 12), ("2h", 24)):
        f["ch" + name] = (c / c.shift(k) - 1) * 100
        f["t" + name] = typ["t" + name]
        f["z" + name] = f["ch" + name] / f["t" + name]
    recent = v.shift(1).rolling(3).sum()                      # son tamamlanmış 15 dk
    base = v.rolling(3).sum().shift(4).rolling(288, min_periods=20).median()  # son ~24 saat
    if tod is not None:
        base = np.fmax(base, tod)
    f["vol_x"] = (recent / base).where(base > 0, 0.0)
    f["vol2x"] = (v.shift(1).rolling(24).sum() / (8 * base)).where(base > 0, 0.0)
    # 15 dakikalık ADX (son tamamlanmış 15 dk mumu; açık mum kullanılmaz)
    m15 = df5.resample("15min", label="left", closed="left").agg(
        {"high": "max", "low": "min", "close": "last"}).dropna()
    a = ta.adx(m15["high"], m15["low"], m15["close"])[0].shift(1)
    f["adx"] = a.reindex(f.index, method="ffill")
    f["adx_up"] = (a > a.shift(3)).reindex(f.index, method="ffill").fillna(False).astype(bool)
    # Dip/tepe dönüşü için: tepeden düşüş / dipten yükseliş (son 4 saat), son 1 saatin ucundan sekme,
    # 15 dk RSI (son tamamlanmış mum), son 1 saatteki en yüksek hacim katı
    hi4, lo4 = df5["high"].rolling(48, min_periods=12).max(), df5["low"].rolling(48, min_periods=12).min()
    f["zdd"] = (c / hi4 - 1) * 100 / f["t2h"]
    f["zru"] = (c / lo4 - 1) * 100 / f["t2h"]
    lo1, hi1 = df5["low"].rolling(12).min(), df5["high"].rolling(12).max()
    f["lo1"], f["hi1"] = lo1, hi1
    f["bounce"] = (c / lo1 - 1) * 100 / f["t15"]
    f["pullback"] = (1 - c / hi1) * 100 / f["t15"]
    f["rsi15"] = ta.rsi(m15["close"]).shift(1).reindex(f.index, method="ffill")
    f["volpk"] = f["vol_x"].rolling(12).max()
    if session_only:
        days = pd.Series([t.astimezone(TR).date() for t in f.index], index=f.index)
        n = days.groupby(days).cumcount() + 1
        f.loc[n <= 4, ["ch15", "z15", "vol_x"]] = 0.0
        f.loc[n <= 13, ["ch1h", "z1h"]] = 0.0
        f.loc[n <= 25, ["ch2h", "z2h", "vol2x"]] = 0.0
    if session_only:  # BIST: önceki günün uçları bugünkü dönüşü sanılmasın
        f.loc[n <= 48, ["zdd", "zru"]] = 0.0
        f.loc[n <= 12, ["bounce", "pullback", "volpk"]] = 0.0
    return f.fillna({"ch15": 0.0, "ch1h": 0.0, "ch2h": 0.0, "z15": 0.0, "z1h": 0.0, "z2h": 0.0,
                     "vol_x": 0.0, "vol2x": 0.0, "adx": 0.0, "zdd": 0.0, "zru": 0.0, "bounce": 0.0,
                     "pullback": 0.0, "rsi15": 50.0, "volpk": 0.0})


def _g(r, k):
    v = r.get(k) if isinstance(r, dict) else r[k]
    return 0.0 if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def trend_ok(r, P: dict) -> bool:
    """Trend başlangıcı koşulları (eşik hariç): 1 saat ile 2 saat aynı yönde ve hareket sürüyor,
    hacim normalin üstünde, 15 dk ADX güçlü ve yükseliyor."""
    c1, c2 = _g(r, "ch1h"), _g(r, "ch2h")
    return (c2 != 0 and np.sign(c1) == np.sign(c2) and abs(c1) >= 0.4 * abs(c2)
            and _g(r, "vol2x") >= P["TREND_VOL"] and _g(r, "adx") >= P["TREND_ADX"] and bool(r.get("adx_up")))


def rev_ok(r, P: dict) -> int:
    """Dip/tepe dönüşü koşulları (eşik hariç): +1 dip (yukarı dönüş), -1 tepe (aşağı dönüş), 0 yok.
    Aşırı RSI + hacim zirvesi yapıp sönmüş + son 1 saatin ucundan dönmeye başlamış."""
    vp, vx, rsi = _g(r, "volpk"), _g(r, "vol_x"), _g(r, "rsi15")
    if vp < P["REV_VOL"] or vx > P["REV_FADE"] * vp:
        return 0
    if rsi <= P["REV_RSI"] and _g(r, "bounce") >= P["REV_BOUNCE"]:
        return 1
    if rsi >= 100 - P["REV_RSI"] and _g(r, "pullback") >= P["REV_BOUNCE"]:
        return -1
    return 0


def classify(r, P: dict) -> list[tuple[str, str]]:
    """Bir ölçüm satırı hangi alarmları tetikler: [(tür, kind)], tür ∈ fast/volume/trend."""
    c15, c1h, c2h = _g(r, "ch15"), _g(r, "ch1h"), _g(r, "ch2h")
    fast = ((abs(_g(r, "z15")) >= P["K15"] and abs(c15) >= P["FLOOR_15M"])
            or (abs(_g(r, "z1h")) >= P["K1H"] and abs(c1h) >= P["FLOOR_1H"]))
    if fast:
        return [("fast", "drop" if (c1h if abs(c1h) >= abs(c15) else c15) < 0 else "pump")]
    out = []
    if _g(r, "vol_x") >= P["VOLUME_X"] and abs(_g(r, "z15")) >= P["VOLUME_K"]:
        out.append(("volume", "volume"))
    if abs(_g(r, "z2h")) >= P["TREND_K"] and trend_ok(r, P):
        out.append(("trend", "trend_up" if c2h > 0 else "trend_down"))
    d = rev_ok(r, P)
    if (d > 0 and _g(r, "zdd") <= -P["REV_K"]) or (d < 0 and _g(r, "zru") >= P["REV_K"]):
        out.append(("rev", "rev_up" if d > 0 else "rev_down"))
    return out


def near(r, P: dict, f: float = 0.5) -> bool:
    """Eşiğin f katına yaklaşan olay (öğrenme için 'aday' olarak kaydedilir)."""
    return (abs(_g(r, "z15")) >= P["K15"] * f or abs(_g(r, "z1h")) >= P["K1H"] * f
            or (_g(r, "vol_x") >= P["VOLUME_X"] * f and abs(_g(r, "z15")) >= P["VOLUME_K"] * f)
            or abs(_g(r, "z2h")) >= P["TREND_K"] * 0.6
            or (_g(r, "zdd") <= -P["REV_K"] * 0.6 and _g(r, "rsi15") <= P["REV_RSI"] + 10)
            or (_g(r, "zru") >= P["REV_K"] * 0.6 and _g(r, "rsi15") >= 90 - P["REV_RSI"]))


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


def _live_metrics(item: str, df5: pd.DataFrame, is_bist: bool, fetch15) -> dict:
    prof = _profile(item, fetch15)
    typ = prof["typ"] if prof else typical_from(df5["close"], (3, 12, 24))
    tod = None
    if prof:
        slots = pd.Series([_slot(t) for t in df5.index], index=df5.index).shift(3)
        tod = slots.map(prof["vol"]).astype(float)
    m = metrics_frame(df5, typ, tod, session_only=is_bist).iloc[-1].to_dict()
    m["price"] = float(m["price"])
    return m


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


def _levels_line(daily, price) -> str:
    res, sup = _levels(daily, price)
    lv = " · ".join(x for x in (f"direnç {fmt_price(res)}" if res else "",
                                f"destek {fmt_price(sup)}" if sup else "") if x)
    return f"\nYakın seviyeler: {lv}" if lv else ""


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
        fetch15 = lambda: get_stock_klines(code + ".IS", "15m", "1mo")[0]  # noqa: E731
    else:
        df5, _ = get_klines(code + "USDT", "5m", 300)
        daily, _ = get_klines(code + "USDT", "1d", 200)
        fetch15 = lambda: get_klines(code + "USDT", "15m", 1000)[0]  # noqa: E731

    m = _live_metrics(item, df5, is_bist, fetch15)
    prev_close = float(daily["close"].iloc[-2])
    if is_bist:  # Yahoo geçmişinde gün eksik olabiliyor; resmi önceki kapanışı kullan
        try:
            import yfinance as yf
            prev_close = float(yf.Ticker(code + ".IS").fast_info["previousClose"]) or prev_close
        except Exception:  # noqa: BLE001
            pass
    day_ch = (m["price"] / prev_close - 1) * 100

    if report:
        return [f"{label}: {fmt_price(m['price'])} · 15dk {m['ch15']:+.2f}% ({abs(m['z15']):.1f}x) · "
                f"1s {m['ch1h']:+.2f}% ({abs(m['z1h']):.1f}x) · 2s {m['ch2h']:+.2f}% ({abs(m['z2h']):.1f}x) · "
                f"gün {day_ch:+.2f}% · hacim {m['vol_x']:.1f}x"]

    P = params()
    sig = dict(classify(m, P))
    msgs = []
    head = f"<b>{label}</b> {fmt_price(m['price'])}"
    if "fast" in sig or (is_bist and abs(day_ch) >= BIST_DAY):
        if "fast" in sig:
            kind, cooldown = sig["fast"], COOLDOWN_FAST
            down = kind == "drop"
        else:  # BIST gün içi toplam değişim: günde bir kez
            down = day_ch < 0
            kind, cooldown = ("day_drop" if down else "day_pump"), COOLDOWN_DAILY
        if _due(code, kind, cooldown):
            title = "🚨🔻 ANİ DÜŞÜŞ" if down else "🚀🟢 ANİ YÜKSELİŞ"
            msgs.append(_alert(
                f"{title} — {head}\n15 dk: {m['ch15']:+.2f}% (normalin {abs(m['z15']):.0f} katı) · "
                f"1 saat: {m['ch1h']:+.2f}% (normalin {abs(m['z1h']):.0f} katı) · gün: {day_ch:+.2f}%\n"
                f"Hacim: normalin {m['vol_x']:.1f} katı\n👉 {cmd}", item, kind, m, day_ch))
    # Hacim: testte yönü tahmin etmedi (~%50) ama sonrasında hareketin sertleştiğini gösterdi.
    if "volume" in sig and _due(code, "volume", COOLDOWN_FAST):
        msgs.append(_alert(
            f"📊🔥 YÜKSEK İŞLEM HACMİ — {head}\n"
            f"Son 15 dk hacmi bu saatin normalinin <b>{m['vol_x']:.1f} katı</b> · "
            f"fiyat 15 dk: {m['ch15']:+.2f}% (normalin {abs(m['z15']):.0f} katı)\n"
            "⚡ Sert hareket ihtimali yüksek; yön hacimden tek başına okunamaz."
            + _levels_line(daily, m["price"]) + f"\n👉 {cmd}", item, "volume", m, day_ch))
    # Trend başlangıcı: 15 dk'lık patlama değil, saatlere yayılan istikrarlı hareket
    if "trend" in sig and _due(code, "trend", COOLDOWN_TREND):
        up = sig["trend"] == "trend_up"
        msgs.append(_alert(
            f"{'📈🟢 YÜKSELİŞ TRENDİ BAŞLIYOR OLABİLİR' if up else '📉🔴 DÜŞÜŞ TRENDİ BAŞLIYOR OLABİLİR'} — {head}\n"
            f"2 saat: <b>{m['ch2h']:+.2f}%</b> (normalin {abs(m['z2h']):.1f} katı) · 1 saat: {m['ch1h']:+.2f}%\n"
            f"Hacim son 2 saatte normalin {m['vol2x']:.1f} katı · 15 dk ADX {m['adx']:.0f} ve yükseliyor"
            + _levels_line(daily, m["price"]) + f"\n👉 {cmd}", item, sig["trend"], m, day_ch))
    # Dip/tepe dönüşü: sert hareketten sonra aşırı RSI + sönen hacim + dönüş başlangıcı
    if "rev" in sig and _due(code, "rev", COOLDOWN_TREND):
        dip = sig["rev"] == "rev_up"
        ext = m["lo1"] if dip else m["hi1"]
        hist = (REV_HIST_DIP if dip else REV_HIST_TOP)
        drop = m["zdd"] * m["t2h"] if dip else m["zru"] * m["t2h"]
        turn = (m["price"] / ext - 1) * 100
        msgs.append(_alert(
            f"{'🔄🟢 DİP DÖNÜŞÜ ADAYI' if dip else '🔄🔴 TEPE DÖNÜŞÜ ADAYI'} — {head}\n"
            f"Son 4 saatte {'zirveden' if dip else 'dipten'} <b>{drop:+.2f}%</b> "
            f"(normalin {abs(m['zdd'] if dip else m['zru']):.1f} katı) · 15 dk RSI {m['rsi15']:.0f}\n"
            f"{'Satış' if dip else 'Alış'} hacmi zirve yaptı (normalin {m['volpk']:.1f} katı), şimdi söndü "
            f"({m['vol_x']:.1f} katı) · {'dipten' if dip else 'tepeden'} {turn:+.2f}% döndü\n"
            f"❗ Geçersizlik: fiyat {fmt_price(ext)} {'altına inerse' if dip else 'üstüne çıkarsa'} dönüş bozulur."
            + _levels_line(daily, m["price"]) +
            f"\n<i>{hist}</i>\n👉 {cmd}", item, sig["rev"], m, day_ch))
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
