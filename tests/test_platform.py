"""Platform tests: login, intake, the assembled view, and the approval gate. Fake DB only."""

from dashboard import repo
from tests import fixtures
from tests.conftest import APPROVER, VIEWER, login


def pending(db, request_id, action="send_rfq"):
    return [a for a in db.tables["approvals"]
            if a["request_id"] == request_id and a["action"] == action and a["status"] == "pending"]


# ---------------------------------------------------------------- login
def test_pages_require_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert client.get("/api/requests").status_code == 401


def test_wrong_password_rejected(client):
    response = login(client, (APPROVER[0], "wrong"))
    assert response.status_code == 401
    assert "Wrong email or password" in response.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_logout_ends_session(approver):
    assert approver.get("/").status_code == 200
    approver.post("/logout")
    assert approver.get("/", follow_redirects=False).status_code == 303


def test_tampered_cookie_rejected(client):
    client.cookies.set("boxsource_session", "eyJ1IjoiYXBwcm92ZXJAdGVzdC5pbnZhbGlkIiwiZXhwIjo5OTk5OTk5OTk5fQ==.bad")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_health_is_public(client):
    assert client.get("/api/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- intake
def test_create_request_from_form_is_idempotent(approver, db):
    form = {"request_id": "SRC-TEST-1", "item": "corrugated box", "ply": "5", "size_l": "18", "size_w": "12",
            "size_h": "12", "quantity_per_month": "50000", "delivery_location": "Gurgaon, Haryana"}
    first = approver.post("/requests", data=form, follow_redirects=False)
    second = approver.post("/requests", data=form, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    rows = [r for r in db.tables["requests"] if r["request_id"] == "SRC-TEST-1"]
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "RECEIVED" and row["status"] == "pending" and row["created_by"] == APPROVER[0]
    assert row["input_json"]["spec"] == {"ply": 5, "size_in": [18.0, 12.0, 12.0], "material": None, "printing": None}
    assert row["input_json"]["needed_by"] is None  # blank stays null for the engine to list as an assumption


def test_create_request_free_text(approver, db):
    approver.post("/requests", data={"request_id": "SRC-TEXT-1", "free_text": "50k 5-ply boxes for Gurgaon"})
    row = next(r for r in db.tables["requests"] if r["request_id"] == "SRC-TEXT-1")
    assert row["input_json"]["free_text"] == "50k 5-ply boxes for Gurgaon"


def test_create_request_validation_message(approver, db):
    response = approver.post("/requests", data={"request_id": "SRC-BAD-1", "item": "box"})
    assert response.status_code == 200 and "item, quantity per month and delivery location" in response.text
    assert not any(r["request_id"] == "SRC-BAD-1" for r in db.tables["requests"])


def test_create_request_json_with_api_token(client, db):
    body = {"request_id": "SRC-API-1", "item": "corrugated box", "quantity_per_month": 1000,
            "delivery_location": "Noida", "spec": {"ply": 3}}
    response = client.post("/requests", json=body, headers={"Authorization": "Bearer test-api-token"})
    assert response.status_code == 201 and response.json() == {"request_id": "SRC-API-1", "created": True}
    again = client.post("/requests", json=body, headers={"Authorization": "Bearer test-api-token"})
    assert again.status_code == 200 and again.json()["created"] is False


def test_viewer_cannot_create_request(viewer, db):
    before = len(db.tables["requests"])
    response = viewer.post("/requests", data={"free_text": "anything"})
    assert response.status_code == 403
    assert len(db.tables["requests"]) == before


# ---------------------------------------------------------------- assembled view
def test_view_matches_output_contract(approver):
    view = approver.get(f"/api/requests/{fixtures.PO_REQ}").json()
    for field in ("request_id", "status", "summary", "candidates", "recommendation", "actions", "cost_inr", "duration_s"):
        assert field in view
    assert view["cost_inr"] == 3.75
    sup1 = next(c for c in view["candidates"] if c["supplier_id"] == "sup_01")
    assert sup1["quoted_price_inr_per_unit"] == 37.2          # from the quotes table
    assert sup1["est_landed_inr_per_unit"] == 38.1            # from result_json
    assert [k["check"] for k in sup1["checks"]][:2] == ["real_active", "manufacturer"]
    statuses = {(a["type"], a["to_supplier"]): a["status"] for a in view["actions"]}
    assert statuses[("send_rfq", "sup_01")] == "done"         # approved + in outbox
    assert statuses[("send_po", "sup_01")] == "pending_approval"


def test_detail_page_renders_for_viewer(viewer):
    page = viewer.get(f"/requests/{fixtures.RFQ_REQ}")
    assert page.status_code == 200
    assert "Fixture Corrugators Pvt Ltd (FAKE)" in page.text
    assert "Read-only account" in page.text
    assert "Approve &amp; send" not in page.text


def test_detail_page_renders_approval_form_for_approver(approver):
    page = approver.get(f"/requests/{fixtures.RFQ_REQ}")
    assert "Approve &amp; send" in page.text and "FAKE request for quotation body" in page.text
    assert 'hx-trigger' not in page.text  # waiting on a human: no polling under the form


def test_po_page_renders(approver):
    page = approver.get(f"/requests/{fixtures.PO_REQ}")
    assert page.status_code == 200 and "Approve &amp; send PO" in page.text and "Quote timeline" in page.text


def test_unknown_request_404(approver):
    assert approver.get("/requests/NOPE-1").status_code == 404


# ---------------------------------------------------------------- approval gate
def test_viewer_cannot_approve(viewer, db):
    a = pending(db, fixtures.RFQ_REQ)[0]
    viewer.post(f"/approvals/{a['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "approve",
                                               "fingerprint": repo.body_fingerprint(a)})
    assert pending(db, fixtures.RFQ_REQ)[0]["status"] == "pending"
    assert len(pending(db, fixtures.RFQ_REQ)) == 2


def test_approver_can_approve_with_fingerprint(approver, db):
    a = pending(db, fixtures.RFQ_REQ)[0]
    approver.post(f"/approvals/{a['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "approve",
                                                 "fingerprint": repo.body_fingerprint(a)})
    row = next(x for x in db.tables["approvals"] if x["id"] == a["id"])
    assert row["status"] == "approved" and row["approver"] == APPROVER[0] and row["decided_at"]


def test_approve_refused_if_email_changed_since_viewing(approver, db):
    a = pending(db, fixtures.RFQ_REQ)[0]
    stale = repo.body_fingerprint(a)
    next(x for x in db.tables["approvals"] if x["id"] == a["id"])["email_body"] = "edited after viewing"
    response = approver.post(f"/approvals/{a['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "approve",
                                                            "fingerprint": stale}, follow_redirects=False)
    assert "email+changed" in response.headers["location"] or "email%20changed" in response.headers["location"]
    assert next(x for x in db.tables["approvals"] if x["id"] == a["id"])["status"] == "pending"


def test_reject_needs_reason(approver, db):
    a = pending(db, fixtures.RFQ_REQ)[0]
    approver.post(f"/approvals/{a['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "reject", "reason": "  "})
    assert next(x for x in db.tables["approvals"] if x["id"] == a["id"])["status"] == "pending"
    approver.post(f"/approvals/{a['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "reject",
                                                 "reason": "Existing supplier"})
    row = next(x for x in db.tables["approvals"] if x["id"] == a["id"])
    assert row["status"] == "rejected" and row["reason"] == "Existing supplier"


def test_decision_is_final(approver, db):
    a = pending(db, fixtures.RFQ_REQ)[0]
    data = {"request_id": fixtures.RFQ_REQ, "decision": "approve", "fingerprint": repo.body_fingerprint(a)}
    approver.post(f"/approvals/{a['id']}", data=data)
    approver.post(f"/approvals/{a['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "reject", "reason": "changed mind"})
    assert next(x for x in db.tables["approvals"] if x["id"] == a["id"])["status"] == "approved"


def test_rfq_tickboxes_untick_requires_reason(approver, db):
    first, second = pending(db, fixtures.RFQ_REQ)
    form = {"approve": [str(first["id"])], f"fingerprint_{first['id']}": repo.body_fingerprint(first),
            f"fingerprint_{second['id']}": repo.body_fingerprint(second)}
    approver.post(f"/requests/{fixtures.RFQ_REQ}/rfq-decisions", data=form)
    assert len(pending(db, fixtures.RFQ_REQ)) == 2  # nothing recorded without a reason

    approver.post(f"/requests/{fixtures.RFQ_REQ}/rfq-decisions", data={**form, "reason": "Too far away"})
    rows = {x["id"]: x for x in db.tables["approvals"]}
    assert rows[first["id"]]["status"] == "approved"
    assert rows[second["id"]]["status"] == "rejected" and rows[second["id"]]["reason"] == "Too far away"


def test_api_token_cannot_approve(client, db):
    a = pending(db, fixtures.RFQ_REQ)[0]
    client.post(f"/approvals/{a['id']}", headers={"Authorization": "Bearer test-api-token"},
                data={"request_id": fixtures.RFQ_REQ, "decision": "approve", "fingerprint": repo.body_fingerprint(a)})
    assert next(x for x in db.tables["approvals"] if x["id"] == a["id"])["status"] == "pending"


# ---------------------------------------------------------------- engine draft format
def _engine_style_approval(db):
    """What engine/actions.py writes: a bare approval row + the draft in a steps row."""
    rid, sid = fixtures.RFQ_REQ, "sup_02"
    approval = next(a for a in db.tables["approvals"] if a["request_id"] == rid and a["supplier_id"] == sid)
    for k in ("email_to", "email_subject", "email_body", "idem_key"):
        approval.pop(k, None)
    db.insert("steps", {"request_id": rid, "step": f"draft:send_rfq:{sid}", "status": "done",
                        "idem_key": repo.draft_step_key(rid, "send_rfq", sid),
                        "output_json": {"to": "sup_02@test-inbox.invalid", "subject": "ENGINE subject",
                                        "body": "ENGINE draft body", "raw": "...", "message_id": "<x>"}})
    return approval


def test_engine_draft_in_steps_is_shown_and_approvable(approver, db):
    approval = _engine_style_approval(db)
    page = approver.get(f"/requests/{fixtures.RFQ_REQ}")
    assert "ENGINE draft body" in page.text and "ENGINE subject" in page.text
    view = approver.get(f"/api/requests/{fixtures.RFQ_REQ}").json()
    action = next(a for a in view["actions"] if a["id"] == approval["id"])
    approver.post(f"/approvals/{approval['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "approve",
                                                        "fingerprint": action["fingerprint"]})
    assert next(a for a in db.tables["approvals"] if a["id"] == approval["id"])["status"] == "approved"


def test_engine_draft_changed_after_viewing_is_refused(approver, db):
    approval = _engine_style_approval(db)
    view = approver.get(f"/api/requests/{fixtures.RFQ_REQ}").json()
    stale = next(a for a in view["actions"] if a["id"] == approval["id"])["fingerprint"]
    next(s for s in db.tables["steps"] if s["step"] == "draft:send_rfq:sup_02")["output_json"]["body"] = "changed"
    approver.post(f"/approvals/{approval['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "approve",
                                                        "fingerprint": stale})
    assert next(a for a in db.tables["approvals"] if a["id"] == approval["id"])["status"] == "pending"


def test_no_draft_anywhere_cannot_be_approved(approver, db):
    approval = _engine_style_approval(db)
    db.tables["steps"] = [s for s in db.tables["steps"] if not s["step"].startswith("draft:")]
    approver.post(f"/approvals/{approval['id']}", data={"request_id": fixtures.RFQ_REQ, "decision": "approve",
                                                        "fingerprint": repo.body_fingerprint(approval)})
    assert next(a for a in db.tables["approvals"] if a["id"] == approval["id"])["status"] == "pending"


# ---------------------------------------------------------------- formatting
def test_indian_number_grouping():
    from dashboard.ui import indian_number, inr
    assert indian_number(12345678.5, 2) == "1,23,45,678.50"
    assert indian_number(50000) == "50,000"
    assert inr(38.5) == "₹38.50"
    assert inr(None) == "—"


def test_safe_url_blocks_javascript():
    from dashboard.ui import safe_url
    assert safe_url("javascript:alert(1)") == "#"
    assert safe_url("https://example.com") == "https://example.com"
