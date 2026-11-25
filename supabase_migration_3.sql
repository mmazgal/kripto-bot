-- 3. güncelleme: alarm öğrenme döngüsü (learn.py) tabloları.
-- Supabase > SQL Editor'e yapıştırıp "Run" deyin (bir kez).

create table if not exists alert_events (
  id           bigserial primary key,
  at           timestamptz not null default now(),
  item         text    not null,           -- 'BTC' veya 'BIST:THYAO'
  kind         text    not null,           -- drop | pump | volume | day_drop | day_pump | funding | sar | rsi30 ... | candidate
  sent         boolean not null default false,  -- alarm gönderildi mi (candidate = eşiğe yaklaşan, gönderilmeyen)
  cfg_version  int     not null default 0, -- o an geçerli eşik ayarı sürümü
  price        numeric not null,
  ch15         numeric,                    -- 15 dk değişim %
  ch1h         numeric,                    -- 1 saat değişim %
  day_ch       numeric,                    -- günlük değişim %
  vol_x        numeric,                    -- hacim katı (saat normaline göre)
  hour         smallint,                   -- TSİ saat
  stage        smallint not null default 0,-- 0: ölçülmedi, 1: 4 saat sonucu var, 2: 24 saat sonucu da var
  typ4         numeric,                    -- varlığın tipik 4 saatlik mutlak hareketi %
  typ24        numeric,                    -- tipik günlük mutlak hareket %
  ret1h        numeric,
  ret4h        numeric,
  up4          numeric,                    -- 4 saat içindeki en yüksek %
  dn4          numeric,                    -- 4 saat içindeki en düşük %
  ret24h       numeric,
  up24         numeric,
  dn24         numeric
);
-- Oynaklığa göre eşikler + trend alarmı + geçmişi yeniden oynatma için sütunlar.
-- (Bu dosyayı daha önce çalıştırdıysanız tekrar çalıştırmanız yeterli; var olanlara dokunmaz.)
alter table alert_events add column if not exists ch2h   numeric;   -- 2 saat değişim %
alter table alert_events add column if not exists z15    numeric;   -- 15 dk değişim / tipik 15 dk hareket
alter table alert_events add column if not exists z1h    numeric;
alter table alert_events add column if not exists z2h    numeric;
alter table alert_events add column if not exists vol2x  numeric;   -- son 2 saat hacmi / normal
alter table alert_events add column if not exists adx    numeric;   -- 15 dk ADX
alter table alert_events add column if not exists adx_up boolean;
alter table alert_events add column if not exists source text not null default 'live';  -- live | backfill

create index if not exists alert_events_at_idx    on alert_events (at);
create index if not exists alert_events_stage_idx on alert_events (stage) where stage < 2;

create table if not exists alert_feedback (
  event_id  bigint   not null,
  chat_id   bigint   not null,
  vote      smallint not null,               -- 1 = işe yaradı, -1 = yaramadı
  at        timestamptz not null default now(),
  primary key (event_id, chat_id)
);

create table if not exists alert_config (
  key         text primary key,              -- 'thresholds'
  version     int  not null,
  params      text not null,                 -- JSON: DROP_15M, DROP_1H, VOLUME_X, VOLUME_MIN_MOVE
  updated_at  timestamptz not null default now()
);

create table if not exists tune_runs (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  definition  text,
  old_params  text,
  new_params  text,
  applied     boolean not null default false,
  metrics     text,
  ai_text     text
);

alter table alert_events   enable row level security;
alter table alert_feedback enable row level security;
alter table alert_config   enable row level security;
alter table tune_runs      enable row level security;
