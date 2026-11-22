# Brief: write the BoxSource submission documents

You are writing the non-code documents for **BoxSource**, a hackathon project. Everything you
need is in this brief; you cannot see the repository. Write in plain, direct English for
judges who will read quickly. Use Markdown that renders on GitHub.

## Hard rules (read first)

1. **Never invent facts.** Do not make up supplier names, company names, URLs, prices, market
   sizes, salaries, fees, statistics or quotes. Where a number is needed and not given below,
   write a clearly marked placeholder like `TBD — from the cost ledger` or
   `ASSUMPTION (verify): …`. A wrong claim costs more than an honest placeholder: the whole
   project is judged on "every fact has a source".
2. **Do not describe features that are not listed below as built.** Section "Build status"
   says what exists. If you describe planned work, label it planned.
3. **Estimates stay estimates.** Transport cost and landed cost are estimates from the client's
   own rates, never sourced facts. Say so wherever they appear.
4. Keep each document tight: judges spend minutes, not hours. Prefer tables and short lists.

## Output you must produce

Produce **four files**, each in its own fenced code block, headed by its path:

1. `README.md` — the main entry point (structure in Section A).
2. `docs/business.md` — financial feasibility, for the finance judge (Section B).
3. `docs/ethics.md` — ethics and safety, for the ethics judge (Section C).
4. `docs/system-design.md` — answers for the system design question in the finals (Section D).

`docs/architecture.md` already exists (Mermaid diagrams of components, data flow, approvals,
retries, cost ledger, exactly-once sequence, state machine). Link to it; do not rewrite it.

---

## Context: the event

ContextCon 05 at BITS Pilani, theme **"Services, Replaced"**: build an agent that does a
whole paid service job from request to result. Track: **Supply chain** — "sourcing agents
are paid to find real makers and run quotes; find real factories for a part, run quotes and
recommend an order."

All teams serve one fictional client: **a mid-size Indian 3PL (third-party logistics)
company**. Its suppliers are real companies. Client details and request formats arrive at
kickoff and may change during the build.

Rules every service must meet:
- **Live data** drives at least one decision. Mock data only in tests.
- **Every fact has a source**: a link to the web page / API response / filing, the exact
  passage or field, and the record's date. No facts from model memory. If nothing settles a
  question the answer is **cannot verify**, naming what is missing.
- **Hidden test cases** include duplicates, lookalike names and unanswerable questions, with
  records dated after **1 September 2026**. A wrong answer counts against a team more than
  "cannot verify".
- **Irreversible actions wait for a person's approval** (here: sending quote-request emails
  and sending the purchase order).
- **Each run has a time limit and a cost limit**; the cost of every call is recorded.
- **At hour 3 a requirement changes** (e.g. a new output field or lower cost limit).
- **Finals:** a judge kills the process at a random moment; after restart every action must
  happen exactly once. Then a 5-minute live code change, then questions including one system
  design question (e.g. 100× the volume, or a second client).
- **Finals scoring (100):** live run 20, robustness (kill/restart) 15, design (live fix +
  system design question) 15, **ethics 25**, **financial feasibility 25** — the finance judge
  checks "the buyer, the price in rupees, the measured cost of each job, and whether a firm
  would pay for it". The ethics judge checks "how the service handles personal data and
  approvals before harmful actions, and how it explains its decisions".
- Required deliverables: code, **a README stating the job and who pays for it today, the
  input, the finished result and the pass mark, and listing each data source the service
  calls**, and an architecture diagram.

## Context: what BoxSource does

**The job today:** a procurement team at the 3PL, or an outside sourcing agent / procurement
consultant, finds packaging manufacturers, checks they are real factories (not traders),
requests quotes, compares them and recommends an order. Sourcing agents are typically paid per
sourcing project or as a percentage of order value. (Do not state specific fee levels unless
marked `ASSUMPTION (verify)`.)

**Running example request:** "We need 50,000 corrugated boxes (5-ply, 18×12×12 in) a month for
our Gurgaon warehouse."

**Finished result:** verified manufacturers shortlisted with evidence; quote requests (RFQs)
emailed after approval; real quotes received and compared on estimated landed cost per box; a
primary and backup supplier and a trial order quantity recommended; the purchase order emailed
after approval — every fact sourced and dated.

**Pass mark:** 0 unverified, non-matching or high-fraud-risk suppliers recommended; 0 duplicate
emails and 0 double-ingested quotes after a restart; every fact in the output sourced, quoted
and dated.

**Input** (structured, or free text that the engine parses; blank fields stay null and are
listed as assumptions to confirm):
```json
{"request_id": "SRC-001", "item": "corrugated box",
 "spec": {"ply": 5, "size_in": [18, 12, 12], "material": "kraft", "printing": "none"},
 "quantity_per_month": 50000, "delivery_location": "Gurgaon, Haryana",
 "needed_by": "2026-12-15", "budget_inr_per_unit": null}
```

**Output** (abridged): `status` (recommended | awaiting_quotes | no_verified_supplier | held),
plain-English `summary`, `candidates` (each with classification manufacturer / trader /
cannot_verify, seven checks each pass / fail / cannot_verify with evidence {fact, source_url,
source_type, exact quote or field, record_date, retrieved_at}, listed price, quoted price,
distance, estimated transport and landed cost, rank), `recommendation` (primary, backup,
trial_order_qty, full_monthly_qty, reasoning, caveats), `actions` (send_rfq / send_po with
status, idempotency key, approved_by, approved_at), `cost_inr`, `duration_s`.

**The seven checks per supplier:**
1. `real_active` — exists (company record or own site with address/registration) and shows
   activity dated on/after 1 Sep 2026.
2. `manufacturer` — at least 2 sourced manufacturing signals from the supplier's own site or an
   official record (makes the item, factory address, machinery/capacity, manufacturing
   registration). Calls itself a trader/dealer with no manufacturing signal → trader (fail).
   Listing-only, no own site → cannot verify.
3. `duplicate` — same domain, GSTIN or phone → merge, keeping all evidence. Similar name but
   different domain/address → kept separate and flagged `lookalike_name`; never merged on name
   alone. Already a client supplier → noted.
4. `spec_match` — sourced evidence of the right ply and material; "custom sizes available"
   counts if ply matches; a contradiction ("only 3-ply") fails.
5. `price` — listed price for the same ply and comparable size, per box, dated on/after 1 Sep
   2026; otherwise cannot verify. Listed price only pre-sorts the shortlist; **ranking uses the
   quoted price** from RFQ replies.
6. `distance` — geocoded supplier and delivery address; distance = straight-line × road factor;
   transport estimate from the client's own per-km rate. Labelled estimate.
7. `fraud_risk` — pass only with an authoritative active record (GSTIN active, or company
   active on MCA) matching the site and no red flag; fail only on a sourced, dated red flag
   (cancelled/struck-off, identity contradiction, credible fraud report, payee mismatch); never
   a bare "scam" label on suspicion; otherwise cannot verify.

**Decision rules (plain code, no LLM):** eligible = manufacturer, real_active, spec_match and
fraud_risk all pass and not a duplicate. RFQs go to every eligible supplier after approval.
Rank suppliers that returned a valid comparable quote by quoted price (ex-GST per box) +
estimated transport per box; tie-break by distance; primary = rank 1, backup = rank 2; trial
order = ceil(monthly quantity × 10%). No eligible supplier → `no_verified_supplier`, listing
what was searched and why each candidate failed. A cost or time limit hit → `held` with
`limit_reached` and the unfinished work listed.

**The LLM is used only for extraction** (parse the request, pull the exact supporting quote
from a stored page, parse reply emails), as strict JSON validated with Pydantic; invalid
output is retried once, then the check is `cannot_verify`. A quoted sentence is kept only if
code finds it, after normalisation, inside the stored page text.

## Context: architecture (summarise; the diagram is in docs/architecture.md)

- **Dashboard + API** — FastAPI on Vercel. A person submits requests, watches the run fill in
  live, reads every check's evidence, and approves or rejects each RFQ (per supplier) and the PO.
- **Sourcing engine** — a separate worker process on a laptop (`cli.py`, `engine/`). Runs the
  state machine, calls the data sources, writes results, sends emails only for approved
  actions, exactly once. Kill it anytime; it resumes from the database.
- **Supabase Postgres** — the only connection between the two ("database as the bus").
- **State machine:** RECEIVED → SPEC_PARSED → CANDIDATES_FOUND → CANDIDATES_CHECKED →
  SHORTLISTED → RECOMMENDED → WAIT_APPROVAL_RFQ → RFQS_SENT → WAIT_QUOTES → QUOTES_RECEIVED →
  RANKED → WAIT_APPROVAL_PO → PO_SENT → DONE; any step → HELD.
- **Exactly once:** deterministic key = sha256(request_id:action:supplier_id), never random or
  LLM-made. A `steps` row is written as "started" before sending; the `outbox` row (primary
  key = that key) is the durable "sent" record; after a crash the worker checks the outbox,
  and the mail server's Sent folder for a header carrying the key, before resending. Email
  drafts are generated once, stored, shown to the approver, and sent verbatim. Received
  quotes are deduplicated on the reply's Message-ID. All LLM/search calls are cached, so a
  restart never pays twice. Proved by an automated test that kills the worker at each
  boundary.
- **Config:** every rule, threshold, rate and limit lives in `config.yaml` (reloaded every
  worker cycle), so the hour-3 change and the live fix are config edits. Current limits:
  ₹25 and 400 seconds per request.

**Data sources the service calls** (list all of these in the README):
| Source | Used for |
|---|---|
| Google Gemini API (grounded Google Search) | finding candidate supplier URLs; LLM extraction |
| Supplier websites (fetched, stored, cached) | the facts and exact quotes for every check |
| OpenStreetMap Nominatim | geocoding for distance (1 request/second, cached) |
| GST portal, MCA company records, RDAP (domain age) | identity and fraud-risk checks |
| Crustdata API (optional, switchable in config) | company data and web search |
| Team-controlled test email inbox (SMTP send, IMAP read) | sending RFQs/PO and reading quote replies — never a real supplier |
| Supabase Postgres | storage and the bus between dashboard and worker (not a fact source) |

## Context: build status (be accurate)

Built and tested:
- Dashboard: login with two roles, request intake (form or free text), live request page
  (pipeline stepper, candidate table with check dots, evidence drawer, recommendation, quote
  timeline, action log, cost ledger), approval gate, dark mode.
- Database schema, approval rules enforced in the database, the API.
- Engine: state machine, resume after kill, exactly-once send and quote intake, cost/time
  ledger and limits, ranking, email drafting, file and SMTP/IMAP transports.
- 46 automated tests, including kill-and-restart tests.

In progress (label as such): discovery and the seven checks against live sources (the
search step currently returns no suppliers); measured cost per run (the `pricing` rates in
config are still 0 — the real per-job cost must come from the ledger after real runs).

## Context: safety and ethics features that exist

- Two logins from environment variables: **approver** (can decide) and **viewer** (read-only).
  An API token can create requests but can never approve.
- Every decision records the approver and time; **rejecting needs a typed reason**.
- The approver sees the exact email text before approving; approving sends back a hash of that
  text and is refused if the draft changed since it was shown; an approval with no draft
  cannot be approved.
- A database trigger makes a decision final.
- No email goes to real suppliers during the event; all go to a team test inbox.
- Only business contact details that suppliers publish are stored; database secret key stays
  server-side; row-level security on; browsers have no table access.
- Every exclusion of a supplier has a plain-English reason and evidence (fair treatment of
  small suppliers with thin websites, who may come back `cannot_verify` rather than "fail").
- `fraud_risk` never labels a supplier a scam without a dated source.
- Estimates (transport, landed cost) are labelled everywhere.

## How to run (for the README)

Dashboard (Python 3.12):
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env        # fill in values; never commit
.venv\Scripts\python -m uvicorn app:app --reload
```
Engine worker:
```
.venv\Scripts\python -m pip install -r engine/requirements.txt
python cli.py submit request.json
python cli.py run [--until-idle] [--interval 2]     # kill and rerun anytime; it resumes
python cli.py approve REQ --action send_rfq [--supplier sup_01 ...] --approver "Name"
python cli.py reject  REQ --action send_rfq --supplier sup_01 --approver "Name" --reason "..."
python cli.py show REQ
python cli.py status
```
Tests: `python -m pytest -q`. UI preview with fake test data, no database needed:
`python -m tests.preview_server`.
Configuration: `config.yaml` (`storage.backend: supabase` and `email.transport: smtp` for the
live demo). Database: apply `supabase/migrations/*.sql` in order. Deploy: Vercel (env vars
`SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `HEALTHCHECK_TOKEN`, `SESSION_SECRET`,
`DEMO_APPROVER_EMAIL/PASSWORD`, `DEMO_VIEWER_EMAIL/PASSWORD`). Worker-only env: Supabase keys,
`GEMINI_API_KEY`, `SMTP_*`, `IMAP_*`. Demo login credentials live only in environment
variables — the README must say where, never the values.

---

## Section A — `README.md` structure

1. Title + one-line pitch.
2. **The job and who pays for it today.**
3. **Input** (short JSON example) and **finished result**.
4. **Pass mark** (exactly the three conditions above).
5. **How it works** — 6–8 bullets, link to `docs/architecture.md`.
6. **Data sources** — the table above.
7. **Hard cases it handles** — duplicate, lookalike name, unanswerable/stale question →
   cannot verify, trader posing as manufacturer, 3-ply-only spec mismatch, no verified
   supplier, limit reached — one line each with the expected result.
8. **Approvals and exactly-once** — short.
9. **Cost and time** — limits, ledger, and "Measured cost per request: `TBD — from the cost
   ledger after live runs`".
10. **Run it** — dashboard, worker, tests, preview, deploy (from the section above).
11. **Demo login** — roles and where credentials live (not the values).
12. **Repository map** — `app.py`, `dashboard/`, `engine/`, `cli.py`, `config.yaml`,
    `supabase/migrations/`, `tests/`, `docs/`.
13. Links to `docs/business.md`, `docs/ethics.md`, `docs/system-design.md`.

## Section B — `docs/business.md` (finance judge, 25 points)

Cover exactly what the judge checks: **the buyer, the price in rupees, the measured cost of each
job, and whether a firm would pay.**
- **Buyer:** who signs the cheque at a mid-size Indian 3PL (e.g. procurement / packaging
  category manager); the trigger (new warehouse, new client, supplier failure, cost review).
- **What they pay today:** the alternatives (in-house procurement time, an outside sourcing
  agent, B2B marketplaces) — describe qualitatively; any rupee figure must be marked
  `ASSUMPTION (verify)`.
- **Proposed price in ₹:** propose a simple model (e.g. a fixed fee per completed sourcing job,
  optionally plus a small success fee on the first PO) with example numbers explicitly marked
  as the team's proposal and assumptions.
- **Cost per job:** a table of cost components (LLM calls, grounded search, web fetches,
  geocoding, hosting/database share, human approval time) with the formula; the measured total
  stays `TBD — from the cost ledger`; state the hard cap of ₹25 per request from config.
- **Unit economics:** price minus cost per job, as a formula with placeholders.
- **Why a firm would pay:** speed (hours vs days), evidence trail for audit, fewer bad
  suppliers (fraud/trader screening), exactly-once actions, human keeps control.
- **Risks and limits:** thin supplier websites → more cannot verify; data-source access; LLM
  price changes; one item category tuned at a time.

## Section C — `docs/ethics.md` (ethics judge, 25 points)

Cover: **personal data**, **approvals before harmful actions**, **explaining decisions**.
- Personal data: what is stored (only published business contacts needed for an RFQ), what is
  not, where secrets live, access control, no real supplier contacted during the event,
  retention suggestion (label as proposal).
- Approvals: which actions are irreversible and gated; per-supplier control; exact text shown;
  hash check; typed reason on reject; decision final and attributed; viewer/API cannot approve.
- Explaining decisions: every check has a result, explanation, evidence with quote, link and
  date, or "missing: …"; every exclusion has a plain-English reason; estimates labelled.
- Fairness to small suppliers: `cannot_verify` instead of "fail" when evidence is thin; never
  merge on name alone; fraud only with a dated source.
- Failure modes and mitigations: hallucinated facts (quote rule), stale data (1 Sep 2026
  cutoff), wrong merges, wrong fraud flags, duplicate emails, runaway cost.
- What a human still decides, and why.

## Section D — `docs/system-design.md` (finals question)

Short answers, each with "what changes" and "what stays the same":
1. **100× the volume** — more workers claiming requests (row-level claiming / leases on the
   `requests` row), the database as the queue, shared page/geo/LLM caches across requests,
   rate limits per source (Nominatim 1 req/s → self-host or batch), cost limits per request,
   dashboard pagination. Keep exactly-once via the same keys.
2. **A second client** — add a `client_id` to every table and to the idempotency key, per-client
   config file (rules, limits, rates, approvers), row-level security per client, separate test
   inboxes, per-client cost ledger and billing.
3. **A new item category** (e.g. pallets or stretch film) — config + spec checks, not
   architecture.
4. **A source goes down** — retries with backoff, cached pages, `cannot_verify` instead of a
   guess, request held with the reason after max attempts.
5. **The hour-3 requirement change and the 5-minute live fix** — why they are config edits.

Now write the four files.
