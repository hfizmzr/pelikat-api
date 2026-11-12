-- ── REPC: REQUIRE AN ACTIVE ORGANIZER (091) ─────────────────
-- repc_check_in_registration and merch_collect_for_runner authorized
-- non-admin callers on get_my_organizer_id() alone (the JWT
-- organizer_id). The access-token hook stamps organizer_id for
-- expired organizers too, so a lapsed organizer could still check
-- runners in and hand over merch until the session role caught up.
-- Both now also require public.organizers.is_active — the same rule
-- the hook derives 'organizer' vs 'expired' from. Admins unchanged.
-- repc_check_in_with_merch only calls these two, so it is covered.
--
-- get_my_organizer_id() itself is deliberately NOT changed: 24 RLS
-- policies and 9 other functions use it, and expired organizers must
-- still read their own rows (renewal / subscription-expired pages).
--
-- Bodies are copied from the live definitions (live is ahead of the
-- local 046/063 files); the only change in each is the new
-- is_active guard right after the existing ownership check.

create or replace function public.repc_check_in_registration(
  p_event_id uuid,
  p_runner_id uuid,
  p_bib_number text,
  p_is_proxy boolean default false,
  p_consent_code text default null::text,
  p_collected_by text default null::text
)
  returns table(success boolean, message text, registration_id uuid, bib_number text, runner_name text, shirt_size text, remaining_qty integer, already_checked_in boolean)
  language plpgsql
  security definer
  set search_path = ''
  as $function$
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

  if not v_is_admin and not exists (
    select 1 from public.organizers o where o.id = v_org_id and o.is_active
  ) then
    return query select false::boolean, 'Organizer subscription is not active'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false::boolean;
    return;
  end if;

  if v_reg.checked_in then
    select esi.initial_qty - esi.claimed_qty
    into v_remaining
    from public.event_shirt_inventory esi
    where esi.event_id = v_reg.event_id
      and esi.size = v_reg.t_shirt_size;

    return query select true::boolean, 'Runner is already checked in'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, v_remaining, true::boolean;
    return;
  end if;

  if p_is_proxy then
    if p_consent_code is null or trim(p_consent_code) = '' then
      return query select false::boolean, 'Proxy collection requires a consent code'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false::boolean;
      return;
    end if;

    select cc.id
    into v_consent_id
    from public.repc_consent_codes cc
    where cc.registration_id = v_reg.id
      and cc.code = upper(trim(p_consent_code))
      and cc.is_used = false
      and cc.expires_at > now()
    for update of cc;

    if not found then
      return query select false::boolean, 'Invalid or expired consent code'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, null::integer, false::boolean;
      return;
    end if;
  end if;

  if v_reg.t_shirt_size is not null then
    select esi.id, esi.initial_qty - esi.claimed_qty
    into v_inventory_id, v_remaining
    from public.event_shirt_inventory esi
    where esi.event_id = v_reg.event_id
      and esi.size = v_reg.t_shirt_size
    for update of esi;

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
    collected_by_user_id,
    event_id,
    runner_id,
    bib_number,
    runner_name,
    collection_method,
    qr_verified_at,
    remaining_qty_after,
    staff_email,
    notes
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
    auth.uid(),
    v_reg.event_id,
    v_reg.runner_id,
    v_reg.bib_number,
    v_reg.full_name,
    case when p_is_proxy then 'proxy' else 'self' end,
    now(),
    v_remaining,
    p_collected_by,
    case when p_is_proxy then 'Proxy collection by ' || coalesce(p_collected_by, auth.uid()::text) else null end
  )
  on conflict on constraint repc_collections_registration_id_unique do nothing;

  if v_consent_id is not null then
    update public.repc_consent_codes
    set is_used = true,
        used_at = now()
    where id = v_consent_id;
  end if;

  return query select true::boolean, 'Check-in successful'::text, v_reg.id, v_reg.bib_number, v_reg.full_name, v_reg.t_shirt_size, v_remaining, false::boolean;
end;
$function$;

create or replace function public.merch_collect_for_runner(
  p_event_id uuid,
  p_runner_id uuid
)
  returns table(order_id uuid, order_number text, item_count integer, collected boolean)
  language plpgsql
  security definer
  set search_path = ''
  as $function$
declare
  v_organizer_id uuid := public.get_my_organizer_id();
  v_is_admin boolean := coalesce(public.is_admin(), false);
  v_event_organizer uuid;
  v_collected uuid[];
begin
  select e.organizer_id into v_event_organizer
  from public.events e
  where e.id = p_event_id;

  if v_event_organizer is null
     or (not v_is_admin and (v_organizer_id is null or v_event_organizer <> v_organizer_id)) then
    raise exception 'Not allowed to hand over merch for this event';
  end if;

  if not v_is_admin and not exists (
    select 1 from public.organizers o where o.id = v_organizer_id and o.is_active
  ) then
    raise exception 'Organizer subscription is not active';
  end if;

  perform 1
  from public.registrations r
  where r.event_id = p_event_id
    and r.runner_id = p_runner_id
    and r.checked_in
  for update of r;

  if not found then
    raise exception 'Runner has not checked in at REPC for this event';
  end if;

  with done as (
    update public.merch_orders mo
    set status       = 'collected',
        collected_at = now(),
        updated_at   = now()
    where mo.event_id = p_event_id
      and mo.runner_id = p_runner_id
      and mo.status = 'ready'
    returning mo.id
  )
  select coalesce(array_agg(done.id), '{}') into v_collected from done;

  if cardinality(v_collected) > 0 then
    insert into public.audit_log (actor_id, actor_email, action, target_id, metadata)
    select auth.uid(), u.email, 'merch.collected_at_repc', p_runner_id,
           jsonb_build_object('event_id', p_event_id, 'order_ids', to_jsonb(v_collected))
    from (select 1) s
    left join auth.users u on u.id = auth.uid();
  end if;

  return query
  select mo.id, mo.order_number,
         coalesce((select sum(oi.quantity)::int from public.merch_order_items oi
                   where oi.order_id = mo.id), 0),
         mo.id = any(v_collected)
  from public.merch_orders mo
  where mo.event_id = p_event_id
    and mo.runner_id = p_runner_id
    and (mo.id = any(v_collected) or mo.status = 'confirmed')
  order by mo.created_at;
end;
$function$;
