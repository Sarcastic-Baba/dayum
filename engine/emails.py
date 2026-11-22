"""RFQ and PO emails. Plain templates (no LLM): generated once, stored before approval, and
reused verbatim on every retry, so the approver sees exactly what is sent.

Recipients are always the team-controlled test inbox (plus-addressed per supplier) —
never a real supplier address during the event."""

from email.message import EmailMessage
from email.utils import formatdate

from .transport import KEY_HEADER


def message_id(key):
    return f"<{key[:40]}@boxsource.local>"


def subject_token(request_id, supplier_id):
    return f"[{request_id}/{supplier_id}]"


def test_recipient(cfg, supplier_id):
    local, _, domain = cfg["email"]["test_recipient"].partition("@")
    return f"{local}+{supplier_id}@{domain}"


def _spec_lines(spec):
    s = spec.get("spec") or {}
    size = s.get("size_in")
    lines = [f"- Item: {spec.get('item', 'n/a')}"]
    if s.get("ply"):
        lines.append(f"- Ply: {s['ply']}-ply")
    if size:
        lines.append(f"- Size: {' x '.join(str(x) for x in size)} in")
    if s.get("material"):
        lines.append(f"- Material: {s['material']}")
    if s.get("printing"):
        lines.append(f"- Printing: {s['printing']}")
    return "\n".join(lines)


def _message(cfg, key, to, subject, body):
    msg = EmailMessage()
    msg["From"] = cfg["email"]["from"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id(key)
    msg[KEY_HEADER] = key
    msg.set_content(body)
    return {"to": to, "subject": subject, "body": body, "message_id": message_id(key),
            "idem_key": key, "raw": msg.as_string()}


def build_rfq(cfg, request_id, spec, cand, key):
    needed = f", needed by {spec['needed_by']}" if spec.get("needed_by") else ""
    subject = (f"Request for quotation {subject_token(request_id, cand['supplier_id'])} — "
               f"{spec.get('item', 'packaging')}")
    body = (
        f"Dear {cand['name']} team,\n\n"
        f"We are sourcing {spec.get('quantity_per_month', 'n/a')} units per month for delivery "
        f"to {spec.get('delivery_location', 'n/a')}{needed}.\n\n"
        f"Specification:\n{_spec_lines(spec)}\n\n"
        "Please reply to this email with:\n"
        "- Price per box in INR, stating whether it is ex-GST or including GST\n"
        "- Minimum order quantity (MOQ)\n"
        "- Lead time in days\n"
        "- Payment terms\n\n"
        "Thank you,\nBoxSource procurement (on behalf of our client)\n")
    return _message(cfg, key, test_recipient(cfg, cand["supplier_id"]), subject, body)


def build_po(cfg, request_id, spec, cand, quote, qty, key):
    subject = (f"Purchase order {subject_token(request_id, cand['supplier_id'])} — "
               f"{spec.get('item', 'packaging')}")
    body = (
        f"Dear {cand['name']} team,\n\n"
        f"Please accept this purchase order, subject to confirmation of your quote.\n\n"
        f"Specification:\n{_spec_lines(spec)}\n\n"
        f"- Trial quantity: {qty}\n"
        f"- Price: INR {quote['price_inr_per_unit']} per unit ({quote['gst_basis']}), "
        f"as quoted in your email received {quote['received_at']}\n"
        f"- Deliver to: {spec.get('delivery_location', 'n/a')}\n\n"
        "Thank you,\nBoxSource procurement (on behalf of our client)\n")
    return _message(cfg, key, test_recipient(cfg, cand["supplier_id"]), subject, body)
