-- Contract v1 (PRD §4, §7, §8, §14). Platform-owned; coordinated with the engine.
-- 1. Engine requests from PRD §14, verbatim: requests.result_json + the quotes table.
-- 2. Platform additions: state/status enums, created_by, updated_at trigger, and the
--    approval draft (the exact email text the approver sees and the engine sends),
--    with a guard that makes a human decision final.
begin;

-- 1a. requests.result_json (PRD §14): assembled output — summary, recommendation,
--     caveats, held reason, duration. Rewritten by the engine after each step.
alter table public.requests add column result_json jsonb;

-- 1b. quotes (PRD §14), exactly-once on receipt via UNIQUE message_id.
create table public.quotes (
  id bigint generated always as identity primary key,
  request_id text not null,
  supplier_id text not null,
  rfq_idem_key text not null references public.steps(idem_key),
  message_id text not null unique,                 -- dedupe received replies exactly-once
  price_inr_per_unit numeric check (price_inr_per_unit is null or price_inr_per_unit >= 0),
  currency text,
  gst_basis text check (gst_basis in ('ex-GST', 'incl-GST', 'unknown')),
  moq integer check (moq is null or moq >= 0),
  lead_time_days integer check (lead_time_days is null or lead_time_days >= 0),
  ply integer,
  size_in text,
  raw_eml text not null,
  source_quote text,                               -- exact line in the email = the source
  parsed_ok boolean not null default false,
  received_at timestamptz not null,
  foreign key (request_id, supplier_id) references public.candidates
);
alter table public.quotes enable row level security;
revoke all on public.quotes from anon, authenticated;
grant all on public.quotes to service_role;

-- 2a. requests: enum guards (PRD §8) and who submitted it.
alter table public.requests
  add column created_by text,
  add constraint requests_state_check check (state in (
    'RECEIVED', 'SPEC_PARSED', 'CANDIDATES_FOUND', 'CANDIDATES_CHECKED', 'SHORTLISTED',
    'RECOMMENDED', 'WAIT_APPROVAL_RFQ', 'RFQS_SENT', 'WAIT_QUOTES', 'QUOTES_RECEIVED',
    'RANKED', 'WAIT_APPROVAL_PO', 'PO_SENT', 'DONE', 'HELD')),
  add constraint requests_status_check check (status in (
    'pending', 'recommended', 'awaiting_quotes', 'no_verified_supplier', 'held'));

create function public.touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

create trigger requests_touch_updated_at
  before update on public.requests
  for each row execute function public.touch_updated_at();

-- 2b. approvals: the draft travels with the approval. The engine fills these when it
--     creates the pending row; the dashboard shows them; the engine sends exactly these.
--     idem_key = sha256(request_id:action:supplier_id), the same key as steps/outbox.
alter table public.approvals
  add column idem_key text unique,
  add column email_to text,
  add column email_subject text,
  add column email_body text,
  add column created_at timestamptz not null default now();

-- A decision is final, and the draft cannot change in the same write as a decision.
create function public.guard_approval_update() returns trigger
language plpgsql as $$
begin
  if old.status <> 'pending' then
    raise exception 'approval % already decided (%)', old.id, old.status;
  end if;
  if new.status <> 'pending' and (
       new.email_to is distinct from old.email_to
    or new.email_subject is distinct from old.email_subject
    or new.email_body is distinct from old.email_body) then
    raise exception 'approval % draft changed during decision', old.id;
  end if;
  return new;
end $$;

create trigger approvals_guard_update
  before update on public.approvals
  for each row execute function public.guard_approval_update();

create index quotes_request_idx on public.quotes(request_id, supplier_id);
create index approvals_request_idx on public.approvals(request_id);
create index candidates_request_idx on public.candidates(request_id);
create index requests_created_idx on public.requests(created_at desc);

grant usage, select on all sequences in schema public to service_role;

commit;
