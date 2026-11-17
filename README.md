# Kripto Teknik Analiz Telegram Botu (BTC & SOL)

Binance'ten (yedek: Bybit) canlı günlük + haftalık mum verisi çeker; Bollinger Bands, Parabolic SAR,
RSI, Stochastic RSI, MACD ve ADX hesaplar, yorumlar ve **1 hafta / 1 ay** tahmini verir.

## Kurulum
1. Telegram'da **@BotFather** → `/newbot` → bot adı verin → size bir **token** verir.
2. `.env.example` dosyasını `.env` olarak kopyalayın, `TELEGRAM_BOT_TOKEN=` kısmına token'ı yazın.
3. `pip install -r requirements.txt`
4. `python bot.py`

## Komutlar
| Komut | Ne yapar |
|---|---|
| `/analiz` (veya "analiz et" yazmak) | BTC + SOL analizi |
| `/analiz btc` / `/analiz sol` | Tek coin |
| `/start` | Yardım + chat ID'niz |

Botu sadece kendiniz kullanmak için `/start` ile öğrendiğiniz chat ID'yi `.env` içindeki `ALLOWED_CHAT_IDS`'e yazın.

## Telegram olmadan test
`python analysis.py` (veya `python analysis.py sol`) raporu terminale basar.

## Tahmin nasıl hesaplanıyor?
- Her gösterge -1 (düşüş) ile +1 (yükseliş) arasında puanlanır, ağırlıklı ortalama alınır
  (MACD 1.5, SAR/ADX/RSI 1.0, StochRSI/Bollinger 0.75). ADX < 20 ise skor sönümlenir.
- **1 hafta**: günlük grafik skoru. **1 ay**: %60 haftalık + %40 günlük skor.
- Fiyat aralığı: son 60 günün volatilitesi × √gün, skor yönünde kaydırılmış ~%68 bant.
- Güven: 6 göstergeden kaçının aynı yönü gösterdiği.

⚠️ Yatırım tavsiyesi değildir.
