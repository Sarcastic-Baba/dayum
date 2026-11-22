"""Quote intake: match a reply email to its RFQ and read the quoted terms from it.

The regex parser reads a structured reply deterministically; the exact line holding the
price is kept as the source. Anything it cannot read stays unparsed (parsed_ok = False)
for the LLM fallback or a human — a price is never guessed."""

import email
import re
from email import policy
from email.utils import parsedate_to_datetime

TOKEN_RE = re.compile(r"\[([A-Za-z0-9_\-]+)/([A-Za-z0-9_\-]+)\]")
PRICE_RE = re.compile(
    r"(?:₹|\brs\.?|\binr)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:/-)?\s*(?:/|per)\s*"
    r"(?:box|boxes|pc|pcs|piece|unit)\b", re.I)
MOQ_RE = re.compile(r"\b(?:moq|minimum order(?: quantity)?)\b\D{0,15}?([0-9][0-9,]*)", re.I)
LEAD_RE = re.compile(r"\blead\s*time\b\D{0,15}?([0-9]+)\s*(day|days|week|weeks)\b", re.I)
PLY_RE = re.compile(r"\b(\d)\s*-?\s*ply\b", re.I)
EX_GST_RE = re.compile(r"\b(ex[- ]?gst|excl(?:uding|\.)?\s+gst|\+\s*gst|plus\s+gst|gst\s+extra)\b", re.I)
INCL_GST_RE = re.compile(r"\b(incl(?:uding|usive\s+of|\.)?\s+gst|gst\s+inclusive)\b", re.I)
REPLY_CUT_RE = re.compile(r"^\s*(on .{5,200}wrote:|-{2,}\s*original message\s*-{2,}|from:\s)", re.I)


def to_message(raw):
    """Parse from bytes: parsing a str mangles non-ASCII bodies (₹ becomes the text '\\u20b9').
    Transports decode with surrogateescape, so this round-trip is lossless."""
    return email.message_from_bytes(raw.encode("utf-8", "surrogateescape"), policy=policy.default)


def parse_email(raw):
    msg = to_message(raw)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)
    try:
        received = parsedate_to_datetime(msg["Date"]).isoformat() if msg["Date"] else None
    except (TypeError, ValueError):
        received = None
    refs = " ".join(filter(None, [msg.get("In-Reply-To"), msg.get("References")])).split()
    return {"message_id": (msg.get("Message-ID") or "").strip(), "refs": refs,
            "subject": msg.get("Subject", ""), "received_at": received, "body": body}


def own_text(body):
    """The reply's own words: stop at the quoted original or the 'On ... wrote:' line."""
    keep = []
    for line in body.splitlines():
        if line.lstrip().startswith(">") or REPLY_CUT_RE.match(line):
            break
        keep.append(line)
    return "\n".join(keep)


def match_rfq(parsed, by_message_id, by_token):
    """Find the RFQ a reply answers: header threading first, then the subject token."""
    for ref in parsed["refs"]:
        if ref in by_message_id:
            return by_message_id[ref]
    m = TOKEN_RE.search(parsed["subject"])
    return by_token.get((m.group(1), m.group(2))) if m else None


def _num(s):
    return float(s.replace(",", ""))


def parse_quote(body):
    text = own_text(body)
    out = {"price_inr_per_unit": None, "currency": None, "gst_basis": "unknown", "moq": None,
           "lead_time_days": None, "ply": None, "source_quote": None, "parsed_ok": False}
    for line in text.splitlines():
        m = PRICE_RE.search(line)
        if m:
            out.update(price_inr_per_unit=_num(m.group(1)), currency="INR",
                       source_quote=line.strip(), parsed_ok=True)
            break
    if EX_GST_RE.search(text):
        out["gst_basis"] = "ex-GST"
    elif INCL_GST_RE.search(text):
        out["gst_basis"] = "incl-GST"
    if m := MOQ_RE.search(text):
        out["moq"] = int(_num(m.group(1)))
    if m := LEAD_RE.search(text):
        n = int(m.group(1))
        out["lead_time_days"] = n * 7 if m.group(2).lower().startswith("week") else n
    if m := PLY_RE.search(text):
        out["ply"] = int(m.group(1))
    return out
