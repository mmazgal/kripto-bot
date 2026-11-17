"""Telegram botu: /analiz  -> BTC + SOL,  /analiz btc  veya  /analiz sol -> tek coin."""
import data  # noqa: F401  (SSL ayarını telegram/httpx'ten önce yapar)
import asyncio
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from analysis import COINS, build_report

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Kripto Teknik Analiz Botu\n\n"
        "/analiz — Bitcoin ve Solana analizi\n"
        "/analiz btc — sadece Bitcoin\n"
        "/analiz sol — sadece Solana\n\n"
        "Göstergeler: Bollinger Bands, Parabolic SAR, RSI, StochRSI, MACD, ADX\n"
        "Tahmin: 1 hafta ve 1 ay\n\n"
        f"Chat ID'niz: {update.effective_chat.id}"
    )


async def analiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    args = [a.upper() for a in (context.args or [])]
    coins = [a for a in args if a in COINS] or list(COINS)
    wait = await update.message.reply_text("⏳ Canlı veriler alınıyor, analiz yapılıyor...")
    for coin in coins:
        try:
            text = await asyncio.to_thread(build_report, coin)
        except Exception as e:  # noqa: BLE001
            log.exception("Analiz hatası")
            text = f"❌ {coin} analizi yapılamadı: {e}"
            await update.message.reply_text(text)
            continue
        await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
    await wait.delete()


async def text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'analiz et', 'btc analiz' gibi düz mesajları da yakalar."""
    msg = (update.message.text or "").lower()
    if "analiz" in msg:
        context.args = [w for w in msg.replace("/", " ").split() if w.upper() in COINS]
        await analiz(update, context)


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN bulunamadı. .env dosyasına ekleyin (bkz. .env.example).")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "yardim", "help"], start))
    app.add_handler(CommandHandler("analiz", analiz))
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
