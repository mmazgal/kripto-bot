"""Telegram botu — kripto + Borsa İstanbul teknik analiz, alarmlar, portföy.

Komutlar için /start yazın. Düz mesajlar da çalışır: "eth", "thyao hisse", "bist".
"""
import data  # noqa: F401  (SSL ayarını telegram/httpx'ten önce yapar)
import asyncio
import logging
import os
from datetime import time as dtime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import alerts
import features as F
import market
from analysis import COINS, compute_item, render_report
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
             "hisse", "hissesi", "bist", "borsa", "/hisse", "/bist"}
STOCK_WORDS = {"hisse", "hissesi", "bist", "borsa"}
CHECK_EVERY = 300          # sn: alarm kontrol sıklığı
SUMMARY_TIME = dtime(9, 0, tzinfo=F.TR)

HELP = (
    "👋 <b>Kripto & Borsa Analiz Botu</b>\n\n"
    "<b>📊 Analiz</b>\n"
    "/analiz — BTC + SOL · /analiz eth — herhangi bir coin\n"
    "/hisse thyao — BIST hissesi · /hisse — BIST 100\n"
    "/bilgi doge — sadece coin bilgisi\n"
    "/karsilastir btc eth sol — yan yana karşılaştırma\n"
    "/piyasa — korku/açgözlülük, BTC dominansı, dolar/TL\n\n"
    "<b>📋 Takip & Alarm</b>\n"
    "/takip — listen · /takip ekle eth · /takip ekle thyao hisse · /takip sil sol\n"
    "/alarm — durum · /alarm ac · /alarm kapat · /alarm durum\n"
    "/hedef btc 60000 — fiyat hedefi · /hedef — liste · /hedef sil 3\n\n"
    "<b>💼 Portföy & Raporlar</b>\n"
    "/portfoy — kâr/zarar · /portfoy ekle btc 0.05 62000 · /portfoy ekle thyao hisse 100 300\n"
    "/portfoy sil 2\n"
    "/ozet — sabah özetini şimdi al · /ozet ac · /ozet kapat (her sabah 09:00)\n"
    "/karne — botun tahminleri ne kadar tuttu\n\n"
    "Komutsuz da yazabilirsin: <i>eth</i>, <i>pepe analiz</i>, <i>thyao hisse</i>, <i>bist</i>"
)


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
ENV_ALERT_CHATS = {int(x) for x in os.environ.get("ALERT_CHAT_IDS", "").replace(" ", "").split(",") if x}


def allowed(update: Update) -> bool:
    return not ALLOWED or update.effective_chat.id in ALLOWED


async def reply(update: Update, text: str, html_mode: bool = True):
    await update.message.reply_text(text, parse_mode="HTML" if html_mode else None,
                                    disable_web_page_preview=True)


async def run(fn, *args):
    return await asyncio.to_thread(fn, *args)


class Waiting:
    """'⏳ ...' mesajını gösterir, iş bitince siler."""
    def __init__(self, update, text="⏳ Canlı veriler alınıyor..."):
        self.update, self.text, self.msg = update, text, None

    async def __aenter__(self):
        self.msg = await self.update.message.reply_text(self.text)

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
def _analyze(item: str, name: str | None, news_query: str | None):
    ctx = compute_item(item, name)
    F.record_prediction(ctx)
    extra = [x for x in [market.sentiment_line(ctx["market"])] if x]
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
    return text, png


async def send_analysis(update: Update, item: str, name: str | None = None, news_query: str | None = None):
    text, png = await run(_analyze, item, name, news_query)
    if png:
        await update.message.reply_photo(png)
    await reply(update, text)


async def process_coin(update: Update, query: str, with_info: bool, with_ta: bool):
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
            await send_analysis(update, symbol, name, f"{name or symbol} kripto" if with_info else None)
        except Exception as ex:  # noqa: BLE001
            log.warning("Teknik analiz hatası (%s): %s", symbol, ex)
            msg = (f"❓ \"{query}\" adında bir coin bulamadım." if coin is None else
                   f"❌ {symbol} için teknik analiz yapılamadı: Binance/Bybit'te {symbol}/USDT paritesi "
                   "yok ya da yeterli geçmiş veri bulunmuyor.")
            await reply(update, msg, html_mode=False)


async def process_stock(update: Update, query: str):
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
        await send_analysis(update, f"BIST:{code}", name, news_q)
    except Exception as ex:  # noqa: BLE001
        log.warning("Hisse teknik analiz hatası (%s): %s", code, ex)
        await reply(update, f"❌ {code} için teknik analiz yapılamadı (yeterli veri yok).", html_mode=False)


# ---------------- Komutlar: analiz ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run(F.ensure_chat, update.effective_chat.id)
    await reply(update, HELP + f"\n\nChat ID: <code>{update.effective_chat.id}</code>")


async def run_request(update: Update, words: list[str], with_info: bool, with_ta: bool = True):
    if not allowed(update):
        return
    queries = [w for w in words if w.lower() not in STOPWORDS][:MAX_ITEMS]
    default = not queries
    async with Waiting(update, "⏳ Canlı veriler alınıyor, analiz yapılıyor..."):
        for q in queries or list(COINS):
            await process_coin(update, q, with_info=with_info and not default, with_ta=with_ta)


async def run_stock_request(update: Update, words: list[str]):
    if not allowed(update):
        return
    queries = [w for w in words if w.lower() not in STOPWORDS][:MAX_ITEMS] or ["XU100"]
    async with Waiting(update, "⏳ Borsa verileri alınıyor, analiz yapılıyor..."):
        for q in queries:
            await process_stock(update, q)


async def analiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_request(update, context.args or [], with_info=True)


async def bilgi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanım: /bilgi eth")
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
    if arg in ("ac", "aç", "on"):
        await run(lambda: F.set_setting(chat, alerts_on=True))
        await update.message.reply_text("✅ Alarmlar açıldı. Eşik aşılınca buraya mesaj gelecek.")
    elif arg in ("kapat", "off"):
        await run(lambda: F.set_setting(chat, alerts_on=False))
        await update.message.reply_text("🔕 Alarmlar kapatıldı. Tekrar açmak için /alarm ac")
    elif arg in ("durum", "test"):
        async with Waiting(update, "⏳ Takip listesi kontrol ediliyor..."):
            items = await run(F.alert_items, chat)
            lines = await run(lambda: alerts.check_all(True, items))
        await update.message.reply_text(
            "📋 Anlık durum (alarm eşikleri: 15dk ±%{}, 1s ±%{}, hacim {}x)\n\n{}".format(
                alerts.DROP_15M, alerts.DROP_1H, alerts.VOLUME_X, "\n".join(lines)))
    else:
        s = await run(F.get_settings, chat)
        items = await run(F.alert_items, chat)
        await update.message.reply_text(
            f"🚨 Alarmlar: {'AÇIK ✅' if s.get('alerts_on') else 'KAPALI 🔕'}\n"
            f"İzlenenler (takip listesi + portföy): {', '.join(F.label(i) for i in items)}\n"
            f"Kontrol sıklığı: {CHECK_EVERY // 60} dakikada bir\n\n"
            "Alarm türleri:\n"
            f"• Ani düşüş/yükseliş: 15 dk'da ±%{alerts.DROP_15M} veya 1 saatte ±%{alerts.DROP_1H}\n"
            f"• Yüksek hacim: son 15 dk hacmi normalin {alerts.VOLUME_X} katı\n"
            "• Teknik sinyal: günlük SAR trend dönüşü, RSI 30/70 geçişi\n"
            "• Fiyat hedefleri: /hedef\n\n"
            "/alarm ac · /alarm kapat · /alarm durum (anlık ölçümler)")


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
        await update.message.reply_text("☀️ Sabah özeti açıldı: her gün 09:00'da gelecek.")
        return
    if arg in ("kapat", "off"):
        await run(lambda: F.set_setting(chat, summary_on=False))
        await update.message.reply_text("🔕 Sabah özeti kapatıldı. Açmak için /ozet ac")
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
    msg = (update.message.text or "").lower().replace("/", " ").replace(",", " ")
    words = msg.split()
    names = [w for w in words if w not in STOPWORDS]
    if STOCK_WORDS & set(words):
        await run_stock_request(update, names)
        return
    if "analiz" in msg or (0 < len(names) <= MAX_ITEMS and len(words) <= 5):
        await run_request(update, names, with_info=bool(names))


# ---------------- Zamanlanmış işler ----------------
async def send_html(bot, chat: int, text: str):
    try:
        await bot.send_message(chat, text, parse_mode="HTML", disable_web_page_preview=True)
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
                    await send_html(context.bot, chat, m)
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


async def summary_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        chats = await run(F.recipients, "summary_on")
    except Exception as ex:  # noqa: BLE001
        log.warning("Özet alıcıları okunamadı: %s", ex)
        return
    for chat in chats:
        text = await run(F.morning_summary, chat)
        await send_html(context.bot, chat, text)


async def keepalive_job(context: ContextTypes.DEFAULT_TYPE):
    """Render ücretsiz planda 15 dk istek gelmezse uyur; kendi adresimizi yoklayarak uyanık kalırız."""
    try:
        import requests
        await asyncio.to_thread(requests.get, context.job.data, timeout=20)
    except Exception as ex:  # noqa: BLE001
        log.warning("Keepalive hatası: %s", ex)


# ---------------- Başlatma ----------------
def init_storage():
    import store
    log.info("Veri deposu: %s", "Supabase" if store.using_supabase() else "yerel store.json")
    for chat in ENV_ALERT_CHATS:  # eski ALERT_CHAT_IDS ayarını veritabanına taşı
        try:
            F.ensure_chat(chat)
        except Exception as ex:  # noqa: BLE001
            log.warning("Ayar kaydı oluşturulamadı (%s): %s", chat, ex)


def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()
    handlers = {
        ("start", "yardim", "help"): start, "analiz": analiz, "bilgi": bilgi, ("hisse", "bist"): hisse,
        "piyasa": piyasa, ("karsilastir", "kiyasla"): karsilastir, "takip": takip, "alarm": alarm,
        "hedef": hedef, ("portfoy", "portfolio"): portfoy, "ozet": ozet, "karne": karne,
    }
    for names, fn in handlers.items():
        app.add_handler(CommandHandler(list(names) if isinstance(names, tuple) else names, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_trigger))
    jq = app.job_queue
    jq.run_repeating(alert_job, interval=CHECK_EVERY, first=30, name="alerts")
    jq.run_repeating(resolve_job, interval=3600, first=120, name="resolve")
    jq.run_daily(summary_job, time=SUMMARY_TIME, name="summary")
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
                                  allowed_updates=Update.ALL_TYPES)
        app.job_queue.run_repeating(keepalive_job, interval=600, first=60,
                                    data=base_url + "/health", name="keepalive")
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
