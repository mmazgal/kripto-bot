"""Telegram botu — kripto + Borsa İstanbul teknik analiz, alarmlar, portföy.

Komutlar için /start yazın. Düz mesajlar da çalışır: "eth", "thyao hisse", "bist".
"""
import data  # noqa: F401  (SSL ayarını telegram/httpx'ten önce yapar)
import asyncio
import html
import logging
import os
from datetime import time as dtime
from pathlib import Path

import re
from datetime import datetime

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          TypeHandler, filters)

import ai
import alerts
import backtest
import events
import features as F
import futures
import learn
import market
import paper
import scanner
from analysis import COINS, compute_item, fmt_num, render_report
from charts import make_chart
from coininfo import build_info_message, find_coin, get_info
from stockinfo import build_stock_info_message, find_stock, get_stock_info

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("bot")

MAX_ITEMS = 3
STOPWORDS = {"analiz", "analizi", "et", "yap", "coin", "bana", "bir", "lütfen", "lutfen",
             "ve", "ile", "bilgi", "hakkında", "hakkinda", "nedir", "kripto", "ver", "/analiz",
             "hisse", "hissesi", "bist", "borsa", "/hisse", "/bist", "kisa", "kısa", "vade", "/kisa"}
STOCK_WORDS = {"hisse", "hissesi", "bist", "borsa"}
SHORT_WORDS = {"kisa", "kısa", "/kisa"}
CHECK_EVERY = 300          # sn: alarm kontrol sıklığı
SUMMARY_TIME = dtime(9, 0, tzinfo=F.TR)
SCAN_TIME = dtime(9, 5, tzinfo=F.TR)
ALERTCARD_TIME = dtime(9, 10, tzinfo=F.TR)   # dünün alarm karnesi
PAPER_TIME = dtime(9, 15, tzinfo=F.TR)
TUNE_TIME = dtime(9, 20, tzinfo=F.TR)        # pazartesi: haftalık eşik ayarı
DAILY_DEADLINE = (12, 0)   # sunucu uyuyup/yeniden başlayıp saati kaçırırsa bu saate kadar telafi edilir
NOT_EDITED = ~filters.UpdateType.EDITED  # mesaj düzenlenince komut tekrar çalışmasın
ITEM_RE = re.compile(r"^(BIST:)?[A-Z0-9]{1,15}$")

HELP = (
    "👋 <b>Kripto & Borsa Analiz Botu</b>\n\n"
    "<b>📊 Analiz</b>\n"
    "/analiz eth — coin: bilgi + grafik + analiz + vadeli işlem verisi + haberler\n"
    "/hisse thyao — BIST hissesi · /hisse — BIST 100\n"
    "/kisa btc — kısa vade (1 saatlik grafik, 24 saat / 3 gün tahmini)\n"
    "/bilgi doge — sadece coin bilgisi\n"
    "/karsilastir btc eth sol — yan yana karşılaştırma\n"
    "/piyasa — korku/açgözlülük, BTC dominansı, dolar/TL\n\n"
    "<b>🔎 Keşif</b>\n"
    "/firsat — günün fırsatları (ilk 100 coin + BIST 100 taraması)\n"
    "/takvim — önemli ekonomik olaylar (Fed, enflasyon...)\n"
    "/backtest btc — bot bu varlıkta geçmişte ne kadar isabetliydi\n\n"
    "<b>📋 Takip & Alarm</b>\n"
    "/takip — listen · /takip ekle eth · /takip ekle thyao hisse · /takip sil sol\n"
    "/alarm — durum · /alarm ac · /alarm kapat · /alarm durum\n"
    "/hedef btc 60000 — fiyat hedefi · /hedef — liste · /hedef sil 3\n"
    "/alarmkarne — alarmlar işe yarıyor mu (bot her hafta eşikleri kendisi ayarlar; alarmlara 👍/👎 ver)\n\n"
    "<b>💼 Portföy & Raporlar</b>\n"
    "/portfoy — kâr/zarar · /portfoy ekle btc 0.05 62000 · /portfoy ekle thyao hisse 100 300\n"
    "/sanal — botun kendi sinyalleriyle yönettiği sanal portföy\n"
    "/ozet — sabah özeti · /ozet ac · /ozet kapat (her sabah 09:00)\n"
    "/karne — botun tahminleri ne kadar tuttu\n\n"
    "Komutsuz da yazabilirsin: <i>eth</i>, <i>pepe analiz</i>, <i>btc kısa</i>, <i>thyao hisse</i>, <i>bist</i>\n"
    "Analizlerin altındaki butonlarla yenileyebilir, takibe ekleyebilir, hedef koyabilirsin."
)

MENU = [
    ("analiz", "Coin analizi (ör. /analiz eth)"), ("hisse", "BIST hissesi (ör. /hisse thyao)"),
    ("kisa", "Kısa vade analizi (ör. /kisa btc)"), ("firsat", "Günün fırsatları taraması"),
    ("piyasa", "Piyasa havası"), ("takvim", "Ekonomik takvim"), ("karsilastir", "Karşılaştır (ör. btc eth sol)"),
    ("takip", "Takip listesi"), ("hedef", "Fiyat hedefleri"), ("alarm", "Alarm ayarları"),
    ("alarmkarne", "Alarmlar işe yarıyor mu"),
    ("portfoy", "Portföyüm"), ("sanal", "Botun sanal portföyü"), ("backtest", "Geçmiş isabet testi"),
    ("karne", "Tahmin karnesi"), ("ozet", "Sabah özeti"), ("bilgi", "Coin bilgi kartı"), ("start", "Yardım"),
]


def load_env():
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


load_env()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x}


def allowed(update: Update) -> bool:
    return not ALLOWED or update.effective_chat.id in ALLOWED


def chunks(text: str, limit: int = 4000) -> list[str]:
    """Telegram 4096 karakter sınırı: uzun metni satır sınırlarından böler (HTML etiketleri satır içinde kapanır)."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # tek satır bile çok uzunsa
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


async def reply(update: Update, text: str, html_mode: bool = True, markup=None):
    parts = chunks(text)
    for i, part in enumerate(parts):
        await update.effective_message.reply_text(part, parse_mode="HTML" if html_mode else None,
                                                  disable_web_page_preview=True,
                                                  reply_markup=markup if i == len(parts) - 1 else None)


async def run(fn, *args):
    return await asyncio.to_thread(fn, *args)


class Waiting:
    """'⏳ ...' mesajını gösterir, iş bitince siler."""
    def __init__(self, update, text="⏳ Canlı veriler alınıyor..."):
        self.update, self.text, self.msg = update, text, None

    async def __aenter__(self):
        self.msg = await self.update.effective_message.reply_text(self.text)

    async def __aexit__(self, *exc):
        try:
            await self.msg.delete()
        except Exception:  # noqa: BLE001
            pass


def split_args(words: list[str]) -> tuple[bool, list[str], list[str]]:
    """Kelimeleri ayırır: (hisse mi, isimler, sayılar)."""
    stock = any(w.lower() in STOCK_WORDS for w in words)
    names, nums = [], []
    for w in words:
        if w.lower() in STOPWORDS:
            continue
        try:
            F.parse_number(w)
            nums.append(w)
        except ValueError:
            names.append(w)
    return stock, names, nums


# ---------------- Analiz gönderimi ----------------
def _analyze(item: str, name: str | None, news_query: str | None, mode: str = "normal"):
    ctx = compute_item(item, name, mode)
    F.record_prediction(ctx)
    if ctx["market"] == "crypto":
        try:
            c = ctx["daily"]["close"]
            chg7 = (float(c.iloc[-1]) / float(c.iloc[-8]) - 1) * 100 if mode == "normal" and len(c) > 8 else None
            ctx["extra_sections"] = futures.section(ctx["coin"], chg7)
        except Exception as ex:  # noqa: BLE001
            log.warning("Vadeli işlem verisi alınamadı: %s", ex)
    extra = [x for x in [market.sentiment_line(ctx["market"])] if x]
    titles = [n["title"] for n in market.news(news_query)] if news_query else []
    ctx["ai_comment"] = ai.comment(ctx, extra, titles)
    text = render_report(ctx, extra)
    if news_query:
        nb = market.news_block(news_query)
        if nb and len(text) + len(nb) < 4000:  # Telegram mesaj sınırı 4096
            text += "\n\n" + nb
    try:
        png = make_chart(ctx)
    except Exception as ex:  # noqa: BLE001
        log.warning("Grafik çizilemedi: %s", ex)
        png = None
    return text, png, ctx


def keyboard(ctx: dict) -> InlineKeyboardMarkup:
    """Analiz mesajının altındaki butonlar. callback_data: tür|varlık|parametre (≤64 bayt)."""
    item, mode = ctx["item"], ctx.get("mode", "normal")
    other = "normal" if mode == "short" else "short"
    d, cur = ctx["d"], ctx["currency"]
    rows = [
        [InlineKeyboardButton("🔄 Yenile", callback_data=f"r|{item}|{mode}"),
         InlineKeyboardButton("📅 Günlük görünüm" if mode == "short" else "⏱ Kısa vade",
                              callback_data=f"r|{item}|{other}")],
        [InlineKeyboardButton("➕ Takibe ekle", callback_data=f"w|{item}"),
         InlineKeyboardButton("🧪 Geçmiş testi", callback_data=f"b|{item}")],
    ]
    hi, lo = d.get("swing_high"), d.get("swing_low")
    tgt = []
    if hi and hi > ctx["price"]:
        tgt.append(InlineKeyboardButton(f"🎯 Direnç {cur}{fmt_num(hi)}", callback_data=f"h|{item}|{hi:.10g}"))
    if lo and lo < ctx["price"]:
        tgt.append(InlineKeyboardButton(f"🎯 Destek {cur}{fmt_num(lo)}", callback_data=f"h|{item}|{lo:.10g}"))
    if tgt:
        rows.append(tgt)
    return InlineKeyboardMarkup(rows)


async def send_analysis(update: Update, item: str, name: str | None = None, news_query: str | None = None,
                        mode: str = "normal"):
    text, png, ctx = await run(_analyze, item, name, news_query, mode)
    if png:
        await update.effective_message.reply_photo(png)
    await reply(update, text, markup=keyboard(ctx))


async def process_coin(update: Update, query: str, with_info: bool, with_ta: bool, mode: str = "normal"):
    coin = None
    try:
        coin = await run(find_coin, query)
    except Exception as ex:  # noqa: BLE001
        log.warning("CoinGecko arama hatası: %s", ex)
    if coin is None and query.upper() not in COINS and not with_ta:
        await reply(update, f"❓ \"{query}\" adında bir coin bulamadım.", html_mode=False)
        return
    symbol = coin["symbol"].upper() if coin else query.upper()
    name = coin["name"] if coin else COINS.get(symbol, {}).get("name")

    if with_info and coin:
        try:
            info = await run(get_info, coin["id"])
            await reply(update, build_info_message(info))
        except Exception as ex:  # noqa: BLE001
            log.warning("Coin bilgisi alınamadı: %s", ex)
            await reply(update, f"⚠️ {symbol} bilgileri şu an alınamadı (CoinGecko).", html_mode=False)
    if with_ta:
        try:
            await send_analysis(update, symbol, name, f"{name or symbol} kripto" if with_info else None, mode)
        except Exception as ex:  # noqa: BLE001
            log.warning("Teknik analiz hatası (%s): %s", symbol, ex)
            msg = (f"❓ \"{query}\" adında bir coin bulamadım." if coin is None else
                   f"❌ {symbol} için teknik analiz yapılamadı: Binance/Bybit'te {symbol}/USDT paritesi "
                   "yok ya da yeterli geçmiş veri bulunmuyor.")
            await reply(update, msg, html_mode=False)


async def process_stock(update: Update, query: str, mode: str = "normal"):
    try:
        ticker = await run(find_stock, query)
    except Exception as ex:  # noqa: BLE001
        log.warning("Hisse arama hatası: %s", ex)
        ticker = None
    if not ticker:
        await reply(update, f"❓ Borsa İstanbul'da \"{query}\" bulunamadı. Hisse kodunu yazmayı dene (ör. THYAO).",
                    html_mode=False)
        return
    code = ticker.removesuffix(".IS")
    news_q = None
    if code.startswith("XU"):
        name = {"XU100": "BIST 100 Endeksi", "XU030": "BIST 30 Endeksi"}.get(code, code)
    elif mode == "short":  # kısa vadede şirket kartı ve haberler tekrar gönderilmez
        name = None
    else:
        name, news_q = None, f"\"{code}\" hisse"
        try:
            info = await run(get_stock_info, ticker)
            name = info.get("longName") or info.get("shortName")
            await reply(update, build_stock_info_message(ticker, info))
        except Exception as ex:  # noqa: BLE001
            log.warning("Şirket bilgisi alınamadı: %s", ex)
            await reply(update, f"⚠️ {code} şirket bilgileri şu an alınamadı.", html_mode=False)
    try:
        await send_analysis(update, f"BIST:{code}", name, news_q, mode)
    except Exception as ex:  # noqa: BLE001
        log.warning("Hisse teknik analiz hatası (%s): %s", code, ex)
        await reply(update, f"❌ {code} için teknik analiz yapılamadı (yeterli veri yok).", html_mode=False)


# ---------------- Komutlar: analiz ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        await run(F.ensure_chat, update.effective_chat.id)
    except Exception as ex:  # noqa: BLE001
        log.warning("Sohbet kaydedilemedi: %s", ex)
    await reply(update, HELP + f"\n\nChat ID: <code>{update.effective_chat.id}</code>" + html.escape(db_warning(), quote=False))


def _mode(words) -> str:
    return "short" if any(w.lower() in SHORT_WORDS for w in words) else "normal"


async def run_request(update: Update, words: list[str], with_info: bool, with_ta: bool = True, mode: str = ""):
    if not allowed(update):
        return
    mode = mode or _mode(words)
    queries = [w for w in words if w.lower() not in STOPWORDS][:MAX_ITEMS]
    default = not queries
    async with Waiting(update, "⏳ Canlı veriler alınıyor, analiz yapılıyor..."):
        for q in queries or list(COINS):
            await process_coin(update, q, with_info=with_info and not default and mode == "normal",
                               with_ta=with_ta, mode=mode)


async def run_stock_request(update: Update, words: list[str], mode: str = ""):
    if not allowed(update):
        return
    mode = mode or _mode(words)
    queries = [w for w in words if w.lower() not in STOPWORDS][:MAX_ITEMS] or ["XU100"]
    async with Waiting(update, "⏳ Borsa verileri alınıyor, analiz yapılıyor..."):
        for q in queries:
            await process_stock(update, q, mode)


async def analiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_request(update, context.args or [], with_info=True)


async def kisa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await reply(update, "Kullanım: /kisa btc · /kisa thyao hisse", html_mode=False)
        return
    if any(w.lower() in STOCK_WORDS for w in args):
        await run_stock_request(update, args, mode="short")
    else:
        await run_request(update, args, with_info=False, mode="short")


async def firsat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    arg = (context.args[0].lower() if context.args else "")
    jobs = []
    if arg in ("", "kripto", "coin"):
        jobs.append(scanner.crypto_report)
    if arg in ("", "bist", "hisse", "borsa"):
        jobs.append(scanner.bist_report)
    async with Waiting(update, "⏳ Piyasa taranıyor (ilk seferde ~30 sn sürer)..."):
        for fn in jobs:
            try:
                await reply(update, await run(fn))
            except Exception as ex:  # noqa: BLE001
                log.warning("Tarama hatası: %s", ex)
                await reply(update, "⚠️ Tarama şu an yapılamadı.", html_mode=False)
    await reply(update, scanner.FOOTER)


async def takvim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await reply(update, await run(events.calendar_report))


async def yz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yapay zekâ (Gemini) bağlantı testi."""
    if not allowed(update):
        return
    await reply(update, "🤖 Yapay zekâ testi\n\n" + await run(ai.diagnose), html_mode=False)


async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    stock, names, _ = split_args(context.args or [])
    if not names:
        await reply(update, "Kullanım: /backtest btc · /backtest thyao hisse", html_mode=False)
        return
    await run_backtest(update, names[0], stock)


async def run_backtest(update: Update, name: str, stock: bool = False, item: str | None = None):
    async with Waiting(update, "⏳ Geçmiş veriler üzerinde test ediliyor..."):
        item = item or await run(F.resolve_item, name, stock)
        if not item:
            await reply(update, f"❓ \"{name}\" bulunamadı.", html_mode=False)
            return
        try:
            await reply(update, await run(backtest.asset_report, item))
        except Exception as ex:  # noqa: BLE001
            log.warning("Backtest hatası (%s): %s", item, ex)
            await reply(update, f"❌ {item} için geçmiş testi yapılamadı (yeterli veri yok).", html_mode=False)


async def sanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    async with Waiting(update, "⏳ Sanal portföy hesaplanıyor..."):
        text = await run(paper.report)
    await reply(update, text)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analiz mesajı altındaki butonlar."""
    q = update.callback_query
    if not allowed(update):
        await q.answer()
        return
    parts = (q.data or "").split("|")
    kind, item = parts[0], parts[1] if len(parts) > 1 else ""
    if kind == "v":  # alarm oyu: v|<kayıt no>|<1/-1>
        try:
            eid, vote = int(item), int(parts[2])
            if vote not in (1, -1):
                raise ValueError
            await run(learn.record_vote, eid, update.effective_chat.id, vote)
        except (IndexError, ValueError):
            await q.answer("Geçersiz istek")
            return
        except Exception as ex:  # noqa: BLE001
            log.warning("Oy kaydedilemedi: %s", ex)
            await q.answer("Oy kaydedilemedi (veritabanı)")
            return
        await q.answer("Oyun kaydedildi, teşekkürler 🙏")
        try:
            await q.edit_message_reply_markup(vote_keyboard(eid, vote))
        except Exception:  # noqa: BLE001
            pass
        return
    if not ITEM_RE.match(item):  # callback verisi istemciden gelir; doğrula
        await q.answer("Geçersiz istek")
        return
    is_bist = item.startswith("BIST:")
    code = item.split(":", 1)[1] if is_bist else item
    chat = update.effective_chat.id
    if kind == "r":
        mode = parts[2] if len(parts) > 2 and parts[2] in ("normal", "short") else "normal"
        await q.answer("Analiz yenileniyor...")
        async with Waiting(update, "⏳ Analiz yapılıyor..."):
            try:
                await send_analysis(update, item, None, None, mode)
            except Exception as ex:  # noqa: BLE001
                log.warning("Buton analiz hatası (%s): %s", item, ex)
                await reply(update, f"❌ {code} analizi yapılamadı.", html_mode=False)
    elif kind == "w":
        await run(F.add_watch, chat, item)
        await q.answer(f"{F.label(item)} takip listene eklendi ✅", show_alert=False)
        await reply(update, f"✅ {F.label(item)} takip listene eklendi (alarm + sabah özeti). Liste: /takip",
                    html_mode=False)
    elif kind == "b":
        await q.answer("Geçmiş testi başlatıldı...")
        await run_backtest(update, code, is_bist, item=item)
    elif kind == "h":
        try:
            target = float(parts[2])
        except (IndexError, ValueError):
            await q.answer("Geçersiz hedef")
            return
        row = await run(F.add_target, chat, item, target)
        yon = "üstüne çıkınca" if row["direction"] == "above" else "altına inince"
        await q.answer("Hedef kuruldu 🎯")
        await reply(update, f"🎯 Hedef #{row['id']}: {F.label(item)} {F.money(item, target)} seviyesinin {yon} "
                            "haber vereceğim. Liste: /hedef", html_mode=False)
    else:
        await q.answer()


async def bilgi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Kullanım: /bilgi eth")
        return
    await run_request(update, context.args, with_info=True, with_ta=False)


async def hisse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_stock_request(update, context.args or [])


async def piyasa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await reply(update, await run(market.market_overview))


async def karsilastir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    stock, names, _ = split_args(context.args or [])
    if len(names) < 2:
        await reply(update, "Kullanım: /karsilastir btc eth sol\nHisse için: /karsilastir thyao asels pgsus hisse",
                    html_mode=False)
        return
    async with Waiting(update, "⏳ Karşılaştırılıyor..."):
        items = [await run(F.resolve_item, n, stock) for n in names[:6]]
        items = [i for i in items if i]
        await reply(update, await run(F.compare, items))


# ---------------- Komutlar: takip / alarm / hedef ----------------
async def takip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat = update.effective_chat.id
    args = context.args or []
    action = args[0].lower() if args else ""
    if action in ("ekle", "sil"):
        stock, names, _ = split_args(args[1:])
        if not names:
            await reply(update, f"Kullanım: /takip {action} eth   ·   /takip {action} thyao hisse", html_mode=False)
            return
        done = []
        for n in names:
            item = await run(F.resolve_item, n, stock)
            if not item:
                await reply(update, f"❓ \"{n}\" bulunamadı.", html_mode=False)
                continue
            if action == "ekle":
                await run(F.add_watch, chat, item)
                done.append(F.label(item))
            elif await run(F.remove_watch, chat, item):
                done.append(F.label(item))
        if done:
            await reply(update, ("✅ Eklendi: " if action == "ekle" else "🗑 Çıkarıldı: ") + ", ".join(done),
                        html_mode=False)
    items = await run(F.get_watchlist, chat)
    await reply(update, "📋 <b>Takip listen</b> (alarm + sabah özeti):\n" +
                "\n".join(f"• {F.label(i)}" for i in items) +
                "\n\n/takip ekle eth · /takip ekle thyao hisse · /takip sil sol")


async def alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("ac", "aç", "on", "kapat", "off"):
        on = arg in ("ac", "aç", "on")
        await run(lambda: F.set_setting(chat, alerts_on=on))
        saved = bool((await run(F.get_settings, chat)).get("alerts_on"))  # gerçekten kaydedildi mi?
        if saved != on:
            await reply(update, "⚠️ Ayar kaydedilemedi." + db_warning(), html_mode=False)
        elif on:
            await update.effective_message.reply_text("✅ Alarmlar açıldı. Eşik aşılınca buraya mesaj gelecek.")
        else:
            await update.effective_message.reply_text("🔕 Alarmlar kapatıldı. Tekrar açmak için /alarm ac")
    elif arg in ("durum", "test"):
        async with Waiting(update, "⏳ Takip listesi kontrol ediliyor..."):
            items = await run(F.alert_items, chat)
            lines = await run(lambda: alerts.check_all(True, items))
        await reply(update, "📋 Anlık durum — parantez içi: o varlığın normal hareketinin kaç katı\n"
                            "(alarm eşikleri: ani hareket {:g}x, trend 2 saatte {:g}x, hacim {:g}x)\n\n{}".format(
                                alerts.K15, alerts.TREND_K, alerts.VOLUME_X, "\n".join(lines)), html_mode=False)
    else:
        s = await run(F.get_settings, chat)
        items = await run(F.alert_items, chat)
        await update.effective_message.reply_text(
            f"🚨 Alarmlar: {'AÇIK ✅' if s.get('alerts_on') else 'KAPALI 🔕'}\n"
            f"İzlenenler (takip listesi + portföy): {', '.join(F.label(i) for i in items)}\n"
            f"Kontrol sıklığı: {CHECK_EVERY // 60} dakikada bir\n\n"
            "Alarm türleri:\n"
            "Eşikler her varlığın kendi oynaklığına göredir (BTC ile PEPE aynı ölçülmez) ve her pazartesi "
            "otomatik ayarlanır (/alarmkarne):\n"
            f"• Ani düşüş/yükseliş: 15 dk'da normal hareketin {alerts.K15:g} katı veya 1 saatte {alerts.K1H:g} katı "
            f"(en az ±%{alerts.FLOOR_15M:g} / ±%{alerts.FLOOR_1H:g})\n"
            f"• Trend başlangıcı: 2 saatte normalin {alerts.TREND_K:g} katı, aynı yönde sürüyor, hacim artmış, ADX yükseliyor\n"
            f"• Yüksek hacim: son 15 dk hacmi o saatin normalinin {alerts.VOLUME_X:g} katı ve fiyat normalin "
            f"{alerts.VOLUME_K:g} katı oynamış\n"
            "• Teknik sinyal: günlük SAR trend dönüşü, RSI 30/70 geçişi\n"
            "• Fiyat hedefleri: /hedef\n\n"
            "/alarm ac · /alarm kapat · /alarm durum (anlık ölçümler)" + db_warning())


async def hedef(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat = update.effective_chat.id
    args = context.args or []
    if args and args[0].lower() == "sil":
        try:
            ok = await run(F.remove_target, chat, int(args[1].lstrip("#")))
        except (IndexError, ValueError):
            ok = False
        await reply(update, "🗑 Hedef silindi." if ok else "❓ Bu numarada hedef yok. Liste için: /hedef", html_mode=False)
        return
    if args:
        stock, names, nums = split_args(args)
        if not names or not nums:
            await reply(update, "Kullanım: /hedef btc 60000   ·   /hedef thyao hisse 350", html_mode=False)
            return
        item = await run(F.resolve_item, names[0], stock)
        if not item:
            await reply(update, f"❓ \"{names[0]}\" bulunamadı.", html_mode=False)
            return
        try:
            row = await run(F.add_target, chat, item, F.parse_number(nums[0]))
        except Exception as ex:  # noqa: BLE001
            await reply(update, f"❌ Hedef eklenemedi: {ex}", html_mode=False)
            return
        yon = "üstüne çıkınca" if row["direction"] == "above" else "altına inince"
        await reply(update, f"🎯 Hedef #{row['id']} kuruldu: {F.label(item)} {F.money(item, float(row['target']))} "
                            f"seviyesinin {yon} haber vereceğim.\n(Şu an: {F.money(item, row['price'])})")
        return
    rows = await run(F.list_targets, chat)
    if not rows:
        await reply(update, "🎯 Bekleyen fiyat hedefin yok.\nKurmak için: /hedef btc 60000 · /hedef thyao hisse 350",
                    html_mode=False)
        return
    lines = [f"#{r['id']} {F.label(r['item'])}: {F.money(r['item'], float(r['target']))} "
             f"{'⬆️ üstü' if r['direction'] == 'above' else '⬇️ altı'}" for r in rows]
    await reply(update, "🎯 <b>Fiyat hedeflerin</b>\n" + "\n".join(lines) + "\n\nSilmek için: /hedef sil &lt;numara&gt;")


# ---------------- Komutlar: portföy / özet / karne ----------------
async def portfoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat = update.effective_chat.id
    args = context.args or []
    action = args[0].lower() if args else ""
    if action == "sil":
        try:
            ok = await run(F.remove_position, chat, int(args[1].lstrip("#")))
        except (IndexError, ValueError):
            ok = False
        await reply(update, "🗑 Pozisyon silindi." if ok else "❓ Bu numarada pozisyon yok. Liste için: /portfoy", html_mode=False)
        return
    if action == "ekle":
        stock, names, nums = split_args(args[1:])
        if not names or len(nums) < 2:
            await reply(update, "Kullanım: /portfoy ekle btc 0.05 62000\n/portfoy ekle thyao hisse 100 300\n"
                                "(miktar, alış fiyatı)", html_mode=False)
            return
        item = await run(F.resolve_item, names[0], stock)
        if not item:
            await reply(update, f"❓ \"{names[0]}\" bulunamadı.", html_mode=False)
            return
        amt, price = F.parse_number(nums[0]), F.parse_number(nums[1])
        row = await run(F.add_position, chat, item, amt, price)
        await reply(update, f"✅ Eklendi #{row['id']}: {F.label(item)} {amt:g} adet, alış {F.money(item, price)}\n"
                            "Portföyündeki varlıklar alarm kontrolüne de dahil edildi.", html_mode=False)
    async with Waiting(update, "⏳ Güncel fiyatlar alınıyor..."):
        text = await run(F.portfolio_report, chat)
    await reply(update, text)


async def ozet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("ac", "aç", "on"):
        await run(lambda: F.set_setting(chat, summary_on=True))
        await update.effective_message.reply_text("☀️ Sabah özeti açıldı: her gün 09:00'da gelecek.")
        return
    if arg in ("kapat", "off"):
        await run(lambda: F.set_setting(chat, summary_on=False))
        await update.effective_message.reply_text("🔕 Sabah özeti kapatıldı. Açmak için /ozet ac")
        return
    async with Waiting(update, "⏳ Özet hazırlanıyor..."):
        text = await run(F.morning_summary, chat)
    await reply(update, text)


async def karne(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await reply(update, await run(F.scorecard))


async def text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'eth', 'pepe analiz', 'thyao hisse', 'bist' gibi düz mesajları yakalar."""
    msg = (update.effective_message.text or "").lower().replace("/", " ").replace(",", " ")
    words = msg.split()
    names = [w for w in words if w not in STOPWORDS]
    mode = _mode(words)
    if STOCK_WORDS & set(words):
        await run_stock_request(update, names, mode)
        return
    if "analiz" in msg or (0 < len(names) <= MAX_ITEMS and len(words) <= 5):
        await run_request(update, names, with_info=bool(names), mode=mode)


# ---------------- Zamanlanmış işler ----------------
def vote_keyboard(event_id: int, chosen: int | None = None) -> InlineKeyboardMarkup:
    """Alarm altındaki oy butonları; callback: v|<kayıt no>|<1/-1>."""
    up, down = "👍 İşe yaradı", "👎 Yaramadı"
    if chosen == 1:
        up = "✅ " + up
    elif chosen == -1:
        down = "✅ " + down
    return InlineKeyboardMarkup([[InlineKeyboardButton(up, callback_data=f"v|{event_id}|1"),
                                  InlineKeyboardButton(down, callback_data=f"v|{event_id}|-1")]])


async def send_html(bot, chat: int, text: str, markup=None):
    try:
        parts = chunks(text)
        for i, part in enumerate(parts):
            await bot.send_message(chat, part, parse_mode="HTML", disable_web_page_preview=True,
                                   reply_markup=markup if i == len(parts) - 1 else None)
    except Exception as ex:  # noqa: BLE001
        log.warning("Mesaj gönderilemedi (%s): %s", chat, ex)


async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        chats = await run(F.recipients, "alerts_on")
    except Exception as ex:  # noqa: BLE001
        log.warning("Alıcılar okunamadı: %s", ex)
        return
    if chats:
        per_chat = {c: await run(F.alert_items, c) for c in chats}
        all_items = sorted({i for items in per_chat.values() for i in items})
        results = await run(alerts.check_items, all_items)
        for chat, items in per_chat.items():
            for item in items:
                for m in results.get(item, []):
                    eid = getattr(m, "event_id", None)
                    await send_html(context.bot, chat, m, vote_keyboard(eid) if eid else None)
    try:  # fiyat hedefleri (alarm kapalı olsa da hedef kuran haberdar olsun)
        for chat, m in await run(F.check_targets):
            await send_html(context.bot, chat, m)
    except Exception as ex:  # noqa: BLE001
        log.warning("Hedef kontrolü hatası: %s", ex)


async def resolve_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        n = await run(F.resolve_predictions)
        if n:
            log.info("%d tahmin sonuçlandırıldı", n)
    except Exception as ex:  # noqa: BLE001
        log.warning("Tahmin çözümleme hatası: %s", ex)
    try:
        n = await run(learn.resolve_events)
        if n:
            log.info("%d alarm sonucu ölçüldü", n)
    except Exception as ex:  # noqa: BLE001
        log.warning("Alarm sonucu ölçme hatası: %s", ex)


async def alertcard_job(context: ContextTypes.DEFAULT_TYPE):
    """Her sabah: dünün alarm karnesi (alarmı açık sohbetlere)."""
    chats = await run(F.recipients, "alerts_on")
    await run(learn.cleanup)
    for chat in chats:
        try:
            items = await run(F.alert_items, chat)
            text = await run(learn.daily_text, items)
            if text:
                await send_html(context.bot, chat, text)
        except Exception as ex:  # noqa: BLE001
            log.warning("Alarm karnesi gönderilemedi (%s): %s", chat, ex)


def _tune_report(force: bool = False) -> str:
    rep = learn.tune(apply=True, force=force)
    prompt = learn.ai_prompt(rep) if rep["status"] != "nodata" else None
    ai_text = ai.ask(prompt) if prompt else None
    if rep["status"] != "nodata":
        learn.save_run(rep, ai_text)
    return learn.weekly_text(rep, ai_text)


async def _all_alert_items() -> list[str]:
    chats = await run(F.recipients, "alerts_on")
    items = set()
    for c in chats:
        items.update(await run(F.alert_items, c))
    return sorted(items)


async def tune_job(context: ContextTypes.DEFAULT_TYPE):
    """Pazartesi sabahı: geçmişi yeniden oynat (yeni eklenen varlıklar da dahil olsun) → eşik ayarı
    (otomatik uygular) → rapor."""
    if datetime.now(F.TR).weekday() != 0:
        return
    res = await run(learn.backfill, await _all_alert_items())
    text = learn.backfill_text(res) + "\n\n" + await run(_tune_report)
    for chat in await run(F.recipients, "alerts_on"):
        await send_html(context.bot, chat, text)


async def alarmkarne(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    arg = (context.args or [""])[0].lower()
    if arg in ("dun", "dün", "gunluk"):
        async with Waiting(update, "⏳ Dünün alarmları hesaplanıyor..."):
            items = await run(F.alert_items, update.effective_chat.id)
            text = await run(learn.daily_text, items)
        await reply(update, text or "Dün için alarm kaydı yok.")
    elif arg in ("ayarla", "ayar", "tune"):
        async with Waiting(update, "⏳ Eşik kombinasyonları deneniyor..."):
            text = await run(_tune_report)
        await reply(update, text)
    elif arg in ("gecmis", "geçmiş", "baslat"):
        async with Waiting(update, f"⏳ Son {learn.BACKFILL_DAYS} gün yeniden oynatılıyor ve eşikler deneniyor "
                                   "(birkaç dakika sürebilir)..."):
            res = await run(learn.backfill, await _all_alert_items() or await run(F.alert_items, update.effective_chat.id))
            text = learn.backfill_text(res) + "\n\n" + await run(_tune_report)
        await reply(update, text)
    else:
        await reply(update, await run(learn.status_text))


async def summary_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        chats = await run(F.recipients, "summary_on")
    except Exception as ex:  # noqa: BLE001
        log.warning("Özet alıcıları okunamadı: %s", ex)
        return
    log.info("Sabah özeti %d sohbete gönderiliyor", len(chats))
    for chat in chats:
        try:  # bir sohbetteki hata diğerlerini engellemesin
            text = await run(F.morning_summary, chat)
        except Exception as ex:  # noqa: BLE001
            log.warning("Sabah özeti hazırlanamadı (%s): %s", chat, ex)
            continue
        await send_html(context.bot, chat, text)


async def scan_job(context: ContextTypes.DEFAULT_TYPE):
    """Her sabah günün fırsatları (sabah özeti açık olanlara)."""
    try:
        chats = await run(F.recipients, "summary_on")
    except Exception as ex:  # noqa: BLE001
        log.warning("Tarama alıcıları okunamadı: %s", ex)
        return
    if not chats:
        return
    texts = []
    for fn in ([scanner.crypto_report] + ([scanner.bist_report] if datetime.now(F.TR).weekday() < 5 else [])):
        try:
            texts.append(await run(fn))
        except Exception as ex:  # noqa: BLE001
            log.warning("Sabah taraması hatası: %s", ex)
    for chat in chats:
        for t in texts:
            await send_html(context.bot, chat, t)
        if texts:
            await send_html(context.bot, chat, scanner.FOOTER)


async def paper_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await run(paper.run_all)
    except Exception as ex:  # noqa: BLE001
        log.warning("Sanal portföy hatası: %s", ex)
        return
    if text:
        for chat in await run(F.recipients, "summary_on"):
            await send_html(context.bot, chat, text)


async def calendar_job(context: ContextTypes.DEFAULT_TYPE):
    """Önemli ekonomik olaydan ~30 dk önce hatırlatma."""
    try:
        msgs = await run(events.upcoming_reminders, 30)
        if msgs:
            for chat in await run(F.recipients, "alerts_on"):
                for m in msgs:
                    await send_html(context.bot, chat, m)
    except Exception as ex:  # noqa: BLE001
        log.warning("Takvim işi hatası: %s", ex)


async def kap_job(context: ContextTypes.DEFAULT_TYPE):
    """Takip/portföydeki BIST hisseleri için KAP/şirket haberleri (hafta içi 08-20)."""
    now = datetime.now(F.TR)
    if now.weekday() >= 5 or not (8 <= now.hour < 20):
        return
    try:
        chats = await run(F.recipients, "alerts_on")
        per_chat = {c: [F.code_of(i) for i in await run(F.alert_items, c)
                        if F.is_bist(i) and not F.code_of(i).startswith("XU")] for c in chats}
        codes = sorted({x for v in per_chat.values() for x in v})
        if not codes:
            return
        found = await run(events.company_news_alerts, codes)
        for chat, cs in per_chat.items():
            for code in cs:
                for m in found.get(code, []):
                    await send_html(context.bot, chat, m)
    except Exception as ex:  # noqa: BLE001
        log.warning("KAP işi hatası: %s", ex)


async def keepalive_job(context: ContextTypes.DEFAULT_TYPE):
    """Render ücretsiz planda 15 dk istek gelmezse uyur; kendi adresimizi yoklayarak uyanık kalırız."""
    try:
        import requests
        await asyncio.to_thread(requests.get, context.job.data, timeout=20)
    except Exception as ex:  # noqa: BLE001
        log.warning("Keepalive hatası: %s", ex)


# ---------------- Sohbet kaydı ve günlük işler ----------------
_registered: set[int] = set()


async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bota yazan her sohbeti (sadece /start değil) alarm/özet alıcısı olarak kaydeder."""
    chat = update.effective_chat
    if not chat or chat.id in _registered or not allowed(update):
        return
    try:
        await run(F.ensure_chat, chat.id)
        _registered.add(chat.id)
    except Exception as ex:  # noqa: BLE001
        log.warning("Sohbet kaydedilemedi (%s): %s", chat.id, ex)


_daily_done: dict[str, str] = {}


async def daily_dispatch(context: ContextTypes.DEFAULT_TYPE):
    """Her dakika: saati gelmiş ama bugün henüz çalışmamış günlük işleri çalıştırır.
    run_daily yerine bu kullanılır; sunucu 09:00'da uyuyor/yeniden başlıyorsa iş kaçmaz,
    veritabanındaki işaret sayesinde yeniden başlatmada iki kez de gönderilmez."""
    now = datetime.now(F.TR)
    today = now.date().isoformat()
    for name, t, fn in (("summary", SUMMARY_TIME, summary_job), ("scan", SCAN_TIME, scan_job),
                        ("alertcard", ALERTCARD_TIME, alertcard_job), ("paper", PAPER_TIME, paper_job),
                        ("tune", TUNE_TIME, tune_job)):
        if _daily_done.get(name) == today:
            continue
        if not ((t.hour, t.minute) <= (now.hour, now.minute) < DAILY_DEADLINE):
            continue
        _daily_done[name] = today
        key = f"daily:{name}:{today}"
        if await run(events.already_notified, key):
            continue
        await run(events.mark_notified, key)
        log.info("Günlük iş çalışıyor: %s", name)
        try:
            await fn(context)
        except Exception as ex:  # noqa: BLE001
            log.warning("Günlük iş hatası (%s): %s", name, ex)


# ---------------- Başlatma ----------------
DB_PROBLEM: str | None = None


def db_warning() -> str:
    return f"\n\n⚠️ Veritabanı sorunu: {DB_PROBLEM}" if DB_PROBLEM else ""


def init_storage():
    global DB_PROBLEM
    import store
    log.info("Veri deposu: %s", "Supabase" if store.using_supabase() else "yerel store.json")
    DB_PROBLEM = store.diagnose()
    if DB_PROBLEM:
        log.error("VERİTABANI SORUNU: %s", DB_PROBLEM)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Komut hata verirse sessiz kalma: kullanıcıya kısa bir hata mesajı gönder."""
    log.error("İşlenmeyen hata: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"⚠️ Bir hata oluştu: {type(context.error).__name__}: {str(context.error)[:300]}" + db_warning())
        except Exception:  # noqa: BLE001
            pass


async def set_menu(app: Application):
    """Telegram'da '/' yazınca çıkan komut menüsü."""
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in MENU])
    except Exception as ex:  # noqa: BLE001
        log.warning("Komut menüsü ayarlanamadı: %s", ex)


def build_app() -> Application:
    app = Application.builder().token(TOKEN).post_init(set_menu).build()
    handlers = {
        ("start", "yardim", "help"): start, "analiz": analiz, "bilgi": bilgi, ("hisse", "bist"): hisse,
        "piyasa": piyasa, ("karsilastir", "kiyasla"): karsilastir, "takip": takip, "alarm": alarm,
        "hedef": hedef, ("portfoy", "portfolio"): portfoy, "ozet": ozet, "karne": karne,
        "kisa": kisa, ("firsat", "firsatlar", "tara"): firsat, "takvim": takvim, "backtest": backtest_cmd,
        "sanal": sanal, ("yz", "ai"): yz, ("alarmkarne", "ogrenme"): alarmkarne,
    }
    app.add_handler(TypeHandler(Update, register_chat), group=-1)
    for names, fn in handlers.items():
        app.add_handler(CommandHandler(list(names) if isinstance(names, tuple) else names, fn,
                                       filters=NOT_EDITED))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_EDITED, text_trigger))
    jq = app.job_queue
    jq.run_repeating(alert_job, interval=CHECK_EVERY, first=30, name="alerts")
    jq.run_repeating(resolve_job, interval=3600, first=120, name="resolve")
    jq.run_repeating(calendar_job, interval=600, first=90, name="calendar")
    jq.run_repeating(kap_job, interval=1200, first=150, name="kap")
    jq.run_repeating(daily_dispatch, interval=60, first=20, name="daily")
    return app


async def run_webhook_server(app: Application, base_url: str):
    """Webhook + /health uç noktası (uyanık kalma yoklaması için) tek bir sunucuda."""
    import hashlib

    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route

    secret = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
    port = int(os.environ.get("PORT", "10000"))
    base_url = base_url.rstrip("/")

    async def telegram(request: Request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return Response(status_code=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return Response()

    async def health(_: Request):
        return PlainTextResponse("ok")

    web = Starlette(routes=[
        Route("/webhook", telegram, methods=["POST"]),
        Route("/health", health, methods=["GET", "HEAD"]),
        Route("/", health, methods=["GET", "HEAD"]),
    ])
    server = uvicorn.Server(uvicorn.Config(web, host="0.0.0.0", port=port, log_level="warning"))

    async with app:
        await app.bot.set_webhook(base_url + "/webhook", secret_token=secret,
                                  allowed_updates=["message", "callback_query"])
        app.job_queue.run_repeating(keepalive_job, interval=600, first=60,
                                    data=base_url + "/health", name="keepalive")
        await set_menu(app)  # post_init yalnızca run_polling/run_webhook'ta çağrılır
        await app.start()
        log.info("Webhook modunda çalışıyor: %s (port %s)", base_url, port)
        await server.serve()
        await app.stop()


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN bulunamadı. .env dosyasına ekleyin (bkz. .env.example).")
    init_storage()
    app = build_app()
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base_url:
        asyncio.run(run_webhook_server(app, base_url))
    else:
        log.info("Bot çalışıyor (polling). Durdurmak için Ctrl+C.")
        app.run_polling()


if __name__ == "__main__":
    main()
