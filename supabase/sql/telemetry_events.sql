-- Anonymous, opt-in usage telemetry. No PII, no paths — every column is a
-- bounded categorical or bucket. Populated by the `telemetry` edge function.
create table if not exists public.telemetry_events (
  id          uuid primary key default gen_random_uuid(),
  received_at timestamptz not null default now(),
  event       text not null,
  client_ts   text,
  v           text,            -- sourcecode version
  py          text,            -- python major.minor
  os          text,            -- linux | macos | windows | other
  arch        text,            -- x64 | arm64 | other
  cmd         text,            -- analyze | prepare-context | telemetry | unknown
  flags       jsonb default '[]'::jsonb,
  output_fmt  text,            -- json | yaml
  repo_size   text,            -- tiny | small | medium | large | huge | unknown
  duration    text,            -- <1s | <5s | <15s | <60s | 60s+ | unknown
  success     boolean,
  error_kind  text,            -- exception class name only
  feature     text,            -- gated feature / task name (closed set)
  install_id  uuid,            -- stable anonymous install id (random, no PII)
  session     text             -- ephemeral 8-char hex, NOT a stable user id
);

-- If the table already exists from an earlier deploy, add the column:
alter table public.telemetry_events add column if not exists install_id uuid;

-- Common query axes: funnel by event, adoption by version, time series,
-- and unique-install / conversion / retention by install_id.
create index if not exists telemetry_events_event_idx      on public.telemetry_events (event);
create index if not exists telemetry_events_received_at_idx on public.telemetry_events (received_at);
create index if not exists telemetry_events_feature_idx     on public.telemetry_events (feature);
create index if not exists telemetry_events_install_idx     on public.telemetry_events (install_id);

-- RLS on, no policies: only the service role (edge function) can write/read.
-- The public anon/publishable key cannot touch this table.
alter table public.telemetry_events enable row level security;
