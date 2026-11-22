"""Irreversible actions (send_rfq, send_po): draft once, wait for approval, send exactly once.

Send protocol for one (request, action, supplier) — PRD §13:
  1. an `outbox` row exists for the key  -> it was sent; nothing to do
  2. write the `steps` row (status 'started') BEFORE sending
  3. on a resumed step, ask the transport whether a message carrying this key already
     went out (a crash between "sent" and "outbox row") -> record it, do not resend
  4. send, write the `outbox` row, mark the step done
"""

from .steps import begin_step, crash_point, finish_step, get_step, idem_key, run_step


def send_key(request_id, action, supplier_id):
    return idem_key(request_id, action, supplier_id)


def draft(db, request_id, action, supplier_id, build):
    """Generate the email once; reruns return the stored draft (the approver's preview).
    The dashboard reads it from steps where step = 'draft:<action>:<supplier_id>'."""
    key = send_key(request_id, action, supplier_id)
    return run_step(db, request_id, f"draft:{action}:{supplier_id}", lambda: build(key),
                    key=idem_key(request_id, "draft", action, supplier_id))


def stored_draft(db, request_id, action, supplier_id):
    """The exact message the approver saw — what gets sent, byte for byte."""
    row = get_step(db, idem_key(request_id, "draft", action, supplier_id))
    return row["output_json"] if row and row["status"] == "done" else None


def request_approval(db, request_id, action, supplier_id, message):
    """A pending approval carrying the exact email the approver will see. The database
    freezes that text once a decision is made (approvals_guard_update)."""
    db.insert("approvals", {"request_id": request_id, "action": action,
                            "supplier_id": supplier_id, "status": "pending",
                            "idem_key": send_key(request_id, action, supplier_id),
                            "email_to": message["to"], "email_subject": message["subject"],
                            "email_body": message["body"]},
              conflict=["request_id", "action", "supplier_id"])


def approvals(db, request_id, action):
    return db.select("approvals", {"request_id": request_id, "action": action}, order=["id"])


def send_once(db, transport, request_id, action, supplier_id, approval_id, message):
    key = send_key(request_id, action, supplier_id)
    if db.select("outbox", {"idem_key": key}):
        row = get_step(db, key)
        if row and row["status"] != "done":
            finish_step(db, key, {"message_id": message["message_id"]})
        return "already_sent"
    resumed = get_step(db, key) is not None
    begin_step(db, request_id, f"{action}:{supplier_id}", key)
    crash_point(f"{action}:before_send")
    if resumed and transport.was_sent(key):
        outcome = "reconciled"  # it went out before the crash; record it, don't resend
    else:
        transport.send(key, message["raw"])
        outcome = "sent"
    crash_point(f"{action}:after_send")
    db.insert("outbox", {"idem_key": key, "approval_id": approval_id, "message": message["raw"]},
              conflict=["idem_key"])
    crash_point(f"{action}:after_outbox")
    finish_step(db, key, {"message_id": message["message_id"], "outcome": outcome})
    return outcome
