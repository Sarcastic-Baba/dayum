"""FAKE rows for tests and local UI preview only. Every name, URL and price here is
invented and marked as such — never load these into the live project."""

import hashlib

RFQ_REQ = "FIXTURE-RFQ"
PO_REQ = "FIXTURE-PO"


def _key(request_id, action, supplier_id):
    return hashlib.sha256(f"{request_id}:{action}:{supplier_id}".encode()).hexdigest()


def _request(request_id, state, status, result=None):
    return {
        "request_id": request_id, "state": state, "status": status, "created_by": "fixture",
        "input_json": {"request_id": request_id, "item": "corrugated box",
                       "spec": {"ply": 5, "size_in": [18, 12, 12], "material": "kraft", "printing": "none"},
                       "quantity_per_month": 50000, "delivery_location": "Gurgaon, Haryana",
                       "needed_by": "2026-12-15", "budget_inr_per_unit": None},
        "spec_json": {"item": "corrugated box",
                      "spec": {"ply": 5, "size_in": [18, 12, 12], "material": "kraft", "printing": None},
                      "quantity_per_month": 50000, "delivery_location": "Gurgaon, Haryana", "needed_by": "2026-12-15"},
        "result_json": result,
    }


def _candidates(request_id):
    return [
        {"request_id": request_id, "supplier_id": "sup_01", "name": "Fixture Corrugators Pvt Ltd (FAKE)",
         "website": "https://example.com/fixture-corrugators", "address": "Plot 1, Fake Industrial Area, Manesar",
         "classification": "manufacturer",
         "data_json": {"listed_price_inr_per_unit": 38.5, "price_basis": "ex-GST", "distance_km": 42,
                       "est_transport_inr_per_unit": 0.9, "rank": 1}},
        {"request_id": request_id, "supplier_id": "sup_02", "name": "Sample Packaging Industries (FAKE)",
         "website": "https://example.org/sample-packaging", "address": "Fake Road, Bawal",
         "classification": "manufacturer",
         "data_json": {"distance_km": 88, "est_transport_inr_per_unit": 1.8, "rank": 2}},
        {"request_id": request_id, "supplier_id": "sup_03", "name": "Fixture Corrugators Traders (FAKE)",
         "website": "https://example.net/fixture-traders", "address": "Fake Market, Delhi",
         "classification": "trader", "data_json": {"flags": ["lookalike_name"]}},
        {"request_id": request_id, "supplier_id": "sup_04", "name": "Fixture Corrugators Pvt. Ltd. (FAKE listing)",
         "website": "https://example.com/fixture-corrugators", "address": None,
         "classification": "manufacturer", "merged_into": "sup_01", "data_json": {}},
    ]


def _checks(request_id):
    rows, evidence = [], []
    plan = {
        "sup_01": {"real_active": "pass", "manufacturer": "pass", "duplicate": "pass", "spec_match": "pass",
                   "price": "pass", "distance": "pass", "fraud_risk": "pass"},
        "sup_02": {"real_active": "pass", "manufacturer": "pass", "duplicate": "pass", "spec_match": "pass",
                   "price": "cannot_verify", "distance": "pass", "fraud_risk": "pass"},
        "sup_03": {"real_active": "pass", "manufacturer": "fail", "duplicate": "pass", "spec_match": "cannot_verify",
                   "price": "cannot_verify", "distance": "cannot_verify", "fraud_risk": "cannot_verify"},
    }
    for sid, results in plan.items():
        for name, result in results.items():
            rows.append({"request_id": request_id, "supplier_id": sid, "check_name": name, "result": result,
                         "explanation": f"FAKE fixture explanation for {name}.",
                         "missing": "FAKE: no published price for 5-ply" if result == "cannot_verify" else None})
            if result != "cannot_verify":
                evidence.append({"request_id": request_id, "supplier_id": sid, "check_name": name,
                                 "fact": f"FAKE fact supporting {name}",
                                 "source_url": f"https://example.com/{sid}/{name}", "source_type": "web_page",
                                 "field_or_quote": "We manufacture 3, 5 and 7 ply corrugated boxes (FAKE QUOTE).",
                                 "record_date": "2026-10-14", "retrieved_at": "2026-11-22T09:40:00+00:00"})
    return rows, evidence


def _draft(request_id, action, sid, name):
    kind = "Request for quotation" if action == "send_rfq" else "Purchase order"
    return {"request_id": request_id, "action": action, "supplier_id": sid, "idem_key": _key(request_id, action, sid),
            "email_to": f"{sid}@test-inbox.invalid", "email_subject": f"[{request_id}/{sid}] {kind}: 5-ply boxes",
            "email_body": f"Dear {name},\n\nFAKE {kind.lower()} body for tests.\n5-ply kraft, 18×12×12 in, "
                          f"50,000 per month to Gurgaon.\n\nRegards,\nBoxSource test"}


def tables() -> dict:
    """Two requests: one waiting on RFQ approval, one waiting on PO approval with quotes."""
    out = {k: [] for k in ("requests", "candidates", "checks", "evidence", "approvals", "steps",
                           "outbox", "quotes", "costs", "logs")}
    out["requests"].append(_request(RFQ_REQ, "WAIT_APPROVAL_RFQ", "recommended", {
        "summary": "FAKE: two eligible manufacturers found; RFQs ready for approval.",
        "assumptions": ["FAKE: printing not stated — assumed none"],
        "duration_s": 96, "limits": {"cost_inr_per_request": 25, "time_s_per_request": 400},
    }))
    out["requests"].append(_request(PO_REQ, "WAIT_APPROVAL_PO", "recommended", {
        "summary": "FAKE: quotes received from two suppliers; PO ready for approval.",
        "duration_s": 210, "limits": {"cost_inr_per_request": 25, "time_s_per_request": 400},
        "recommendation": {"primary": "sup_01", "backup": "sup_02", "trial_order_qty": 5000,
                           "full_monthly_qty": 50000, "reasoning": "FAKE reasoning.",
                           "caveats": ["FAKE caveat: ranked on quotes received 22 Nov 2026"]},
        "candidates": [{"supplier_id": "sup_01", "est_landed_inr_per_unit": 38.1},
                       {"supplier_id": "sup_02", "est_landed_inr_per_unit": 39.6}],
    }))
    for req in (RFQ_REQ, PO_REQ):
        out["candidates"] += _candidates(req)
        checks, evidence = _checks(req)
        out["checks"] += checks
        out["evidence"] += evidence
        out["costs"] += [{"request_id": req, "step": "discover", "call_type": "crustdata", "units": 3, "inr": 1.5},
                         {"request_id": req, "step": "checks", "call_type": "llm", "units": 12, "inr": 2.25}]
        out["logs"].append({"request_id": req, "level": "info", "message": "FAKE log line", "at": "2026-11-22T09:41:00+00:00"})

    out["approvals"] += [_draft(RFQ_REQ, "send_rfq", "sup_01", "Fixture Corrugators"),
                         _draft(RFQ_REQ, "send_rfq", "sup_02", "Sample Packaging")]

    # PO request: both RFQs approved and sent, quotes in, PO pending.
    for sid, price in (("sup_01", 37.2), ("sup_02", 37.8)):
        approval = {**_draft(PO_REQ, "send_rfq", sid, sid), "id": 900 + int(sid[-1]), "status": "approved",
                    "approver": "approver@test.invalid", "decided_at": "2026-11-22T09:50:00+00:00"}
        out["approvals"].append(approval)
        out["steps"].append({"request_id": PO_REQ, "step": "send_rfq", "idem_key": approval["idem_key"], "status": "done"})
        out["outbox"].append({"idem_key": approval["idem_key"], "approval_id": approval["id"], "message": "FAKE eml"})
        out["quotes"].append({"request_id": PO_REQ, "supplier_id": sid, "rfq_idem_key": approval["idem_key"],
                              "message_id": f"<fake-{sid}@test.invalid>", "price_inr_per_unit": price,
                              "currency": "INR", "gst_basis": "ex-GST", "moq": 5000, "lead_time_days": 12,
                              "raw_eml": "FAKE", "source_quote": f"Rate: Rs {price} per box ex-GST (FAKE)",
                              "received_at": "2026-11-22T10:05:00+00:00", "parsed_ok": True})
    out["approvals"].append(_draft(PO_REQ, "send_po", "sup_01", "Fixture Corrugators"))
    return out
