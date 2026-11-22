# BoxSource — Platform & Dashboard (Chaitanya / Codex)

> **Start with [`DASHBOARD.md`](DASHBOARD.md)** — the full dashboard requirements and the exact
> data contract the engine writes. This file is the brief and the visual design spec.

> Your build brief. Canonical specs: [`../PRD-sourcing.md`](../PRD-sourcing.md) and [`../background-check-criteria.md`](../background-check-criteria.md). Read both before coding. This file tells you **your half** and where the boundary is. You've already scaffolded `app.py`, the Supabase migration and the Vercel config — this continues from there.

## Mission
Build the **hosted surface** a human uses to run BoxSource: the Supabase schema, the FastAPI API, and the Vercel dashboard. The user submits a request, watches the sourcing run fill in, and **approves or rejects the irreversible actions** (sending RFQs, sending the PO). You render and collect; you never compute a verdict or send an email.

## You own
- **Supabase**: the project, the schema and all migrations (`supabase/migrations/*.sql`). You own the **canonical schema — it is the contract.** **Engine needs, as a new migration (drop-in SQL in PRD §14): the `quotes` table and a `requests.result_json` column** (the assembled output the dashboard renders). RLS stays on; only `service_role` has access.
- **API** (`app.py`, FastAPI on Vercel): `POST /requests` (create a request → `requests` row, `state=RECEIVED`), `GET /requests/{id}` (assemble the full view from all tables), `POST /approvals/{id}` (approve/reject with a typed reason), and the existing `/api/health` + `/api/ready`.
- **Dashboard** (`dashboard/`): request list; request detail — parsed spec + assumptions, candidate table (rank, classification badge, listed vs **quoted** price, distance, est. landed cost), each candidate expanding to its checks (green/red/grey) with evidence (fact, quote, link, date), recommendation + caveats, pending approvals (per-supplier RFQ tick-boxes + email preview, PO preview, approve/reject), cost + time, action log.
- **Auth** and **Vercel deploy** (env vars, build, preview → prod).

## You do NOT build (Ujjwal's half)
The sourcing engine/worker: discovery, the 7 checks, evidence/quote verification, ranking, recommendation, RFQ/PO sending, or quote parsing. You **render what the engine wrote** and **collect approvals**; you never decide pass/fail or send email. Don't put long-running sourcing work inside a Vercel function — the engine is a separate worker.

## The boundary — talk only through Supabase (DB as the bus)
- **You write:** `requests` (on create) and `approvals` (on approve/reject, with `approver`, `reason`, `decided_at`).
- **You read & render:** `candidates`, `checks`, `evidence`, `quotes`, the recommendation, `costs`, `logs`, and the draft email bodies in `steps.output_json` (show the **exact** text before anything is sent).
- **You never** set a check result, write a quote, or insert into `outbox`. The full protocol is **PRD §4** — the engine flips `requests.state` and creates `pending` approval rows; you surface them and record the human's decision.

## Hard rules (these are how you're scored)
1. **Approval before irreversible actions.** The dashboard is the gate for `send_rfq` (per supplier) and `send_po`. Rejection **requires a typed reason** (the schema enforces it — surface the error cleanly). The approver must see the exact email text first.
2. **Show sources, show estimates as estimates.** Every fact renders with its quote, link and date. Transport/landed costs are labelled estimates (the engine marks them) — never present them as sourced facts.
3. **Cost + time visible.** Render `cost_inr` and `duration_s` from the ledger; the finance judge looks here.
4. **Don't break the contract.** Any schema change is coordinated with Ujjwal and reflected in `PRD-sourcing.md`. Keep `state`/`status`/enum values exactly as the spec says.
5. **Secrets stay server-side.** Only the backend uses `SUPABASE_SECRET_KEY`; browser roles have no table access (RLS). Never expose the service key to the client.

## Design — make it beautiful (you own the look)
The dashboard is what the judges watch for the whole 3-minute demo. Aim for a clean, modern, trustworthy B2B product (think Linear or the Stripe dashboard), **not** a default-HTML admin page. The team is not UI-focused — this is entirely your call, held to the bar below.

**Stack (no build step, Vercel-friendly):** Jinja2 templates served by FastAPI + Tailwind CSS + htmx (live partial refresh) + Alpine.js (tabs, modals, drawers) + Lucide icons + the Inter font.

**Design tokens — define once, use everywhere:**
- Neutral grays for surfaces and text; **one** accent (indigo `#4F46E5`); semantic colours: pass = emerald, fail = rose, cannot_verify = amber, pending = slate.
- Cards 12px radius, inputs/buttons 8px; soft shadows; 8px spacing grid; generous whitespace.
- `tabular-nums` on every price, quantity and cost; ₹ formatting with Indian digit grouping.
- Light **and** dark mode (follow `prefers-color-scheme`, toggle in the header).

**Screens:**
1. **Request list** — table: id, item, location, status pill, cost ₹, last update. A "New request" button opens a form (structured fields plus a free-text box).
2. **Request detail**
   - Header: item + spec summary, delivery location, status pill, cost and time chips with limit progress bars.
   - **Live pipeline stepper** across the top showing the state machine (Received → Parsed → Found → Checked → Shortlisted → RFQ approval → RFQs sent → Awaiting quotes → Quotes in → Ranked → PO approval → PO sent → Done), the current step pulsing. Refresh every 2–3 s with htmx while a run is active — **the page filling in live is the demo's wow moment.**
   - Candidate table: rank, name, classification badge, seven small check dots (green/red/amber, tooltips), listed vs **quoted** price, distance, est. landed cost labelled *estimate*.
   - Click a candidate → **evidence drawer** (slide-over): each check with icon, result and explanation; each fact as a quote block with source link, record date and retrieved date; `cannot_verify` shows what's missing.
   - **Quote timeline** per supplier: RFQ sent → awaiting reply → quote received (price, MOQ, lead time, link to the email source).
   - Recommendation card: primary + backup, trial quantity, reasoning, caveats as callouts.
   - **Approvals panel:** the RFQ email rendered like a letter, per-supplier checkboxes, Approve (primary button) / Reject (modal that requires a typed reason). Same pattern for the PO.
   - Action log at the bottom.
3. **States:** skeleton loaders, friendly empty states, toasts on approve/reject, clear error messages.

**Quality bar:** responsive to phone width (no horizontal scroll), keyboard navigable with visible focus rings, AA contrast, aria labels on icon-only buttons, consistent alignment, no unstyled browser defaults.

## Demo login (test credentials for the demo)
- Put the dashboard behind a login (Supabase Auth email + password, or a simple session login in FastAPI).
- Seed **two test accounts**: an **approver** (can approve/reject) and a **viewer** (read-only, for judges to click around). Credentials come from `.env` (`DEMO_APPROVER_EMAIL`, `DEMO_APPROVER_PASSWORD`, `DEMO_VIEWER_EMAIL`, `DEMO_VIEWER_PASSWORD`) — never hard-coded or committed.
- The logged-in user's name is written to `approvals.approver`, so every approval is attributable (this feeds the ethics score).
- Test the login flow end to end: wrong password rejected, viewer cannot approve, approver can, logout works.
- Add a short "Demo login" section to the README saying where the test credentials live (not the credentials themselves).

## First moves
1. **P0 with Ujjwal:** freeze the schema (add `quotes`), the request/output JSON, and the state/status enums (PRD §4, §7, §14). Apply the migration; `/api/ready` green against the live project.
2. **P1:** `POST /requests` + `GET /requests/{id}` reading from Supabase; seed a couple of fake candidate/check/evidence rows so you can build the UI before the engine is ready.
3. Then P3→P6 (PRD §16 platform track): list + detail render → approvals UI → cost/time + action log → deploy and verify against the live worker.

## Definition of done
A judge can, from the deployed dashboard: submit a request; watch candidates, checks and evidence appear; read the recommendation with caveats; approve RFQs per supplier and the PO (each with a preview, reject with a typed reason); and see cost + time — all driven live from Supabase, with the engine running as a separate worker.

## Env (`.env`, never commit)
`SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `HEALTHCHECK_TOKEN` (already in use by `/api/ready`), and the demo login accounts: `DEMO_APPROVER_EMAIL`, `DEMO_APPROVER_PASSWORD`, `DEMO_VIEWER_EMAIL`, `DEMO_VIEWER_PASSWORD`.
