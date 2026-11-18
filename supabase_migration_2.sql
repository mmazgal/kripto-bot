-- 2. güncelleme: KAP/takvim bildirimleri ve sanal portföy tabloları.
-- Supabase > SQL Editor'e yapıştırıp "Run" deyin (bir kez).

create table if not exists notified (
  key  text primary key,               -- tekrar bildirim engeli (takvim, KAP haberleri)
  at   timestamptz not null default now()
);

create table if not exists paper_state (
  market      text primary key,        -- 'crypto' | 'bist'
  cash        numeric not null,
  start_cash  numeric not null,
  started_at  timestamptz not null default now(),
  bench       text                     -- başlangıç fiyatları (JSON), al-ve-tut kıyası için
);

create table if not exists paper_positions (
  market       text not null,
  item         text not null,
  qty          numeric not null,
  entry_price  numeric not null,
  entry_at     timestamptz not null default now(),
  primary key (market, item)
);

create table if not exists paper_trades (
  id      bigserial primary key,
  market  text not null,
  item    text not null,
  side    text not null,                -- 'AL' | 'SAT'
  qty     numeric not null,
  price   numeric not null,
  at      timestamptz not null default now(),
  reason  text,
  pnl     numeric
);

alter table notified        enable row level security;
alter table paper_state     enable row level security;
alter table paper_positions enable row level security;
alter table paper_trades    enable row level security;
