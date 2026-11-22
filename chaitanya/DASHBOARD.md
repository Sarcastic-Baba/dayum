# Dashboard — full requirements for Chaitanya's agent

> Build the BoxSource dashboard from this file. It is self-contained: scope, the exact data
> contract (what the sourcing engine writes, field by field), every screen, behaviour rules,
> and the acceptance checklist. Visual design tokens and the stack are in
> [`AGENTS.md`](AGENTS.md) → *Design*; product context is in [`../PRD-sourcing.md`](../PRD-sourcing.md).

## 0. Where things are (already set up)

| What | Where |
|---|---|
| Hosting | Vercel project **`dayum`** → https://dayum-six.vercel.app — connected to `Sarcastic-Baba/dayum`; **every push to `main` deploys** |
| Database | Supabase project **`contextcon`** (ref `xyivrxjdvckfibdfwgza`) — your schema is live; all 12 tables exist |
| Env vars on Vercel (prod, preview, dev) | `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `HEALTHCHECK_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `CRUSTDATA_API_KEY` |
| Still to add (you choose the values) | `DEMO_APPROVER_EMAIL`, `DEMO_APPROVER_PASSWORD`, `DEMO_VIEWER_EMAIL`, `DEMO_VIEWER_PASSWORD` (`vercel env add …`) |
| Sourcing engine | a separate worker on Ujjwal's laptop (`python cli.py run`), reading and writing the same Supabase |

The dashboard reads and writes Supabase **server-side only**, with `SUPABASE_SECRET_KEY` over
PostgREST (the pattern in `app.py` → `/api/ready`). Browser roles have no table access (RLS on,
no policies). Never send the secret key to the browser.

## 1. Scope

**You build:** the FastAPI app (`app.py`) and its pages — login, request list, new-request form,
request detail (live), approvals — deployed on Vercel.

**You never:** compute a check, ranking or recommendation; send an email; or write to
`candidates`, `checks`, `evidence`, `quotes`, `steps`, `outbox`, `costs`, `logs`. The engine owns
those. **The dashboard writes exactly two things:** new `requests` rows, and decisions on
`approvals` rows.

No long-running work in a Vercel function: live updates are the browser polling (htmx), not a
server loop.

## 2. Data contract

### 2.1 `requests` — one row per sourcing request

| Column | Notes |
|---|---|
| `request_id` | text PK. Dashboard requests: `WEB-` + a short unique id. Voice: `VOICE-…`. CLI: as given |
| `input_json` | what was asked (shape below) |
| `spec_json` | the engine's normalized copy of the request; null until the worker parses it |
| `state` | state-machine position — 15 values, §2.6 |
| `status` | `pending` · `recommended` · `awaiting_quotes` · `no_verified_supplier` · `held` |
| `result_json` | **the read model for the detail page** (§2.2); **null until the worker first picks the request up** |
| `created_by` | logged-in username for dashboard requests; `voice` / `cli` for the others |
| `created_at`, `updated_at` | `updated_at` is set by a trigger |

**Creating a request (the form):** insert `{request_id, input_json, created_by}` only — `state`
and `status` default to `RECEIVED` / `pending`, and the worker takes over. `input_json`:

```json
{
  "request_id": "WEB-7K2P9Q",
  "item": "corrugated box",
  "spec": {"ply": 5, "size_in": [18, 12, 12], "material": "kraft", "printing": "none"},
  "quantity_per_month": 50000,
  "delivery_location": "Gurgaon, Haryana",
  "needed_by": "2026-12-15",
  "budget_inr_per_unit": null
}
```

Required fields in the form: item, quantity per month, delivery location, ply, size (L × W × H in
inches). Optional: material, printing, needed-by date, budget. Collect **structured fields** —
the engine does not parse free text yet (a free-text request would end on hold).

Voice requests also carry `input_json.channel = "voice"`, `input_json.call_id` and
`input_json.caller_words` (what the caller said). Show a small "Phone" badge for them. The
caller's phone number is never stored; don't add it.

### 2.2 `result_json` — everything the detail page renders

Rebuilt by the engine after every step. Every key below is present once the worker has touched
the request; values are `null` (or `[]`) until that stage is reached.

```jsonc
{
  "request_id": "WEB-7K2P9Q",
  "state": "WAIT_APPROVAL_RFQ",            // same as requests.state
  "status": "recommended",                // same as requests.status
  "summary": "2 verified manufacturer(s) shortlisted: … RFQs drafted and waiting for approval.",
  "assumptions": ["spec.material not given — needs confirmation"],   // show as callouts
  "shortlist": ["sup_01", "sup_02"],      // eligible supplier ids, in shortlist order
  "recommendation": {                     // two shapes, by stage:
    "stage": "shortlist",                 //  before quotes:
    "send_rfqs_to": ["sup_01", "sup_02"],
    "reasoning": "…", "caveats": ["…"]
    // after quotes: {"stage": "quotes", "primary": "sup_01", "backup": "sup_02",
    //   "trial_order_qty": 5000, "full_monthly_qty": 50000, "reasoning": "…", "caveats": ["…"]}
  },
  "held_reason": null,                    // text when state = HELD
  "not_eligible": [                       // every excluded supplier, with the reason
    {"supplier_id": "sup_03", "name": "…", "reason": "not eligible: manufacturer=cannot_verify"}
  ],
  "rfqs_sent_at": null,                   // ISO time the RFQs went out
  "candidates": [
    {
      "supplier_id": "sup_01", "name": "…", "website": "…", "address": "…",
      "classification": "manufacturer",   // manufacturer | trader | cannot_verify
      "merged_into": null,                // set on a duplicate: id of the row it merged into
      "flags": ["lookalike_name"],        // may be empty
      "checks": [
        {"check": "manufacturer",         // real_active | manufacturer | duplicate | spec_match
                                          //  | price | distance | fraud_risk
         "result": "pass",                // pass | fail | cannot_verify
         "explanation": "…",
         "evidence": [{"fact": "…", "source_url": "…", "source_type": "web_page",
                       "field_or_quote": "exact sentence or API field",
                       "record_date": "2026-10-14", "retrieved_at": "2026-11-22T15:10:00+00:00"}],
         "missing": "…"}                  // only present when result = cannot_verify
      ],
      "listed_price_inr_per_unit": 36.0,  "price_basis": "ex-GST",   // pre-RFQ hint only
      "quoted_price_inr_per_unit": 37.2,  "quote_basis": "ex-GST",   // from the supplier's reply
      "quoted_moq": 10000, "quoted_lead_time_days": 12,
      "quote_source": "Our price is Rs. 37.20 per box ex-GST for 5 ply.",
      "quote_received_at": "2026-11-22T16:05:00+05:30",
      "distance_km": 30,
      "est_transport_inr_per_unit": null, // ESTIMATE — label it
      "est_landed_inr_per_unit": 37.2,    // ESTIMATE — label it
      "rank": 1                           // null until ranked on quotes
    }
  ],
  "actions": [
    {"type": "send_rfq",                  // send_rfq | send_po
     "to_supplier": "sup_01",
     "status": "pending_approval",        // pending_approval | approved | done (sent) | skipped (rejected)
     "idempotency_key": "…", "approved_by": null, "approved_at": null, "reason": null}
  ],
  "cost_inr": 0.0,                        // measured cost so far (ledger)
  "duration_s": 12.4,                     // active processing time (human waiting excluded)
  "updated_at": "2026-11-22T17:20:00+00:00"
}
```

### 2.3 `approvals` — the human gate (the dashboard's only other write)

The engine inserts one **pending** row per irreversible action: one `send_rfq` per shortlisted
supplier, then one `send_po` for the recommended supplier.

| Column | Use |
|---|---|
| `id`, `request_id`, `action`, `supplier_id` | identity (unique on request + action + supplier) |
| `status` | `pending` → `approved` / `rejected` |
| `email_to`, `email_subject`, `email_body` | **the exact email that will be sent** — render it as a letter |
| `approver`, `reason`, `decided_at` | written by the dashboard when deciding |
| `idem_key`, `created_at` | engine bookkeeping; display not needed |

**Deciding:** one UPDATE per row, only while it is pending:

```sql
update approvals
   set status = 'approved' | 'rejected', approver = <logged-in user>,
       reason = <typed reason>, decided_at = now()
 where id = <id> and status = 'pending';
```

- **Reject needs a typed, non-empty reason** — the database enforces it; enforce it in the UI
  too and show the error nicely if it still fails.
- **Never change `email_*` in the decision update** — your trigger `approvals_guard_update`
  rejects it, and a decided row can never change again. Render decided rows read-only.
- **The engine waits until no approval for that action is pending**, then sends every approved
  one exactly once. So the RFQ panel should decide all rows: checkboxes per supplier →
  "Approve selected" plus one reason box for the unticked ones ("Reject the rest").
- Only the **approver** account may decide; the viewer sees everything read-only.

### 2.4 `quotes` — supplier replies (read-only for you)

`supplier_id`, `price_inr_per_unit`, `currency`, `gst_basis` (`ex-GST` | `incl-GST` | `unknown`),
`moq`, `lead_time_days`, `ply`, `size_in`, `source_quote` (the exact line the price came from),
`parsed_ok`, `received_at`, `raw_eml` (the full email — show on demand, e.g. "View email").
A reply with `parsed_ok = false` means "reply received, no price readable — needs a person".

### 2.5 `outbox`, `costs`, `logs` (read-only)

- `outbox`: one row per email **actually sent** (`approval_id`, `created_at`, `message`) → "sent at"
  times for the quote timeline. Join on `approval_id`.
- `costs`: the per-call ledger (`step`, `call_type`, `units`, `inr`, `at`) → cost breakdown.
- `logs`: `message` is a JSON string `{request_id, step, supplier_id, status, duration_ms, cost_inr,
  detail?}` → the action log, newest first.

### 2.6 States → labels (for the pipeline stepper)

| state | label | | state | label |
|---|---|---|---|---|
| RECEIVED | Queued | | RFQS_SENT | RFQs sent |
| SPEC_PARSED | Request understood | | WAIT_QUOTES | Waiting for quotes |
| CANDIDATES_FOUND | Suppliers found | | QUOTES_RECEIVED | Quotes in |
| CANDIDATES_CHECKED | Suppliers verified | | RANKED | Ranked |
| SHORTLISTED | Shortlisted | | WAIT_APPROVAL_PO | Waiting for PO approval |
| RECOMMENDED | Shortlist ready | | PO_SENT | PO sent |
| WAIT_APPROVAL_RFQ | Waiting for RFQ approval | | DONE | Done |
| | | | HELD | On hold (terminal, red) |

`WAIT_*` states mean **the engine is waiting for a person or for supplier replies** — say so in
the UI ("Waiting for your approval", "Waiting for supplier replies"). Status badges: `pending`
slate, `recommended` indigo, `awaiting_quotes` amber, `no_verified_supplier` rose, `held` rose.

## 3. Screens

1. **Login** — email + password against the two demo accounts. Wrong password → error. Logout.
2. **Request list** (`/`) — newest first: id, item + spec summary, location, status badge,
   state label, cost ₹, last update, a Phone badge for voice requests. "New request" button.
3. **New request** — the structured form (§2.1), validation, then redirect to its detail page.
4. **Request detail** (`/requests/{id}`):
   - Header: item + spec, quantity, location, created by, status badge, cost ₹ and time chips
     against the limits (₹25 and 400 s, from `config.yaml` → `limits`).
   - **Live pipeline stepper** (§2.6) with the current step highlighted. Poll a partial every
     2–3 s with htmx **while the state is not DONE or HELD**; stop at those.
   - `result_json` null → "Queued — waiting for the sourcing agent to pick this up".
   - Summary, then assumptions as callouts.
   - **Candidate table** — rank, name, classification badge, seven check dots (green pass, red
     fail, amber cannot_verify, tooltips), listed price (hint) vs quoted price, distance,
     est. landed cost labelled *estimate*. Duplicates (`merged_into`) greyed, with "merged into".
   - **Evidence drawer** (click a row) — each check: icon, result, explanation; each evidence
     item as a quote block with source link, record date, retrieved date; `missing` for
     cannot_verify. This is what the ethics judge looks at.
   - **Not eligible** — every excluded supplier with its plain-English reason.
   - **Recommendation card** — shortlist stage: who gets RFQs; quotes stage: primary, backup,
     trial quantity, reasoning, caveats.
   - **Quote timeline** per supplier — RFQ approved → sent (outbox time) → awaiting reply →
     quote received (price, basis, MOQ, lead time, the exact source line, "View email").
   - **Approvals panel** — pending RFQ/PO approvals: the email rendered as a letter (to,
     subject, body exactly), per-supplier checkboxes, Approve / Reject with reason. Decided
     approvals read-only with approver, time and reason.
   - **HELD** — red banner with `held_reason`, plus the not-eligible list for
     `no_verified_supplier` ("No supplier could be verified — by design, none is recommended").
   - Action log and cost breakdown (collapsible).

## 4. Rules that are scored

- **Approvals:** nothing irreversible without a person; the approver sees the exact text; reject
  needs a reason; the approver's name is recorded (`approvals.approver`).
- **Sources:** every fact renders with its quote, link and dates. Never show a fact without its
  source.
- **Estimates labelled:** transport and landed costs always say *estimate*.
- **Listed vs quoted:** listed price is a pre-RFQ hint; the ranking uses quoted prices — say so.
- **Fairness:** every excluded supplier is listed with its reason.
- **Cost + time visible:** `cost_inr` and `duration_s` on the detail page (finance judge).
- **Privacy:** business contact details only; never show or store a caller's phone number.

## 5. Developing before real data exists

The database starts empty. Ujjwal will seed a demo request (`DEMO-UI-1`) into Supabase,
produced by the real engine from test fixtures, parked at *Waiting for RFQ approval* with two
fixture suppliers, evidence, and pending approvals carrying email previews. Build against it;
it is clearly marked as fixture data and is deleted before the demo. Until then, build against
§2.2 directly.

## 6. Acceptance checklist

- [ ] Login works for approver and viewer; wrong password rejected; viewer cannot decide.
- [ ] List shows every request, newest first, with status, cost and the Phone badge for voice.
- [ ] The form creates a `WEB-…` request; the worker picks it up (state leaves RECEIVED).
- [ ] Detail page polls live and stops at DONE/HELD; handles `result_json = null`.
- [ ] Evidence drawer shows quote, link and dates for every fact.
- [ ] Approvals: letter preview; approve selected + reject the rest with a reason; decided rows
      read-only; trigger errors shown as friendly messages.
- [ ] HELD / no_verified_supplier shows the reason and the not-eligible list.
- [ ] Estimates labelled; listed vs quoted explained; cost and time shown.
- [ ] Responsive to phone width, dark mode, AA contrast, keyboard navigable (see AGENTS.md).
- [ ] Deployed: https://dayum-six.vercel.app; `/api/health` ok; `/api/ready` (Bearer
      `HEALTHCHECK_TOKEN`) reports the database connected.
