"""Alarm öğrenme döngüsü: kaydet → sonucu ölç → puanla → eşikleri ayarla → tekrar ölç.

1. Kayıt: gönderilen her alarm ve eşiğin yarısına yaklaşan her "aday" olay alert_events tablosuna yazılır.
   Sadece gönderilenleri tutsaydık kaçırdıklarımızı ve eşiği değiştirmenin etkisini göremezdik.
2. Ölçüm: olaydan 4 ve 24 saat sonra fiyatın gittiği yer, arada gördüğü en yüksek/en düşük seviye.
   Hareketler o varlığın "tipik" 4 saatlik / günlük hareketine bölünerek ölçülür (BTC ile PEPE kıyaslanabilsin).
3. Puanlama: "işe yaradı"nın birkaç tanımı aynı anda ölçülür (DEFINITIONS). Kullanıcının 👍/👎 oylarıyla en çok
   örtüşen tanım hedef seçilir; yeterli oy yoksa "sert" tanımı kullanılır.
4. Ayar (haftalık): son 14 günün kayıtları üzerinde eşik kombinasyonları simüle edilir, F0.5 skoru
   (isabeti, yakalama oranından daha önemli sayar) en yüksek olan seçilir. Bir turda her eşik en fazla %25
   değişir; bu sınır önceki ayara dönmeyi de mümkün kılar (kötü çıkan değişiklik sonraki hafta geri alınır).
"""
import data  # noqa: F401  (SSL ayarları)

import itertools
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np

import store

log = logging.getLogger("learn")
TR = timezone(timedelta(hours=3))

TUNABLE = ("K15", "K1H", "VOLUME_X", "VOLUME_K", "TREND_K")
LIMITS = {"K15": (4.0, 25.0), "K1H": (4.0, 25.0), "VOLUME_X": (2.0, 10.0), "VOLUME_K": (1.0, 10.0),
          "TREND_K": (2.0, 10.0)}
STEPS = (0.8, 0.9, 1.0, 1.1, 1.25)     # bir turda en fazla %25 değişim (5 eşik → 3125 kombinasyon)
CAND_GAP = 900                         # aynı varlık için iki aday kaydı arasında en az 15 dk
BACKFILL_DAYS = 14                     # geçmişi yeniden oynatma süresi
TUNE_DAYS = 14                         # ayar için bakılan geçmiş
MIN_DATA_DAYS = 7                      # ilk ayar için en az bu kadar günlük kayıt
MIN_FIRED = 12                         # simülasyonda en az bu kadar alarm çıkmalı
MAX_PER_ITEM_DAY = 2.5                 # bundan sık alarm üreten ayar kabul edilmez (yakalamak için
                                       # eşikleri sınırsız gevşetmesin)
MIN_GAIN = 0.03                        # yeni ayar skoru en az bu kadar iyi olmalı
MIN_VOTES = 8                          # hedef tanımı oylardan seçmek için gereken oy
RETENTION_DAYS = 90
TUNED_KINDS = ("candidate", "drop", "pump", "volume", "trend_up", "trend_down")
DIRECTIONAL = {"drop": -1, "day_drop": -1, "trend_down": -1, "pump": 1, "day_pump": 1, "trend_up": 1}
METRIC_COLS = ("ch15", "ch1h", "ch2h", "z15", "z1h", "z2h", "vol_x", "vol2x", "adx")

# "İşe yaradı" tanımları: (açıklama, fonksiyon(olay, yön) -> True/False/None)
DEFINITIONS = {
    # Not: 4 saat içindeki en uç nokta, tipik net hareketin doğal olarak ~2 katıdır (testte sıradan anların
    # %87'si 2 katı geçti), bu yüzden eşik 3 kat.
    "sert": ("4 saat içinde sert hareket (yön fark etmez, tipik hareketin 3 katı)",
             lambda r, d: None if r.get("up4") is None else max(r["up4"], -r["dn4"]) >= 3 * r["typ4"]),
    "devam": ("4 saatte aynı yönde devam (tipik hareket kadar)",
              lambda r, d: None if r.get("ret4h") is None or not d else d * r["ret4h"] >= r["typ4"]),
    "trend": ("24 saatte aynı yönde güçlü trend (tipik günlük hareketin 1.5 katı)",
              lambda r, d: None if r.get("ret24h") is None or not d else d * r["ret24h"] >= 1.5 * r["typ24"]),
    "donus": ("4 saatte ters yöne dönüş (dip/tepe)",
              lambda r, d: None if r.get("ret4h") is None or not d else -d * r["ret4h"] >= r["typ4"]),
}
DEFAULT_DEF = "sert"

_last_cand: dict[str, float] = {}
_db_off_until = 0.0
_cfg_cache: tuple[float, dict | None] = (0.0, None)
_typ_cache: dict[str, tuple[float, tuple[float, float]]] = {}


# ---------------- Yardımcılar ----------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _f(x):
    return None if x is None else float(x)


def _db_ok() -> bool:
    return time.time() >= _db_off_until


def _db_fail(ex):
    """Tablo yoksa (SQL çalıştırılmadıysa) her 5 dakikada log kirletme: 1 saat sustur."""
    global _db_off_until
    _db_off_until = time.time() + 3600
    log.warning("Alarm kaydı yazılamadı (supabase_migration_3.sql çalıştırıldı mı?): %s", ex)


def direction(r: dict) -> int | None:
    k = r.get("kind")
    if k in DIRECTIONAL:
        return DIRECTIONAL[k]
    if k in ("candidate", "volume"):
        c15, c1h = _f(r.get("ch15")) or 0.0, _f(r.get("ch1h")) or 0.0
        v = c1h if abs(c1h) >= abs(c15) else c15
        return 1 if v > 0 else -1 if v < 0 else None
    return None


def _norm(r: dict) -> dict:
    out = dict(r)
    for k in ("price", "day_ch", "typ4", "typ24", "ret1h", "ret4h", "up4", "dn4", "ret24h", "up24", "dn24") + METRIC_COLS:
        out[k] = _f(r.get(k))
    out["adx_up"] = r.get("adx_up") in (True, "true", 1)
    return out


def label(r: dict, definition: str) -> bool | None:
    r = _norm(r)
    if not r.get("typ4"):
        return None
    return DEFINITIONS[definition][1](r, direction(r))


# ---------------- Eşik ayarı (veritabanında) ----------------
def get_config(force: bool = False) -> dict:
    """{'version': n, 'params': {...}} — kayıt yoksa alerts.py'deki varsayılanlar (sürüm 0)."""
    global _cfg_cache
    if not force and _cfg_cache[1] and time.time() - _cfg_cache[0] < 600:
        return _cfg_cache[1]
    import alerts
    cfg = {"version": 0, "params": dict(alerts.DEFAULTS)}
    try:
        rows = store.select("alert_config", {"key": "thresholds"})
        if rows:
            cfg = {"version": int(rows[0]["version"]),
                   "params": {**alerts.DEFAULTS, **json.loads(rows[0]["params"])}}
    except Exception as ex:  # noqa: BLE001
        log.warning("Alarm ayarı okunamadı, varsayılanlar kullanılıyor: %s", ex)
    _cfg_cache = (time.time(), cfg)
    return cfg


def apply_config():
    """Veritabanındaki eşikleri alerts modülüne uygular (alarm kontrolünden önce çağrılır)."""
    import alerts
    cfg = get_config()
    for k, v in cfg["params"].items():
        if k in TUNABLE:
            setattr(alerts, k, float(v))
    alerts.CFG_VERSION = cfg["version"]


def save_config(params: dict, version: int):
    global _cfg_cache
    store.insert("alert_config", {"key": "thresholds", "version": version,
                                  "params": json.dumps(params), "updated_at": _iso(_now())}, upsert=True)
    _cfg_cache = (0.0, None)


# ---------------- 1. Kayıt ----------------
def _row(at: datetime, item: str, kind: str, sent: bool, m: dict, day_ch: float, version: int,
         source: str) -> dict:
    row = {"at": _iso(at), "item": item, "kind": kind, "sent": sent, "cfg_version": version,
           "price": float(m["price"]), "day_ch": round(float(day_ch), 3), "hour": at.astimezone(TR).hour,
           "stage": 0, "source": source, "adx_up": bool(m.get("adx_up"))}
    for k in METRIC_COLS:
        v = m.get(k)
        row[k] = None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 3)
    return row


def log_event(item: str, kind: str, sent: bool, m: dict, day_ch: float) -> int | None:
    if not _db_ok():
        return None
    import alerts
    row = _row(_now(), item, kind, sent, m, day_ch, getattr(alerts, "CFG_VERSION", 0), "live")
    try:
        res = store.insert("alert_events", row)
        if kind in TUNED_KINDS:
            _last_cand[item] = time.time()
        return res.get("id")
    except Exception as ex:  # noqa: BLE001
        _db_fail(ex)
        return None


def maybe_log_candidate(item: str, m: dict, day_ch: float):
    """Eşiğe yaklaşan ama alarm olmayan olay: simülasyonun 'kaçırılanları' görebilmesi için."""
    import alerts
    if alerts.near(m, alerts.params()) and time.time() - _last_cand.get(item, 0) >= CAND_GAP:
        log_event(item, "candidate", False, m, day_ch)


# ---------------- 2. Sonuç ölçümü ----------------
def _is_bist(item: str) -> bool:
    return item.upper().startswith("BIST:")


def _code(item: str) -> str:
    return item.split(":", 1)[1].upper() if _is_bist(item) else item.upper()


def typical(item: str) -> tuple[float, float]:
    """Tipik mutlak hareket (%): 4 saatlik ve günlük medyan. 6 saat önbellekli."""
    hit = _typ_cache.get(item)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    from data import get_klines, get_stock_klines
    if _is_bist(item):
        h1, _ = get_stock_klines(_code(item) + ".IS", "60m", "3mo")
        d1, _ = get_stock_klines(_code(item) + ".IS", "1d", "6mo")
    else:
        h1, _ = get_klines(_code(item) + "USDT", "1h", 720)
        d1, _ = get_klines(_code(item) + "USDT", "1d", 90)
    c = np.log(h1["close"])
    typ4 = float((c - c.shift(4)).abs().median() * 100)
    typ24 = float(np.log(d1["close"]).diff().abs().median() * 100)
    val = (max(typ4, 0.05), max(typ24, 0.1))
    _typ_cache[item] = (time.time(), val)
    return val


def _path(item: str, start: datetime, hours: int):
    """Olay anından sonraki 5 dakikalık mumlar."""
    from data import get_klines_since, get_stock_klines
    if _is_bist(item):
        df, _ = get_stock_klines(_code(item) + ".IS", "5m", "1mo")
    else:
        df = get_klines_since(_code(item) + "USDT", "5m", int(start.timestamp() * 1000), hours * 12 + 2)
    if df.empty:
        return df
    return df[(df.index > start) & (df.index <= start + timedelta(hours=hours))]


def _window(path, start, price, hours):
    w = path[path.index <= start + timedelta(hours=hours)]
    if w.empty:
        return None
    return {"ret": round((float(w["close"].iloc[-1]) / price - 1) * 100, 3),
            "up": round((float(w["high"].max()) / price - 1) * 100, 3),
            "dn": round((float(w["low"].min()) / price - 1) * 100, 3)}


def resolve_events(limit: int = 120) -> int:
    """4 saati dolan olaylara 1s/4s sonuçlarını, 24 saati dolanlara 24s sonuçlarını yazar."""
    if not _db_ok():
        return 0
    now = _now()
    try:
        rows = store.select("alert_events", {"stage": ("lt", 2), "at": ("lte", _iso(now - timedelta(hours=4)))})
    except Exception as ex:  # noqa: BLE001
        _db_fail(ex)
        return 0
    done = 0
    for r in rows[:limit]:
        try:
            at, price, stage = _parse(r["at"]), float(r["price"]), int(r.get("stage") or 0)
            age = (now - at).total_seconds() / 3600
            if stage >= 1 and age < 24.5:
                continue
            path = _path(r["item"], at, 24)
            if path.empty:
                continue
            vals = {}
            if stage < 1:
                w1, w4 = _window(path, at, price, 1), _window(path, at, price, 4)
                if not w4:
                    continue
                typ4, typ24 = typical(r["item"])
                vals.update({"ret1h": w1["ret"] if w1 else None, "ret4h": w4["ret"], "up4": w4["up"],
                             "dn4": w4["dn"], "typ4": round(typ4, 3), "typ24": round(typ24, 3), "stage": 1})
            if age >= 24.5:
                w24 = _window(path, at, price, 24)
                if w24:
                    vals.update({"ret24h": w24["ret"], "up24": w24["up"], "dn24": w24["dn"], "stage": 2})
            if vals:
                store.update("alert_events", vals, {"id": r["id"]})
                done += 1
        except Exception as ex:  # noqa: BLE001
            log.warning("Alarm sonucu ölçülemedi (#%s %s): %s", r.get("id"), r.get("item"), ex)
    return done


# ---------------- Geçmişi yeniden oynatma (backfill) ----------------
def _history(item: str, days: int):
    """Yeniden oynatma için 5 dakikalık geçmiş (+10 gün ısınma: tipik hareket ve hacim profili için)."""
    from data import get_klines_since, get_stock_klines
    if _is_bist(item):
        return get_stock_klines(_code(item) + ".IS", "5m", "60d")[0]
    import pandas as pd
    start = int((_now() - timedelta(days=days + 11)).timestamp() * 1000)
    parts, end = [], int(_now().timestamp() * 1000)
    while start < end:
        df = get_klines_since(_code(item) + "USDT", "5m", start, 1000)
        if df.empty:
            break
        parts.append(df)
        start = int(df.index[-1].timestamp() * 1000) + 1
        if len(df) < 1000:
            break
    return pd.concat(parts).loc[lambda d: ~d.index.duplicated()]


def _replay_item(item: str, days: int, until: datetime) -> list[dict]:
    """Bir varlığın geçmişini canlı kontrolle aynı ölçüm/karar koduyla yeniden oynatır.
    Eşiğe yaklaşan anları (15 dk arayla) sonuçlarıyla birlikte olay satırı olarak döndürür."""
    import pandas as pd
    import alerts
    df = _history(item, days)
    bist = _is_bist(item)
    lc = np.log(df["close"])
    typ = {f"t{n}": ((lc - lc.shift(b)).abs().rolling("10D", min_periods=200).median().shift(1) * 100)
           for n, b in (("15", 3), ("1h", 12), ("2h", 24))}
    recent = df["volume"].shift(1).rolling(3).sum()
    key = pd.Series([alerts._slot(t) for t in df.index], index=df.index).shift(3)
    tod = recent.groupby(key).transform(lambda s: s.shift(1).rolling(30, min_periods=6).median())
    f = alerts.metrics_frame(df, typ, tod, session_only=bist)
    P = alerts.params()
    typ4, typ24 = typical(item)
    start = _now() - timedelta(days=days)
    rows, last = [], -1e18
    idx = df.index
    for t, r in f[(f.index >= start) & (f.index <= until)].iterrows():
        if any(pd.isna(r[k]) for k in ("t15", "t1h", "t2h")) or not alerts.near(r, P):
            continue
        ts = t.timestamp()
        if ts - last < CAND_GAP:
            continue
        last = ts
        price = float(r["price"])
        row = _row(t.to_pydatetime(), item, "candidate", False, r.to_dict(), 0.0, -1, "backfill")
        row.update(dict.fromkeys(("ret1h", "ret4h", "up4", "dn4", "ret24h", "up24", "dn24")))  # toplu ekleme: aynı anahtarlar
        i = idx.searchsorted(t, side="right")
        for h, cols in ((1, ("ret1h", None, None)), (4, ("ret4h", "up4", "dn4")), (24, ("ret24h", "up24", "dn24"))):
            j = idx.searchsorted(t + timedelta(hours=h), side="right")
            if t + timedelta(hours=h) > idx[-1] or j <= i:
                continue
            w = df.iloc[i:j]
            row[cols[0]] = round((float(w["close"].iloc[-1]) / price - 1) * 100, 3)
            if cols[1]:
                row[cols[1]] = round((float(w["high"].max()) / price - 1) * 100, 3)
                row[cols[2]] = round((float(w["low"].min()) / price - 1) * 100, 3)
        if row.get("ret4h") is None:
            continue
        row.update({"typ4": round(typ4, 3), "typ24": round(typ24, 3), "stage": 2 if row.get("ret24h") is not None else 1})
        rows.append(row)
    return rows


def backfill(items: list[str], days: int = BACKFILL_DAYS) -> dict[str, int]:
    """Son `days` günü yeniden oynatıp alert_events'e (source='backfill') yazar; öncekini siler.
    Canlı kayıtların başladığı andan sonrası oynatılmaz (çift sayılmasın)."""
    out = {}
    live = [r for r in _events(_now() - timedelta(days=days)) if r.get("source") != "backfill"]
    for item in items:
        try:
            first_live = min((_parse(r["at"]) for r in live if r["item"] == item), default=_now())
            rows = _replay_item(item, days, min(first_live, _now() - timedelta(hours=4)))
            store.delete("alert_events", {"item": item, "source": "backfill"})
            store.insert_many("alert_events", rows)
            out[item] = len(rows)
        except Exception as ex:  # noqa: BLE001
            log.warning("Geçmiş yeniden oynatılamadı (%s): %s", item, ex)
            out[item] = -1
    return out


def backfill_text(res: dict[str, int]) -> str:
    ok = {k: v for k, v in res.items() if v >= 0}
    bad = [k for k, v in res.items() if v < 0]
    s = (f"⏪ Son {BACKFILL_DAYS} gün yeniden oynatıldı: " +
         ", ".join(f"{k.replace('BIST:', '')} {v} olay" for k, v in ok.items()))
    if bad:
        s += f"\n⚠️ Oynatılamayan: {', '.join(bad)}"
    return s


def cleanup():
    try:
        store.delete("alert_events", {"at": ("lt", _iso(_now() - timedelta(days=RETENTION_DAYS)))})
    except Exception as ex:  # noqa: BLE001
        log.warning("Eski alarm kayıtları silinemedi: %s", ex)


# ---------------- 3. Oylar, sebepler ve hedef tanımı ----------------
# Oy değeri: 1 = 👍; -1 = 👎 (sebep yok); -2..-5 = 👎 + sebep. Veritabanında ek sütun gerekmesin diye
# sebep oy değerinin içinde tutulur (vote > 0 → işe yaradı).
REASONS = {-2: ("late", "⏰ Geç kaldı"), -3: ("rev", "↩️ Ters yöne gitti"),
           -4: ("small", "🤏 Hareket küçük kaldı"), -5: ("mute", "🙅 İlgilendirmiyor")}
VOTES = (1, -1) + tuple(REASONS)
GROUP_PARAMS = {"fast": ("K15", "K1H"), "volume": ("VOLUME_X", "VOLUME_K"), "trend": ("TREND_K",)}
GROUP_TR = {"fast": "ani hareket", "volume": "yüksek hacim", "trend": "trend başlangıcı",
            "teknik": "teknik sinyal", "funding": "vadeli işlem"}
MIN_REASON = 3            # bir sebebin eşiği değiştirmesi için en az bu kadar oy (son 14 gün)
MUTE_DAYS = 30


def group_of(kind: str) -> str:
    if kind in ("drop", "pump", "day_drop", "day_pump"):
        return "fast"
    if kind in ("trend_up", "trend_down"):
        return "trend"
    if kind in ("volume", "funding"):
        return kind
    return "teknik"


def record_vote(event_id: int, chat_id: int, vote: int):
    store.insert("alert_feedback", {"event_id": event_id, "chat_id": chat_id, "vote": vote,
                                    "at": _iso(_now())}, upsert=True)


def get_event(event_id: int) -> dict | None:
    rows = store.select("alert_events", {"id": event_id})
    return rows[0] if rows else None


def reason_summary(days: int = TUNE_DAYS) -> dict[str, dict[str, int]]:
    """{alarm grubu: {sebep: oy sayısı}} — son `days` gündeki 👎 sebepleri."""
    since = _now() - timedelta(days=days)
    votes = [v for v in _votes() if int(v["vote"]) in REASONS and _parse(v["at"]) >= since]
    if not votes:
        return {}
    by_id = {int(r["id"]): r for r in _events(since - timedelta(days=1))}
    out: dict[str, dict[str, int]] = {}
    for v in votes:
        ev = by_id.get(int(v["event_id"]))
        if ev:
            g = out.setdefault(group_of(ev["kind"]), {})
            code = REASONS[int(v["vote"])][0]
            g[code] = g.get(code, 0) + 1
    return out


def nudges(summary: dict) -> dict[str, str]:
    """Geri bildirimden eşik yönü: 'Geç kaldı' çoksa düşür ('late'), 'küçük kaldı' çoksa yükselt ('small')."""
    out = {}
    for g, c in summary.items():
        late, small = c.get("late", 0), c.get("small", 0)
        if g in GROUP_PARAMS and max(late, small) >= MIN_REASON and late != small:
            out[g] = "late" if late > small else "small"
    return out


_rev_cache: tuple[float, set] = (0.0, set())


def reversal_groups() -> set[str]:
    """'Ters yöne gitti' en sık sebep olan ve ölçümde de çoğunlukla ters dönen alarm grupları (1 saat önbellek)."""
    global _rev_cache
    if time.time() - _rev_cache[0] < 3600:
        return _rev_cache[1]
    out = set()
    try:
        for g, c in reason_summary(30).items():
            if c.get("rev", 0) >= MIN_REASON and c.get("rev", 0) == max(c.values()):
                sent = [r for r in _events(_now() - timedelta(days=30))
                        if r.get("sent") and group_of(r["kind"]) == g]
                labs = [x for x in (label(r, "donus") for r in sent) if x is not None]
                if labs and sum(labs) / len(labs) >= 0.5:
                    out.add(g)
    except Exception as ex:  # noqa: BLE001
        log.warning("Ters dönüş notu hesaplanamadı: %s", ex)
    _rev_cache = (time.time(), out)
    return out


# Sessize alma: notified tablosunda "mute:<sohbet>:<varlık>:<grup>" anahtarı (yeni tablo gerekmesin)
def mute(chat_id: int, item: str, group: str):
    store.insert("notified", {"key": f"mute:{chat_id}:{item}:{group}", "at": _iso(_now())}, upsert=True)


def mutes() -> set[tuple[int, str, str]]:
    """Geçerli sessize almalar: {(sohbet, varlık, grup)}."""
    out = set()
    try:
        limit = _now() - timedelta(days=MUTE_DAYS)
        for r in store.select("notified", {"at": ("gte", _iso(limit))}):
            if str(r["key"]).startswith("mute:"):  # varlık "BIST:THYAO" gibi ':' içerebilir
                chat, rest = r["key"][5:].split(":", 1)
                item, group = rest.rsplit(":", 1)
                out.add((int(chat), item, group))
    except Exception as ex:  # noqa: BLE001
        log.warning("Sessize almalar okunamadı: %s", ex)
    return out


def clear_mutes(chat_id: int) -> int:
    n = 0
    for c, item, group in mutes():
        if c == chat_id:
            store.delete("notified", {"key": f"mute:{c}:{item}:{group}"})
            n += 1
    return n


def mutes_text(chat_id: int) -> str:
    mine = sorted((i, g) for c, i, g in mutes() if c == chat_id)
    if not mine:
        return "🔔 Sessize alınmış alarm yok."
    return ("🙅 Sessize alınanlar (30 gün):\n" +
            "\n".join(f"• {i.replace('BIST:', '')} — {GROUP_TR.get(g, g)}" for i, g in mine) +
            "\n\nHepsini geri açmak: /alarm sessiz temizle")


def _events(since: datetime) -> list[dict]:
    return store.select("alert_events", {"at": ("gte", _iso(since))})


def _votes() -> list[dict]:
    try:
        return store.select("alert_feedback")
    except Exception:  # noqa: BLE001
        return []


def definition_agreement(events_by_id: dict, votes: list[dict]) -> dict[str, tuple[float, int]]:
    """Her tanım için oylarla örtüşme oranı: (oran, oy sayısı)."""
    out = {}
    for name in DEFINITIONS:
        ok = n = 0
        for v in votes:
            ev = events_by_id.get(int(v["event_id"]))
            lab = label(ev, name) if ev else None
            if lab is None:
                continue
            n += 1
            ok += int(lab == (int(v["vote"]) > 0))
        out[name] = (ok / n if n else 0.0, n)
    return out


def choose_definition(events_by_id: dict, votes: list[dict]) -> tuple[str, str]:
    agr = definition_agreement(events_by_id, votes)
    best = max(agr, key=lambda k: (agr[k][0], agr[k][1]))
    if agr[best][1] >= MIN_VOTES:
        return best, f"oylarınla %{agr[best][0] * 100:.0f} örtüşüyor ({agr[best][1]} oy)"
    total = len(votes)
    return DEFAULT_DEF, f"henüz yeterli oy yok ({total}/{MIN_VOTES}), varsayılan tanım"


# ---------------- 4. Simülasyon ve ayar ----------------
class Sim:
    """Simülasyon için olaylar numpy dizilerine çevrilir (3125 kombinasyon hızlı denensin).
    Karar mantığı alerts.classify ile aynıdır (vektör hali)."""

    def __init__(self, rows: list[dict]):
        import alerts
        P = alerts.params()
        self.rows = rows
        g = lambda k: np.array([abs(r[k] or 0.0) for r in rows])  # noqa: E731
        self.az15, self.az1h, self.az2h = g("z15"), g("z1h"), g("z2h")
        self.ac15, self.ac1h, self.vx = g("ch15"), g("ch1h"), np.array([r["vol_x"] or 0.0 for r in rows])
        self.tok = np.array([alerts.trend_ok(r, P) for r in rows])   # eşikten bağımsız trend koşulları
        self.lab = np.array([r["_lab"] for r in rows])
        self.ts = np.array([r["_ts"] for r in rows])
        self.item = [r["item"] for r in rows]
        self.floor15, self.floor1h = P["FLOOR_15M"], P["FLOOR_1H"]
        self.n_days = max(len({r["_day"] for r in rows}), 1)
        self.n_items = max(len(set(self.item)), 1)

    def run(self, p: dict) -> dict:
        import alerts
        fast = ((self.az15 >= p["K15"]) & (self.ac15 >= self.floor15)) | \
               ((self.az1h >= p["K1H"]) & (self.ac1h >= self.floor1h))
        vol = ~fast & (self.vx >= p["VOLUME_X"]) & (self.az15 >= p["VOLUME_K"])
        trd = ~fast & self.tok & (self.az2h >= p["TREND_K"])
        fired = hits = 0
        caught = np.zeros(len(self.rows), dtype=bool)
        for typ, mask in (("fast", fast), ("volume", vol), ("trend", trd)):
            cd = alerts.COOLDOWNS[typ]
            last: dict[str, float] = {}
            for i in np.flatnonzero(mask):
                if self.ts[i] - last.get(self.item[i], -1e18) < cd:
                    continue
                last[self.item[i]] = self.ts[i]
                fired += 1
                hits += int(self.lab[i])
                caught[i] = True
        positives = int(self.lab.sum())
        prec = hits / fired if fired else 0.0
        rec = int((caught & self.lab).sum()) / positives if positives else 0.0
        b2 = 0.25  # F0.5: isabet yakalamadan 2 kat önemli
        f = (1 + b2) * prec * rec / (b2 * prec + rec) if prec + rec else 0.0
        return {"fired": fired, "hits": hits, "precision": prec, "recall": rec, "f": f,
                "per_item_day": fired / self.n_days / self.n_items}


def simulate(rows: list[dict], params: dict, definition: str = DEFAULT_DEF) -> dict:
    return Sim(rows).run(params)


def _prepare(rows: list[dict], definition: str) -> list[dict]:
    out = []
    for r in rows:
        if r.get("kind") not in TUNED_KINDS or r.get("z15") is None:
            continue
        lab = label(r, definition)
        if lab is None:
            continue
        x = _norm(r)
        at = _parse(r["at"])
        x.update({"_lab": bool(lab), "_ts": at.timestamp(), "_day": at.astimezone(TR).date()})
        out.append(x)
    out.sort(key=lambda x: x["_ts"])
    return out


def tune(apply: bool = True, force: bool = False) -> dict:
    """Haftalık ayar turu. Döner: rapor sözlüğü (metin için weekly_text)."""
    cfg = get_config(force=True)
    cur = {k: float(cfg["params"][k]) for k in TUNABLE}
    since = _now() - timedelta(days=TUNE_DAYS)
    raw = _events(since)
    if not raw:
        return {"status": "nodata", "cur": cur, "version": cfg["version"]}
    first = min(_parse(r["at"]) for r in raw)
    data_days = (_now() - first).total_seconds() / 86400
    by_id = {int(r["id"]): r for r in store.select("alert_events", {"at": ("gte", _iso(_now() - timedelta(days=RETENTION_DAYS)))})}
    votes = _votes()
    definition, why = choose_definition(by_id, votes)
    rows = _prepare(raw, definition)
    sim = Sim(rows)
    base = sim.run(cur)
    rep = {"status": "ok", "cur": cur, "version": cfg["version"], "definition": definition, "why": why,
           "data_days": data_days, "n_rows": len(rows), "base": base, "new": cur, "best": base, "applied": False,
           "agreement": definition_agreement(by_id, votes)}
    if data_days < MIN_DATA_DAYS and not force:
        rep["status"] = "waiting"
        return rep
    # Senin 👎 sebeplerin: "geç kaldı" çoksa o grubun eşikleri yalnız düşebilir, "küçük kaldı" çoksa yalnız
    # yükselebilir. Bu durumda değişiklik, skor kazancı küçük olsa da uygulanır (geri bildirim önceliklidir).
    rep["reasons"] = reason_summary()
    rep["nudges"] = nudges(rep["reasons"])
    forced = {k: rep["nudges"][g] for g, ks in GROUP_PARAMS.items() if g in rep["nudges"] for k in ks}
    steps_of = {k: ((0.8, 0.9) if forced.get(k) == "late" else (1.1, 1.25) if forced.get(k) == "small" else STEPS)
                for k in TUNABLE}
    best, best_p = (None, cur) if forced else (base, cur)
    for steps in itertools.product(*(steps_of[k] for k in TUNABLE)):
        p = {k: round(min(max(cur[k] * s, LIMITS[k][0]), LIMITS[k][1]), 2) for k, s in zip(TUNABLE, steps)}
        res = sim.run(p)
        if res["fired"] < MIN_FIRED or res["per_item_day"] > MAX_PER_ITEM_DAY:
            continue
        if best is None or res["f"] > best["f"] + 1e-9 or (
                math.isclose(res["f"], best["f"]) and res["fired"] < best["fired"]):
            best, best_p = res, p
    best = best or base
    rep.update({"best": best, "new": best_p})
    changed = best_p != cur and (bool(forced) or best["f"] >= base["f"] + MIN_GAIN)
    if changed and apply:
        save_config(best_p, cfg["version"] + 1)
        rep["applied"] = True
        rep["version"] = cfg["version"] + 1
    elif not changed:
        rep["new"] = cur
    return rep


# ---------------- Raporlar ----------------
NAMES = {"K15": "Ani hareket 15 dk (normalin katı)", "K1H": "Ani hareket 1 saat (normalin katı)",
         "VOLUME_X": "Hacim (normalin katı)", "VOLUME_K": "Hacim alarmı için fiyat (normalin katı)",
         "TREND_K": "Trend başlangıcı 2 saat (normalin katı)"}
KIND_TR = {"drop": "ani düşüş", "pump": "ani yükseliş", "volume": "yüksek hacim", "day_drop": "BIST gün içi düşüş",
           "day_pump": "BIST gün içi yükseliş", "funding": "vadeli işlem", "candidate": "aday",
           "trend_up": "yükseliş trendi", "trend_down": "düşüş trendi"}


def _pct(a, b):
    return f"%{a / b * 100:.0f}" if b else "-"


def missed_moves(items: list[str], day_start: datetime, sent: list[dict]) -> list[str]:
    """Gün içinde 4 saatte tipik hareketin 2.5 katı oynayan anlar ve öncesinde alarm var mıydı."""
    out = []
    for item in items:
        try:
            typ4, _ = typical(item)
            path = _path(item, day_start, 24)
            if len(path) < 60:
                continue
            c = path["close"]
            times = [t for t in (_parse(s["at"]) for s in sent if s["item"] == item)]
            i = 0
            while i + 48 < len(c):
                mv = (float(c.iloc[i + 48]) / float(c.iloc[i]) - 1) * 100
                if abs(mv) >= 2.5 * typ4:
                    t0 = c.index[i]
                    caught = any(t0 - timedelta(hours=1) <= t <= t0 + timedelta(hours=2) for t in times)
                    out.append(f"{'✅' if caught else '❌'} {item.replace('BIST:', '')} "
                               f"saat {t0.astimezone(TR):%H:%M} → 4 saatte {mv:+.1f}%"
                               + ("" if caught else " — alarm yoktu"))
                    i += 48
                else:
                    i += 12
        except Exception as ex:  # noqa: BLE001
            log.warning("Kaçırılan hareket hesaplanamadı (%s): %s", item, ex)
    return out


def daily_text(items: list[str]) -> str | None:
    """Dünün alarm karnesi (HTML). Kayıt yoksa None."""
    today = datetime.now(TR).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=1)
    rows = [r for r in _events(start.astimezone(timezone.utc))
            if _parse(r["at"]) < today and r.get("source") != "backfill"]
    sent = [r for r in rows if r.get("sent")]
    cands = [r for r in rows if r.get("kind") == "candidate"]
    if not rows:
        return None
    L = [f"<b>📒 Alarm karnesi — {start:%d.%m.%Y}</b>"]
    by_kind = {}
    for r in sent:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    L.append(f"Gönderilen alarm: <b>{len(sent)}</b>" +
             (" (" + ", ".join(f"{KIND_TR.get(k, k)} {n}" for k, n in by_kind.items()) + ")" if by_kind else "") +
             f" · eşiğe yaklaşan aday: {len(cands)}")
    L.append("")
    L.append("<b>İşe yaradı mı?</b> (gönderilen / karşılaştırma için tüm adaylar)")
    for name, (desc, _) in DEFINITIONS.items():
        s = [label(r, name) for r in sent]
        s = [x for x in s if x is not None]
        c = [label(r, name) for r in cands]
        c = [x for x in c if x is not None]
        if s or c:
            L.append(f"• {desc}: {_pct(sum(s), len(s))} ({sum(s)}/{len(s)}) · adaylarda {_pct(sum(c), len(c))}")
    ids = {int(r["id"]) for r in sent}
    v = [x for x in _votes() if int(x["event_id"]) in ids]
    if v:
        up = sum(1 for x in v if int(x["vote"]) > 0)
        why = {}
        for x in v:
            if int(x["vote"]) in REASONS:
                k = REASONS[int(x["vote"])][1]
                why[k] = why.get(k, 0) + 1
        L.append(f"Senin oyların: 👍 {up} · 👎 {len(v) - up}" +
                 (" (" + ", ".join(f"{k} {n}" for k, n in why.items()) + ")" if why else ""))
    mm = missed_moves(items, start.astimezone(timezone.utc), sent)
    if mm:
        L.append("")
        L.append("<b>Dünün büyük hareketleri</b> (tipik 4 saatlik hareketin 2.5 katı)")
        L.extend(mm[:8])
    cfg = get_config()
    L.append("")
    L.append(f"<i>Ayar sürümü v{cfg['version']} · sonuçlar 4 saat sonra ölçülür; haftalık ayar pazartesi 09:20. "
             "Alarmların altındaki 👍/👎 ile 'işe yaradı'yı sen tanımlarsın. Ayrıntı: /alarmkarne</i>")
    return "\n".join(L)


def reasons_lines(summary: dict) -> list[str]:
    icons = {code: text.split()[0] for code, text in REASONS.values()}
    return [f"• {GROUP_TR.get(g, g)}: " + " · ".join(f"{icons[k]} {REASONS_TR[k]} {n}" for k, n in c.items())
            for g, c in summary.items()]


REASONS_TR = {code: text.split(" ", 1)[1] for code, text in REASONS.values()}


def weekly_text(rep: dict, ai_text: str | None = None) -> str:
    L = ["<b>🧪 Haftalık alarm ayarı</b>"]
    if rep["status"] == "nodata":
        L.append("Henüz kayıt yok. Alarmlar çalıştıkça veri birikecek.")
        return "\n".join(L)
    b, n = rep["base"], rep["best"]
    L.append(f"Hedef tanım: <b>{DEFINITIONS[rep['definition']][0]}</b> — {rep['why']}")
    L.append(f"Veri: {rep['data_days']:.1f} gün, {rep['n_rows']} ölçülmüş olay")
    if rep["status"] == "waiting":
        L.append(f"⏳ Ayar için en az {MIN_DATA_DAYS} günlük veri gerekiyor; şimdilik sadece ölçüyorum.")
    L.append(f"Mevcut ayar (v{rep['version'] - (1 if rep['applied'] else 0)}): {b['fired']} alarm, isabet "
             f"%{b['precision'] * 100:.0f}, büyük hareketleri yakalama %{b['recall'] * 100:.0f}")
    if rep["applied"]:
        L.append(f"✅ <b>Yeni ayar uygulandı (v{rep['version']})</b>: {n['fired']} alarm, isabet "
                 f"%{n['precision'] * 100:.0f}, yakalama %{n['recall'] * 100:.0f}")
        for k in TUNABLE:
            if rep["new"][k] != rep["cur"][k]:
                L.append(f"• {NAMES[k]}: {rep['cur'][k]:g} → <b>{rep['new'][k]:g}</b>")
    elif rep["status"] == "ok":
        L.append("➖ Daha iyi bir ayar bulunamadı (fark anlamlı değil); mevcut eşikler korunuyor.")
    if rep.get("reasons"):
        L.append("")
        L.append("<b>👎 Geri bildirimlerin (14 gün)</b>")
        L.extend(reasons_lines(rep["reasons"]))
        for g, how in rep.get("nudges", {}).items():
            L.append(f"→ {GROUP_TR[g]}: " + ("eşikler <b>düşürüldü</b> (daha erken uyarı)" if how == "late"
                                             else "eşikler <b>yükseltildi</b> (daha büyük hareketlerde uyarı)")
                     + ("" if rep.get("applied") else " — uygun ayar bulunamadı, sonraki hafta tekrar denenecek"))
    if ai_text:
        L.append("")
        L.append("<b>🤖 Yapay zekâ değerlendirmesi</b>")
        import html
        L.append(html.escape(ai_text))
    L.append("")
    L.append("<i>Simülasyon geçmiş verinin yaklaşık bir tekrarıdır; her eşik turda en fazla %25 değişir, "
             "kötü çıkan değişiklik sonraki hafta geri alınabilir.</i>")
    return "\n".join(L)


def ai_prompt(rep: dict) -> str | None:
    """Gemini'ye gönderilecek özet: başarısız alarmların türe/saate/varlığa göre dağılımı."""
    if rep["status"] == "nodata" or not rep.get("n_rows"):
        return None
    rows = _prepare(_events(_now() - timedelta(days=TUNE_DAYS)), rep["definition"])
    sent = [r for r in rows if r.get("sent")]

    def breakdown(key):
        g = {}
        for r in sent:
            k = key(r)
            a = g.setdefault(k, [0, 0])
            a[0] += 1
            a[1] += int(r["_lab"])
        return ", ".join(f"{k}: {h}/{n}" for k, (n, h) in sorted(g.items(), key=lambda x: -x[1][0])[:8])

    stats = [
        f"Hedef tanım: {DEFINITIONS[rep['definition']][0]}",
        f"Mevcut eşikler: {rep['cur']}",
        f"Mevcut ayar simülasyonu: {rep['base']}",
        f"En iyi bulunan: {rep['new']} -> {rep['best']} (uygulandı: {rep['applied']})",
        f"Gönderilen alarmlar türe göre (isabet/toplam): {breakdown(lambda r: KIND_TR.get(r['kind'], r['kind']))}",
        f"Varlığa göre: {breakdown(lambda r: r['item'])}",
        f"Saate göre (TSİ): {breakdown(lambda r: int(r.get('hour') or 0))}",
        f"Hacim katı 4-6 / 6+ : {breakdown(lambda r: '6+' if (r['vol_x'] or 0) >= 6 else '<6')}",
        f"Tanımların oylarla örtüşmesi: {rep['agreement']}",
        f"Kullanıcının 👎 sebepleri (grup: sebep sayısı; late=geç kaldı, rev=ters yöne gitti, "
        f"small=hareket küçük kaldı, mute=ilgilendirmiyor): {rep.get('reasons') or 'yok'}",
    ]
    return ("Bir kripto/BIST Telegram alarm botunun son 2 haftalık alarm performansı aşağıda. "
            "Türkçe, en fazla 6 cümleyle: hangi alarmlar işe yaramadı, olası nedeni ne (saat, varlık, tür), "
            "eşiklerde veya kuralda ne denenebilir? Sadece verilen sayılara dayan, sayı uydurma, düz metin yaz.\n\n"
            + "\n".join(stats))


def status_text() -> str:
    """/alarmkarne: mevcut ayar, son 7 günün özeti, son ayar turu."""
    cfg = get_config(force=True)
    L = [f"<b>📒 Alarm öğrenme durumu</b> (ayar v{cfg['version']})"]
    for k in TUNABLE:
        L.append(f"• {NAMES[k]}: <b>{float(cfg['params'][k]):g}</b>")
    try:
        allrows = _events(_now() - timedelta(days=TUNE_DAYS))
        rows = [r for r in allrows if r.get("source") != "backfill" and _parse(r["at"]) >= _now() - timedelta(days=7)]
        replay = sum(1 for r in allrows if r.get("source") == "backfill")
    except Exception as ex:  # noqa: BLE001
        return "\n".join(L) + ("\n\n⚠️ Alarm kayıt tabloları okunamadı. Supabase SQL Editor'de "
                               f"<code>supabase_migration_3.sql</code> çalıştırılmalı.\n({str(ex)[:150]})")
    sent = [r for r in rows if r.get("sent")]
    measured = [r for r in sent if r.get("stage") and int(r["stage"]) >= 1]
    L.append("")
    L.append(f"Son 7 gün: {len(rows)} kayıt, {len(sent)} gönderilen alarm, {len(measured)}'i ölçüldü")
    for name, (desc, _) in DEFINITIONS.items():
        s = [x for x in (label(r, name) for r in measured) if x is not None]
        if s:
            L.append(f"• {desc}: {_pct(sum(s), len(s))} ({sum(s)}/{len(s)})")
    votes = _votes()
    L.append(f"Toplam oy: {len(votes)} (hedef tanımı seçmek için {MIN_VOTES} gerekli)")
    rs = reason_summary()
    if rs:
        L.append(f"👎 sebepleri (14 gün; bir sebep {MIN_REASON} kez seçilince eşiği o yöne iter):")
        L.extend(reasons_lines(rs))
    L.append(f"Geçmişten yeniden oynatılmış olay: {replay}" + ("" if replay else " — /alarmkarne gecmis ile başlat"))
    try:
        runs = store.select("tune_runs", order="id.desc")[:1]
        if runs:
            r = runs[0]
            L.append(f"Son ayar turu: {_parse(r['at']).astimezone(TR):%d.%m %H:%M} · "
                     f"{'uygulandı ✅' if r.get('applied') else 'değişiklik yok'}")
    except Exception:  # noqa: BLE001
        pass
    L.append("")
    L.append("<i>/alarmkarne dun — dünün karnesi · /alarmkarne ayarla — ayar turunu şimdi çalıştır · "
             "/alarmkarne gecmis — son 14 günü yeniden oynatıp hemen ayarla</i>")
    return "\n".join(L)


def save_run(rep: dict, ai_text: str | None):
    try:
        store.insert("tune_runs", {"at": _iso(_now()), "definition": rep.get("definition"),
                                   "old_params": json.dumps(rep.get("cur")), "new_params": json.dumps(rep.get("new")),
                                   "applied": bool(rep.get("applied")),
                                   "metrics": json.dumps({"base": rep.get("base"), "best": rep.get("best")}),
                                   "ai_text": ai_text})
    except Exception as ex:  # noqa: BLE001
        log.warning("Ayar turu kaydedilemedi: %s", ex)
