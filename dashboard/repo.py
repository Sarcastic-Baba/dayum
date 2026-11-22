"""Read and write the rows the platform owns (chaitanya/DASHBOARD.md §1–§2).

The dashboard writes exactly two things: new `requests` rows and decisions on `approvals`.
Everything else is rendered from what the engine wrote — no verdict is computed here."""

import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from pydantic import BaseModel, Field, field_validator

from dashboard import settings
from dashboard.db import Conflict, Rejected

TERMINAL_STATES = {"DONE", "HELD"}
CHECK_ORDER = ["real_active", "manufacturer", "duplicate", "spec_match", "price", "distance", "fraud_risk"]
ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: readable aloud


# ---------------------------------------------------------------- input (DASHBOARD.md §2.1)
class Spec(BaseModel):
    ply: int = Field(ge=1, le=9)
    size_in: list[float]
    material: str | None = None
    printing: str | None = None

    @field_validator("size_in")
    @classmethod
    def three_positive_dims(cls, v):
        if len(v) != 3 or any(d <= 0 for d in v):
            raise ValueError("size must be three positive numbers: length, width, height in inches")
        return v


class RequestInput(BaseModel):
    """Structured fields only: the engine does not parse free text yet."""
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{3,40}$")
    item: str = Field(min_length=2, max_length=80)
    spec: Spec
    quantity_per_month: int = Field(gt=0, le=10_000_000)
    delivery_location: str = Field(min_length=2, max_length=120)
    needed_by: date | None = None
    budget_inr_per_unit: float | None = Field(default=None, gt=0)


def new_request_id() -> str:
    """WEB- + 6 readable characters. Generated when the form is rendered, so a double
    submit hits the primary key and returns the same request instead of a duplicate."""
    return "WEB-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(6))


def idem_key(request_id: str, action: str, supplier_id: str) -> str:
    """PRD §13: deterministic action key, identical to the engine's."""
    return hashlib.sha256(f"{request_id}:{action}:{supplier_id}".encode()).hexdigest()


def draft_step_key(request_id: str, action: str, supplier_id: str) -> str:
    """Engine's stored draft: steps.idem_key = sha256(request_id:draft:action:supplier_id)."""
    return hashlib.sha256(f"{request_id}:draft:{action}:{supplier_id}".encode()).hexdigest()


def body_fingerprint(approval: dict) -> str:
    """Hash of the exact email the approver was shown; the decision must quote it back."""
    text = "\n".join(approval.get(k) or "" for k in ("email_to", "email_subject", "email_body"))
    return hashlib.sha256(text.encode()).hexdigest()


def with_draft(approval: dict, draft: dict | None) -> dict:
    """The approval's own email_* columns (the engine fills them); if empty, fall back to the
    engine's stored draft so an older row still shows the exact message it would send."""
    if (approval.get("email_body") or "").strip() or not draft:
        return approval
    return {**approval, "email_to": draft.get("to"), "email_subject": draft.get("subject"),
            "email_body": draft.get("body")}


def _draft_for(db, approval: dict) -> dict | None:
    key = draft_step_key(approval["request_id"], approval["action"], approval["supplier_id"])
    rows = db.select("steps", {"idem_key": f"eq.{key}", "status": "eq.done"}, columns="output_json")
    return (rows[0].get("output_json") or None) if rows else None


# ---------------------------------------------------------------- writes
def create_request(db, data: RequestInput, created_by: str) -> tuple[str, bool]:
    """Insert {request_id, input_json, created_by}; state/status take their defaults
    (RECEIVED / pending) and the worker takes over. Returns (request_id, created)."""
    request_id = data.request_id or new_request_id()
    input_json = data.model_dump(mode="json")
    input_json["request_id"] = request_id
    try:
        db.insert("requests", {"request_id": request_id, "input_json": input_json, "created_by": created_by})
    except Conflict:
        return request_id, False
    return request_id, True


class DecisionError(Exception):
    pass


def decide_approval(db, approval_id: int, decision: str, approver: str,
                    reason: str | None, fingerprint: str | None) -> dict:
    """One UPDATE, only while pending. Approving needs the fingerprint of the exact email
    shown; rejecting needs a typed reason. Database refusals become friendly messages."""
    if decision not in ("approved", "rejected"):
        raise DecisionError("Choose approve or reject.")
    reason = (reason or "").strip() or None
    rows = db.select("approvals", {"id": f"eq.{int(approval_id)}"})
    if not rows:
        raise DecisionError("That approval no longer exists; reload the page.")
    approval = with_draft(rows[0], _draft_for(db, rows[0]))
    if approval["status"] != "pending":
        raise DecisionError(f"Already {approval['status']} by {approval.get('approver') or 'someone'}.")
    if decision == "rejected" and not reason:
        raise DecisionError("A rejection needs a typed reason.")
    if decision == "approved":
        if not (approval.get("email_body") or "").strip():
            raise DecisionError("There is no email draft to review yet, so it cannot be approved.")
        if fingerprint != body_fingerprint(approval):
            raise DecisionError("The email changed since you opened the page. Reload and review it again.")
    try:
        updated = db.update(
            "approvals",
            {"id": f"eq.{int(approval_id)}", "status": "eq.pending"},
            {"status": decision, "approver": approver, "reason": reason,
             "decided_at": datetime.now(timezone.utc).isoformat()},
        )
    except Rejected as exc:
        raise DecisionError(_friendly(str(exc))) from None
    if not updated:
        raise DecisionError("Someone else decided this first; reload to see their decision.")
    return updated[0]


def _friendly(db_message: str) -> str:
    text = db_message.lower()
    if "already decided" in text:
        return "This approval was already decided; decisions are final."
    if "draft changed" in text:
        return "The email draft changed during the decision, so nothing was recorded. Reload and review it."
    if "reason" in text or "check constraint" in text:
        return "A rejection needs a typed reason."
    return "The database refused the decision. Reload and try again."


# ---------------------------------------------------------------- reads
def _in(values) -> str:
    return "in.(" + ",".join('"' + str(v).replace('"', '') + '"' for v in values) + ")"


def spec_summary(input_json: dict | None) -> str:
    """'5-ply · 18 × 12 × 12 in' from the request input."""
    spec = (input_json or {}).get("spec") or {}
    parts = []
    if spec.get("ply"):
        parts.append(f"{spec['ply']}-ply")
    if spec.get("size_in"):
        parts.append(" × ".join(f"{d:g}" for d in spec["size_in"]) + " in")
    return " · ".join(parts)


def list_requests(db, limit: int = 100) -> list[dict]:
    rows = db.select("requests", order="created_at.desc", limit=limit,
                     columns="request_id,input_json,state,status,created_at,updated_at,created_by")
    costs = _cost_by_request(db, [r["request_id"] for r in rows])
    for r in rows:
        inp = r["input_json"] or {}
        r["cost_inr"] = costs.get(r["request_id"], 0.0)
        r["item"] = inp.get("item") or "—"
        r["spec_summary"] = spec_summary(inp)
        r["location"] = inp.get("delivery_location") or "—"
        r["is_voice"] = inp.get("channel") == "voice"
    return rows


def _cost_by_request(db, request_ids) -> dict:
    if not request_ids:
        return {}
    totals: dict[str, float] = {}
    for c in db.select("costs", {"request_id": _in(request_ids)}, columns="request_id,inr"):
        totals[c["request_id"]] = totals.get(c["request_id"], 0.0) + float(c["inr"])
    return totals


def get_request_view(db, request_id: str) -> dict | None:
    """Everything the detail page renders. Candidates, checks and evidence come from their
    tables (always current); narrative fields from result_json (§2.2); approvals, quotes,
    outbox, costs and logs from their tables, so a decision shows immediately."""
    by_req = {"request_id": f"eq.{request_id}"}

    def approvals_and_sent():
        approvals = db.select("approvals", by_req, order="id.asc")
        sent = db.select("outbox", {"approval_id": _in([a["id"] for a in approvals])},
                         columns="idem_key,approval_id,created_at") if approvals else []
        return approvals, sent

    with ThreadPoolExecutor(max_workers=9) as pool:
        jobs = {
            "request": pool.submit(db.select, "requests", by_req),
            "candidates": pool.submit(db.select, "candidates", by_req),
            "checks": pool.submit(db.select, "checks", by_req),
            "evidence": pool.submit(db.select, "evidence", by_req, "*", "id.asc"),
            "quotes": pool.submit(db.select, "quotes", by_req, "*", "received_at.asc"),
            "approvals": pool.submit(approvals_and_sent),
            "costs": pool.submit(db.select, "costs", by_req, "*", "at.asc"),
            "logs": pool.submit(db.select, "logs", by_req, "*", "at.desc", 200),
            "drafts": pool.submit(db.select, "steps", {**by_req, "step": "like.draft:*", "status": "eq.done"},
                                  "idem_key,output_json"),
        }
        got = {name: job.result() for name, job in jobs.items()}
    if not got["request"]:
        return None
    req = got["request"][0]
    result = req.get("result_json")
    narrative = result or {}
    inp = req.get("input_json") or {}
    approvals, sent_rows = got["approvals"]
    sent_at = {o["approval_id"]: o.get("created_at") for o in sent_rows}

    by_supplier = {c.get("supplier_id"): c for c in narrative.get("candidates") or [] if isinstance(c, dict)}
    names = {c["supplier_id"]: c["name"] for c in got["candidates"]}
    names.update({sid: c.get("name") for sid, c in by_supplier.items() if c.get("name")})
    candidates = [_candidate(c, got["checks"], got["evidence"], got["quotes"], by_supplier.get(c["supplier_id"], {}))
                  for c in got["candidates"]]
    candidates.sort(key=lambda c: (c["merged_into"] is not None, c["rank"] is None, c["rank"] or 0, c["name"]))

    drafts = {s["idem_key"]: s.get("output_json") for s in got["drafts"]}
    actions = []
    for a in approvals:
        a = with_draft(a, drafts.get(draft_step_key(request_id, a["action"], a["supplier_id"])))
        key = a.get("idem_key") or idem_key(request_id, a["action"], a["supplier_id"])
        status = {"pending": "pending_approval", "rejected": "skipped"}.get(a["status"], "approved")
        if a["status"] == "approved" and a["id"] in sent_at:
            status = "done"
        actions.append({
            "id": a["id"], "type": a["action"], "to_supplier": a["supplier_id"],
            "supplier_name": names.get(a["supplier_id"], a["supplier_id"]), "status": status,
            "idempotency_key": key, "approved_by": a.get("approver"), "approved_at": a.get("decided_at"),
            "reason": a.get("reason"), "sent_at": sent_at.get(a["id"]),
            "email_to": a.get("email_to"), "email_subject": a.get("email_subject"),
            "email_body": a.get("email_body"), "fingerprint": body_fingerprint(a),
        })

    limits = narrative.get("limits") or settings.limits()
    return {
        "request_id": request_id,
        "state": req["state"],
        "status": req["status"],
        "result_ready": result is not None,
        "held_reason": narrative.get("held_reason"),
        "summary": narrative.get("summary"),
        "input": inp,
        "spec": req.get("spec_json"),
        "spec_summary": spec_summary(inp),
        "is_voice": inp.get("channel") == "voice",
        "caller_words": inp.get("caller_words"),
        "assumptions": narrative.get("assumptions") or [],
        "shortlist": narrative.get("shortlist") or [],
        "not_eligible": narrative.get("not_eligible") or [],
        "rfqs_sent_at": narrative.get("rfqs_sent_at"),
        "candidates": candidates,
        "names": names,
        "recommendation": narrative.get("recommendation"),
        "actions": actions,
        "cost_inr": round(sum(float(c["inr"]) for c in got["costs"]), 2),
        "duration_s": _number(narrative.get("duration_s")),
        "limits": limits,
        "cost_by_type": _cost_by_type(got["costs"]),
        "logs": [_log_line(row) for row in got["logs"]],
        "created_at": req["created_at"],
        "updated_at": req["updated_at"],
        "created_by": req.get("created_by"),
    }


def _candidate(c, checks, evidence, quotes, result_fields) -> dict:
    sid = c["supplier_id"]
    data = {**(c.get("data_json") or {}), **{k: v for k, v in result_fields.items() if v is not None}}
    own_checks = {k["check_name"]: k for k in checks if k["supplier_id"] == sid}
    if not own_checks and result_fields.get("checks"):  # engine read model, if tables are empty
        own_checks = {k["check"]: {**k, "check_name": k["check"]} for k in result_fields["checks"]}
        evidence = [{**e, "supplier_id": sid, "check_name": k["check"]}
                    for k in result_fields["checks"] for e in k.get("evidence") or []]
    ordered = [n for n in CHECK_ORDER if n in own_checks] + sorted(set(own_checks) - set(CHECK_ORDER))
    view_checks = [{
        "check": name, "result": own_checks[name]["result"], "explanation": own_checks[name].get("explanation"),
        "missing": own_checks[name].get("missing"),
        "evidence": [e for e in evidence if e["supplier_id"] == sid and e["check_name"] == name],
    } for name in ordered]
    replies = [q for q in quotes if q["supplier_id"] == sid]
    parsed = [q for q in replies if q.get("parsed_ok")]
    quote = parsed[-1] if parsed else None
    return {
        "supplier_id": sid, "name": c["name"], "website": c.get("website"), "address": c.get("address"),
        "classification": c["classification"], "merged_into": c.get("merged_into"),
        "flags": data.get("flags") or [],
        "checks": view_checks,
        "listed_price_inr_per_unit": data.get("listed_price_inr_per_unit"),
        "price_basis": data.get("price_basis"),
        "quoted_price_inr_per_unit": quote["price_inr_per_unit"] if quote else data.get("quoted_price_inr_per_unit"),
        "quote_basis": quote["gst_basis"] if quote else data.get("quote_basis"),
        "quoted_moq": quote["moq"] if quote else data.get("quoted_moq"),
        "quoted_lead_time_days": quote["lead_time_days"] if quote else data.get("quoted_lead_time_days"),
        "quote_source": quote["source_quote"] if quote else data.get("quote_source"),
        "quote_received_at": quote["received_at"] if quote else data.get("quote_received_at"),
        "quote_email": quote.get("raw_eml") if quote else None,
        "unparsed_replies": [{"received_at": q.get("received_at"), "raw_eml": q.get("raw_eml")}
                             for q in replies if not q.get("parsed_ok")],
        "distance_km": data.get("distance_km"),
        "est_transport_inr_per_unit": data.get("est_transport_inr_per_unit"),
        "est_landed_inr_per_unit": data.get("est_landed_inr_per_unit"),
        "rank": data.get("rank"),
    }


def _log_line(row: dict) -> dict:
    """logs.message is a JSON string {request_id, step, supplier_id, status, duration_ms,
    cost_inr, detail?}; plain text is shown as-is."""
    try:
        parsed = json.loads(row.get("message") or "")
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return {"at": row.get("at"), "level": row.get("level"), "text": row.get("message")}
    return {"at": row.get("at"), "level": row.get("level"), "step": parsed.get("step"),
            "supplier_id": parsed.get("supplier_id"), "status": parsed.get("status"),
            "duration_ms": parsed.get("duration_ms"), "cost_inr": parsed.get("cost_inr"),
            "detail": parsed.get("detail")}


def _cost_by_type(costs) -> list[dict]:
    groups: dict[str, dict] = {}
    for c in costs:
        g = groups.setdefault(c["call_type"], {"call_type": c["call_type"], "calls": 0, "inr": 0.0})
        g["calls"] += 1
        g["inr"] += float(c["inr"])
    return sorted(groups.values(), key=lambda g: -g["inr"])


def _number(value) -> float | None:
    """Engine ledger values; never derived from timestamps (that would count human waiting)."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def approvals_version(view: dict) -> str:
    """Changes whenever an approval appears, is decided, or is sent — the approvals panel
    re-renders only then, so a half-filled form is never replaced under the user."""
    parts = [f"{a['id']}:{a['status']}:{a['fingerprint']}" for a in view["actions"]]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
