-- Supabase > SQL Editor'e yapıştırıp "Run" deyin. Bir kez çalıştırmanız yeterli.

create table if not exists settings (
  chat_id     bigint primary key,
  alerts_on   boolean not null default true,
  summary_on  boolean not null default true
);

create table if not exists watchlist (
  chat_id  bigint not null,
  item     text   not null,          -- 'BTC' veya 'BIST:THYAO'
  primary key (chat_id, item)
);

create table if not exists price_targets (
  id          bigserial primary key,
  chat_id     bigint  not null,
  item        text    not null,
  target      numeric not null,
  direction   text    not null,       -- 'above' | 'below'
  created_at  timestamptz not null default now(),
  triggered   boolean not null default false
);

create table if not exists portfolio (
  id         bigserial primary key,
  chat_id    bigint  not null,
  item       text    not null,
  amount     numeric not null,
  buy_price  numeric not null,
  created_at timestamptz not null default now()
);

create table if not exists predictions (
  id            bigserial primary key,
  item          text    not null,
  horizon_days  int     not null,     -- 7 veya 30
  created_date  date    not null,
  created_at    timestamptz not null default now(),
  due_at        timestamptz not null,
  price         numeric not null,
  score         numeric not null,
  target        numeric not null,
  low           numeric not null,
  high          numeric not null,
  resolved      boolean not null default false,
  final_price   numeric,
  correct       boolean,
  in_range      boolean,
  unique (item, horizon_days, created_date)
);

-- Güvenlik: tablolara sadece bot (service_role anahtarı) erişebilsin.
alter table settings      enable row level security;
alter table watchlist     enable row level security;
alter table price_targets enable row level security;
alter table portfolio     enable row level security;
alter table predictions   enable row level security;
