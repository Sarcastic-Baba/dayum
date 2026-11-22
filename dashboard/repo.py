"""Read and write the rows the platform owns. No verdicts are computed here:
the dashboard only renders what the engine wrote and records human decisions."""

import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

from dashboard.db import Conflict

TERMINAL_STATES = {"DONE", "HELD"}
CHECK_ORDER = ["real_active", "manufacturer", "duplicate", "spec_match", "price", "distance", "fraud_risk"]


# ---------------------------------------------------------------- input (PRD §7)
class Spec(BaseModel):
    ply: int | None = Field(default=None, ge=1, le=9)
    size_in: list[float] | None = None
    material: str | None = None
    printing: str | None = None

    @field_validator("size_in")
    @classmethod
    def three_positive_dims(cls, v):
        if v is not None and (len(v) != 3 or any(d <= 0 for d in v)):
            raise ValueError("size_in must be three positive numbers [L, W, H] in inches")
        return v


class RequestInput(BaseModel):
    """The PRD §7 input. Missing fields stay null; the engine lists them as assumptions.
    `free_text` carries an unstructured request for parse_request.py."""
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{3,40}$")
    item: str | None = None
    spec: Spec = Field(default_factory=Spec)
    quantity_per_month: int | None = Field(default=None, gt=0)
    delivery_location: str | None = None
    needed_by: date | None = None
    budget_inr_per_unit: float | None = Field(default=None, gt=0)
    free_text: str | None = None

    @model_validator(mode="after")
    def enough_to_start(self):
        if not (self.free_text or "").strip() and not (
                self.item and self.quantity_per_month and self.delivery_location):
            raise ValueError("give either a free-text request, or item, quantity per month and delivery location")
        return self


def new_request_id() -> str:
    """Generated when the form is rendered, so a double-submit hits the primary key
    and returns the same request instead of creating a duplicate."""
    return "SRC-" + datetime.now(timezone.utc).strftime("%y%m%d") + "-" + secrets.token_hex(3).upper()


def idem_key(request_id: str, action: str, supplier_id: str) -> str:
    """PRD §13: deterministic action key, identical to the engine's."""
    return hashlib.sha256(f"{request_id}:{action}:{supplier_id}".encode()).hexdigest()


def body_fingerprint(approval: dict) -> str:
    """Hash of the exact email the approver was shown; the decision must quote it back."""
    text = "\n".join(approval.get(k) or "" for k in ("email_to", "email_subject", "email_body"))
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------- writes
def create_request(db, data: RequestInput, created_by: str) -> tuple[str, bool]:
    """Insert a RECEIVED request. Returns (request_id, created); created is False when
    the same id already existed (a repeated submit)."""
    request_id = data.request_id or new_request_id()
    input_json = data.model_dump(mode="json", exclude_none=False)
    input_json["request_id"] = request_id
    if not input_json.get("free_text"):
        input_json.pop("free_text", None)
    try:
        db.insert("requests", {"request_id": request_id, "input_json": input_json,
                               "state": "RECEIVED", "status": "pending", "created_by": created_by})
    except Conflict:
        return request_id, False
    return request_id, True


class DecisionError(Exception):
    pass


def decide_approval(db, approval_id: int, decision: str, approver: str,
                    reason: str | None, fingerprint: str | None) -> dict:
    """Record a human decision on one pending approval. Approving requires the
    fingerprint of the exact email text shown; rejecting requires a typed reason."""
    if decision not in ("approved", "rejected"):
        raise DecisionError("decision must be approve or reject")
    reason = (reason or "").strip() or None
    rows = db.select("approvals", {"id": f"eq.{int(approval_id)}"})
    if not rows:
        raise DecisionError("approval not found")
    approval = rows[0]
    if approval["status"] != "pending":
        raise DecisionError(f"already {approval['status']} by {approval.get('approver')}")
    if decision == "rejected" and not reason:
        raise DecisionError("a rejection needs a typed reason")
    if decision == "approved":
        if not (approval.get("email_body") or "").strip():
            raise DecisionError("no email draft to preview yet, so it cannot be approved")
        if fingerprint != body_fingerprint(approval):
            raise DecisionError("the email changed since you viewed it; reload and review again")
    updated = db.update(
        "approvals",
        {"id": f"eq.{int(approval_id)}", "status": "eq.pending"},
        {"status": decision, "approver": approver, "reason": reason,
         "decided_at": datetime.now(timezone.utc).isoformat()},
    )
    if not updated:
        raise DecisionError("someone else decided this approval first; reload")
    return updated[0]


# ---------------------------------------------------------------- reads
def list_requests(db, limit: int = 100) -> list[dict]:
    rows = db.select("requests", order="created_at.desc", limit=limit,
                     columns="request_id,input_json,state,status,created_at,updated_at,created_by")
    costs = _cost_by_request(db, [r["request_id"] for r in rows])
    for r in rows:
        r["cost_inr"] = costs.get(r["request_id"], 0.0)
        r["item"] = (r["input_json"] or {}).get("item") or "(free text)"
        r["location"] = (r["input_json"] or {}).get("delivery_location") or "-"
    return rows


def _in(values) -> str:
    return "in.(" + ",".join('"' + str(v).replace('"', '') + '"' for v in values) + ")"


def _cost_by_request(db, request_ids) -> dict:
    if not request_ids:
        return {}
    totals: dict[str, float] = {}
    for c in db.select("costs", {"request_id": _in(request_ids)}, columns="request_id,inr"):
        totals[c["request_id"]] = totals.get(c["request_id"], 0.0) + float(c["inr"])
    return totals


def get_request_view(db, request_id: str) -> dict | None:
    """Assemble the PRD §7 output shape (plus UI extras) from all tables."""
    by_req = {"request_id": f"eq.{request_id}"}

    def approvals_and_sent():
        approvals = db.select("approvals", by_req, order="id.asc")
        sent = {o["idem_key"] for o in db.select("outbox", {"approval_id": _in([a["id"] for a in approvals])},
                                                 columns="idem_key,approval_id")} if approvals else set()
        return approvals, sent

    # Independent reads run concurrently: the page polls every few seconds.
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {
            "request": pool.submit(db.select, "requests", by_req),
            "candidates": pool.submit(db.select, "candidates", by_req),
            "checks": pool.submit(db.select, "checks", by_req),
            "evidence": pool.submit(db.select, "evidence", by_req, "*", "id.asc"),
            "quotes": pool.submit(db.select, "quotes", by_req, "*", "received_at.asc"),
            "approvals": pool.submit(approvals_and_sent),
            "costs": pool.submit(db.select, "costs", by_req, "*", "at.asc"),
            "logs": pool.submit(db.select, "logs", by_req, "*", "at.desc", 200),
        }
        got = {name: job.result() for name, job in jobs.items()}
    if not got["request"]:
        return None
    req = got["request"][0]
    candidates, checks, evidence, quotes = got["candidates"], got["checks"], got["evidence"], got["quotes"]
    (approvals, outbox_keys), costs, logs = got["approvals"], got["costs"], got["logs"]

    result = req.get("result_json") or {}
    result_by_supplier = {c.get("supplier_id"): c for c in result.get("candidates") or [] if isinstance(c, dict)}
    names = {c["supplier_id"]: c["name"] for c in candidates}
    view_candidates = [_candidate(c, checks, evidence, quotes, result_by_supplier.get(c["supplier_id"], {}))
                       for c in candidates]
    view_candidates.sort(key=lambda c: (c["rank"] is None, c["rank"] or 0, c["merged_into"] is not None, c["name"]))

    actions = []
    for a in approvals:
        key = a.get("idem_key") or idem_key(request_id, a["action"], a["supplier_id"])
        status = {"pending": "pending_approval", "rejected": "skipped"}.get(a["status"], "approved")
        if a["status"] == "approved" and key in outbox_keys:
            status = "done"
        actions.append({
            "id": a["id"], "type": a["action"], "to_supplier": a["supplier_id"],
            "supplier_name": names.get(a["supplier_id"], a["supplier_id"]), "status": status,
            "idempotency_key": key, "approved_by": a.get("approver"), "approved_at": a.get("decided_at"),
            "reason": a.get("reason"), "email_to": a.get("email_to"), "email_subject": a.get("email_subject"),
            "email_body": a.get("email_body"), "fingerprint": body_fingerprint(a),
        })

    cost_inr = round(sum(float(c["inr"]) for c in costs), 2)
    return {
        "request_id": request_id,
        "state": req["state"],
        "status": req["status"],
        "status_reason": result.get("held_reason") or result.get("status_reason"),
        "summary": result.get("summary"),
        "input": req["input_json"],
        "spec": req.get("spec_json"),
        "assumptions": result.get("assumptions") or (req.get("spec_json") or {}).get("assumptions") or [],
        "candidates": view_candidates,
        "recommendation": result.get("recommendation"),
        "actions": actions,
        "cost_inr": cost_inr,
        "duration_s": _duration(result),
        "limits": result.get("limits") or {},  # engine copies config.limits here, for the progress bars
        "cost_by_type": _cost_by_type(costs),
        "logs": logs,
        "created_at": req["created_at"],
        "updated_at": req["updated_at"],
        "created_by": req.get("created_by"),
    }


def _candidate(c, checks, evidence, quotes, result_fields) -> dict:
    sid = c["supplier_id"]
    data = {**(c.get("data_json") or {}), **{k: v for k, v in result_fields.items() if v is not None}}
    own_checks = {k["check_name"]: k for k in checks if k["supplier_id"] == sid}
    ordered = [n for n in CHECK_ORDER if n in own_checks] + sorted(set(own_checks) - set(CHECK_ORDER))
    view_checks = [{
        "check": name, "result": own_checks[name]["result"], "explanation": own_checks[name]["explanation"],
        "missing": own_checks[name].get("missing"),
        "evidence": [e for e in evidence if e["supplier_id"] == sid and e["check_name"] == name],
    } for name in ordered]
    valid_quotes = [q for q in quotes if q["supplier_id"] == sid and q.get("parsed_ok")]
    quote = valid_quotes[-1] if valid_quotes else None
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
        "unparsed_replies": sum(1 for q in quotes if q["supplier_id"] == sid and not q.get("parsed_ok")),
        "distance_km": data.get("distance_km"),
        "est_transport_inr_per_unit": data.get("est_transport_inr_per_unit"),
        "est_landed_inr_per_unit": data.get("est_landed_inr_per_unit"),
        "rank": data.get("rank"),
    }


def _cost_by_type(costs) -> list[dict]:
    groups: dict[str, dict] = {}
    for c in costs:
        g = groups.setdefault(c["call_type"], {"call_type": c["call_type"], "calls": 0, "inr": 0.0})
        g["calls"] += 1
        g["inr"] += float(c["inr"])
    return sorted(groups.values(), key=lambda g: -g["inr"])


def _duration(result) -> float | None:
    """Run time as recorded by the engine's ledger (result_json.duration_s). Not derived
    from timestamps: wall-clock time would include hours spent waiting for a human."""
    try:
        return float(result["duration_s"]) if result.get("duration_s") is not None else None
    except (TypeError, ValueError):
        return None
