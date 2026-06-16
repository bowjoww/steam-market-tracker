-- =============================================================================
-- Steam Market Tracker (Team VIX) — schema multi-item / multi-usuario
-- Idempotente: pode rodar quantas vezes quiser.
-- Roda no projeto Supabase de voces (tabelas com prefixo steam_, isoladas do resto).
-- =============================================================================

create extension if not exists pgcrypto;

-- ----------------------------------------------------------------------------- itens acompanhados
create table if not exists steam_watches (
  id                  uuid primary key default gen_random_uuid(),
  created_at          timestamptz not null default now(),
  -- item
  steam_url           text not null,
  appid               int  not null,
  market_hash_name    text not null,
  currency            int  not null default 7,        -- 7 = BRL
  currency_label      text not null default 'BRL',
  label               text,                            -- nome amigavel pra exibir
  icon_url            text,
  -- dono / alerta
  discord_user_id     text,                            -- ID numerico p/ @mencao
  discord_handle      text,                            -- so pra exibir
  -- parametros do stop movel
  sell_threshold_pct  numeric not null default 10,
  watch_threshold_pct numeric not null default 6,
  rearm_threshold_pct numeric not null default 4,
  smoothing_window    int     not null default 3,
  active              boolean not null default true,
  -- estado (atualizado pelo poller)
  current_floor       numeric,
  smoothed            numeric,
  peak                numeric,
  drawdown_pct        numeric,
  stop_price          numeric,
  signal              text not null default 'HOLD',
  trend               text,
  last_alert_signal   text not null default 'HOLD',
  last_checked_at     timestamptz,
  last_error          text
);

-- evita o mesmo item duplicado para o mesmo dono
create unique index if not exists steam_watches_uniq
  on steam_watches (appid, market_hash_name, coalesce(discord_user_id, ''));

-- ----------------------------------------------------------------------------- historico de precos
create table if not exists steam_price_points (
  id          bigint generated always as identity primary key,
  watch_id    uuid not null references steam_watches(id) on delete cascade,
  ts          timestamptz not null default now(),
  floor       numeric not null,
  median_sale numeric,
  volume      text
);

create index if not exists steam_price_points_watch_ts
  on steam_price_points (watch_id, ts);

-- ----------------------------------------------------------------------------- RLS
alter table steam_watches      enable row level security;
alter table steam_price_points enable row level security;

-- leitura publica (painel) ----------------------------------------------------
drop policy if exists steam_watches_read on steam_watches;
create policy steam_watches_read on steam_watches for select using (true);

drop policy if exists steam_points_read on steam_price_points;
create policy steam_points_read on steam_price_points for select using (true);

-- insercao publica (formulario do site): so INSERT; estado fica no default -----
drop policy if exists steam_watches_insert on steam_watches;
create policy steam_watches_insert on steam_watches for insert with check (true);

-- (sem policy de UPDATE/DELETE para anon -> so o service_role do poller/bot mexe)

-- ----------------------------------------------------------------------------- grants
-- RLS sozinha nao basta no Supabase: precisa GRANT de tabela tambem.
grant select, insert on steam_watches            to anon, authenticated;
grant select          on steam_price_points       to anon, authenticated;
grant all             on steam_watches, steam_price_points to service_role;
