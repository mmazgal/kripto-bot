-- 4. güncelleme: dip/tepe dönüşü alarmının ölçümleri (alert_events'e sütunlar).
-- Supabase > SQL Editor'e yapıştırıp "Run" deyin (bir kez; tekrar çalıştırmak zararsızdır).
-- Çalıştırılmasa da bot çalışır ve kayıt tutar; yalnız dönüş alarmı öğrenme döngüsünde ayarlanamaz.

alter table alert_events add column if not exists zdd      numeric;  -- son 4 saatin zirvesinden düşüş / tipik 2s hareket
alter table alert_events add column if not exists zru      numeric;  -- son 4 saatin dibinden yükseliş / tipik 2s hareket
alter table alert_events add column if not exists bounce   numeric;  -- son 1 saatin dibinden sekme / tipik 15 dk hareket
alter table alert_events add column if not exists pullback numeric;  -- son 1 saatin tepesinden geri çekilme / tipik 15 dk
alter table alert_events add column if not exists rsi15    numeric;  -- 15 dk RSI (son tamamlanmış mum)
alter table alert_events add column if not exists volpk    numeric;  -- son 1 saatteki en yüksek hacim katı
