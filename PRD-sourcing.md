# PRD — BoxSource: Packaging Supplier Sourcing Service (ContextCon 05, Supply chain track)

> This is the **shared build spec** for BoxSource. The build is split between two agents working in parallel — see **§4** and the per-agent briefs in [`ujjwal/CLAUDE.md`](ujjwal/CLAUDE.md) (sourcing engine) and [`chaitanya/AGENTS.md`](chaitanya/AGENTS.md) (platform + dashboard).
> This file and [`background-check-criteria.md`](background-check-criteria.md) are the **canonical specs**. Any change to the contract (schema, state machine, JSON shapes) must be reflected here and told to the other agent.

## 1. One-line summary
An agent that does the whole job a sourcing agent is paid for: from the request **"we need 50,000 corrugated boxes (5-ply, 18×12×12 in) a month for our Gurgaon warehouse"** to the finished result **"verified manufacturers shortlisted, quote requests sent (after approval), real quotes collected and compared, and an order recommendation with a source for every fact."**

## 2. Hackathon rules that drive the design
- Client: a fictional mid-size Indian 3PL. Its suppliers are **real** companies. Client details and request formats come at kickoff and may change.
- **Live data** must drive at least one decision. Mock data **only in tests**.
- **Every fact has a source**: URL/API response, exact passage or field, record date. **No facts from model memory.** If nothing settles a question → `cannot_verify` + what is missing.
- **Irreversible actions wait for human approval** (here: sending quote-request emails, sending the purchase order).
- **Time and cost limit per run**; cost of every call recorded.
- Hidden test cases include duplicates, lookalike names, unanswerable questions; all records dated **after 1 Sep 2026**. **A wrong answer is worse than `cannot_verify`.**
- Hour-3 requirement change → must be a config/format change.
- On stage the process is **killed at a random moment**; after restart **every action happens exactly once**.
- 5-minute live code change on stage → keep rules in config, code simple.

## 3. Goals / non-goals / scope
**Goals**
1. Find real candidate suppliers for the requested item near the delivery location.
2. Classify each: verified manufacturer / trader / cannot_verify — with sources.
3. Screen each for spec match and **fraud risk**; capture listed price where published — each sourced and dated.
4. Send RFQs (after approval), collect **real quotes**, rank by landed cost per unit from the **quoted** price; recommend a primary, a backup and an order quantity.
5. Draft and send RFQs and a purchase order only after approval; **exactly once**.
6. Zero recommendations of unverified, non-matching, or high-fraud-risk suppliers.

**Non-goals**
- No scraping behind captchas or logins; respect site terms. Unreachable source → `cannot_verify`.
- No emails to real suppliers. All outbound email goes to **team-controlled test inboxes**; during the demo the team replies as the "seller" so the full send→reply→compare loop runs live.
- No real payments. (Optional: advance payment via Razorpay test mode — only if time remains.)
- LLM is used **only for extraction** (parse request, pull sourced quotes, parse reply emails); ranking and decisions are plain code.

**Scope** — built for the client's **single item category** as released at kickoff (running example: corrugated boxes). Spec, search and price logic are parameterized in `config.yaml`; this is **not** a general any-product engine. If the kickoff item differs, retune config and the spec checks — not the architecture.

## 4. Architecture and the two-agent build split
Two agents build in parallel. **The Supabase database is the integration boundary ("DB as the bus").** Neither side calls the other directly; they communicate only through rows in Supabase.

```
  Human ──▶ Dashboard (Vercel, FastAPI)              Sourcing engine (worker, runs on a laptop)
            │  writes: requests, approvals            │  claims requests; writes candidates, checks,
            │  reads:  everything the engine wrote    │  evidence, quotes, recommendation, costs, logs,
            ▼                                         ▼  outbox; reads approvals; sends RFQ/PO emails
         ┌──────────────────────── Supabase Postgres ────────────────────────┐
         │ requests · candidates · checks · evidence · quotes · steps ·       │
         │ approvals · outbox · costs · pages · geo_cache · logs              │
         └────────────────────────────────────────────────────────────────────┘
```

**Who owns what**
| Part | Owner | Builds |
|---|---|---|
| **Platform + Dashboard** | **Chaitanya** (ChatGPT/Codex) — brief: `chaitanya/AGENTS.md` | Supabase schema + migrations; FastAPI API (`app.py`); the Vercel dashboard (request intake, candidate/evidence views, approvals UI, cost/time, action log); auth; deploy. |
| **Sourcing engine** | **Ujjwal** (Claude Code) — brief: `ujjwal/CLAUDE.md` | The worker: orchestrator + resume, parse, discover, the 7 checks + evidence, rank, recommend, RFQ/PO send, quote-intake poller, cost ledger, exactly-once, tests. |

**Why a separate worker (not a Vercel function):** sourcing runs take minutes and must survive a mid-run kill, and the quote poller is a long-lived loop. Vercel serverless functions are for short requests, so the dashboard/API live on Vercel while the **engine runs as a standalone worker on the laptop** during the demo (the judge kills it, the team restarts it, it resumes from Supabase). This is the robustness demo.

**The contract (both sides honour, changes coordinated):**
1. **Intake** — dashboard inserts a `requests` row (`state=RECEIVED`, `status=pending`, `input_json`).
2. **Run** — worker claims `RECEIVED` requests, runs the pipeline, writes `candidates` / `checks` / `evidence`, advances `requests.state`, writes the shortlist recommendation, then sets `state=WAIT_APPROVAL_RFQ` and creates `approvals` rows (`action=send_rfq`, `status=pending`) with the draft email bodies stored (`steps.output_json`).
3. **Approve RFQ** — dashboard shows pending approvals + email previews; the user approves/rejects per supplier → updates the `approvals` row (`status`, `approver`, `reason`, `decided_at`).
4. **Send + collect** — worker sends RFQ emails for `approved` rows (exactly-once via `outbox`/`idem_key`), `state=RFQS_SENT→WAIT_QUOTES`; the poller ingests replies into `quotes`, `state=QUOTES_RECEIVED→RANKED`, updates the recommendation.
5. **Approve PO** — `state=WAIT_APPROVAL_PO`; dashboard shows the PO preview; the user approves; worker sends the PO (exactly-once), `state=PO_SENT→DONE`.

Schema is **platform-owned**; the engine consumes it and requests additions (e.g. the `quotes` table, §14) rather than editing the migration itself.

## 5. Tech stack
- Python 3.12. **Supabase (Postgres)** for all durable state — replaces the PRD's original SQLite. The engine talks to it via the Supabase REST/`postgrest` or `psycopg`; the API via the service key.
- **Platform/API:** FastAPI on **Vercel** (`app.py` is the ASGI entrypoint; `vercel.json` selects it). Dashboard rendered server-side or as a light SPA — platform's choice, no heavy build required.
- **Engine:** a standalone worker process + `cli.py` (`run` / `resume` / `approve`) for local dev. Runs on the laptop for the demo.
- `httpx` with timeouts + retries (backoff, max 3).
- LLM wrapper `llm.py` (Anthropic or OpenAI via env), strict JSON output validated with Pydantic.
- Search/fetch: **Crustdata** (web search, fetch, company data) — read docs.crustdata.com; fallback to another web search API if a key is available.
- Geocoding/distance: **OpenStreetMap Nominatim** (respect its policy: 1 req/s, custom User-Agent, cache results) + haversine; road factor from config.
- Email: **SMTP** to send RFQ/PO, **IMAP** (or a mailbox API) to poll replies, against team-controlled test inboxes.
- `config.yaml` reloaded at the start of each run.
- Tests: `pytest`.
- `.env` (never commit): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `HEALTHCHECK_TOKEN`, `LLM_PROVIDER`, `LLM_API_KEY`, `CRUSTDATA_API_KEY`, `SEARCH_API_KEY` (optional), `SMTP_*`, `IMAP_*` for the test inboxes.

## 6. Repository layout
```
dayum/
  app.py                     # [platform] FastAPI entrypoint (Vercel) — health/ready + API
  vercel.json, .vercelignore # [platform] Vercel config
  requirements.txt
  supabase/
    migrations/*.sql         # [platform] canonical schema (the contract)
  dashboard/                 # [platform] dashboard UI (templates or SPA)
  engine/                    # [engine] the sourcing worker
    worker.py                #   claim requests, drive the state machine, resume
    orchestrator.py          #   state machine
    steps.py                 #   idempotent step runner (exactly-once)
    io_format.py             #   kickoff input/output format <-> internal
    parse_request.py         #   request -> structured spec (LLM extraction + validation)
    discover.py              #   find candidate suppliers
    checks/                  #   real_active, manufacturer, duplicate, spec_match,
                             #   price, distance, fraud_risk
    rank.py, recommend.py    #   landed cost + ranking + primary/backup (pure code)
    actions/
      rfq_email.py           #   draft + send RFQs (approval, exactly-once)
      purchase_order.py      #   draft + send PO (approval, exactly-once)
      quote_intake.py        #   IMAP poll + parse replies -> quotes (exactly-once)
    sources/ crustdata.py, web.py, geo.py
    evidence.py              #   Evidence model + quote verification
    ledger.py                #   cost + time ledger
    db.py                    #   Supabase access
    llm.py, config.py
  cli.py                     # [engine] run / resume / approve (local dev)
  config.yaml
  tests/ cases/, test_cases.py, test_exactly_once.py
  docs/ architecture.md (Mermaid), sources.md (from M0 probe)
  ujjwal/CLAUDE.md           # engine build brief (Ujjwal / Claude Code)
  chaitanya/AGENTS.md        # platform build brief (Chaitanya / Codex)
  PRD-sourcing.md, background-check-criteria.md, README.md
```
Package names are a guide; the hard rule is the ownership boundary and the DB contract, not the folder names.

## 7. Input / output contract
(Keep all format mapping in `io_format.py`; update when the kickoff format arrives.)

**Input** (written by the dashboard into `requests.input_json`)
```json
{
  "request_id": "SRC-001",
  "item": "corrugated box",
  "spec": {"ply": 5, "size_in": [18, 12, 12], "material": "kraft", "printing": "none"},
  "quantity_per_month": 50000,
  "delivery_location": "Gurgaon, Haryana",
  "needed_by": "2026-12-15",
  "budget_inr_per_unit": null
}
```
If the request is free text, `parse_request.py` turns it into this structure; any field it cannot fill stays `null` and is listed as an assumption needing confirmation.

**Output** (assembled from Supabase rows; the shape the dashboard renders and screening reads)
```json
{
  "request_id": "SRC-001",
  "status": "recommended | awaiting_quotes | no_verified_supplier | held",
  "summary": "Plain-English paragraph",
  "candidates": [
    {
      "supplier_id": "sup_01",
      "name": "...",
      "classification": "manufacturer | trader | cannot_verify",
      "checks": [
        {"check": "real_active | manufacturer | duplicate | spec_match | price | distance | fraud_risk",
         "result": "pass | fail | cannot_verify",
         "explanation": "...",
         "evidence": [{"fact": "...", "source_url": "...", "source_type": "web_page | api_response | filing",
                       "field_or_quote": "...", "record_date": "2026-10-14", "retrieved_at": "2026-11-22T15:10:00+05:30"}],
         "missing": "only when cannot_verify"}
      ],
      "listed_price_inr_per_unit": 38.5,      "price_basis": "ex-GST | incl-GST | unknown",
      "quoted_price_inr_per_unit": 37.2,      "quote_basis": "ex-GST | incl-GST | unknown",
      "quoted_moq": 10000, "quoted_lead_time_days": 12,
      "quote_source": "data/outbox/<idem>.eml line 6", "quote_received_at": "2026-11-22T16:05:00+05:30",
      "distance_km": 42, "est_transport_inr_per_unit": 0.9,
      "est_landed_inr_per_unit": 38.1, "rank": 1
    }
  ],
  "recommendation": {
    "primary": "sup_01", "backup": "sup_03",
    "trial_order_qty": 5000, "full_monthly_qty": 50000,
    "reasoning": "...",
    "caveats": ["Ranked on the quote received 22 Nov 2026; listed price was an earlier hint only"]
  },
  "actions": [
    {"type": "send_rfq | send_po", "to_supplier": "sup_01", "status": "pending_approval | approved | done | skipped",
     "idempotency_key": "...", "approved_by": "...", "approved_at": "..."}
  ],
  "cost_inr": 6.1,
  "duration_s": 140
}
```

## 8. State machine
```
RECEIVED → SPEC_PARSED → CANDIDATES_FOUND → CANDIDATES_CHECKED → SHORTLISTED → RECOMMENDED
  → WAIT_APPROVAL_RFQ → RFQS_SENT → WAIT_QUOTES → QUOTES_RECEIVED → RANKED
  → WAIT_APPROVAL_PO → PO_SENT → DONE
(any step) → HELD   if limits hit or no verified supplier (status no_verified_supplier)
```
- `state` lives in `requests.state` (Supabase). Saved after every step; on startup the worker resumes every request not in `DONE`, `HELD` or a `WAIT_*` state.
- `status` (output) is one of `recommended | awaiting_quotes | no_verified_supplier | held`.
- RFQ approval is per supplier (the approver can untick suppliers). PO approval is a separate step after quotes.

## 9. Discovery (`discover.py`)
- Queries built from spec + location, e.g. `"corrugated box manufacturer" Gurgaon`, `"5 ply corrugated box" manufacturer Manesar`, nearby industrial areas listed in `config.search_areas`.
- Sources: Crustdata web search + company search; supplier websites; public B2B listing pages **only if fetchable without login/captcha and allowed by the site**.
- Keep up to `config.max_candidates` (default 10). Store every fetched page in `pages` (cache).
- Each candidate gets: name, website, address/city, listing URLs — all with evidence.

## 10. Checks (per candidate)
Canonical table: [`background-check-criteria.md`](background-check-criteria.md). Summary below.

### 10.1 Quote rule (applies everywhere)
- LLM returns the exact supporting sentence; code verifies it is a substring of the stored page text after normalization (lowercase, collapse whitespace, normalize quotes/dashes). Not found → fact discarded.
- API facts cite a JSON field path; code verifies path and value.
- "Current" facts (active, price) need `record_date >= config.min_record_date`. Older → `cannot_verify` (stale).

### 10.2 real_active
- Evidence the company exists (company data record, own website with address, registration number shown on site) **and** is active (record/page dated ≥ `min_record_date`). Exists + active → pass; evidence of closure → fail; otherwise → cannot_verify.

### 10.3 manufacturer (manufacturer vs trader)
- Pass only if ≥ `config.manufacturer_min_signals` sourced signals from the supplier's **own** site or an official record (manufactures the item; factory/plant address; machinery/capacity; registration type manufacturing). Explicit "trader/dealer/distributor" with no manufacturing signal → `trader` (fail). Listing-only, no site → `cannot_verify`.

### 10.4 duplicate
- Match on domain, GSTIN, phone (and carefully normalized name). Exact domain/GSTIN/phone → **merge** (keep all evidence). Similar name, different domain/address → **keep separate, flag `lookalike_name`**; never merge on name alone. Already in the client's supplier records → note it, don't treat as new.

### 10.5 spec_match
- Sourced evidence of the right ply and material; "custom sizes available" + ply match counts if `config.accept_custom_size_statement`. Contradiction (e.g. "only 3-ply") → fail. No statement → cannot_verify.

### 10.6 price (listed — a shortlist hint only)
- Extract listed price, unit, size/ply, MOQ, GST basis, date. Must refer to the same ply and a comparable size; per-kg not converted unless box weight is sourced. No comparable published price → `cannot_verify` listed price. **This feeds only the pre-RFQ shortlist order; the ranking price is the quoted price (§11, §12).**

### 10.7 distance
- Geocode supplier + delivery location via Nominatim (cached). `distance_km = haversine × config.road_factor`. `est_transport_inr_per_unit = distance_km × config.transport_inr_per_km_per_load / config.boxes_per_load`. These are **estimates from client config**, labelled as such, never presented as sourced facts.

### 10.8 fraud_risk
- Confirms a genuine trading entity, not a shell/scam operator. Every signal sourced and dated; **never** a bare "scam" label on suspicion — a wrong fraud call costs more than `cannot_verify`.
- **pass (low risk):** an authoritative identity record is active and matches the site — GSTIN **active** on the GST portal, or company/LLP status **active** (not struck-off) on MCA — and no red flag below.
- **fail (high risk):** a sourced, dated red flag — GSTIN/company cancelled, struck-off or not found; site identity contradicts the official record; credible fraud/complaint/news naming the company; payee/bank name not matching the legal entity.
- **cannot_verify:** no authoritative record, or nothing confirms/contradicts legitimacy (safe default).
- Sources: GST portal, MCA, WHOIS domain age (weak signal, noted), Crustdata/web for complaints. A `fraud_risk = fail` supplier is never ranked, recommended or sent an RFQ.

## 11. Ranking and recommendation (pure code, no LLM)
```
eligible  = manufacturer == pass AND real_active == pass AND spec_match == pass
            AND fraud_risk == pass AND not duplicate-of-another
shortlist = eligible, pre-sorted by listed_price where published (hint and caveat only, never the final basis)
# after approval, RFQs go to the whole shortlist; final ranking uses the returned quotes
quoted    = shortlist suppliers that replied with a valid, comparable quote
est_landed = quoted_price (normalized to ex-GST per box) + est_transport_per_unit
rank quoted by est_landed ascending; tie-break by distance
primary = rank 1, backup = rank 2
trial_order_qty = ceil(quantity_per_month × config.trial_order_fraction)
```
- RFQs go to every eligible supplier (listed price or not); ranking happens on the quotes returned. No reply yet → status `awaiting_quotes` with the pending RFQs listed.
- If no eligible supplier → status `no_verified_supplier`; list what was searched and why each candidate failed.
- If a budget is given and all quotes exceed it → say so; still list the cheapest.

## 12. RFQ email round-trip (quote intake)
The RFQ is sent by **real email** to a team-controlled test inbox; in the demo the team replies as the "seller", so the collect-and-compare loop runs live on stage.
- **Send:** after approval, `rfq_email.py` sends the stored RFQ body via SMTP to the supplier's test address. The `Message-ID` embeds the send idempotency key. Exactly-once via `outbox` (§13).
- **Poll:** `quote_intake.py` runs a loop (part of the worker) checking IMAP for replies. For the demo it can also be triggered on demand.
- **Match:** a reply is tied to its RFQ by `In-Reply-To`/`References` (the original `Message-ID`) or a token in the subject (`[SRC-001/sup_01]`). Unmatched replies are logged, not guessed.
- **Exactly-once on receipt:** each reply is processed once — dedupe on the reply's `Message-ID` (stored `UNIQUE` in `quotes`). A restart re-scans the inbox and skips already-ingested replies.
- **Extract:** the LLM pulls price, currency, GST basis, MOQ, lead time, ply/size the quote refers to — strict JSON, Pydantic-validated. The **raw `.eml` is stored and the exact quoted line is the source** for the quoted price (same quote rule as §10.1). Unparseable/ambiguous → `cannot_verify`, surfaced for a human; never guessed.
- **Rank:** valid quotes feed §11. A supplier that never replies is listed, not ranked.

## 13. Actions and exactly-once (critical)
Actions: `send_rfq` (one per approved supplier), `send_po` (primary supplier only).
1. `idempotency_key = sha256(request_id + ":" + action + ":" + supplier_id)` — deterministic, never LLM-generated, never random.
2. Insert a `steps` row status `started` (UNIQUE `idem_key`) **before** sending.
3. Send, then record it in the `outbox` table keyed by `idem_key` (PK) — a durable record that the message went out. (A copy of the `.eml` may also be written for inspection.) If the `outbox`/`steps` key already exists → skip (already sent).
4. Mark the step `done`.
5. On restart: for `started` rows, check `outbox` for the key first; resend only if absent.
- Email bodies are generated **once**, stored in `steps.output_json`, shown to the approver before sending; a restart reuses the stored body (no regeneration).
- All LLM and search calls cached by (`request_id`, step, input hash) so restarts don't re-pay.
- Received quotes are deduped on `Message-ID` (§12) — exactly-once in **both** directions.
- `tests/test_exactly_once.py`: SIGKILL the worker at each step boundary (send RFQ, ingest quote, send PO), restart, assert each email is sent once and each quote ingested once.

**RFQ email content:** item spec, quantity, delivery location, needed-by date, request for price per box (ex-GST), MOQ, lead time, payment terms. **PO email content:** supplier, item spec, trial quantity, quoted price used and its source/date, delivery details, "subject to confirmation of quote".

## 14. Data model (Supabase / Postgres)
Canonical schema: `supabase/migrations/*.sql` (**platform-owned**). It already provides, keyed as below:
`requests` (state + status), `candidates`, `checks`, `evidence`, `steps` (UNIQUE `idem_key`), `approvals` (UNIQUE `request_id,action,supplier_id`; reject needs a reason), `outbox` (PK `idem_key`, the durable send record), `costs`, `pages`, `geo_cache`, `logs`. RLS on; only `service_role` has access.

**To add (engine needs it — request from platform):** a `quotes` table for RFQ replies:
```sql
quotes(
  id bigint primary key,
  request_id text, supplier_id text,          -- FK -> candidates
  rfq_idem_key text references steps(idem_key),
  message_id text unique,                      -- dedupe received replies exactly-once
  price_inr_per_unit numeric, currency text, gst_basis text,
  moq integer, lead_time_days integer, ply integer, size_in text,
  raw_eml text, source_quote text,             -- exact line in the email = the source
  received_at timestamptz, parsed_ok boolean
);
```

## 15. Config (`config.yaml`)
```yaml
min_record_date: 2026-09-01
max_candidates: 10
search_areas: [Gurgaon, Manesar, Faridabad, Noida, Bawal]
manufacturer_min_signals: 2
accept_custom_size_statement: true
road_factor: 1.3
transport_inr_per_km_per_load: 0      # fill from client records; label as estimate
boxes_per_load: 0                     # fill from client records
trial_order_fraction: 0.1
approval_required: [send_rfq, send_po]
quote_wait_s: 600                     # how long to wait for replies before status awaiting_quotes
limits:
  cost_inr_per_request: 25
  time_s_per_request: 400
pricing:                              # INR per unit for the cost ledger — fill with real rates
  llm_input_per_1k_tokens: 0.0
  llm_output_per_1k_tokens: 0.0
  crustdata_per_call: 0.0
  web_search_per_call: 0.0
output_fields: [request_id, status, summary, candidates, recommendation, actions, cost_inr, duration_s]
```
- Cost or time limit hit → stop discovery/checks, rank what is finished, status `held` with reason `limit_reached` and unfinished work listed.
- `transport_*`, `boxes_per_load` and `pricing.*` ship as `0` placeholders — **fill with real client/provider rates** or transport and cost outputs are degenerate.

## 16. Milestones (by track, parallel)
Shared **M0 — freeze the contract** (both agents, first): agree the Supabase schema (incl. `quotes`), the request/output JSON, and the state/status enums. Nothing else starts until these are fixed.

**Engine track (Ujjwal)** — detail in `ujjwal/CLAUDE.md`
- E1 Reality probe (`scripts/probe.py`) → `docs/sources.md`: do Crustdata/web/Nominatim actually return real suppliers? Tune discovery/checks to what works.
- E2 Skeleton + resume against Supabase (checks stubbed `cannot_verify`); kill + resume works.
- E3 Discovery + the 7 checks + evidence/quote rule.
- E4 Rank + recommend; assemble output JSON.
- E5 Actions + exactly-once + quote intake (send RFQ, poll/ingest replies, send PO); `test_exactly_once.py`.
- E6 Test cases (`tests/cases/`), rehearse kill/restart.

**Platform track (Chaitanya)** — detail in `chaitanya/AGENTS.md`
- P1 Schema finalised + applied (add `quotes`); health/ready green.
- P2 API: create request, read full request view, approvals endpoint.
- P3 Dashboard: request list + detail rendering candidates/checks/evidence from seeded rows.
- P4 Approvals UI (per-supplier RFQ tick-boxes + email preview, PO preview, approve/reject with typed reason).
- P5 Cost/time + action log; polish.
- P6 Deploy to Vercel; verify against live engine.

## 17. Dashboard UI (platform)
- `/` — requests: id, item, location, status, cost.
- `/requests/{id}` — parsed spec + assumptions; candidate table (rank, name, classification badge, listed vs **quoted** price, distance, est. landed cost); each candidate expands to its checks (green/red/grey) with evidence (fact, quote, link, date); recommendation box with caveats; pending approvals — RFQ list with tick boxes per supplier and email preview, PO preview; Approve / Reject (reject needs a typed reason); cost + time used; action log.
- `POST /requests`, `POST /approvals/{id}`.

## 18. Logging
- One structured line per step: request_id, step, supplier_id, status, duration_ms, cost_inr → `logs` table (and a local `run.log` for the worker).

## 19. Ethics requirements
- No personal data beyond business contact details shown publicly by the supplier; store only what's needed for the RFQ.
- Never contact real suppliers during the event; team-controlled test inboxes only.
- Never present an estimate (transport cost, landed cost) as a sourced fact — label it.
- Every exclusion of a supplier has a plain-English reason and evidence (fair treatment of small suppliers).
- Every human override requires a typed reason, logged.
- Approver sees the exact email text before anything is sent.

## 20. README must include
- The job and who pays for it today (sourcing agents / procurement consultants, paid per project or a percentage of order value).
- Input, finished result, pass mark: **0 unverified or non-matching suppliers recommended, 0 duplicate emails after restart, every fact sourced.**
- Every data source called (Crustdata, web search/fetch, supplier sites, Nominatim/OpenStreetMap, GST/MCA, LLM provider).
- How to run the worker, resume, approve; how to run tests; measured cost per request from the ledger.
- **Financial feasibility:** what BoxSource would charge vs. a human sourcing agent, and the measured cost per run (for the finance judge).

## 21. Rules for the build agents
- Never invent suppliers, URLs, prices, quotes or API fields. Unknown API shape → read docs or probe and inspect real output.
- Mock data only inside `tests/`.
- All thresholds, lists and rates in `config.yaml`.
- LLM output must be strict JSON validated by Pydantic; invalid → retry once → `cannot_verify`.
- Small functions, clear names — a human must make a live change in 5 minutes.
- Respect the ownership boundary (§4): the engine never builds UI; the platform never computes a verdict or sends an email. Talk only through Supabase.
- Coordinate every contract change (schema, JSON, enums) and reflect it in this file.
- Commit after each milestone with a clear message.

## 22. Future directions (not in current hackathon scope)

### Voice intake agent (time permitting)
- A phone/voice front end so the user can simply **call the agent**, speak the requirement, and answer follow-up questions — instead of opening the dashboard and typing the query.
- Scope is **intake only**: speech-to-text → `parse_request.py` (same structured spec + assumptions as the typed path) → the agent reads the parsed spec back to confirm → hand off to the existing orchestrator. Discovery, checks, ranking, RFQ/PO and approvals are unchanged.
- Follow-up questions use the existing rule that any field `parse_request.py` cannot fill stays `null` and must be confirmed before a run (§7); the agent asks only about the `null` fields.
- Irreversible actions (send RFQ, send PO) are **never approved by voice alone** — approval stays in the dashboard to preserve the human-in-the-loop requirement (§13, §19).
- Benefit: reduces the user's effort to a single phone call and lowers the barrier for non-technical procurement staff.
