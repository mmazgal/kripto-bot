"""Gemini (Google AI Studio ücretsiz katmanı) ile analiz raporuna doğal dilde yorum.

GEMINI_API_KEY yoksa ya da istek başarısız olursa None döner; rapor kural tabanlı yorumla gider.
Yapay zekâya yalnızca botun hesapladığı göstergeler, seviyeler ve haber başlıkları verilir
(portföy/kişisel veri gönderilmez); yeni sayı uydurmaması istenir.
"""
import data  # noqa: F401  (SSL ayarları; requests'ten önce)

import logging
import os
import re
import time

import requests

import indicators as ta

log = logging.getLogger("ai")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Önce GEMINI_MODEL (ayarlıysa), sonra Google'ın "en güncel Flash" takma adı, sonra sabit sürüm
MODELS = [m for m in (os.getenv("GEMINI_MODEL"), "gemini-flash-latest", "gemini-2.5-flash") if m]
CACHE_TTL = 1800          # aynı varlık+mod için 30 dk aynı yorumu kullan (kotayı korur)
MAX_CHARS = 900           # Telegram 4096 sınırı için yorum uzunluğu üst sınırı
_cache: dict[str, tuple[float, str]] = {}
_bad_models: set[str] = set()

PROMPT = """Sen deneyimli bir teknik analistsin. Aşağıdaki veriler bir Telegram botunun hesapladığı \
teknik analiz sonuçlarıdır. Bunları bir yatırımcıya Türkçe, sade ve akıcı biçimde yorumla.

Kurallar:
- 4-6 cümle, en fazla ~700 karakter. Düz metin yaz: Markdown, madde işareti, başlık, emoji KULLANMA.
- YALNIZCA verilen sayıları kullan; yeni fiyat, hedef veya yüzde UYDURMA.
- Göstergeler arasındaki uyumu/çelişkiyi, kısa ve uzun vadeli grafiğin ilişkisini, izlenecek destek/direnci anlat.
- Haber başlıkları varsa ve fiyatı etkileyebilecek gibiyse kısaca değin; alakasızsa hiç bahsetme.
- "Al", "sat" gibi kesin tavsiye verme. Kesinlik iddia etme; bu göstergelerin geçmişte 1 haftalık \
yön isabetinin ~%50 olduğunu bil, ihtiyatlı ol.

VERİLER:
{data}"""


def enabled() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _num(x: float) -> str:
    return f"{x:.6g}"


def _context_text(ctx: dict, extra_lines: list[str], news_titles: list[str]) -> str:
    d, w, cur = ctx["d"], ctx["w"], ctx["currency"]
    lb = ctx.get("labels") or {"main": "Günlük", "higher": "Haftalık", "h1": "1 Hafta", "h2": "1 Ay"}
    L = [f"Varlık: {ctx['name']} ({ctx['pair']}), para birimi: {cur or 'puan'}",
         f"Fiyat: {_num(ctx['price'])} ({ctx['chg']:+.2f}% {ctx['chg_label']})"]
    L += [_strip_html(x) for x in extra_lines if x]
    L.append(f"{lb['main']} grafik göstergeleri (puan -1 düşüş … +1 yükseliş):")
    for n, (s, val, txt) in d["signals"].items():
        L.append(f"- {n}: puan {s:+.2f}, değer {val}. {txt}")
    L.append(f"{lb['main']} toplam skor {d['score']:+.2f}, ADX {d['adx']:.0f}, gösterge uyumu {d['agreement']}/6")
    L.append(f"{lb['higher']} grafik skoru {w['score']:+.2f}; " +
             ", ".join(f"{n} {v[0]:+.2f}" for n, v in w["signals"].items()))
    L.append(f"Direnç: {_num(d['bb']['upper'])} (Bollinger üst), {_num(d['swing_high'])} (son 20 mum zirve)")
    L.append(f"Destek: {_num(d['bb']['lower'])} (Bollinger alt), {_num(d['swing_low'])} (son 20 mum dip)")
    L.append(f"Parabolic SAR seviyesi: {_num(d['sar'])}")
    for x in ta.divergence_text(d.get("divergences", [])):
        L.append(f"Uyumsuzluk: {x}")
    for x in ctx.get("extra_sections", []):
        if x:
            L.append(_strip_html(x))
    fw, fm = ctx["fw"], ctx["fm"]
    L.append(f"{lb['h1']} tahmini: skor {ctx['week_score']:+.2f}, merkez {_num(fw['center'])} ({fw['pct']:+.1f}%), "
             f"~%68 aralık {_num(fw['low'])} – {_num(fw['high'])}")
    L.append(f"{lb['h2']} tahmini: skor {ctx['month_score']:+.2f}, merkez {_num(fm['center'])} ({fm['pct']:+.1f}%), "
             f"~%68 aralık {_num(fm['low'])} – {_num(fm['high'])}")
    if news_titles:
        L.append("Son haber başlıkları:")
        L += [f"- {t}" for t in news_titles]
    return "\n".join(L)


def _call(model: str, prompt: str) -> str:
    r = requests.post(URL.format(model=model),
                      headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                      json={"contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048}},
                      timeout=25)
    if r.status_code == 404:
        _bad_models.add(model)  # model adı kalkmış/değişmiş: bir sonrakini dene
    r.raise_for_status()
    cand = (r.json().get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def _clean(text: str) -> str:
    text = re.sub(r"[*_#`]+", "", text)          # olası Markdown işaretleri
    text = re.sub(r"\s*\n\s*", " ", text).strip()
    if len(text) > MAX_CHARS:                      # cümle sonunda kes
        cut = text[:MAX_CHARS]
        text = cut[: cut.rfind(".") + 1] or cut + "…"
    return text


def comment(ctx: dict, extra_lines: list[str] | None = None, news_titles: list[str] | None = None) -> str | None:
    """Analiz bağlamı için yapay zekâ yorumu; kapalıysa veya hata olursa None."""
    if not enabled():
        return None
    key = f"{ctx['item']}|{ctx.get('mode', 'normal')}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    prompt = PROMPT.format(data=_context_text(ctx, extra_lines or [], news_titles or []))
    for model in MODELS:
        if model in _bad_models:
            continue
        try:
            text = _clean(_call(model, prompt))
            if text:
                _cache[key] = (time.time(), text)
                return text
        except Exception as ex:  # noqa: BLE001
            msg = str(ex).replace(os.environ.get("GEMINI_API_KEY", "") or "\0", "***")
            log.warning("Gemini yorumu alınamadı (%s): %s", model, msg)
            if "429" in msg:  # kota doldu: diğer modeli de zorlamadan çık
                return None
    return None


if __name__ == "__main__":
    import sys
    from analysis import compute_item
    sys.stdout.reconfigure(encoding="utf-8")
    c = compute_item((sys.argv[1] if len(sys.argv) > 1 else "BTC").upper())
    print(_context_text(c, [], []))
    print("\n--- Yorum ---\n", comment(c) or "(GEMINI_API_KEY yok ya da hata)")
