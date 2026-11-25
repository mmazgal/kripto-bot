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

TUNABLE = ("DROP_15M", "DROP_1H", "VOLUME_X", "VOLUME_MIN_MOVE")
LIMITS = {"DROP_15M": (1.0, 6.0), "DROP_1H": (1.5, 10.0), "VOLUME_X": (2.0, 10.0), "VOLUME_MIN_MOVE": (0.2, 2.0)}
STEPS = (0.8, 0.9, 1.0, 1.1, 1.25)     # bir turda en fazla %25 değişim
CAND_FRACTION = 0.5                    # eşiğin bu oranına ulaşan olay aday olarak kaydedilir
CAND_GAP = 900                         # aynı varlık için iki aday kaydı arasında en az 15 dk
SIM_COOLDOWN = 2 * 3600                # simülasyonda aynı varlık+tür için alarm tekrar süresi
TUNE_DAYS = 14                         # ayar için bakılan geçmiş
MIN_DATA_DAYS = 7                      # ilk ayar için en az bu kadar günlük kayıt
MIN_FIRED = 12                         # simülasyonda en az bu kadar alarm çıkmalı
MAX_PER_ITEM_DAY = 2.5                 # bundan sık alarm üreten ayar kabul edilmez (yakalamak için
                                       # eşikleri sınırsız gevşetmesin)
MIN_GAIN = 0.03                        # yeni ayar skoru en az bu kadar iyi olmalı
MIN_VOTES = 8                          # hedef tanımı oylardan seçmek için gereken oy
RETENTION_DAYS = 90
TUNED_KINDS = ("candidate", "drop", "pump", "volume")
DIRECTIONAL = {"drop": -1, "day_drop": -1, "pump": 1, "day_pump": 1}

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
    for k in ("price", "ch15", "ch1h", "day_ch", "vol_x", "typ4", "typ24", "ret1h", "ret4h",
              "up4", "dn4", "ret24h", "up24", "dn24"):
        out[k] = _f(r.get(k))
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
def log_event(item: str, kind: str, sent: bool, m: dict, day_ch: float) -> int | None:
    if not _db_ok():
        return None
    import alerts
    row = {"at": _iso(_now()), "item": item, "kind": kind, "sent": sent,
           "cfg_version": getattr(alerts, "CFG_VERSION", 0), "price": m["price"],
           "ch15": round(m["ch15"], 3), "ch1h": round(m["ch1h"], 3), "day_ch": round(day_ch, 3),
           "vol_x": round(float(m["vol_x"]), 2), "hour": datetime.now(TR).hour, "stage": 0}
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
    f = CAND_FRACTION
    near = (abs(m["ch15"]) >= alerts.DROP_15M * f or abs(m["ch1h"]) >= alerts.DROP_1H * f
            or (m["vol_x"] >= alerts.VOLUME_X * f and abs(m["ch15"]) >= alerts.VOLUME_MIN_MOVE * f))
    if near and time.time() - _last_cand.get(item, 0) >= CAND_GAP:
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


def cleanup():
    try:
        store.delete("alert_events", {"at": ("lt", _iso(_now() - timedelta(days=RETENTION_DAYS)))})
    except Exception as ex:  # noqa: BLE001
        log.warning("Eski alarm kayıtları silinemedi: %s", ex)


# ---------------- 3. Oylar ve hedef tanımı ----------------
def record_vote(event_id: int, chat_id: int, vote: int):
    store.insert("alert_feedback", {"event_id": event_id, "chat_id": chat_id, "vote": vote,
                                    "at": _iso(_now())}, upsert=True)


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
def simulate(rows: list[dict], params: dict, definition: str) -> dict:
    """Verilen eşiklerle bu olaylardan hangileri alarm olurdu; isabet, yakalama ve F0.5 skoru."""
    last: dict[tuple[str, str], float] = {}
    fired = hits = 0
    items_days = set()
    positives = sum(1 for r in rows if r["_lab"])
    for r in rows:
        c15, c1h, vx = abs(r["ch15"] or 0), abs(r["ch1h"] or 0), r["vol_x"] or 0
        fast = c15 >= params["DROP_15M"] or c1h >= params["DROP_1H"]
        vol = vx >= params["VOLUME_X"] and c15 >= params["VOLUME_MIN_MOVE"] and not fast
        typ = "fast" if fast else "volume" if vol else None
        if not typ:
            continue
        key = (r["item"], typ)
        if r["_ts"] - last.get(key, -1e18) < SIM_COOLDOWN:
            continue
        last[key] = r["_ts"]
        fired += 1
        hits += int(r["_lab"])
        items_days.add((r["item"], r["_day"]))
    prec = hits / fired if fired else 0.0
    rec = hits / positives if positives else 0.0
    b2 = 0.25  # F0.5: isabet yakalamadan 2 kat önemli
    f = (1 + b2) * prec * rec / (b2 * prec + rec) if prec + rec else 0.0
    per_day = fired / max(len({r["_day"] for r in rows}), 1) / max(len({r["item"] for r in rows}), 1)
    return {"fired": fired, "hits": hits, "precision": prec, "recall": rec, "f": f, "per_item_day": per_day}


def _prepare(rows: list[dict], definition: str) -> list[dict]:
    out = []
    for r in rows:
        if r.get("kind") not in TUNED_KINDS:
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
    base = simulate(rows, cur, definition)
    rep = {"status": "ok", "cur": cur, "version": cfg["version"], "definition": definition, "why": why,
           "data_days": data_days, "n_rows": len(rows), "base": base, "new": cur, "best": base, "applied": False,
           "agreement": definition_agreement(by_id, votes)}
    if data_days < MIN_DATA_DAYS and not force:
        rep["status"] = "waiting"
        return rep
    best, best_p = base, cur
    for steps in itertools.product(STEPS, repeat=len(TUNABLE)):
        p = {k: round(min(max(cur[k] * s, LIMITS[k][0]), LIMITS[k][1]), 2) for k, s in zip(TUNABLE, steps)}
        res = simulate(rows, p, definition)
        if res["fired"] < MIN_FIRED or res["per_item_day"] > MAX_PER_ITEM_DAY:
            continue
        if res["f"] > best["f"] + 1e-9 or (math.isclose(res["f"], best["f"]) and res["fired"] < best["fired"]):
            best, best_p = res, p
    rep.update({"best": best, "new": best_p})
    changed = best_p != cur and best["f"] >= base["f"] + MIN_GAIN
    if changed and apply:
        save_config(best_p, cfg["version"] + 1)
        rep["applied"] = True
        rep["version"] = cfg["version"] + 1
    elif not changed:
        rep["new"] = cur
    return rep


# ---------------- Raporlar ----------------
NAMES = {"DROP_15M": "Ani hareket (15 dk) %", "DROP_1H": "Ani hareket (1 saat) %",
         "VOLUME_X": "Hacim katı", "VOLUME_MIN_MOVE": "Hacim alarmı için fiyat %"}
KIND_TR = {"drop": "ani düşüş", "pump": "ani yükseliş", "volume": "yüksek hacim", "day_drop": "BIST gün içi düşüş",
           "day_pump": "BIST gün içi yükseliş", "funding": "vadeli işlem", "candidate": "aday"}


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
    rows = [r for r in _events(start.astimezone(timezone.utc)) if _parse(r["at"]) < today]
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
        L.append(f"Senin oyların: 👍 {up} · 👎 {len(v) - up}")
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
        rows = _events(_now() - timedelta(days=7))
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
    try:
        runs = store.select("tune_runs", order="id.desc")[:1]
        if runs:
            r = runs[0]
            L.append(f"Son ayar turu: {_parse(r['at']).astimezone(TR):%d.%m %H:%M} · "
                     f"{'uygulandı ✅' if r.get('applied') else 'değişiklik yok'}")
    except Exception:  # noqa: BLE001
        pass
    L.append("")
    L.append("<i>/alarmkarne dun — dünün karnesi · /alarmkarne ayarla — ayar turunu şimdi çalıştır</i>")
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
