# 📊 Kripto & Borsa İstanbul Teknik Analiz Telegram Botu

Telegram üzerinden **herhangi bir kripto parayı** ve **Borsa İstanbul hissesini** canlı verilerle analiz eden,
**1 hafta / 1 ay tahmini** veren, piyasayı 7/24 izleyip **alarm** gönderen, **portföy** ve **tahmin karnesi** tutan kişisel asistan.

Tamamen ücretsiz servislerle çalışır: Render (sunucu), Supabase (veritabanı), Binance/Bybit/Yahoo/CoinGecko (veri).

> ⚠️ **Yatırım tavsiyesi değildir.** Teknik göstergeler geçmiş fiyata dayanır; haber ve makro gelişmeler sonucu tamamen değiştirebilir.

---

## ✨ Özellikler

| | Özellik | Açıklama |
|---|---|---|
| 📈 | **Teknik analiz** | 6 gösterge (Bollinger Bands, Parabolic SAR, RSI, Stochastic RSI, MACD, ADX), günlük + haftalık grafik, Türkçe yorum |
| 🔮 | **Tahmin** | 1 hafta ve 1 ay için yön, hedef fiyat, ~%68 olasılıklı fiyat aralığı ve güven derecesi |
| 🖼 | **Grafik görseli** | Mumlar, Bollinger bantları, SAR noktaları, destek/direnç çizgileri, RSI ve MACD panelleri |
| ℹ️ | **Coin bilgi kartı** | Nedir / neden oluşturuldu (Türkçe çeviri), piyasa değeri, hacim, arz, zirve/dip, sıralama |
| 🏢 | **Şirket bilgi kartı** | Sektör, çalışan sayısı, piyasa değeri, hacim, 52 hafta zirve/dip, temettü, ne iş yaptığı |
| 📰 | **Haberler** | Her analizin altında o varlıkla ilgili son 3 Türkçe haber |
| 🌡 | **Piyasa havası** | Kripto Korku/Açgözlülük endeksi, BTC dominansı, BIST 100 günlük değişim, USD/TRY |
| ⚖️ | **Karşılaştırma** | Birden fazla varlığı tek tabloda: günlük/haftalık değişim, RSI, 1H/1A yönü |
| 🚨 | **Alarmlar** | Ani düşüş/yükseliş, hacim patlaması, SAR trend dönüşü, RSI aşırı alım/satım |
| 🎯 | **Fiyat hedefi** | Belirlediğin seviye yukarı/aşağı kırılınca haber |
| ☀️ | **Sabah özeti** | Her gün 09:00'da piyasa havası + takip listesinin durumu |
| 💼 | **Portföy** | Kâr/zarar takibi; kripto ($) + BIST (₺) ve TL cinsinden genel toplam |
| 📒 | **Tahmin karnesi** | Bot her tahminini kaydeder, süresi dolunca doğruluğunu ölçer |

---

## 💬 Komutlar

### Analiz
| Komut | Ne yapar |
|---|---|
| `/analiz` | Bitcoin + Solana analizi |
| `/analiz eth` · `/analiz pepe doge` | Herhangi bir coin (en fazla 3): bilgi kartı + grafik + analiz + haberler |
| `/bilgi avax` | Sadece coin bilgi kartı |
| `/hisse thyao` · `/hisse asels sasa` | BIST hissesi: şirket kartı + grafik + analiz + haberler |
| `/hisse` | BIST 100 endeksi analizi (`bist30` de yazılabilir) |
| `/karsilastir btc eth sol` | Yan yana tablo (hisseler için: `/karsilastir thyao asels hisse`) |
| `/piyasa` | Genel piyasa havası |

### Takip, alarm ve hedefler
| Komut | Ne yapar |
|---|---|
| `/takip` | Takip listesini gösterir (varsayılan: BTC, SOL) |
| `/takip ekle eth` · `/takip ekle thyao hisse` · `/takip sil sol` | Listeyi düzenler; alarm ve sabah özeti bu listeyi kullanır |
| `/alarm` · `/alarm ac` · `/alarm kapat` | Alarm durumu, açma/kapama |
| `/alarm durum` | İzlenen varlıkların anlık ölçümleri (15 dk / 1 saat / gün değişimi, hacim katı) |
| `/hedef btc 60000` · `/hedef thyao hisse 350` | Fiyat hedefi kurar (yön otomatik belirlenir) |
| `/hedef` · `/hedef sil 3` | Hedefleri listeler / siler |

### Portföy ve raporlar
| Komut | Ne yapar |
|---|---|
| `/portfoy` | Güncel kâr/zarar |
| `/portfoy ekle btc 0.05 62000` | Pozisyon ekler (miktar, alış fiyatı) |
| `/portfoy ekle thyao hisse 100 300,50` | BIST pozisyonu (Türkçe ondalık virgül de olur) |
| `/portfoy sil 2` | Pozisyon siler |
| `/ozet` · `/ozet ac` · `/ozet kapat` | Sabah özetini şimdi al / her gün 09:00'da aç-kapat |
| `/karne` | Geçmiş tahminlerin yön doğruluğu ve aralık isabeti |
| `/start` | Tüm komutlar + chat ID |

### Komutsuz kullanım
Düz mesaj da yeterli: `eth` · `pepe analiz` · `analiz et avax` · `thyao hisse` · `aselsan hisse` · `bist`

Coinler kısa ya da uzun adla bulunur (`eth` = `ethereum`); aynı kısaltmada piyasa değeri en yüksek olan seçilir.
Hisseler kodla ya da şirket adıyla bulunur (`aselsan` → `ASELS`).

---

## ⚙️ Nasıl çalışır?

### Puanlama ve tahmin
- Her gösterge **-1 (düşüş) ile +1 (yükseliş)** arasında puanlanır ve ağırlıklı ortalama alınır:
  MACD 1.5 · Parabolic SAR 1.0 · ADX 1.0 · RSI 1.0 · StochRSI 0.75 · Bollinger 0.75.
  ADX < 20 (zayıf trend) ise skor %25 sönümlenir.
- **1 hafta** tahmini günlük grafik skorundan, **1 ay** tahmini %60 haftalık + %40 günlük skordan hesaplanır.
- **Fiyat aralığı:** son 60 günün volatilitesi × √gün, skor yönünde kaydırılmış ~%68 olasılık bandı.
  Borsa İstanbul için işlem günü kullanılır (1 hafta = 5, 1 ay = 21 gün).
- **Güven:** 6 göstergeden kaçının aynı yönü gösterdiği (5+ Yüksek, 4 Orta, altı Düşük).
- Skor etiketleri: ≥0.5 Güçlü Yükseliş · ≥0.2 Yükseliş · ±0.2 Nötr · ≤-0.2 Düşüş · ≤-0.5 Güçlü Düşüş.

### Alarmlar (her 5 dakikada bir)
| Alarm | Koşul | Tekrar |
|---|---|---|
| 🚨 Ani düşüş / 🚀 ani yükseliş | 15 dk'da ±%2.5 veya 1 saatte ±%4 (BIST'te ayrıca gün içi ±%4) | 2 saatte bir |
| 📊 Yüksek hacim | Son 15 dk hacmi, son 24 saatin 15 dk'lık medyanının 4 katı | 2 saatte bir |
| 🔔 Teknik sinyal | Günlük SAR trend dönüşü, RSI 30'un altına / 70'in üstüne geçiş | Günde bir |
| 🎯 Fiyat hedefi | `/hedef` ile kurulan seviye kırıldığında | Bir kez |

İzlenen varlıklar: takip listesi + portföydekiler. BIST hisseleri yalnızca seans saatlerinde (hafta içi 10:00–18:10) kontrol edilir.
Eşikler `alerts.py` dosyasının başındadır.

### Tahmin karnesi
Her `/analiz`, `/hisse`, `/karsilastir` ve sabah özeti, tahminleri kaydeder (varlık + ufuk başına günde bir).
Süresi dolan tahmin saatlik olarak gerçek fiyatla karşılaştırılır:
- **Yön doğruluğu:** yükseliş/düşüş beklentisi tuttu mu (nötr tahminde fiyat aralıkta kaldı mı)
- **Aralık isabeti:** gerçekleşen fiyat tahmin edilen aralığın içinde mi

---

## 🔌 Veri kaynakları (hepsi ücretsiz, API anahtarı gerektirmez)

| Veri | Kaynak |
|---|---|
| Kripto fiyat/mum | Binance (yedek: Bybit) |
| Coin bilgisi, BTC dominansı | CoinGecko |
| BIST fiyat/mum, şirket bilgisi, USD/TRY | Yahoo Finance (`yfinance`, ~15 dk gecikmeli) |
| Korku/Açgözlülük endeksi | alternative.me |
| Haberler | Google Haberler RSS (Türkçe) |
| Çeviri | Google Translate ücretsiz uç noktası (yedek: MyMemory) |

---

## 🗂 Proje yapısı

| Dosya | Görevi |
|---|---|
| `bot.py` | Telegram komutları, zamanlanmış işler (alarm, özet, karne), webhook sunucusu |
| `analysis.py` | Gösterge puanlama, tahmin, rapor metni (`python analysis.py eth` ile terminalden test) |
| `indicators.py` | Gösterge formülleri (harici TA kütüphanesi yok) |
| `charts.py` | Grafik görseli (matplotlib) |
| `alerts.py` | Alarm kontrolleri ve eşikler |
| `features.py` | Takip listesi, hedef, portföy, karne, karşılaştırma, sabah özeti |
| `data.py` | Kripto (Binance/Bybit) ve BIST (Yahoo) mum verisi, SSL ayarları |
| `coininfo.py` · `stockinfo.py` | Coin ve şirket bilgi kartları |
| `market.py` | Piyasa havası, haberler, anlık fiyat |
| `translate.py` | Ücretsiz İngilizce → Türkçe çeviri |
| `store.py` | Veri katmanı: Supabase REST (yoksa yerel `store.json`) |
| `supabase_schema.sql` | Veritabanı tabloları |
| `render.yaml` | Render kurulum tanımı (ücretsiz plan, Frankfurt) |

---

## 🚀 Kurulum

### 1. Telegram botu
Telegram'da **@BotFather** → `/newbot` → bot adı ve kullanıcı adı → **token**'ı alın.

### 2. Supabase (veritabanı)
1. **SQL Editor**'e `supabase_schema.sql` içeriğini yapıştırıp **Run** deyin.
2. **Project Settings → API** sayfasından *Project URL* ve **service_role** anahtarını alın.

### 3. Render (sunucu)
1. GitHub ile giriş → **New + → Blueprint** → bu repo. `render.yaml` ayarları otomatik okur.
2. **Environment** bölümüne ekleyin:

| Değişken | Değer |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token'ı |
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_KEY` | Supabase **service_role** anahtarı (gizli tutun) |
| `ALLOWED_CHAT_IDS` | *(önerilir)* Sadece sizin chat ID'niz; boşsa bot herkese açık |
| `ALERT_CHAT_IDS` | *(eski ayar)* Varsa ilk açılışta veritabanına taşınır |

GitHub'a her gönderimde Render botu otomatik günceller (Auto-Deploy).

**Render ücretsiz plan notu:** 15 dakika istek gelmeyen servis uyur. Bot alarmları kaçırmamak için kendi `/health`
adresini 10 dakikada bir yoklayarak uyanık kalır; ayda 750 saatlik ücretsiz kullanım tek servis için yeterlidir.

### Yerelde çalıştırma
```bash
pip install -r requirements.txt
cp .env.example .env      # token'ı (ve isterseniz Supabase bilgilerini) yazın
python bot.py             # webhook adresi yoksa polling modunda çalışır
python analysis.py eth    # Telegram olmadan terminalde rapor
python analysis.py bist thyao
```
Supabase tanımlı değilse veriler yerel `store.json`'a yazılır (git'e gönderilmez).
Kurumsal ağlarda SSL denetimi için Windows sertifika deposu otomatik kullanılır (`truststore`).

---

## ⚠️ Sınırlar
- BIST verisi ~15 dakika gecikmelidir (anlık veri ücretli).
- Yahoo'nun BIST için F/K ve PD/DD oranları güvenilir olmadığından gösterilmez.
- Binance/Bybit'te USDT paritesi olmayan coinlerde yalnızca bilgi kartı gelir; 60 günden kısa geçmişi olanlarda teknik analiz yapılamaz.
- Çeviri ve haberler resmi olmayan ücretsiz uç noktalara dayanır; erişilemezse açıklama İngilizce gelir veya haber bölümü atlanır.
- Alarmlar en fazla 5 dakika gecikmeyle gelir.
