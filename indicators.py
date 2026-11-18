"""Teknik analiz göstergeleri (harici TA kütüphanesi gerektirmez)."""
import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def stoch_rsi(close: pd.Series, rsi_period=14, stoch_period=14, k_smooth=3, d_smooth=3):
    r = rsi(close, rsi_period)
    lo = r.rolling(stoch_period).min()
    hi = r.rolling(stoch_period).max()
    st = ((r - lo) / (hi - lo)).replace([np.inf, -np.inf], np.nan).fillna(0.5) * 100
    k = st.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return k, d


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def bollinger(close: pd.Series, period=20, mult=2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    percent_b = (close - lower) / (upper - lower)
    bandwidth = (upper - lower) / mid
    return upper, mid, lower, percent_b, bandwidth


def true_range(high, low, close):
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(high, low, close, period=14):
    return true_range(high, low, close).ewm(alpha=1 / period, adjust=False).mean()


def adx(high, low, close, period=14):
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    atr_ = atr(high, low, close, period)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_ = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx_, plus_di, minus_di


def parabolic_sar(high: pd.Series, low: pd.Series, step=0.02, max_step=0.2):
    """SAR değerleri ve trend (+1 yükseliş, -1 düşüş) döndürür."""
    h, l = high.values, low.values
    n = len(h)
    sar = np.zeros(n)
    trend = np.zeros(n, dtype=int)
    up = n < 2 or h[1] >= h[0]
    af = step
    ep = h[0] if up else l[0]
    sar[0] = l[0] if up else h[0]
    trend[0] = 1 if up else -1
    for i in range(1, n):
        prev = sar[i - 1]
        s = prev + af * (ep - prev)
        if up:
            s = min(s, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < s:  # trend döndü
                up, s, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, max_step)
        else:
            s = max(s, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, max_step)
        sar[i] = s
        trend[i] = 1 if up else -1
    return pd.Series(sar, index=high.index), pd.Series(trend, index=high.index)


def pivots(series: pd.Series, k: int = 3, kind: str = "low") -> list[int]:
    """Yerel dip/tepe noktalarının konumları. Bir nokta, k mum öncesi ve sonrasının en düşüğü/yükseği ise pivottur
    (son k mum doğrulanamadığı için pivot olamaz -> gelecek bilgisi kullanılmaz)."""
    v = series.values
    out = []
    for i in range(k, len(v) - k):
        win = v[i - k:i + k + 1]
        if np.isnan(win).any():
            continue
        if (kind == "low" and v[i] == win.min()) or (kind == "high" and v[i] == win.max()):
            if not out or i - out[-1] > k:
                out.append(i)
    return out


def divergences(df: pd.DataFrame, lookback: int = 60, recent: int = 8, k: int = 3) -> list[dict]:
    """Klasik uyumsuzluklar (RSI ve MACD ile):
    - Pozitif (boğa): fiyat daha düşük dip, osilatör daha yüksek dip  -> düşüş zayıflıyor
    - Negatif (ayı): fiyat daha yüksek tepe, osilatör daha düşük tepe -> yükseliş zayıflıyor
    Son pivot, son `recent` mum içinde olmalı (güncel sinyal)."""
    c, h, l = df["close"], df["high"], df["low"]
    oscs = {"RSI": rsi(c), "MACD": macd(c)[0]}
    n = len(df)
    start = max(0, n - lookback)
    found = []
    for kind, price in (("bull", l), ("bear", h)):
        piv = [i for i in pivots(price.iloc[start:], k, "low" if kind == "bull" else "high")]
        piv = [i + start for i in piv]
        if len(piv) < 2:
            continue
        a, b = piv[-2], piv[-1]
        if n - 1 - b > recent or b - a < 5:
            continue
        for name, osc in oscs.items():
            pa, pb_, oa, ob = price.iloc[a], price.iloc[b], osc.iloc[a], osc.iloc[b]
            if np.isnan(oa) or np.isnan(ob):
                continue
            if kind == "bull" and pb_ < pa and ob > oa:
                found.append({"type": "bull", "osc": name, "bars_ago": n - 1 - b, "span": b - a})
            if kind == "bear" and pb_ > pa and ob < oa:
                found.append({"type": "bear", "osc": name, "bars_ago": n - 1 - b, "span": b - a})
    return found


def divergence_text(found: list[dict]) -> list[str]:
    out = []
    for kind in ("bull", "bear"):
        oscs = [f["osc"] for f in found if f["type"] == kind]
        if not oscs:
            continue
        ago = min(f["bars_ago"] for f in found if f["type"] == kind)
        both = " ve ".join(sorted(set(oscs)))
        if kind == "bull":
            out.append(f"🟢 Pozitif uyumsuzluk ({both}): fiyat daha düşük dip yaptı ama {both} daha yüksek dip yaptı "
                       f"({ago} mum önce) -> düşüş ivmesi zayıflıyor, dönüş sinyali olabilir.")
        else:
            out.append(f"🔴 Negatif uyumsuzluk ({both}): fiyat daha yüksek tepe yaptı ama {both} daha düşük tepe yaptı "
                       f"({ago} mum önce) -> yükseliş ivmesi zayıflıyor, düzeltme sinyali olabilir.")
    return out
