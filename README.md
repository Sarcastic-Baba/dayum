# BoxSource

Packaging supplier sourcing for an Indian logistics company. Replaces work done
by sourcing agents: discover manufacturers, verify evidence, compare quotes and
prepare an order recommendation. See `PRD-sourcing.md` for the planned workflow.

## Current status

Deployment foundation only: FastAPI health endpoints and an initial Supabase
Postgres migration. Discovery, sourcing, authentication, approvals and execution
are not implemented yet. No real supplier emails are sent.

## Local setup

Use Python 3.12:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app:app --reload
```

Fill `.env` with your Supabase project URL, backend secret key and a randomly
generated `HEALTHCHECK_TOKEN`. Never commit credentials.

`GET /api/health` checks the application. `GET /api/ready` checks database access
and the requests table; it requires `Authorization: Bearer <HEALTHCHECK_TOKEN>`.

## Supabase

Create or choose a dedicated project, then apply
`supabase/migrations/20261122000100_boxsource.sql` in its SQL Editor.
Alternatively, with the Supabase CLI:

```powershell
npx supabase login
npx supabase init
npx supabase link --project-ref YOUR_PROJECT_REF
npx supabase db push
```

The migration enables RLS and denies browser roles access to all application
tables. Only the backend uses the secret key. User authentication and authorization
must be implemented before adding data or approval endpoints.

## Vercel

The root `app.py` exports the FastAPI application. Vercel installs dependencies
from `requirements.txt`; `vercel.json` selects FastAPI.

```powershell
vercel login
vercel link
vercel env add SUPABASE_URL
vercel env add SUPABASE_SECRET_KEY
vercel env add HEALTHCHECK_TOKEN
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

