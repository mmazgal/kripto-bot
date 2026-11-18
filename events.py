"""Ekonomik takvim (ForexFactory ücretsiz haftalık akışı) ve KAP/şirket haberi alarmları (Google Haberler)."""
import data  # noqa: F401  (SSL ayarları)

import hashlib
import html
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

import market
import store
from translate import to_turkish

log = logging.getLogger("events")
TR = timezone(timedelta(hours=3))
HEADERS = {"User-Agent": "Mozilla/5.0 (crypto-ta-bot)"}
e = html.escape

COUNTRIES = {"USD": "🇺🇸", "EUR": "🇪🇺", "CNY": "🇨🇳"}
# TCMB faiz kararı (PPK) gibi Türkiye olayları ücretsiz akışta yok; tarihleri buraya ekleyin:
# ("2026-12-11T14:00:00+03:00", "TCMB faiz kararı (PPK)")
TR_EVENTS: list[tuple[str, str]] = []

_cache: dict[str, tuple[float, object]] = {}


# ---------------- Tekrar bildirim engeli (kalıcı) ----------------
def already_notified(key: str) -> bool:
    try:
        return bool(store.select("notified", {"key": key}))
    except Exception as ex:  # noqa: BLE001
        log.warning("notified okunamadı: %s", ex)
        return False


def mark_notified(key: str):
    try:
        store.insert("notified", {"key": key, "at": datetime.now(timezone.utc).isoformat()}, upsert=True)
    except Exception as ex:  # noqa: BLE001
        log.warning("notified yazılamadı: %s", ex)


# ---------------- Ekonomik takvim ----------------
def _fetch_week() -> list[dict]:
    hit = _cache.get("ff")
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=HEADERS, timeout=15)
    r.raise_for_status()
    _cache["ff"] = (time.time(), r.json())
    return _cache["ff"][1]


def events(days_ahead: int = 7, include_past_hours: int = 0) -> list[dict]:
    """Yüksek etkili olaylar (TSİ saatiyle), zamana göre sıralı."""
    now = datetime.now(TR)
    out = []
    try:
        raw = _fetch_week()
    except Exception as ex:  # noqa: BLE001
        log.warning("Takvim alınamadı: %s", ex)
        raw = []
    for x in raw:
        if x.get("impact") != "High" or x.get("country") not in COUNTRIES:
            continue
        try:
            dt = datetime.fromisoformat(x["date"]).astimezone(TR)
        except Exception:  # noqa: BLE001
            continue
        out.append({"dt": dt, "flag": COUNTRIES[x["country"]], "title": x["title"],
                    "forecast": x.get("forecast") or "", "previous": x.get("previous") or ""})
    for iso, title in TR_EVENTS:
        out.append({"dt": datetime.fromisoformat(iso).astimezone(TR), "flag": "🇹🇷", "title": title,
                    "forecast": "", "previous": "", "tr": True})
    lo, hi = now - timedelta(hours=include_past_hours), now + timedelta(days=days_ahead)
    return sorted([x for x in out if lo <= x["dt"] <= hi], key=lambda x: x["dt"])


# Sık görülen olaylar için sabit çeviriler (çeviri servisine istek atmaz, anlamı korur)
TITLE_TR = [
    ("FOMC Meeting Minutes", "FOMC toplantı tutanakları"), ("FOMC Statement", "FOMC karar metni"),
    ("Federal Funds Rate", "Fed faiz kararı"), ("FOMC Press Conference", "Fed basın toplantısı"),
    ("FOMC Economic Projections", "Fed ekonomik projeksiyonları"),
    ("Non-Farm Employment Change", "Tarım dışı istihdam (NFP)"), ("Unemployment Rate", "İşsizlik oranı"),
    ("Core CPI", "Çekirdek enflasyon (TÜFE)"), ("CPI", "Enflasyon (TÜFE)"),
    ("Core PCE Price Index", "Çekirdek PCE enflasyonu"), ("Core PPI", "Çekirdek ÜFE"), ("PPI", "Üretici enflasyonu (ÜFE)"),
    ("Core Retail Sales", "Çekirdek perakende satışlar"), ("Retail Sales", "Perakende satışlar"),
    ("Advance GDP", "GSYH öncü"), ("Prelim GDP", "GSYH ön"), ("Final GDP", "GSYH nihai"), ("GDP", "GSYH (büyüme)"),
    ("ISM Manufacturing PMI", "ISM imalat PMI"), ("ISM Services PMI", "ISM hizmet PMI"),
    ("Unemployment Claims", "İşsizlik başvuruları"), ("JOLTS Job Openings", "JOLTS açık iş ilanları"),
    ("Main Refinancing Rate", "ECB faiz kararı"), ("Monetary Policy Statement", "Para politikası metni"),
    ("ECB Press Conference", "ECB basın toplantısı"),
]


def _title_tr(ev: dict) -> str:
    t = ev["title"]
    if ev.get("tr"):
        return t
    if "Speaks" in t:
        who = t.replace("Speaks", "").strip()
        return f"{who} konuşması"
    for en, tr in TITLE_TR:
        if t.startswith(en) or t == en:
            return tr + t[len(en):].replace("m/m", "(aylık)").replace("y/y", "(yıllık)").replace("q/q", "(çeyreklik)")
    return to_turkish(t) or t


GUNLER = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]


def _line(ev: dict, with_day=True) -> str:
    when = (f"{GUNLER[ev['dt'].weekday()]} {ev['dt'].strftime('%d.%m %H:%M')}" if with_day
            else ev["dt"].strftime("%H:%M"))
    extra = []
    if ev["forecast"]:
        extra.append(f"beklenti {ev['forecast']}")
    if ev["previous"]:
        extra.append(f"önceki {ev['previous']}")
    tail = f" <i>({e(', '.join(extra))})</i>" if extra else ""
    return f"{ev['flag']} {when} — {e(_title_tr(ev))}{tail}"


def calendar_report() -> str:
    evs = events(7)
    L = ["<b>📅 Önemli Ekonomik Olaylar</b> (yüksek etkili, TSİ)", ""]
    if not evs:
        L.append("Önümüzdeki günlerde yüksek etkili olay görünmüyor.")
    L.extend(_line(x) for x in evs[:15])
    L.append("\n<i>Olaydan 30 dk önce hatırlatma gelir. Fed/ABD verileri kripto ve borsada sert hareket yaratabilir.</i>")
    if not TR_EVENTS:
        L.append("<i>TCMB faiz kararı tarihleri ücretsiz kaynakta yok; eklenmesini istersen söyle.</i>")
    return "\n".join(L)


def today_block() -> str:
    """Sabah özeti için: bugün ve yarının olayları."""
    now = datetime.now(TR)
    evs = [x for x in events(2) if x["dt"].date() in (now.date(), (now + timedelta(days=1)).date())]
    if not evs:
        return ""
    L = ["<b>📅 Bugün/yarın önemli olaylar</b>"]
    L.extend(_line(x) for x in evs[:6])
    return "\n".join(L)


def upcoming_reminders(minutes: int = 30) -> list[str]:
    """Başlamasına <= `minutes` kalan ve daha önce hatırlatılmamış olaylar."""
    now = datetime.now(TR)
    msgs = []
    for ev in events(1):
        if now <= ev["dt"] <= now + timedelta(minutes=minutes):
            key = "ev:" + hashlib.sha1(f"{ev['dt'].isoformat()}|{ev['title']}".encode()).hexdigest()[:16]
            if already_notified(key):
                continue
            mark_notified(key)
            mins = int((ev["dt"] - now).total_seconds() // 60)
            msgs.append(f"⏰ <b>{mins} dk sonra önemli veri</b>\n{_line(ev, with_day=False)}\n"
                        "Açıklama anında fiyatlarda sert dalgalanma olabilir.")
    return msgs


# ---------------- KAP / şirket haberleri ----------------
def company_news_alerts(codes: list[str], max_age_hours: int = 3) -> dict[str, list[str]]:
    """Her BIST kodu için son saatlerde çıkan KAP/şirket haberleri (Google Haberler üzerinden).
    Resmi KAP akışı değildir; KAP bildirimlerini aktaran haber sitelerini yakalar."""
    out = {}
    now = datetime.now(timezone.utc)
    for code in codes:
        items = market.news(f"\"{code}\" KAP", 8)
        msgs = []
        for it in items:
            title = it["title"] or ""
            if code.upper() not in title.upper():
                continue
            if not it["date"] or now - it["date"] > timedelta(hours=max_age_hours):
                continue
            key = "kap:" + hashlib.sha1((it["link"] or title).encode()).hexdigest()[:16]
            if already_notified(key):
                continue
            mark_notified(key)
            msgs.append(f"📢 <b>{e(code)} haberi</b> <i>({e(it['source'])})</i>\n"
                        f"<a href=\"{e(it['link'] or '')}\">{e(title)}</a>\n👉 /hisse {code.lower()}")
        if msgs:
            out[code] = msgs[:3]
    return out
