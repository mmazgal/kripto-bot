"""Ücretsiz İngilizce -> Türkçe çeviri (API anahtarı gerekmez).

1. Google Translate ücretsiz uç noktası
2. Yedek: MyMemory (günde ~5000 karakter ücretsiz)
Hepsi başarısız olursa None döner; çağıran taraf orijinal metni kullanır.
"""
import data  # noqa: F401  (SSL ayarı; requests'ten önce yüklenmeli)

import logging

import requests

log = logging.getLogger("translate")
HEADERS = {"User-Agent": "Mozilla/5.0 (crypto-ta-bot)"}
_cache: dict[str, str] = {}


def _chunks(text: str, size: int = 450):
    """Metni satır/cümle sınırlarından bölerek parçalar halinde verir."""
    buf = ""
    for line in text.split("\n"):
        parts = [line] if len(line) <= size else [s + "." for s in line.split(". ") if s]
        for p in parts:
            if len(buf) + len(p) + 1 > size and buf:
                yield buf
                buf = ""
            buf = f"{buf}\n{p}" if buf else p
    if buf:
        yield buf


def _google(text: str) -> str:
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": "tr", "dt": "t", "q": text},
        headers=HEADERS, timeout=10,
    )
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])


def _mymemory(text: str) -> str:
    r = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text, "langpair": "en|tr"}, headers=HEADERS, timeout=10,
    )
    r.raise_for_status()
    out = r.json().get("responseData", {}).get("translatedText")
    if not out or "MYMEMORY WARNING" in out:
        raise RuntimeError("MyMemory limit/boş yanıt")
    return out


def to_turkish(text: str) -> str | None:
    if not text:
        return text
    if text in _cache:
        return _cache[text]
    for name, fn in (("Google", _google), ("MyMemory", _mymemory)):
        try:
            result = "\n".join(fn(c) for c in _chunks(text))
            _cache[text] = result
            return result
        except Exception as e:  # noqa: BLE001
            log.warning("%s çeviri hatası: %s", name, e)
    return None
