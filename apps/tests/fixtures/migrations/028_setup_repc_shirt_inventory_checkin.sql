begin;

-- REPC shirt inventory + atomic check-in support.

create table if not exists public.event_shirt_inventory (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.events(id) on delete cascade,
  organizer_id uuid not null references public.organizers(id) on delete cascade,
  size text not null check (size in ('XS', 'S', 'M', 'L', 'XL', 'XXL')),
  initial_qty integer not null default 0 check (initial_qty >= 0),
  claimed_qty integer not null default 0 check (claimed_qty >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (claimed_qty <= initial_qty),
  unique (event_id, size)
);

alter table public.event_shirt_inventory enable row level security;

drop policy if exists "organizer manages event shirt inventory" on public.event_shirt_inventory;

create policy "organizer manages event shirt inventory"
on public.event_shirt_inventory
for all
using (
  organizer_id = public.get_my_organizer_id()
  or public.is_admin()
)
with check (
  organizer_id = public.get_my_organizer_id()
  or public.is_admin()
);

create or replace function public.touch_event_shirt_inventory_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists event_shirt_inventory_touch_updated_at
on public.event_shirt_inventory;

create trigger event_shirt_inventory_touch_updated_at
before update on public.event_shirt_inventory
for each row
execute function public.touch_event_shirt_inventory_updated_at();

-- Add REPC collection metadata.
alter table public.repc_collections
add column if not exists shirt_size text check (
  shirt_size is null or shirt_size in ('XS', 'S', 'M', 'L', 'XL', 'XXL')
),
add column if not exists shirt_inventory_id uuid references public.event_shirt_inventory(id),
add column if not exists collected_by_user_id uuid references auth.users(id);

create unique index if not exists repc_collections_one_per_registration_idx
on public.repc_collections (registration_id);

-- Seed zero-quantity shirt rows for existing events.
-- Organizer can update quantities later.
insert into public.event_shirt_inventory (event_id, organizer_id, size, initial_qty, claimed_qty)
select e.id, e.organizer_id, s.size, 0, 0
from public.events e
cross join (
  values ('XS'), ('S'), ('M'), ('L'), ('XL'), ('XXL')
) as s(size)
on conflict (event_id, size) do nothing;

create or replace function public.repc_check_in_registration(
  p_event_id uuid,
  p_runner_id uuid,
  p_bib_number text,
  p_is_proxy boolean default false,
  p_consent_code text default null,
  p_collected_by text default null
)
returns table (
  success boolean,
  message text,
  registration_id uuid,
  bib_number text,
  runner_name text,
  shirt_size text,
  remaining_qty integer,
  already_checked_in boolean
)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_reg record;
  v_org_id uuid := public.get_my_organizer_id();
  v_is_admin boolean := coalesce(public.is_admin(), false);
  v_inventory_id uuid;
  v_remaining integer;
  v_consent_id uuid;
begin
  select
    r.id,
    r.event_id,
    r.organizer_id,
    r.runner_id,
    r.bib_number,
    r.checked_in,
    rp.full_name,
    nullif(rp.t_shirt_size, '') as t_shirt_size
  into v_reg
  from public.registrations r
  left join public.runner_profiles rp on rp.id = r.runner_id
  where r.event_id = p_event_id
    and r.runner_id = p_runner_id
    and r.bib_number = p_bib_number
  for update of r;

  if not found then
    return query select false, 'Registration not found', null::uuid, null::text, null::text, null::text, null::integer, false;
    return;
  end if;

  if not v_is_admin and (v_org_id is null or v_reg.organizer_id <> v_org_id) then
    return query select false, 'Not allowed to check in this registration', v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false;
    return;
  end if;

  if v_reg.checked_in then
    select initial_qty - claimed_qty
    into v_remaining
    from public.event_shirt_inventory
    where event_id = v_reg.event_id
      and size = v_reg.t_shirt_size;

    return query select true, 'Runner is already checked in', v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, v_remaining, true;
    return;
  end if;

  if p_is_proxy then
    if p_consent_code is null or trim(p_consent_code) = '' then
      return query select false, 'Proxy collection requires a consent code', v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false;
      return;
    end if;

    select id
    into v_consent_id
    from public.repc_consent_codes
    where registration_id = v_reg.id
      and code = upper(trim(p_consent_code))
      and is_used = false
      and expires_at > now()
    for update;

    if not found then
      return query select false, 'Invalid or expired consent code', v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false;
      return;
    end if;
  end if;

  if v_reg.t_shirt_size is not null then
    select id, initial_qty - claimed_qty
    into v_inventory_id, v_remaining
    from public.event_shirt_inventory
    where event_id = v_reg.event_id
      and size = v_reg.t_shirt_size
    for update;

    if not found then
      return query select false, 'No shirt inventory configured for this size', v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false;
      return;
    end if;

    if v_remaining <= 0 then
      return query select false, 'No shirts remaining for this size', v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, 0, false;
      return;
    end if;

    update public.event_shirt_inventory
    set claimed_qty = claimed_qty + 1
    where id = v_inventory_id
    returning initial_qty - claimed_qty into v_remaining;
  end if;

  update public.registrations
  set checked_in = true,
      checked_in_at = now()
  where id = v_reg.id;

  insert into public.repc_collections (
    registration_id,
    organizer_id,
    collected_by,
    collected_at,
    is_proxy,
    consent_code_id,
    shirt_size,
    shirt_inventory_id,
    collected_by_user_id
  )
  values (
    v_reg.id,
    v_reg.organizer_id,
    coalesce(p_collected_by, auth.uid()::text),
    now(),
    p_is_proxy,
    v_consent_id,
    v_reg.t_shirt_size,
    v_inventory_id,
    auth.uid()
  )
  on conflict (registration_id) do nothing;

  if v_consent_id is not null then
    update public.repc_consent_codes
    set is_used = true
    where id = v_consent_id;
  end if;

  return query select true, 'Check-in successful', v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, v_remaining, false;
end;
$$;

grant execute on function public.repc_check_in_registration(
  uuid,
  uuid,
  text,
  boolean,
  text,
  text
) to authenticated;

commit;
