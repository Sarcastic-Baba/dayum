-- Initial server-only BoxSource schema. No browser access policies are granted.
begin;

create table public.requests (
  request_id text primary key,
  input_json jsonb not null,
  spec_json jsonb,
  state text not null default 'RECEIVED',
  status text not null default 'pending',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.candidates (
  request_id text not null references public.requests,
  supplier_id text not null,
  name text not null,
  website text,
  address text,
  classification text not null default 'cannot_verify'
    check (classification in ('manufacturer', 'trader', 'cannot_verify')),
  merged_into text,
  data_json jsonb not null default '{}',
  primary key (request_id, supplier_id),
  foreign key (request_id, merged_into) references public.candidates(request_id, supplier_id)
);

create table public.checks (
  request_id text not null,
  supplier_id text not null,
  check_name text not null,
  result text not null check (result in ('pass', 'fail', 'cannot_verify')),
  explanation text not null,
  missing text,
  primary key (request_id, supplier_id, check_name),
  foreign key (request_id, supplier_id) references public.candidates
);

create table public.evidence (
  id bigint generated always as identity primary key,
  request_id text not null,
  supplier_id text not null,
  check_name text not null,
  fact text not null,
  source_url text not null,
  source_type text not null,
  field_or_quote text not null,
  record_date date,
  retrieved_at timestamptz not null default now(),
  foreign key (request_id, supplier_id) references public.candidates
);

create table public.steps (
  id bigint generated always as identity primary key,
  request_id text not null references public.requests,
  step text not null,
  idem_key text not null unique,
  status text not null default 'started' check (status in ('started', 'done', 'failed')),
  output_json jsonb,
  started_at timestamptz not null default now(),
  done_at timestamptz,
  attempts integer not null default 0 check (attempts >= 0)
);

create table public.approvals (
  id bigint generated always as identity primary key,
  request_id text not null,
  action text not null check (action in ('send_rfq', 'send_po')),
  supplier_id text not null,
  status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  approver text,
  reason text,
  decided_at timestamptz,
  unique (request_id, action, supplier_id),
  foreign key (request_id, supplier_id) references public.candidates,
  check (status = 'pending' or (approver is not null and decided_at is not null)),
  check (status <> 'rejected' or length(trim(reason)) > 0 and reason is not null)
);

-- Durable test outbox, not a real email transport. The workflow must enforce
-- approval and insert using the deterministic key. SMTP exactly-once is not implied.
create table public.outbox (
  idem_key text primary key references public.steps(idem_key),
  approval_id bigint not null references public.approvals,
  message text not null,
  created_at timestamptz not null default now()
);

create table public.costs (
  id bigint generated always as identity primary key,
  request_id text not null references public.requests,
  step text not null,
  call_type text not null,
  units numeric not null check (units >= 0),
  inr numeric not null check (inr >= 0),
  at timestamptz not null default now()
);

create table public.pages (
  url text not null,
  fetched_at timestamptz not null default now(),
  content_hash text not null,
  text text not null,
  primary key (url, content_hash)
);

create table public.geo_cache (
  query text primary key,
  lat double precision not null check (lat between -90 and 90),
  lon double precision not null check (lon between -180 and 180),
  source_json jsonb not null,
  fetched_at timestamptz not null default now()
);

create table public.logs (
  id bigint generated always as identity primary key,
  request_id text not null references public.requests,
  level text not null,
  message text not null,
  at timestamptz not null default now()
);

create index evidence_request_idx on public.evidence(request_id, supplier_id);
create index steps_request_idx on public.steps(request_id);
create index costs_request_idx on public.costs(request_id);
create index logs_request_idx on public.logs(request_id, at);

do $$
declare t text;
begin
  foreach t in array array['requests', 'candidates', 'checks', 'evidence',
      'steps', 'approvals', 'outbox', 'costs', 'pages', 'geo_cache', 'logs']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
    execute format('grant all on public.%I to service_role', t);
  end loop;
end $$;

grant usage, select on all sequences in schema public to service_role;
commit;
