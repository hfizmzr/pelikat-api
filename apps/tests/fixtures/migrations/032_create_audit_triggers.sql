-- Enhance audit_log table with snapshot columns and actor denormalization
alter table public.audit_log
  add column if not exists old_data jsonb,
  add column if not exists new_data jsonb,
  add column if not exists actor_email text,
  add column if not exists actor_name text;

-- Create audit schema for helper functions
create schema if not exists audit;

-- Trigger function: captures full OLD and NEW row snapshots
-- Stores them in old_data / new_data JSONB columns for immutable history
create or replace function audit.log_change()
returns trigger as $$
begin
  insert into public.audit_log (
    actor_id,
    action,
    target_id,
    old_data,
    new_data,
    metadata
  )
  values (
    null, -- actor_id is set by app-level logging; triggers can't reliably know the user
    TG_OP || ' ' || TG_TABLE_NAME,
    coalesce(new.id, old.id),
    case when TG_OP in ('UPDATE', 'DELETE') then to_jsonb(old) else null end,
    case when TG_OP in ('INSERT', 'UPDATE') then to_jsonb(new) else null end,
    jsonb_build_object(
      'table', TG_TABLE_NAME,
      'operation', TG_OP,
      'schema', TG_TABLE_SCHEMA
    )
  );

  return coalesce(new, old);
end;
$$ language plpgsql security definer;

-- Attach triggers to core tables
-- Organizers
drop trigger if exists audit_organizers on public.organizers;
create trigger audit_organizers
  after insert or update or delete on public.organizers
  for each row execute function audit.log_change();

-- Events
drop trigger if exists audit_events on public.events;
create trigger audit_events
  after insert or update or delete on public.events
  for each row execute function audit.log_change();

-- Race categories
drop trigger if exists audit_race_categories on public.race_categories;
create trigger audit_race_categories
  after insert or update or delete on public.race_categories
  for each row execute function audit.log_change();

-- Registrations
drop trigger if exists audit_registrations on public.registrations;
create trigger audit_registrations
  after insert or update or delete on public.registrations
  for each row execute function audit.log_change();

-- Runner profiles
drop trigger if exists audit_runner_profiles on public.runner_profiles;
create trigger audit_runner_profiles
  after insert or update or delete on public.runner_profiles
  for each row execute function audit.log_change();

-- Merch products
drop trigger if exists audit_merch_products on public.merch_products;
create trigger audit_merch_products
  after insert or update or delete on public.merch_products
  for each row execute function audit.log_change();

-- Merch orders
drop trigger if exists audit_merch_orders on public.merch_orders;
create trigger audit_merch_orders
  after insert or update or delete on public.merch_orders
  for each row execute function audit.log_change();

-- Photo tags
drop trigger if exists audit_photo_tags on public.photo_tags;
create trigger audit_photo_tags
  after insert or update or delete on public.photo_tags
  for each row execute function audit.log_change();

-- Repc collections
drop trigger if exists audit_repc_collections on public.repc_collections;
create trigger audit_repc_collections
  after insert or update or delete on public.repc_collections
  for each row execute function audit.log_change();

-- Repc consent codes
drop trigger if exists audit_repc_consent_codes on public.repc_consent_codes;
create trigger audit_repc_consent_codes
  after insert or update or delete on public.repc_consent_codes
  for each row execute function audit.log_change();

-- Event shirt inventory
drop trigger if exists audit_event_shirt_inventory on public.event_shirt_inventory;
create trigger audit_event_shirt_inventory
  after insert or update or delete on public.event_shirt_inventory
  for each row execute function audit.log_change();
