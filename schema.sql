-- Esquema de Supabase (Postgres) de enable-banking: refleja lo que hay en producción.
--
-- Los roles y sus contraseñas NO se guardan en el repo. Antes de aplicar este fichero en un
-- entorno nuevo, créalos a mano en el SQL Editor (looker es el usuario de Looker Studio):
--   create role app_fetcher login password '<contraseña>';   -- fetch.py: leer, insertar, actualizar
--   create role app_backup  login password '<contraseña>';   -- backup.py: solo leer
--   create role looker      login password '<contraseña>';

create table if not exists public.transactions (
    id           bigint generated always as identity primary key,
    account      text    not null default 'historico',
    booking_date date    not null,
    amount       numeric not null,
    currency     text    not null default 'EUR',
    category     text,
    description  text,
    source       text    not null default 'manual',
    external_id  text,
    raw          jsonb,
    is_transfer  boolean not null default false
);

-- De dónde sale la categoría: 'regla' (categorias.yml), 'historial' (categorize.py), 'ia' (ia.py), 'viaje'
-- (viajes.py) o 'manual'. Vacía en lo anterior a esta columna. Una regla puede corregir lo que puso el historial
-- o la IA, nada más.
alter table public.transactions add column if not exists category_source text;
alter table public.transactions drop constraint if exists transactions_category_source_check;
alter table public.transactions add constraint transactions_category_source_check
    check (category_source in ('regla', 'historial', 'ia', 'viaje', 'manual'));

-- Nombre del viaje (viajes.fechas de categorias.yml) de los gastos que viajes.py ha puesto en Viajes.
alter table public.transactions add column if not exists viaje text;

-- Evita duplicados al relanzar fetch.py (on conflict (account, external_id)).
create unique index if not exists transactions_external_uq on public.transactions (account, external_id);
create index if not exists transactions_date_idx on public.transactions (booking_date);

-- RLS activado y sin políticas para anon/authenticated: la API pública de Supabase no ve nada.
alter table public.transactions enable row level security;

drop policy if exists looker_read on public.transactions;
create policy looker_read on public.transactions for select to looker using (true);

drop policy if exists app_fetcher_select on public.transactions;
create policy app_fetcher_select on public.transactions for select to app_fetcher using (true);

drop policy if exists app_fetcher_insert on public.transactions;
create policy app_fetcher_insert on public.transactions for insert to app_fetcher with check (true);

drop policy if exists app_fetcher_update on public.transactions;
create policy app_fetcher_update on public.transactions for update to app_fetcher using (true) with check (true);

drop policy if exists app_backup_select on public.transactions;
create policy app_backup_select on public.transactions for select to app_backup using (true);

-- Permisos mínimos: ninguno de los dos roles puede borrar y app_fetcher solo puede cambiar las columnas que fetch.py
-- actualiza (nunca el importe, la cuenta ni el id externo): si se filtrara su contraseña no podrían reescribirse importes.
grant select, insert on public.transactions to app_fetcher;
revoke update on public.transactions from app_fetcher;
grant update (booking_date, category, category_source, is_transfer, viaje) on public.transactions to app_fetcher;
grant select on public.transactions to app_backup;

-- Vistas para Looker Studio. security_invoker: se ejecutan con los permisos de quien consulta, así que
-- siguen mandando la RLS de transactions y el rol looker solo ve lo que su política le deja ver.
-- Los traspasos entre cuentas propias (is_transfer) no cuentan como gasto ni ingreso. Solo se suman movimientos
-- en EUR: sin conversión, sumar otras monedas daría cifras falsas (fetch.py avisa si llega alguno).
-- looker lee la tabla por columnas y sin `raw` (JSON completo del banco: IBAN de la contraparte, referencias...).
revoke select on public.transactions from looker;
grant select (id, account, booking_date, amount, currency, category, description, source, external_id, is_transfer,
              category_source, viaje) on public.transactions to looker;

-- Gasto (en positivo) por mes, cuenta y categoría.
create or replace view public.v_gasto_mensual_categoria with (security_invoker = true) as
select date_trunc('month', booking_date)::date as mes,
       account,
       coalesce(category, 'Sin categoría') as categoria,
       -sum(amount) as gasto,
       count(*) as movimientos
from public.transactions
where amount < 0 and not is_transfer and currency = 'EUR'
group by 1, 2, 3;

-- Ingresos, gastos y ahorro (ingresos - gastos) por mes, todas las cuentas juntas.
create or replace view public.v_ahorro_mensual with (security_invoker = true) as
select date_trunc('month', booking_date)::date as mes,
       coalesce(sum(amount) filter (where amount > 0), 0) as ingresos,
       -coalesce(sum(amount) filter (where amount < 0), 0) as gastos,
       sum(amount) as ahorro
from public.transactions
where not is_transfer and currency = 'EUR'
group by 1;

-- Saldo de partida de cada cuenta, antes de su primer movimiento guardado. Es el dato que falta para que los
-- saldos cuadren con el banco: saldo del banco menos la suma de lo guardado. Se rellena a mano (los importes
-- no se guardan en el repo) y las cuentas que no estén aquí, como `historico`, no salen en las vistas de saldo.
create table if not exists public.saldos_iniciales (
    account text    primary key,
    saldo   numeric not null
);
alter table public.saldos_iniciales enable row level security;
drop policy if exists looker_read on public.saldos_iniciales;
create policy looker_read on public.saldos_iniciales for select to looker using (true);
grant select on public.saldos_iniciales to looker;

-- Se rellena a mano y no se puede regenerar: backup.py también la exporta, con app_backup (solo lectura).
drop policy if exists app_backup_select on public.saldos_iniciales;
create policy app_backup_select on public.saldos_iniciales for select to app_backup using (true);
grant select on public.saldos_iniciales to app_backup;

-- fetch.py compara con el saldo del banco el que sale de saldos_iniciales + movimientos (solo lectura).
drop policy if exists app_fetcher_select on public.saldos_iniciales;
create policy app_fetcher_select on public.saldos_iniciales for select to app_fetcher using (true);
grant select on public.saldos_iniciales to app_fetcher;

-- Saldo de cada cuenta al final de cada mes: saldo inicial + movimientos guardados hasta entonces
-- (traspasos incluidos, porque sí mueven dinero entre cuentas).
create or replace view public.v_saldo_mensual with (security_invoker = true) as
select m.account,
       m.mes,
       s.saldo + sum(m.neto) over (partition by m.account order by m.mes) as saldo
from (
    select account, date_trunc('month', booking_date)::date as mes, sum(amount) as neto
    from public.transactions
    where currency = 'EUR'
    group by 1, 2
) m
join public.saldos_iniciales s on s.account = m.account;

-- Saldo de cada cuenta ahora (hasta el último movimiento guardado).
create or replace view public.v_saldo_actual with (security_invoker = true) as
select s.account,
       s.saldo + coalesce(sum(t.amount), 0) as saldo,
       max(t.booking_date) as ultimo_movimiento
from public.saldos_iniciales s
left join public.transactions t on t.account = s.account and t.currency = 'EUR'
group by s.account, s.saldo;

-- Cuánto costó cada viaje (solo lo que viajes.py puso en Viajes: lo pagado antes, como un vuelo, no cuenta).
create or replace view public.v_gasto_por_viaje with (security_invoker = true) as
select viaje,
       min(booking_date) as desde,
       max(booking_date) as hasta,
       -sum(amount) as gasto,
       count(*) as movimientos
from public.transactions
where viaje is not null and amount < 0 and not is_transfer and currency = 'EUR'
group by 1;

grant select on public.v_gasto_mensual_categoria, public.v_ahorro_mensual, public.v_saldo_mensual,
    public.v_saldo_actual, public.v_gasto_por_viaje to looker;
