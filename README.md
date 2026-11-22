# BoxSource

Packaging supplier sourcing for an Indian logistics company. Replaces work done
by sourcing agents: discover manufacturers, verify evidence, compare quotes and
prepare an order recommendation. See `PRD-sourcing.md` for the planned workflow.

## Current status

**Platform (dashboard + API):** login, request intake (structured or free text),
the live request view (pipeline stepper, candidates, evidence drawer,
recommendation, quote timeline, action log, cost ledger) and the approval gate
for RFQs and the PO are built and tested against fake fixture data. The schema
contract is in `supabase/migrations/`.
**Engine (worker):** built separately (see `ujjwal/CLAUDE.md`). The dashboard
never computes a verdict and never sends email; it renders what the engine
writes to Supabase and records human decisions. No real supplier emails are sent.

## Local setup

Use Python 3.12 (3.13 also works locally):

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app:app --reload
```

Fill `.env` with your Supabase project URL, backend secret key, a randomly
generated `HEALTHCHECK_TOKEN` and `SESSION_SECRET`, and the demo accounts.
Never commit credentials.

## Demo login

The dashboard is behind a login with two accounts, both defined only in
environment variables (`.env` locally, Vercel project settings when deployed) —
never in the code or the repo:

| Account | Variables | Can |
|---|---|---|
| Approver | `DEMO_APPROVER_EMAIL`, `DEMO_APPROVER_PASSWORD` | submit requests, approve or reject RFQs and the PO |
| Viewer | `DEMO_VIEWER_EMAIL`, `DEMO_VIEWER_PASSWORD` | read everything, change nothing (for judges) |

The logged-in email is written to `approvals.approver` with the decision time.
Rejecting needs a typed reason. Approving is refused if the email text changed
after the approver viewed it. `API_TOKEN` lets scripts create and read requests
but can never approve.

## Preview the UI and run tests (no Supabase needed)

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m tests.preview_server   # http://127.0.0.1:8000
```

The preview uses clearly fake fixture rows from `tests/fixtures.py` (log in as
`approver@preview.local` / `preview-approver` or `viewer@preview.local` /
`preview-viewer`; these exist only in the preview script). Fixture data is never
loaded into the live project.

`GET /api/health` checks the application. `GET /api/ready` checks database access
and the requests table; it requires `Authorization: Bearer <HEALTHCHECK_TOKEN>`.

## Supabase

Create or choose a dedicated project, then apply both migrations in order in its
SQL Editor: `20261122000100_boxsource.sql`, then `20261122000200_contract_v1.sql`.
Alternatively, with the Supabase CLI:

```powershell
npx supabase login
npx supabase init
npx supabase link --project-ref YOUR_PROJECT_REF
npx supabase db push
```

The migration enables RLS and denies browser roles access to all application
tables. Only the backend uses the secret key; the browser never sees it.

## Vercel

The root `app.py` exports the FastAPI application. Vercel installs dependencies
from `requirements.txt`; `vercel.json` selects FastAPI.

```powershell
vercel login
vercel link
vercel env add SUPABASE_URL
vercel env add SUPABASE_SECRET_KEY
vercel env add HEALTHCHECK_TOKEN
vercel env add SESSION_SECRET
vercel env add DEMO_APPROVER_EMAIL
vercel env add DEMO_APPROVER_PASSWORD
vercel env add DEMO_VIEWER_EMAIL
vercel env add DEMO_VIEWER_PASSWORD
vercel deploy
```

Select the intended environments when adding variables. Verify the preview's
health and authenticated readiness endpoints before `vercel deploy --prod`.

## Persistence and execution

Supabase replaces the PRD's SQLite database and file outbox for hosted deployment.
The outbox stores test messages with unique action keys. The future action runner
must enforce approvals and atomically persist each message and completion.
The schema alone does not implement exactly-once execution.

Vercel serves HTTP requests. Long sourcing runs must use durable, resumable steps
and a separately designed worker or scheduler; do not rely on in-process background
tasks surviving function termination. No worker is configured yet.

## Planned acceptance criteria

Zero unverified or non-matching supplier recommendations, no duplicate actions
after restart, and a source for every factual claim. Planned sources include
Crustdata/search, public supplier websites, Nominatim and an LLM for extraction.
Measured cost per job will come from the cost ledger once implemented.

