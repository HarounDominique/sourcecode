-- License event ordering guard.
--
-- Problem this fixes: the Lemon Squeezy webhook used blind last-writer-wins
-- upserts. LS delivers events out of order and Edge Functions run concurrently,
-- so a stale/duplicate `subscription_paused`/`expired` arriving after a
-- `subscription_payment_success` could clobber a paying customer's row to
-- status='inactive'. There was no recency check and no atomicity.
--
-- This migration adds:
--   1. users.last_event_at  — the LS event timestamp of the last APPLIED change.
--   2. apply_license_event() — an atomic, recency-guarded upsert. A row is
--      locked (FOR UPDATE), and a status change is applied only when the
--      incoming event is not older than the last one we applied. Older events
--      can never downgrade a newer state. The license_key is still persisted
--      from an out-of-order event when the row has none yet (immutable fact).
--
-- Run order in the Supabase SQL editor: execute each statement block below.
-- A named dollar-quote tag ($fn$) is used so the editor cannot mis-split the
-- function body on the semicolons inside it.

-- 1. Ordering column ---------------------------------------------------------
alter table users
  add column if not exists last_event_at timestamptz;

-- 2. Atomic, recency-guarded apply function ----------------------------------
create or replace function apply_license_event(
  p_email          text,
  p_desired_status text,            -- 'active' | 'inactive' | null (no status change)
  p_event_at       timestamptz,
  p_features       jsonb default null,
  p_license_key    text  default null,
  p_plan           text  default 'pro'
) returns users
language plpgsql
as $fn$
declare
  v_row users;
begin
  -- Lock the existing row for this customer (no-op if absent).
  select * into v_row from users where email = p_email for update;

  -- New customer: insert. A brand-new event with no prior state wins.
  if v_row.id is null then
    insert into users (email, plan, status, features, license_key, last_event_at, updated_at)
    values (
      p_email,
      coalesce(p_plan, 'pro'),
      coalesce(p_desired_status, 'active'),
      coalesce(p_features, '[]'::jsonb),
      p_license_key,
      p_event_at,
      now()
    )
    returning * into v_row;
    return v_row;
  end if;

  -- Existing row: apply state only if this event is NOT stale.
  -- `>=` (not `>`) so a webhook retry with the same timestamp re-applies the
  -- identical state harmlessly (idempotent).
  if v_row.last_event_at is null or p_event_at >= v_row.last_event_at then
    update users set
      plan          = coalesce(p_plan, plan),
      status        = coalesce(p_desired_status, status),
      features      = coalesce(p_features, features),
      license_key   = coalesce(p_license_key, license_key),
      last_event_at = p_event_at,
      updated_at    = now()
    where id = v_row.id
    returning * into v_row;
  else
    -- Stale event: never downgrade or clobber newer state. Still persist the
    -- license key if we don't have one yet (key creation can arrive late).
    if p_license_key is not null and v_row.license_key is null then
      update users set license_key = p_license_key, updated_at = now()
      where id = v_row.id
      returning * into v_row;
    end if;
  end if;

  return v_row;
end;
$fn$;

-- 3. Lock down execution -----------------------------------------------------
-- PostgREST grants EXECUTE to PUBLIC by default. Without this, anyone holding
-- the anon key could call /rpc/apply_license_event and self-grant Pro. Only the
-- service role (used by the Edge Function) may call it.
revoke all on function apply_license_event(text, text, timestamptz, jsonb, text, text)
  from public, anon, authenticated;

grant execute on function apply_license_event(text, text, timestamptz, jsonb, text, text)
  to service_role;
