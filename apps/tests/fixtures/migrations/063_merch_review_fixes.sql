-- ============================================================================
-- 063 — Merch review fixes (PR #22 CodeRabbit findings on 062)
--
--  • merch_restock_orders(): lock variant rows in id order before restocking,
--    the same order merch_checkout() uses, so concurrent checkouts,
--    cancellations and the expiry sweep cannot deadlock each other.
--  • repc_check_in_with_merch(): REPC check-in and merch hand-over in ONE
--    transaction. Previously the app called repc_check_in_registration() and
--    then merch_collect_for_runner() separately, so a failed hand-over still
--    reported a successful check-in. Now if the hand-over fails, the check-in
--    rolls back too and the operator sees the error.
--  • merch_collect_for_runner(): internal only (no direct execute for
--    authenticated) and refuses runners who are not checked in.
--
--  062 is already applied, so these changes are a new migration rather than
--  edits to 062. repc_check_in_registration() itself is unchanged.
-- ============================================================================

-- ── RESTOCK: deterministic lock order ───────────────────────────────────────
create or replace function public.merch_restock_orders(p_order_ids uuid[])
returns void
language plpgsql
security definer
set search_path to ''
as $$
begin
  perform 1
  from public.merch_variants v
  where v.id in (
    select oi.variant_id
    from public.merch_order_items oi
    where oi.order_id = any(p_order_ids)
  )
  order by v.id
  for update of v;

  update public.merch_variants v
  set stock = v.stock + r.qty
  from (
    select variant_id, sum(quantity)::int as qty
    from public.merch_order_items
    where order_id = any(p_order_ids)
    group by variant_id
  ) r
  where v.id = r.variant_id;
end;
$$;

revoke all on function public.merch_restock_orders(uuid[]) from public, anon, authenticated;

-- ── REPC HAND-OVER (internal) ───────────────────────────────────────────────
-- Same as 062, plus: the runner must be checked in for this event. Only
-- called from repc_check_in_with_merch(); organizers mark orders collected
-- by hand through merch_update_order_status().
create or replace function public.merch_collect_for_runner(
  p_event_id uuid,
  p_runner_id uuid
)
returns table (order_id uuid, order_number text, item_count int, collected boolean)
language plpgsql
security definer
set search_path to ''
as $$
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
$$;

revoke all on function public.merch_collect_for_runner(uuid, uuid) from public, anon, authenticated;

-- ── REPC CHECK-IN + MERCH HAND-OVER (one transaction) ───────────────────────
-- Same arguments and result columns as repc_check_in_registration(), plus
-- `merch`: [{order_number, item_count, collected}] — orders handed over at
-- this scan (collected = true) and paid orders not packed yet (false).
-- Runs on re-scans of an already checked-in runner too, so orders marked
-- ready after the first scan are still handed over. The registration row
-- stays locked (by repc_check_in_registration) until the transaction ends,
-- so concurrent scans of the same runner are serialised.
create or replace function public.repc_check_in_with_merch(
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
  already_checked_in boolean,
  merch jsonb
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_check record;
  v_merch jsonb;
begin
  select c.* into v_check
  from public.repc_check_in_registration(
    p_event_id, p_runner_id, p_bib_number, p_is_proxy, p_consent_code, p_collected_by
  ) c;

  if coalesce(v_check.success, false) then
    select coalesce(
             jsonb_agg(jsonb_build_object(
               'order_number', m.order_number,
               'item_count', m.item_count,
               'collected', m.collected
             )),
             '[]'::jsonb
           )
    into v_merch
    from public.merch_collect_for_runner(p_event_id, p_runner_id) m;
  end if;

  return query select
    v_check.success, v_check.message, v_check.registration_id, v_check.bib_number,
    v_check.runner_name, v_check.shirt_size, v_check.remaining_qty,
    v_check.already_checked_in, v_merch;
end;
$$;

revoke all on function public.repc_check_in_with_merch(uuid, uuid, text, boolean, text, text) from public, anon;
grant execute on function public.repc_check_in_with_merch(uuid, uuid, text, boolean, text, text) to authenticated;
