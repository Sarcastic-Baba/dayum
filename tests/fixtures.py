"""FAKE rows for tests and local UI preview only. Every name, URL, price and email here is
invented and marked as such — never load these into the live project. Shapes follow
chaitanya/DASHBOARD.md §2 (what the engine writes)."""

import hashlib
import json

RFQ_REQ = "FIXTURE-RFQ"
PO_REQ = "FIXTURE-PO"
HELD_REQ = "FIXTURE-HELD"
VOICE_REQ = "VOICE-FIXTURE"
DONE_REQ = "FIXTURE-DONE"

NAMES = {"sup_01": "Fixture Corrugators Pvt Ltd (FAKE)", "sup_02": "Sample Packaging Industries (FAKE)",
         "sup_03": "Fixture Corrugators Traders (FAKE)", "sup_04": "Fixture Corrugators Pvt. Ltd. (FAKE listing)"}
SPEC = {"ply": 5, "size_in": [18, 12, 12], "material": "kraft", "printing": "none"}


def _key(*parts):
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def _input(request_id, **extra):
    return {"request_id": request_id, "item": "corrugated box", "spec": dict(SPEC), "quantity_per_month": 50000,
            "delivery_location": "Gurgaon, Haryana", "needed_by": "2026-12-15", "budget_inr_per_unit": None, **extra}


def _request(request_id, state, status, result=None, created_by="approver@test.invalid", **input_extra):
    return {"request_id": request_id, "state": state, "status": status, "created_by": created_by,
            "input_json": _input(request_id, **input_extra), "spec_json": _input(request_id),
            "result_json": result, "created_at": "2026-11-22T09:30:00+00:00", "updated_at": "2026-11-22T09:45:00+00:00"}


def _result(request_id, state, status, **narrative):
    base = {"request_id": request_id, "state": state, "status": status, "summary": None, "assumptions": [],
            "shortlist": [], "recommendation": None, "held_reason": None, "not_eligible": [],
            "rfqs_sent_at": None, "candidates": [], "actions": [], "cost_inr": 0.0, "duration_s": None,
            "updated_at": "2026-11-22T09:45:00+00:00"}
    base.update(narrative)
    return base


def _candidates(request_id):
    rows = [
        {"supplier_id": "sup_01", "website": "https://example.com/fixture-corrugators",
         "address": "Plot 1, Fake Industrial Area, Manesar", "classification": "manufacturer",
         "data_json": {"listed_price_inr_per_unit": 36.0, "price_basis": "ex-GST", "distance_km": 30}},
        {"supplier_id": "sup_02", "website": "https://example.org/sample-packaging",
         "address": "Fake Road, Bawal", "classification": "manufacturer", "data_json": {"distance_km": 88}},
        {"supplier_id": "sup_03", "website": "https://example.net/fixture-traders", "address": "Fake Market, Delhi",
         "classification": "trader", "data_json": {"flags": ["lookalike_name"]}},
        {"supplier_id": "sup_04", "website": "https://example.com/fixture-corrugators", "address": None,
         "classification": "manufacturer", "merged_into": "sup_01", "data_json": {}},
    ]
    return [{"request_id": request_id, "name": NAMES[r["supplier_id"]], **r} for r in rows]


PLAN = {
    "sup_01": {"real_active": "pass", "manufacturer": "pass", "duplicate": "pass", "spec_match": "pass",
               "price": "pass", "distance": "pass", "fraud_risk": "pass"},
    "sup_02": {"real_active": "pass", "manufacturer": "pass", "duplicate": "pass", "spec_match": "pass",
               "price": "cannot_verify", "distance": "pass", "fraud_risk": "pass"},
    "sup_03": {"real_active": "pass", "manufacturer": "fail", "duplicate": "pass", "spec_match": "cannot_verify",
               "price": "cannot_verify", "distance": "cannot_verify", "fraud_risk": "cannot_verify"},
}


def _checks(request_id, plan=PLAN):
    rows, evidence = [], []
    for sid, results in plan.items():
        for name, result in results.items():
            rows.append({"request_id": request_id, "supplier_id": sid, "check_name": name, "result": result,
                         "explanation": f"FAKE fixture explanation for {name}.",
                         "missing": "FAKE: no dated source states this" if result == "cannot_verify" else None})
            if result != "cannot_verify":
                evidence.append({"request_id": request_id, "supplier_id": sid, "check_name": name,
                                 "fact": f"FAKE fact supporting {name}",
                                 "source_url": f"https://example.com/{sid}/{name}", "source_type": "web_page",
                                 "field_or_quote": "We manufacture 3, 5 and 7 ply corrugated boxes (FAKE QUOTE).",
                                 "record_date": "2026-10-14", "retrieved_at": "2026-11-22T09:40:00+00:00"})
    return rows, evidence


def _draft(request_id, action, sid):
    kind = "Request for quotation" if action == "send_rfq" else "Purchase order"
    return {"request_id": request_id, "action": action, "supplier_id": sid, "status": "pending",
            "idem_key": _key(request_id, action, sid), "email_to": "rfq-test@example.invalid",
            "email_subject": f"{kind} [{request_id}/{sid}] — corrugated box",
            "email_body": f"Dear {NAMES[sid]},\n\nFAKE {kind.lower()} body for tests.\n"
                          f"Item: corrugated box, 5-ply kraft, 18 × 12 × 12 in.\nQuantity: 50,000 per month, "
                          f"delivered to Gurgaon, Haryana.\n\nPlease reply with price per box (ex-GST), MOQ and "
                          f"lead time.\n\nRegards,\nBoxSource (test)"}


def _log(request_id, step, status, supplier_id=None, ms=120, cost=0.0, at="2026-11-22T09:41:00+00:00"):
    return {"request_id": request_id, "level": "info", "at": at, "message": json.dumps(
        {"request_id": request_id, "step": step, "supplier_id": supplier_id, "status": status,
         "duration_ms": ms, "cost_inr": cost})}


NOT_ELIGIBLE = [{"supplier_id": "sup_03", "name": NAMES["sup_03"],
                 "reason": "not eligible: manufacturer=fail (calls itself a trader; no manufacturing signal)"}]


def tables() -> dict:
    out = {k: [] for k in ("requests", "candidates", "checks", "evidence", "approvals", "steps",
                           "outbox", "quotes", "costs", "logs")}

    # 1. Waiting for RFQ approval.
    out["requests"].append(_request(RFQ_REQ, "WAIT_APPROVAL_RFQ", "recommended", _result(
        RFQ_REQ, "WAIT_APPROVAL_RFQ", "recommended",
        summary="FAKE: 2 verified manufacturer(s) shortlisted. RFQs drafted and waiting for approval.",
        assumptions=["FAKE: printing confirmed as none by the client?"], shortlist=["sup_01", "sup_02"],
        not_eligible=NOT_ELIGIBLE, duration_s=96,
        recommendation={"stage": "shortlist", "send_rfqs_to": ["sup_01", "sup_02"],
                        "reasoning": "FAKE: both passed every eligibility check.",
                        "caveats": ["FAKE: listed price is a hint only; ranking waits for quotes."]})))

    # 2. Quotes in, waiting for PO approval.
    out["requests"].append(_request(PO_REQ, "WAIT_APPROVAL_PO", "recommended", _result(
        PO_REQ, "WAIT_APPROVAL_PO", "recommended",
        summary="FAKE: 2 quote(s) compared. Recommended: Fixture Corrugators.", shortlist=["sup_01", "sup_02"],
        not_eligible=NOT_ELIGIBLE, rfqs_sent_at="2026-11-22T09:52:00+00:00", duration_s=210,
        recommendation={"stage": "quotes", "primary": "sup_01", "backup": "sup_02", "trial_order_qty": 5000,
                        "full_monthly_qty": 50000, "reasoning": "FAKE reasoning.",
                        "caveats": ["FAKE caveat: ranked on quotes received 22 Nov 2026"]},
        candidates=[{"supplier_id": "sup_01", "rank": 1, "est_landed_inr_per_unit": 38.1},
                    {"supplier_id": "sup_02", "rank": 2, "est_landed_inr_per_unit": 39.6}])))

    for req in (RFQ_REQ, PO_REQ):
        out["candidates"] += _candidates(req)
        checks, evidence = _checks(req)
        out["checks"] += checks
        out["evidence"] += evidence
        out["costs"] += [{"request_id": req, "step": "discover", "call_type": "grounded_search", "units": 3, "inr": 1.5,
                          "at": "2026-11-22T09:31:00+00:00"},
                         {"request_id": req, "step": "checks", "call_type": "llm", "units": 12, "inr": 2.25,
                          "at": "2026-11-22T09:35:00+00:00"}]
        out["logs"] += [_log(req, "discover", "done", ms=2100, cost=1.5),
                        _log(req, "check:manufacturer", "fail", "sup_03", at="2026-11-22T09:42:00+00:00")]

    out["approvals"] += [_draft(RFQ_REQ, "send_rfq", "sup_01"), _draft(RFQ_REQ, "send_rfq", "sup_02")]

    for n, (sid, price) in enumerate((("sup_01", 37.2), ("sup_02", 37.8)), start=1):
        approval = {**_draft(PO_REQ, "send_rfq", sid), "id": 900 + n, "status": "approved",
                    "approver": "approver@test.invalid", "decided_at": "2026-11-22T09:50:00+00:00"}
        out["approvals"].append(approval)
        out["steps"].append({"request_id": PO_REQ, "step": f"send_rfq:{sid}", "idem_key": approval["idem_key"], "status": "done"})
        out["outbox"].append({"idem_key": approval["idem_key"], "approval_id": approval["id"], "message": "FAKE eml",
                              "created_at": "2026-11-22T09:52:00+00:00"})
        out["quotes"].append({"request_id": PO_REQ, "supplier_id": sid, "rfq_idem_key": approval["idem_key"],
                              "message_id": f"<fake-{sid}@test.invalid>", "price_inr_per_unit": price,
                              "currency": "INR", "gst_basis": "ex-GST", "moq": 5000, "lead_time_days": 12,
                              "raw_eml": f"From: {sid}@test.invalid\nSubject: Re: RFQ\n\nRate: Rs {price} per box ex-GST (FAKE)",
                              "source_quote": f"Rate: Rs {price} per box ex-GST (FAKE)",
                              "received_at": "2026-11-22T10:05:00+00:00", "parsed_ok": True})
    out["approvals"].append(_draft(PO_REQ, "send_po", "sup_01"))

    # 3. Held: no verified supplier.
    out["requests"].append(_request(HELD_REQ, "HELD", "no_verified_supplier", _result(
        HELD_REQ, "HELD", "no_verified_supplier", duration_s=40, not_eligible=NOT_ELIGIBLE,
        held_reason="no supplier passed every eligibility check (manufacturer, real_active, spec_match, fraud_risk)")))
    out["candidates"].append({**_candidates(HELD_REQ)[2]})
    checks, evidence = _checks(HELD_REQ, {"sup_03": PLAN["sup_03"]})
    out["checks"] += checks
    out["evidence"] += evidence

    # 4. A phone request the worker has not picked up yet (result_json null).
    out["requests"].append(_request(VOICE_REQ, "RECEIVED", "pending", None, created_by="voice", channel="voice",
                                    call_id="call-fake-1", caller_words="FAKE: we need fifty thousand five ply boxes for Gurgaon"))
    return out
