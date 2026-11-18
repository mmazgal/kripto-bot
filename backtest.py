"""Geriye dönük test: botun puanlama sistemi geçmişte ne kadar işe yarardı?

- Yön isabeti: skor ≥0.2 "yükseliş", ≤-0.2 "düşüş" dediğinde fiyat o yöne gitti mi?
  Ham isabet yanıltıcı olabilir (piyasa zaten çoğu gün yükseldiyse "hep yükseliş" demek de tutar),
  bu yüzden her zaman "her gün rastgele/hep al" kıyasıyla birlikte raporlanır.
- Strateji: 1H skoru ≥0.2 iken al, ≤-0.2 olunca sat (aradaki bölgede pozisyon korunur).
  İşlem başına %0.1 komisyon. Al-ve-tut ile karşılaştırılır.
- Gelecek bilgisi sızmaz: gün t'deki haftalık skor yalnızca t'den önce kapanmış haftalardan hesaplanır;
  karar t kapanışında verilir, getirisi t→t+1'den itibaren işler.

Terminalden: python backtest.py                -> sepet raporu + ağırlık optimizasyonu
             python backtest.py btc bist:thyao -> tek varlık raporu
"""
import data  # noqa: F401  (SSL ayarları)

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from analysis import DEFAULT_WEIGHTS, WEIGHTS, score_series
from data import get_klines, get_stock_klines

FEE = 0.001
THRESH = 0.2
INDICATORS = list(DEFAULT_WEIGHTS)

CRYPTO_BASKET = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "LTC"]
BIST_BASKET = ["BIST:XU100", "BIST:THYAO", "BIST:GARAN", "BIST:AKBNK", "BIST:ASELS", "BIST:BIMAS",
               "BIST:KCHOL", "BIST:SISE", "BIST:TUPRS", "BIST:EREGL", "BIST:FROTO"]


def load_daily(item: str) -> tuple[pd.DataFrame, bool]:
    if item.upper().startswith("BIST:"):
        df, _ = get_stock_klines(item.split(":", 1)[1].upper() + ".IS", "1d", "max")
        return df.iloc[-1500:], True
    df, _ = get_klines(item.upper() + "USDT", "1d", 1000)
    return df, False


def weekly_aligned(df: pd.DataFrame, weights) -> pd.Series:
    """Haftalık skor, sadece tamamlanmış haftalarla, günlük indekse hizalanmış."""
    wk = df.resample("W-SUN").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                   "volume": "sum"}).dropna()
    ws = score_series(wk, weights)["total"]
    # Hafta etiketi o haftanın son günü; o hafta ancak ertesi gün "bilinir"
    ws.index = ws.index + pd.Timedelta(days=1)
    idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
    s = ws.copy()
    s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
    out = s.reindex(s.index.union(idx)).ffill().reindex(idx)
    out.index = df.index
    return out


def prepare(item: str, weights=None) -> pd.DataFrame:
    df, is_bist = load_daily(item)
    sc = score_series(df, weights)
    sc["week_score"] = sc["total"]
    sc["month_score"] = (0.6 * weekly_aligned(df, weights) + 0.4 * sc["total"]).clip(-1, 1)
    h1, h2 = (5, 21) if is_bist else (7, 30)
    c = df["close"]
    sc["close"] = c
    sc["fwd_w"] = c.shift(-h1) / c - 1
    sc["fwd_m"] = c.shift(-h2) / c - 1
    sc["ret1"] = c.pct_change().shift(-1)  # t kapanışında alınan kararın getirisi
    sc.attrs.update(item=item, is_bist=is_bist, h=(h1, h2))
    return sc


def direction_stats(score: pd.Series, fwd: pd.Series) -> dict:
    m = score.notna() & fwd.notna()
    s, f = score[m], fwd[m]
    up, dn = s >= THRESH, s <= -THRESH
    base_up = float((f > 0).mean()) if len(f) else np.nan
    res = {"days": int(len(f)), "base_up": base_up, "base_ret": float(f.mean()) if len(f) else np.nan}
    res["n_up"], res["n_dn"] = int(up.sum()), int(dn.sum())
    res["hit_up"] = float((f[up] > 0).mean()) if up.any() else np.nan
    res["hit_dn"] = float((f[dn] < 0).mean()) if dn.any() else np.nan
    res["ret_up"] = float(f[up].mean()) if up.any() else np.nan
    res["ret_dn"] = float(f[dn].mean()) if dn.any() else np.nan
    sig = up | dn
    hits = ((f > 0) & up) | ((f < 0) & dn)
    res["hit"] = float(hits[sig].mean()) if sig.any() else np.nan
    # Kıyas: aynı günlerde "hep yükseliş" deseydik isabet
    res["naive"] = float(((f > 0)[sig]).mean()) if sig.any() else np.nan
    return res


def strategy(sc: pd.DataFrame) -> dict:
    s = sc["week_score"]
    pos = pd.Series(np.nan, index=sc.index)
    pos[s >= THRESH] = 1.0
    pos[s <= -THRESH] = 0.0
    pos = pos.ffill().fillna(0.0)
    r = sc["ret1"].fillna(0.0)
    valid = s.notna()
    pos, r = pos[valid], r[valid]
    trades = pos.diff().abs().fillna(pos.iloc[0] if len(pos) else 0)
    strat = (1 + pos * r - trades * FEE).cumprod()
    hold = (1 + r).cumprod()
    dd = lambda eq: float((eq / eq.cummax() - 1).min()) if len(eq) else np.nan  # noqa: E731
    return {"strat": float(strat.iloc[-1] - 1) if len(strat) else np.nan,
            "hold": float(hold.iloc[-1] - 1) if len(hold) else np.nan,
            "dd_strat": dd(strat), "dd_hold": dd(hold), "trades": int(trades.sum()),
            "exposure": float(pos.mean()) if len(pos) else np.nan,
            "start": str(sc.index[valid][0].date()) if valid.any() else "", "end": str(sc.index[-1].date())}


def indicator_ic(frames: list[pd.DataFrame], col="fwd_w") -> dict:
    """Her göstergenin skoru ile gelecekteki getiri arasındaki sıra korelasyonu (Spearman)."""
    out = {}
    for ind in INDICATORS:
        xs, ys = [], []
        for sc in frames:
            m = sc[ind].notna() & sc[col].notna()
            xs.append(sc.loc[m, ind])
            ys.append(sc.loc[m, col])
        x, y = pd.concat(xs), pd.concat(ys)
        out[ind] = float(x.rank().corr(y.rank())) if len(x) > 30 else 0.0
    return out


# ---------------- Rapor (Telegram) ----------------
def _p(x, d=0):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"%{x * 100:.{d}f}"


def _sp(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.1f}%"


def asset_report(item: str) -> str:
    sc = prepare(item)
    h1, h2 = sc.attrs["h"]
    w, m, st = direction_stats(sc["week_score"], sc["fwd_w"]), direction_stats(sc["month_score"], sc["fwd_m"]), strategy(sc)
    label = f"{item.split(':', 1)[1]} (BIST)" if sc.attrs["is_bist"] else item.upper()
    unit = "işlem günü" if sc.attrs["is_bist"] else "gün"
    L = [f"<b>🧪 Geriye Dönük Test — {label}</b>", f"Dönem: {st['start']} → {st['end']} ({w['days']} gün)", ""]
    for name, s, h in (("1 Hafta", w, h1), ("1 Ay", m, h2)):
        L.append(f"<b>{name} tahmini</b> ({h} {unit} sonrası)")
        L.append(f"• Yön isabeti: <b>{_p(s['hit'])}</b> · aynı günlerde 'hep yükseliş' deseydik: {_p(s['naive'])}")
        L.append(f"• 'Yükseliş' dediğinde ({s['n_up']} gün): fiyat %{s['hit_up'] * 100:.0f} yükseldi, ort. {_sp(s['ret_up'])}"
                 if s["n_up"] else "• 'Yükseliş' sinyali hiç olmadı")
        L.append(f"• 'Düşüş' dediğinde ({s['n_dn']} gün): fiyat %{s['hit_dn'] * 100:.0f} düştü, ort. {_sp(s['ret_dn'])}"
                 if s["n_dn"] else "• 'Düşüş' sinyali hiç olmadı")
        L.append(f"• Kıyas: rastgele bir günde ort. getiri {_sp(s['base_ret'])}, yükselme oranı {_p(s['base_up'])}")
        L.append("")
    L.append("<b>Sinyallerle al-sat yapılsaydı</b> (komisyon dahil)")
    L.append(f"• Bot stratejisi: <b>{_sp(st['strat'])}</b> · en büyük düşüş {_sp(st['dd_strat'])} · {st['trades']} işlem")
    L.append(f"• Al ve tut: <b>{_sp(st['hold'])}</b> · en büyük düşüş {_sp(st['dd_hold'])}")
    L.append(f"• Zamanın %{st['exposure'] * 100:.0f}'ında pozisyondaydı")
    L.append("")
    verdict = []
    if not np.isnan(w["hit"]) and not np.isnan(w["naive"]):
        diff = w["hit"] - w["naive"]
        verdict.append("1 haftalık yön tahmini kıyasa göre " +
                       ("<b>daha iyi</b>" if diff > 0.03 else "<b>daha kötü</b>" if diff < -0.03 else "<b>yaklaşık aynı</b>") +
                       f" ({diff * 100:+.0f} puan).")
    if not np.isnan(st["strat"]):
        verdict.append("Strateji al-ve-tut'tan " + ("daha fazla kazandırırdı" if st["strat"] > st["hold"] else "daha az kazandırırdı") +
                       (", ama düşüşleri daha sınırlıydı." if st["dd_strat"] > st["dd_hold"] else "."))
    L.append("💬 " + " ".join(verdict))
    L.append("\n<i>Geçmiş performans geleceği garanti etmez. Ağırlıklar: "
             + ("backtest ile optimize edilmiş" if WEIGHTS != DEFAULT_WEIGHTS else "varsayılan") + ".</i>")
    return "\n".join(L)


# ---------------- Sepet testi + ağırlık optimizasyonu (terminal) ----------------
def basket_eval(items, weights, split=None):
    """split=None: tüm dönem; ('train'|'test', oran): zaman bazlı bölme."""
    rows, frames = [], []
    for it in items:
        try:
            sc = prepare(it, weights)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {it}: {e}")
            continue
        if split:
            part, frac = split
            valid = sc[sc["total"].notna()]
            cut = valid.index[int(len(valid) * frac)]
            sc = sc[sc.index < cut] if part == "train" else sc[sc.index >= cut]
        frames.append(sc)
        rows.append((it, direction_stats(sc["week_score"], sc["fwd_w"]),
                     direction_stats(sc["month_score"], sc["fwd_m"]), strategy(sc)))
    return rows, frames


def summarize(rows):
    def avg(key, part):
        v = [r[part][key] for r in rows if not np.isnan(r[part][key])]
        return float(np.mean(v)) if v else np.nan
    return {"hit_w": avg("hit", 1), "naive_w": avg("naive", 1), "hit_m": avg("hit", 2), "naive_m": avg("naive", 2),
            "strat": float(np.mean([r[3]["strat"] for r in rows])), "hold": float(np.mean([r[3]["hold"] for r in rows])),
            "dd_strat": float(np.mean([r[3]["dd_strat"] for r in rows])), "dd_hold": float(np.mean([r[3]["dd_hold"] for r in rows]))}


def optimize(items, frac=0.7):
    print(f"Eğitim dönemi: her varlığın ilk %{frac * 100:.0f}'i · Test: son %{(1 - frac) * 100:.0f}")
    _, train_frames = basket_eval(items, DEFAULT_WEIGHTS, ("train", frac))
    ic = indicator_ic(train_frames)
    print("Gösterge bilgi katsayıları (eğitim, 1H getirisiyle Spearman):",
          {k: round(v, 3) for k, v in ic.items()})
    pos = {k: max(v, 0.0) for k, v in ic.items()}
    if sum(pos.values()) <= 0:
        print("Hiçbir gösterge pozitif ilişki göstermedi -> varsayılan ağırlıklar korunur.")
        return None, ic
    mean_pos = np.mean([v for v in pos.values() if v > 0])
    new = {k: round(min(2.0, v / mean_pos), 2) for k, v in pos.items()}
    return new, ic


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if args:
        import re
        for a in args:
            print(re.sub(r"<[^>]+>", "", asset_report(a.upper())), "\n")
        sys.exit()
    for name, basket in (("KRİPTO", CRYPTO_BASKET), ("BIST", BIST_BASKET)):
        print(f"\n===== {name} sepeti — mevcut ağırlıklar, tüm dönem =====")
        rows, _ = basket_eval(basket, DEFAULT_WEIGHTS)
        for it, w, m, st in rows:
            print(f"{it:<12} 1H isabet {_p(w['hit'])} (kıyas {_p(w['naive'])}) | 1A {_p(m['hit'])} (kıyas {_p(m['naive'])})"
                  f" | strateji {_sp(st['strat'])} vs tut {_sp(st['hold'])} | DD {_sp(st['dd_strat'])} vs {_sp(st['dd_hold'])}")
        print("ORTALAMA:", {k: round(v, 3) for k, v in summarize(rows).items()})

    all_items = CRYPTO_BASKET + BIST_BASKET
    print("\n===== Ağırlık optimizasyonu (zaman bazlı eğitim/test) =====")
    new, ic = optimize(all_items)
    if new:
        print("Önerilen ağırlıklar:", new)
        for label, wts in (("varsayılan", DEFAULT_WEIGHTS), ("optimize", new)):
            rows, _ = basket_eval(all_items, wts, ("test", 0.7))
            print(f"TEST dönemi [{label}]:", {k: round(v, 3) for k, v in summarize(rows).items()})
        if "--save" in sys.argv:
            Path(__file__).with_name("weights.json").write_text(json.dumps(
                {"weights": new, "ic_train": ic, "created": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print("weights.json kaydedildi.")
