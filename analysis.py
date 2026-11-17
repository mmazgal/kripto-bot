"""Göstergeleri hesaplar, yorumlar ve 1 hafta / 1 ay tahmini üretir.

Komut satırından test:  python analysis.py [btc|sol]
"""
import contextvars
import html
import math
import sys
from datetime import datetime, timezone, timedelta

import numpy as np

import indicators as ta
from data import get_klines, get_stock_klines

# Para birimi sembolü (kripto: $, BIST: ₺). ContextVar: eşzamanlı isteklerde karışmaz.
CURRENCY = contextvars.ContextVar("currency", default="$")

COINS = {
    "BTC": {"symbol": "BTCUSDT", "name": "Bitcoin"},
    "SOL": {"symbol": "SOLUSDT", "name": "Solana"},
}

# Gösterge ağırlıkları (toplam skora katkı)
WEIGHTS = {"MACD": 1.5, "SAR": 1.0, "ADX": 1.0, "RSI": 1.0, "StochRSI": 0.75, "Bollinger": 0.75}


def fmt_num(x: float) -> str:
    """Küçük fiyatlı coinler için anlamlı basamakları koruyarak biçimlendirir."""
    a = abs(x)
    if a >= 1000:
        return f"{x:,.0f}"
    if a >= 1:
        return f"{x:,.2f}"
    if a == 0:
        return "0"
    decimals = min(12, -math.floor(math.log10(a)) + 3)
    return f"{x:.{decimals}f}"


def fmt_price(p: float) -> str:
    return CURRENCY.get() + fmt_num(p)


def score_label(s: float) -> str:
    if s >= 0.5:
        return "🟢🟢 GÜÇLÜ YÜKSELİŞ"
    if s >= 0.2:
        return "🟢 YÜKSELİŞ EĞİLİMİ"
    if s > -0.2:
        return "⚪ NÖTR / YATAY"
    if s > -0.5:
        return "🔴 DÜŞÜŞ EĞİLİMİ"
    return "🔴🔴 GÜÇLÜ DÜŞÜŞ"


def analyze_frame(df) -> dict:
    """Bir zaman dilimi (günlük/haftalık) için tüm göstergeleri hesaplar ve puanlar."""
    c, h, l = df["close"], df["high"], df["low"]
    price = float(c.iloc[-1])
    out = {"price": price, "signals": {}}

    # --- Bollinger Bands (20, 2) ---
    up, mid, lo, pb, bw = ta.bollinger(c)
    pb_v = float(pb.iloc[-1])
    bw_v = float(bw.iloc[-1])
    bw_rank = float((bw.iloc[-120:] < bw_v).mean())  # son 120 mumdaki yüzdelik
    if pb_v > 1:
        s, t = -0.5, "Fiyat üst bandın ÜSTÜNDE: aşırı uzama, geri çekilme riski."
    elif pb_v < 0:
        s, t = 0.5, "Fiyat alt bandın ALTINDA: aşırı satım, tepki yükselişi olası."
    else:
        s = float(np.clip(pb_v - 0.5, -0.5, 0.5))
        pos = "üst banda yakın (alıcılar baskın)" if pb_v > 0.7 else (
            "alt banda yakın (satıcılar baskın)" if pb_v < 0.3 else "orta bant civarında")
        t = f"Fiyat {pos}."
    if bw_rank < 0.2:
        t += " ⚠️ Bantlar SIKIŞMIŞ: yakında sert bir kırılım hareketi beklenebilir."
    elif bw_rank > 0.8:
        t += " Bantlar çok geniş: volatilite yüksek."
    out["bb"] = {"upper": float(up.iloc[-1]), "mid": float(mid.iloc[-1]), "lower": float(lo.iloc[-1]),
                 "pb": pb_v}
    out["signals"]["Bollinger"] = (s * 2, f"%B={pb_v:.2f}", t)  # [-1,1] aralığına ölçekle

    # --- Parabolic SAR ---
    sar, trend = ta.parabolic_sar(h, l)
    sar_v = float(sar.iloc[-1])
    tr = int(trend.iloc[-1])
    flipped = (trend.iloc[-4:] != tr).any()
    if tr == 1:
        t = f"SAR fiyatın ALTINDA ({fmt_price(sar_v)}): yükseliş trendi. Bu seviye dinamik destek/stop."
    else:
        t = f"SAR fiyatın ÜSTÜNDE ({fmt_price(sar_v)}): düşüş trendi. Bu seviye dinamik direnç."
    if flipped:
        t += " 🔄 Trend son birkaç mumda YÖN DEĞİŞTİRDİ."
    out["sar"] = sar_v
    out["signals"]["SAR"] = (float(tr), "Yükseliş" if tr == 1 else "Düşüş", t)

    # --- RSI (14) ---
    r = ta.rsi(c)
    r_v = float(r.iloc[-1])
    r_prev = float(r.iloc[-4])
    if r_v >= 70:
        s, t = -1.0, "Aşırı ALIM bölgesi: düzeltme riski yüksek."
    elif r_v <= 30:
        s, t = 1.0, "Aşırı SATIM bölgesi: tepki yükselişi ihtimali yüksek."
    elif r_v >= 50:
        s, t = 0.5, "50 üzerinde: momentum alıcılardan yana."
    else:
        s, t = -0.5, "50 altında: momentum satıcılardan yana."
    t += f" ({'yükseliyor' if r_v > r_prev else 'düşüyor'})"
    out["signals"]["RSI"] = (s, f"{r_v:.1f}", t)

    # --- Stochastic RSI (14,14,3,3) ---
    k, d = ta.stoch_rsi(c)
    k_v, d_v = float(k.iloc[-1]), float(d.iloc[-1])
    if k_v < 20:
        s, t = (1.0, "Dip bölgesinde K, D'yi yukarı kesti: AL sinyali.") if k_v > d_v + 2 else \
               (0.5, "Dip bölgesinde (aşırı satım), dönüş için K/D kesişimi bekleniyor.")
    elif k_v > 80:
        s, t = (-1.0, "Tepe bölgesinde K, D'yi aşağı kesti: SAT sinyali.") if k_v < d_v - 2 else \
               (-0.5, "Tepe bölgesinde (aşırı alım), dikkat.")
    else:
        s, t = (0.5, "K > D: kısa vadeli momentum yukarı.") if k_v > d_v else \
               (-0.5, "K < D: kısa vadeli momentum aşağı.")
    out["signals"]["StochRSI"] = (s, f"K={k_v:.0f} D={d_v:.0f}", t)

    # --- MACD (12,26,9) ---
    line, sig, hist = ta.macd(c)
    m_v, s_v, h_v, h_prev = map(float, (line.iloc[-1], sig.iloc[-1], hist.iloc[-1], hist.iloc[-2]))
    above = m_v > s_v
    s = (1.0 if above else -1.0) + (0.5 if h_v > h_prev else -0.5)
    t = "MACD sinyal çizgisinin " + ("ÜSTÜNDE (pozitif)." if above else "ALTINDA (negatif).")
    if h_v >= 0:
        t += " Histogram pozitif ve " + ("büyüyor: yükseliş ivmesi artıyor." if h_v > h_prev
                                          else "küçülüyor: yükseliş ivmesi zayıflıyor.")
    else:
        t += " Histogram negatif ve " + ("daralıyor: düşüş ivmesi azalıyor." if h_v > h_prev
                                          else "derinleşiyor: düşüş ivmesi artıyor.")
    cross = np.sign(hist.iloc[-4:]).diff().abs().gt(0).any()
    if cross:
        t += " ✳️ Yakın zamanda " + ("YUKARI (al) kesişimi." if above else "AŞAĞI (sat) kesişimi.")
    t += " Sıfır çizgisinin " + ("üstünde." if m_v > 0 else "altında.")
    out["signals"]["MACD"] = (s / 1.5, f"{fmt_num(m_v)} / sinyal {fmt_num(s_v)}", t)

    # --- ADX (14) ---
    a, pdi, mdi = ta.adx(h, l, c)
    a_v, p_v, mi_v = float(a.iloc[-1]), float(pdi.iloc[-1]), float(mdi.iloc[-1])
    direction = 1.0 if p_v > mi_v else -1.0
    strength = min(a_v / 40, 1.0)
    if a_v < 20:
        st_t = "Trend ZAYIF / yatay piyasa"
    elif a_v < 25:
        st_t = "Trend oluşuyor"
    elif a_v < 40:
        st_t = "GÜÇLÜ trend"
    else:
        st_t = "ÇOK GÜÇLÜ trend"
    t = f"{st_t}; " + ("+DI > -DI: alıcılar baskın." if direction > 0 else "-DI > +DI: satıcılar baskın.")
    t += " ADX " + ("artıyor." if a_v > float(a.iloc[-4]) else "azalıyor.")
    out["adx"] = a_v
    out["signals"]["ADX"] = (direction * strength, f"{a_v:.1f} (+DI {p_v:.0f} / -DI {mi_v:.0f})", t)

    # --- Toplam skor ---
    total = sum(WEIGHTS[n] * v[0] for n, v in out["signals"].items()) / sum(WEIGHTS.values())
    # Zayıf trendde (ADX<20) sinyaller daha az güvenilir -> skoru sönümle
    if a_v < 20:
        total *= 0.75
    out["score"] = float(np.clip(total, -1, 1))
    agree = sum(1 for v in out["signals"].values() if np.sign(v[0]) == np.sign(total) and v[0] != 0)
    out["agreement"] = agree

    # Volatilite ve seviyeler
    out["atr"] = float(ta.atr(h, l, c).iloc[-1])
    out["swing_high"] = float(h.iloc[-20:].max())
    out["swing_low"] = float(l.iloc[-20:].min())
    return out


def confidence(agreement: int, n: int = 6) -> str:
    if agreement >= 5:
        return "Yüksek"
    if agreement >= 4:
        return "Orta"
    return "Düşük"


def forecast(price: float, score: float, daily_vol: float, days: int) -> dict:
    """Skor + tarihsel volatiliteye göre beklenen fiyat aralığı (~%68 güven bandı)."""
    sigma = daily_vol * math.sqrt(days)
    drift = score * sigma * 0.6  # skor yönünde kayma
    center = price * math.exp(drift)
    low = price * math.exp(drift - sigma)
    high = price * math.exp(drift + sigma)
    return {"center": center, "low": low, "high": high, "pct": (center / price - 1) * 100}


def build_report(coin: str, name: str | None = None, market: str = "crypto") -> str:
    """market='crypto': coin ('BTC', 'eth') USDT paritesi Binance/Bybit'te aranır.
    market='bist': coin Borsa İstanbul kodu ('THYAO'), veri Yahoo Finance'ten."""
    coin = coin.upper()
    if market == "bist":
        ticker = coin if coin.endswith(".IS") else coin + ".IS"
        coin = ticker[:-3]
        CURRENCY.set("" if coin.startswith("XU") else "₺")  # endeksler puan cinsinden
        meta = {"name": name or coin}
        pair, chg_label, week_days, month_days = f"{coin} · BIST", "günlük", 5, 21  # işlem günü
        daily, src = get_stock_klines(ticker, "1d", "2y")
        src += " (~15 dk gecikmeli)"
        load_weekly = lambda: get_stock_klines(ticker, "1wk", "6y")  # noqa: E731
    else:
        CURRENCY.set("$")
        meta = COINS.get(coin) or {"symbol": coin + "USDT", "name": name or coin}
        pair, chg_label, week_days, month_days = f"{coin}/USDT", "24s", 7, 30
        daily, src = get_klines(meta["symbol"], "1d", 500)
        load_weekly = lambda: get_klines(meta["symbol"], "1w", 300)  # noqa: E731
    d = analyze_frame(daily)
    try:  # yeni coin/hisselerde yeterli haftalık mum olmayabilir
        weekly, _ = load_weekly()
        w = analyze_frame(weekly)
    except Exception:  # noqa: BLE001
        w = d
    price = d["price"]
    prev_close = float(daily["close"].iloc[-2])
    if market == "bist":  # Yahoo geçmişinde gün eksik olabiliyor; resmi önceki kapanışı kullan
        try:
            import yfinance as yf
            prev_close = float(yf.Ticker(ticker).fast_info["previousClose"]) or prev_close
        except Exception:  # noqa: BLE001
            pass
    chg24 = (price / prev_close - 1) * 100
    daily_vol = float(np.log(daily["close"]).diff().iloc[-60:].std())

    week_score = d["score"]
    month_score = float(np.clip(0.6 * w["score"] + 0.4 * d["score"], -1, 1))
    fw = forecast(price, week_score, daily_vol, week_days)
    fm = forecast(price, month_score, daily_vol, month_days)

    now = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    e = html.escape
    L = []
    L.append(f"<b>📊 {e(meta['name'])} ({pair}) Teknik Analiz</b>")
    L.append(f"🕒 {now} (TSİ) · Kaynak: {src}")
    L.append(f"💰 Fiyat: <b>{fmt_price(price)}</b> ({chg24:+.2f}% {chg_label})")
    L.append("")
    L.append("<b>— Günlük Grafik Göstergeleri —</b>")
    icons = lambda v: "🟢" if v > 0.15 else ("🔴" if v < -0.15 else "⚪")  # noqa: E731
    for name in ("Bollinger", "SAR", "RSI", "StochRSI", "MACD", "ADX"):
        s, val, txt = d["signals"][name]
        title = "Parabolic SAR" if name == "SAR" else ("Bollinger Bands" if name == "Bollinger" else name)
        L.append(f"{icons(s)} <b>{title}</b>: {e(val)}\n   ↳ {e(txt)}")
    L.append("")
    L.append("<b>— Haftalık Grafik (büyük resim) —</b>")
    L.append(" · ".join(f"{icons(v[0])}{n}" for n, v in w["signals"].items()))
    L.append(f"Haftalık skor: {w['score']:+.2f} → {score_label(w['score'])}")
    L.append("")
    L.append("<b>— Önemli Seviyeler —</b>")
    L.append(f"Direnç: {fmt_price(d['bb']['upper'])} (BB üst) · {fmt_price(d['swing_high'])} (20g zirve)")
    L.append(f"Destek: {fmt_price(d['bb']['lower'])} (BB alt) · {fmt_price(d['swing_low'])} (20g dip)")
    L.append(f"SAR (trend dönüş seviyesi): {fmt_price(d['sar'])}")
    L.append("")
    L.append("<b>🔮 TAHMİN</b>")
    L.append(f"<b>1 Hafta:</b> {score_label(week_score)} (skor {week_score:+.2f}, güven: {confidence(d['agreement'])})")
    L.append(f"   Hedef ≈ {fmt_price(fw['center'])} ({fw['pct']:+.1f}%) · Aralık {fmt_price(fw['low'])} – {fmt_price(fw['high'])}")
    L.append(f"<b>1 Ay:</b> {score_label(month_score)} (skor {month_score:+.2f}, güven: {confidence(w['agreement'])})")
    L.append(f"   Hedef ≈ {fmt_price(fm['center'])} ({fm['pct']:+.1f}%) · Aralık {fmt_price(fm['low'])} – {fmt_price(fm['high'])}")
    L.append("")
    L.append("<b>💬 Yorum</b>")
    L.append(e(commentary(d, w, week_score, month_score)))
    L.append("")
    L.append("<i>⚠️ Bu bir yatırım tavsiyesi değildir. Teknik göstergeler geçmiş fiyata dayanır; "
             "haber/makro gelişmeler sonucu tamamen değiştirebilir. Aralıklar ~%68 olasılık bandıdır.</i>")
    return "\n".join(L)


def commentary(d: dict, w: dict, ws: float, ms: float) -> str:
    sig = d["signals"]
    parts = []
    trend_word = "yükseliş" if sig["SAR"][0] > 0 else "düşüş"
    parts.append(f"Günlükte Parabolic SAR {trend_word} trendini gösteriyor; ADX {d['adx']:.0f} ile "
                 + ("trend güçlü." if d["adx"] >= 25 else "trend gücü zayıf, yatay seyir ihtimali var."))
    rsi_v = float(sig["RSI"][1])
    if rsi_v >= 70 or sig["StochRSI"][0] <= -1:
        parts.append("Osilatörler aşırı alım uyarısı veriyor, kısa vadede kâr satışı/düzeltme görülebilir.")
    elif rsi_v <= 30 or sig["StochRSI"][0] >= 1:
        parts.append("Osilatörler aşırı satımda, kısa vadeli tepki alımları gelebilir.")
    if sig["MACD"][0] > 0 and sig["SAR"][0] > 0:
        parts.append("MACD ve SAR aynı yönde (pozitif): yükseliş sinyali teyitli.")
    elif sig["MACD"][0] < 0 and sig["SAR"][0] < 0:
        parts.append("MACD ve SAR aynı yönde (negatif): düşüş sinyali teyitli.")
    else:
        parts.append("MACD ile SAR çelişiyor: trend dönüşü arifesinde olabiliriz, teyit beklemek mantıklı.")
    if np.sign(w["score"]) != np.sign(d["score"]) and abs(w["score"]) > 0.2:
        parts.append("Haftalık ve günlük grafik ters yönde: günlük hareket büyük trend içinde bir "
                     + ("düzeltme olabilir." if w["score"] > 0 else "tepki yükselişi olabilir."))
    if ws > 0.2:
        parts.append(f"1 haftalık görünüm olumlu; {fmt_price(d['bb']['upper'])} direnci kırılırsa yükseliş hızlanabilir.")
    elif ws < -0.2:
        parts.append(f"1 haftalık görünüm olumsuz; {fmt_price(d['bb']['lower'])} desteği izlenmeli.")
    else:
        parts.append("1 haftalık görünüm kararsız; bantlar arasında dalgalı seyir beklenebilir.")
    if ms > 0.2:
        parts.append("1 aylık perspektifte ana eğilim yukarı.")
    elif ms < -0.2:
        parts.append("1 aylık perspektifte ana eğilim aşağı.")
    else:
        parts.append("1 aylık perspektifte net bir yön yok.")
    return " ".join(parts)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    market = "bist" if args and args[0].lower() == "bist" else "crypto"
    targets = [a.upper() for a in args if a.lower() != "bist"] or list(COINS)
    for t in targets:
        print(build_report(t, market=market))
        print("\n" + "=" * 60 + "\n")
