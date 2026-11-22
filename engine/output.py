"""Assemble the output JSON (PRD §7) from the tables and store it in requests.result_json,
which is what the dashboard renders. Narrative fields (summary, recommendation, held
reason, ...) are carried over between refreshes; everything else is rebuilt from rows."""

from .actions import send_key
from .ledger import active_seconds, total_cost
from .steps import now_iso

NARRATIVE = ("summary", "assumptions", "shortlist", "recommendation", "held_reason",
             "not_eligible", "rfqs_sent_at")


def _candidates(db, rid):
    checks, evidence, quotes = {}, {}, {}
    for e in db.select("evidence", {"request_id": rid}, order=["id"]):
        evidence.setdefault((e["supplier_id"], e["check_name"]), []).append(
            {k: e[k] for k in ("fact", "source_url", "source_type", "field_or_quote",
                               "record_date", "retrieved_at")})
    for c in db.select("checks", {"request_id": rid}, order=["check_name"]):
        checks.setdefault(c["supplier_id"], []).append({
            "check": c["check_name"], "result": c["result"], "explanation": c["explanation"],
            "evidence": evidence.get((c["supplier_id"], c["check_name"]), []),
            **({"missing": c["missing"]} if c["missing"] else {})})
    for q in db.select("quotes", {"request_id": rid}, order=["id"]):
        if q["parsed_ok"]:
            quotes[q["supplier_id"]] = q
    out = []
    for c in db.select("candidates", {"request_id": rid}, order=["supplier_id"]):
        d, q = c["data_json"] or {}, quotes.get(c["supplier_id"])
        out.append({
            "supplier_id": c["supplier_id"], "name": c["name"], "website": c["website"],
            "address": c["address"], "classification": c["classification"],
            "merged_into": c["merged_into"], "flags": d.get("flags", []),
            "checks": checks.get(c["supplier_id"], []),
            "listed_price_inr_per_unit": d.get("listed_price_inr_per_unit"),
            "price_basis": d.get("price_basis", "unknown"),
            "quoted_price_inr_per_unit": q["price_inr_per_unit"] if q else None,
            "quote_basis": q["gst_basis"] if q else None,
            "quoted_moq": q["moq"] if q else None,
            "quoted_lead_time_days": q["lead_time_days"] if q else None,
            "quote_source": q["source_quote"] if q else None,
            "quote_received_at": q["received_at"] if q else None,
            "distance_km": d.get("distance_km"),
            "est_transport_inr_per_unit": d.get("est_transport_inr_per_unit"),
            "est_landed_inr_per_unit": d.get("est_landed_inr_per_unit"),
            "rank": d.get("rank"),
        })
    return out


def _actions(db, rid):
    approvals = db.select("approvals", {"request_id": rid}, order=["id"])
    sent = {o["idem_key"] for o in db.select("outbox", {"approval_id": ("in", [a["id"] for a in approvals])})} \
        if approvals else set()
    out = []
    for a in approvals:
        key = send_key(rid, a["action"], a["supplier_id"])
        status = ("done" if key in sent else
                  {"pending": "pending_approval", "rejected": "skipped"}.get(a["status"], "approved"))
        out.append({"type": a["action"], "to_supplier": a["supplier_id"], "status": status,
                    "idempotency_key": key, "approved_by": a["approver"],
                    "approved_at": a["decided_at"], "reason": a["reason"]})
    return out


def refresh(db, cfg, request_id, **narrative):
    req = db.select("requests", {"request_id": request_id})[0]
    old = req["result_json"] or {}
    keep = {k: old.get(k) for k in NARRATIVE}
    keep.update(narrative)
    full = {
        "request_id": request_id, "state": req["state"], "status": req["status"],
        **keep,
        "candidates": _candidates(db, request_id),
        "actions": _actions(db, request_id),
        "cost_inr": total_cost(db, request_id),
        "duration_s": active_seconds(db, request_id),
        "limits": cfg.get("limits"),  # the dashboard's cost/time progress bars
        "updated_at": now_iso(),
    }
    db.update("requests", {"request_id": request_id}, {"result_json": full, "updated_at": now_iso()})
    return full
