# BoxSource — Sourcing Engine (Ujjwal / Claude Code)

> Your build brief. Canonical specs: [`../PRD-sourcing.md`](../PRD-sourcing.md) and [`../background-check-criteria.md`](../background-check-criteria.md). Read both before coding. This file tells you **your half** and where the boundary is.

## Mission
Build the **worker** that turns a `requests` row in Supabase into verified candidate suppliers, a sourced recommendation, and — after human approval — RFQ/PO emails plus ingested quotes, **exactly once, with a source for every fact**. The worker runs on the laptop during the demo; when the judge kills it, it restarts and resumes from Supabase.

## You own
- **The worker**: `engine/worker.py` (claim requests, drive the state machine, resume), `orchestrator.py`, `steps.py` (idempotent runner), `ledger.py` (cost + time), `db.py` (Supabase), `llm.py`, `config.py`, `io_format.py`.
- **Pipeline**: `parse_request.py`, `discover.py`, the 7 checks in `checks/` (`real_active`, `manufacturer`, `duplicate`, `spec_match`, `price`, `distance`, `fraud_risk`), `evidence.py` (quote verification), `rank.py`, `recommend.py`.
- **Actions**: `actions/rfq_email.py` (draft + SMTP send), `actions/purchase_order.py`, `actions/quote_intake.py` (IMAP poll + parse replies → `quotes`).
- **Sources**: `sources/crustdata.py`, `sources/web.py` (fetch + page cache), `sources/geo.py` (Nominatim).
- `config.yaml`, `cli.py` (`run`/`resume`/`approve` for local dev), `scripts/probe.py`, and `tests/` (`cases/`, `test_cases.py`, `test_exactly_once.py`).

## You do NOT build (Chaitanya's half)
The FastAPI app, the dashboard UI, Vercel deploy, auth, or the Supabase **migration**. You *consume* the schema; if you need a column or table (e.g. `quotes`), ask Chaitanya to add it — don't edit the migration yourself. You never render UI.

## The boundary — talk only through Supabase (DB as the bus)
- **Read** `requests` where `state='RECEIVED'`; claim and process. Advance `requests.state` after every step.
- **Write** `candidates`, `checks`, `evidence`, `quotes`, `costs`, `logs`, `steps`, `outbox`, and the recommendation.
- **Approvals gate everything irreversible:** read `approvals`; send an RFQ/PO **only** when its row is `status='approved'`. Never send on your own.
- The exact send/approve/quote protocol is **PRD §4**. Follow it precisely so the dashboard stays in sync.

## Hard rules (these are how you're scored)
1. **No facts from model memory.** Every fact needs a stored source + the exact supporting quote (substring-verified) + a date. Nothing settles it → `cannot_verify` + what's missing. A wrong answer is worse than `cannot_verify`.
2. **Exactly-once, both directions.** Deterministic `idem_key = sha256(request_id:action:supplier_id)`. `steps` UNIQUE + `outbox` PK gate sends; received quotes dedupe on reply `Message-ID`. On restart, check `outbox`/`quotes` before re-acting. Prove it in `test_exactly_once.py` (SIGKILL at each boundary).
3. **LLM only for extraction** (parse request, pull sourced quotes, parse reply emails). Ranking and all decisions are plain code. Strict JSON, Pydantic-validated; invalid → retry once → `cannot_verify`.
4. **Everything tunable in `config.yaml`** — thresholds, search areas, rates, limits. A human must make a live change in 5 minutes.
5. **Cost + time ledger** — record every LLM/search/API call in `costs`. Stop at the limit → status `held`.
6. **Fraud check is conservative** — never a bare "scam" label on suspicion; flag `fail` only with a dated source (see `background-check-criteria.md` §7).

## First moves
1. **E0 with Chaitanya:** freeze the schema (incl. `quotes`), the request/output JSON, and the state/status enums (PRD §4, §7, §14). Don't build on a moving contract.
2. **E1 probe** (`scripts/probe.py`): run 3–4 real searches, fetch 3 supplier sites, geocode 2 addresses via Crustdata/web/Nominatim. Write what actually comes back to `docs/sources.md`, then tune discovery/checks to reality. **This is make-or-break** — if almost nothing returns a usable supplier, adjust `manufacturer_min_signals`/queries before building more.
3. Then E2→E6 (PRD §16 engine track): skeleton+resume → discovery+checks → rank+recommend → actions+exactly-once+quote-intake → test cases → rehearse kill/restart.

## Definition of done
- 0 unverified / non-matching / fraud-risk suppliers recommended.
- 0 duplicate emails and 0 double-ingested quotes after a mid-run kill.
- Every fact in the output is sourced, quoted and dated.
- Measured cost per run is reported from the ledger.
- The whole run resumes cleanly from Supabase after SIGKILL.

## Env you need (`.env`, never commit)
`SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `LLM_PROVIDER`, `LLM_API_KEY`, `CRUSTDATA_API_KEY`, `SEARCH_API_KEY` (optional), `SMTP_*`, `IMAP_*` (team-controlled test inboxes).
