alter table public.repc_collections
  add constraint repc_collections_registration_id_unique unique (registration_id);

create or replace function public.repc_check_in_registration(
  p_event_id uuid,
  p_runner_id uuid,
  p_bib_number text,
  p_is_proxy boolean default false,
  p_consent_code text default null,
  p_collected_by text default null
) returns table(
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
  set search_path = ''
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
    return query select false::boolean, 'Registration not found'::text, null::uuid, null::text, null::text, null::text, null::integer, false::boolean;
    return;
  end if;

  if not v_is_admin and (v_org_id is null or v_reg.organizer_id <> v_org_id) then
    return query select false::boolean, 'Not allowed to check in this registration'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false::boolean;
    return;
  end if;

  if v_reg.checked_in then
    select initial_qty - claimed_qty
    into v_remaining
    from public.event_shirt_inventory
    where event_id = v_reg.event_id
      and size = v_reg.t_shirt_size;

    return query select true::boolean, 'Runner is already checked in'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, v_remaining, true::boolean;
    return;
  end if;

  if p_is_proxy then
    if p_consent_code is null or trim(p_consent_code) = '' then
      return query select false::boolean, 'Proxy collection requires a consent code'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false::boolean;
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
      return query select false::boolean, 'Invalid or expired consent code'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false::boolean;
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
      return query select false::boolean, 'No shirt inventory configured for this size'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false::boolean;
      return;
    end if;

    if v_remaining <= 0 then
      return query select false::boolean, 'No shirts remaining for this size'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, 0, false::boolean;
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
  on conflict on constraint repc_collections_registration_id_unique do nothing;

  if v_consent_id is not null then
    update public.repc_consent_codes
    set is_used = true
    where id = v_consent_id;
  end if;

  return query select true::boolean, 'Check-in successful'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, v_remaining, false::boolean;
end;
$$;
