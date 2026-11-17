"""Telegram botu.

/analiz              -> BTC + SOL teknik analiz
/analiz eth pepe     -> her coin için bilgi + teknik analiz
/bilgi doge          -> sadece coin bilgisi
/hisse thyao asels   -> Borsa İstanbul hissesi: şirket bilgisi + teknik analiz (/hisse = BIST 100)
Düz mesaj: "eth", "pepe analiz", "thyao hisse", "bist" gibi yazılar da çalışır.
"""
import data  # noqa: F401  (SSL ayarını telegram/httpx'ten önce yapar)
import asyncio
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from analysis import COINS, build_report
from coininfo import build_info_message, find_coin, get_info
from stockinfo import build_stock_info_message, find_stock, get_stock_info

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

MAX_COINS = 3
STOPWORDS = {"analiz", "analizi", "et", "yap", "coin", "bana", "bir", "lütfen", "lutfen",
             "ve", "ile", "bilgi", "hakkında", "hakkinda", "nedir", "kripto", "ver", "/analiz",
             "hisse", "hissesi", "bist", "borsa", "/hisse", "/bist"}
STOCK_WORDS = {"hisse", "hissesi", "bist", "borsa"}


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


async def reply(update: Update, text: str, html_mode: bool = True):
    await update.message.reply_text(text, parse_mode="HTML" if html_mode else None,
                                    disable_web_page_preview=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Kripto Teknik Analiz Botu\n\n"
        "/analiz — Bitcoin ve Solana analizi\n"
        "/analiz eth — herhangi bir coin: bilgi + teknik analiz\n"
        "/analiz eth pepe doge — aynı anda en fazla 3 coin\n"
        "/bilgi avax — sadece coin bilgisi (hacim, tarihçe, nedir)\n\n"
        "🇹🇷 Borsa İstanbul:\n"
        "/hisse thyao — hisse: şirket bilgisi + teknik analiz\n"
        "/hisse — BIST 100 endeksi analizi\n"
        "(\"thyao hisse\" veya \"aselsan hisse\" yazman da yeterli)\n\n"
        "Komut yazmadan sadece coin adını (ör. \"eth\" veya \"pepe analiz\") yazman da yeterli.\n\n"
        "Göstergeler: Bollinger Bands, Parabolic SAR, RSI, StochRSI, MACD, ADX\n"
        "Tahmin: 1 hafta ve 1 ay\n\n"
        f"Chat ID'niz: {update.effective_chat.id}"
    )


async def process_coin(update: Update, query: str, with_info: bool, with_ta: bool):
    """Tek bir coin için bilgi ve/veya teknik analiz mesajlarını gönderir."""
    coin = None
    try:
        coin = await asyncio.to_thread(find_coin, query)
    except Exception as e:  # noqa: BLE001
        log.warning("CoinGecko arama hatası: %s", e)

    if coin is None and query.upper() not in COINS:
        # CoinGecko bulamadı/erişilemedi: girdiyi doğrudan sembol olarak dene
        if not with_ta:
            await reply(update, f"❓ \"{query}\" adında bir coin bulamadım.", html_mode=False)
            return
    symbol = coin["symbol"].upper() if coin else query.upper()
    name = coin["name"] if coin else None

    if with_info and coin:
        try:
            info = await asyncio.to_thread(get_info, coin["id"])
            await reply(update, build_info_message(info))
        except Exception as e:  # noqa: BLE001
            log.warning("Coin bilgisi alınamadı: %s", e)
            await reply(update, f"⚠️ {symbol} bilgileri şu an alınamadı (CoinGecko).", html_mode=False)

    if with_ta:
        try:
            text = await asyncio.to_thread(build_report, symbol, name)
            await reply(update, text)
        except Exception as e:  # noqa: BLE001
            log.warning("Teknik analiz hatası (%s): %s", symbol, e)
            msg = f"❌ {symbol} için teknik analiz yapılamadı: Binance/Bybit'te {symbol}/USDT paritesi " \
                  "yok ya da yeterli geçmiş veri bulunmuyor."
            if coin is None:
                msg = f"❓ \"{query}\" adında bir coin bulamadım."
            await reply(update, msg, html_mode=False)


async def process_stock(update: Update, query: str):
    """Tek bir BIST hissesi/endeksi için şirket bilgisi + teknik analiz gönderir."""
    try:
        ticker = await asyncio.to_thread(find_stock, query)
    except Exception as e:  # noqa: BLE001
        log.warning("Hisse arama hatası: %s", e)
        ticker = None
    if not ticker:
        await reply(update, f"❓ Borsa İstanbul'da \"{query}\" bulunamadı. Hisse kodunu yazmayı dene (ör. THYAO).",
                    html_mode=False)
        return
    code = ticker.removesuffix(".IS")
    if code.startswith("XU"):  # endekslerde şirket kartı yok
        name = {"XU100": "BIST 100 Endeksi", "XU030": "BIST 30 Endeksi"}.get(code, code)
    else:
        name = None
        try:
            info = await asyncio.to_thread(get_stock_info, ticker)
            name = info.get("longName") or info.get("shortName")
            await reply(update, build_stock_info_message(ticker, info))
        except Exception as e:  # noqa: BLE001
            log.warning("Şirket bilgisi alınamadı: %s", e)
            await reply(update, f"⚠️ {code} şirket bilgileri şu an alınamadı.", html_mode=False)
    try:
        text = await asyncio.to_thread(build_report, code, name, "bist")
        await reply(update, text)
    except Exception as e:  # noqa: BLE001
        log.warning("Hisse teknik analiz hatası (%s): %s", code, e)
        await reply(update, f"❌ {code} için teknik analiz yapılamadı (yeterli veri yok).", html_mode=False)


async def run_stock_request(update: Update, words: list[str]):
    if not allowed(update):
        return
    queries = [w for w in words if w.lower() not in STOPWORDS][:MAX_COINS] or ["XU100"]
    wait = await update.message.reply_text("⏳ Borsa verileri alınıyor, analiz yapılıyor...")
    for q in queries:
        await process_stock(update, q)
    try:
        await wait.delete()
    except Exception:  # noqa: BLE001
        pass


async def hisse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_stock_request(update, context.args or [])


async def run_request(update: Update, words: list[str], with_info: bool, with_ta: bool = True):
    if not allowed(update):
        return
    queries = [w for w in words if w.lower() not in STOPWORDS][:MAX_COINS]
    default = not queries
    if default:
        queries = list(COINS)
    wait = await update.message.reply_text("⏳ Canlı veriler alınıyor, analiz yapılıyor...")
    for q in queries:
        await process_coin(update, q, with_info=with_info and not default, with_ta=with_ta)
    try:
        await wait.delete()
    except Exception:  # noqa: BLE001
        pass


async def analiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_request(update, context.args or [], with_info=True)


async def bilgi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanım: /bilgi eth")
        return
    await run_request(update, context.args, with_info=True, with_ta=False)


async def text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'eth', 'pepe analiz', 'analiz et avax' gibi düz mesajları yakalar."""
    msg = (update.message.text or "").lower().replace("/", " ").replace(",", " ")
    words = msg.split()
    coins = [w for w in words if w not in STOPWORDS]
    if STOCK_WORDS & set(words):
        await run_stock_request(update, coins)
        return
    if "analiz" in msg or (0 < len(coins) <= MAX_COINS and len(words) <= 5):
        await run_request(update, coins, with_info=bool(coins))


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN bulunamadı. .env dosyasına ekleyin (bkz. .env.example).")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "yardim", "help"], start))
    app.add_handler(CommandHandler("analiz", analiz))
    app.add_handler(CommandHandler("bilgi", bilgi))
    app.add_handler(CommandHandler(["hisse", "bist"], hisse))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_trigger))

    # Render gibi platformlarda webhook modu (RENDER_EXTERNAL_URL otomatik gelir), yoksa polling
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base_url:
        import hashlib
        secret = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
        port = int(os.environ.get("PORT", "10000"))
        log.info("Webhook modunda çalışıyor: %s (port %s)", base_url, port)
        app.run_webhook(
            listen="0.0.0.0", port=port, url_path="webhook",
            webhook_url=base_url.rstrip("/") + "/webhook", secret_token=secret,
        )
    else:
        log.info("Bot çalışıyor (polling). Durdurmak için Ctrl+C.")
        app.run_polling()


if __name__ == "__main__":
    main()
