"""Presentation helpers for templates: formatting and the pipeline stepper. No business logic."""

import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# State labels (chaitanya/DASHBOARD.md §2.6), in pipeline order. HELD is terminal, shown apart.
STATE_LABELS = {
    "RECEIVED": "Queued",
    "SPEC_PARSED": "Request understood",
    "CANDIDATES_FOUND": "Suppliers found",
    "CANDIDATES_CHECKED": "Suppliers verified",
    "SHORTLISTED": "Shortlisted",
    "RECOMMENDED": "Shortlist ready",
    "WAIT_APPROVAL_RFQ": "Waiting for RFQ approval",
    "RFQS_SENT": "RFQs sent",
    "WAIT_QUOTES": "Waiting for quotes",
    "QUOTES_RECEIVED": "Quotes in",
    "RANKED": "Ranked",
    "WAIT_APPROVAL_PO": "Waiting for PO approval",
    "PO_SENT": "PO sent",
    "DONE": "Done",
    "HELD": "On hold",
}
STEPPER = [(label, [state]) for state, label in STATE_LABELS.items() if state != "HELD"]
HUMAN_WAIT = {"WAIT_APPROVAL_RFQ", "WAIT_APPROVAL_PO"}

STATUS_STYLE = {
    "pending": ("slate", "Pending"),
    "recommended": ("indigo", "Recommended"),
    "awaiting_quotes": ("amber", "Awaiting quotes"),
    "no_verified_supplier": ("rose", "No verified supplier"),
    "held": ("rose", "Held"),
}

RESULT_STYLE = {"pass": "emerald", "fail": "rose", "cannot_verify": "amber"}
CLASSIFICATION_STYLE = {"manufacturer": "emerald", "trader": "rose", "cannot_verify": "amber"}
CHECK_LABEL = {
    "real_active": "Real & active", "manufacturer": "Manufacturer", "duplicate": "Duplicate",
    "spec_match": "Spec match", "price": "Listed price", "distance": "Distance", "fraud_risk": "Fraud risk",
}


def stepper(state: str) -> list[dict]:
    """One dict per step: label + 'done' | 'current' | 'todo'. HELD freezes progress where it stopped."""
    current = next((i for i, (_, states) in enumerate(STEPPER) if state in states), None)
    steps = []
    for i, (label, _) in enumerate(STEPPER):
        if current is None:
            status = "todo"
        elif i < current or state == "DONE":
            status = "done"
        elif i == current:
            status = "current"
        else:
            status = "todo"
        steps.append({"label": label, "status": status})
    return steps


def indian_number(value, decimals: int = 0) -> str:
    """12345678.5 -> '1,23,45,678.50' (Indian digit grouping)."""
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if number < 0 else ""
    whole, _, frac = f"{abs(number):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return sign + whole + (f".{frac}" if frac else "")


def inr(value, decimals: int = 2) -> str:
    text = indian_number(value, decimals)
    return text if text == "—" else "₹" + text


def ist(value) -> str:
    """ISO timestamp -> '22 Nov, 15:10 IST'."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b, %H:%M IST")


def day(value) -> str:
    """Date or ISO timestamp -> '14 Oct 2026'."""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(value)


def duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(float(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def size(spec) -> str:
    dims = (spec or {}).get("size_in")
    if not dims:
        return ""
    return " × ".join(indian_number(d, 0) if float(d).is_integer() else str(d) for d in dims) + " in"


_REASON_PART = re.compile(r"^\s*([a-z_]+)\s*=\s*(pass|fail|cannot_verify)\b\s*(.*)$")


def reason_parts(reason) -> dict:
    """'not eligible: manufacturer=fail (calls itself a trader), real_active=cannot_verify'
    -> {'checks': [{'label': 'Manufacturer', 'result': 'fail', 'note': 'calls itself a trader'}, ...],
        'text': None}. Anything not in that shape is returned as plain text."""
    text = str(reason or "").strip()
    text = re.sub(r"^not eligible:\s*", "", text, flags=re.I)
    checks = []
    for part in re.split(r",\s*(?=[a-z_]+\s*=)", text):
        m = _REASON_PART.match(part)
        if not m:
            return {"checks": [], "text": text}
        name, result, note = m.groups()
        checks.append({"label": CHECK_LABEL.get(name, name.replace("_", " ").capitalize()),
                       "result": result, "note": note.strip().strip("()").strip()})
    return {"checks": checks, "text": None}


def safe_url(value) -> str:
    """Only http(s) links are rendered as hrefs; scraped data must never inject javascript: URLs."""
    text = str(value or "").strip()
    return text if text.lower().startswith(("https://", "http://")) else "#"


def install(templates) -> None:
    env = templates.env
    env.filters.update(inr=inr, num=indian_number, ist=ist, day=day, duration=duration, size=size,
                       safe_url=safe_url, reason_parts=reason_parts)
    env.globals.update(stepper=stepper, STATUS_STYLE=STATUS_STYLE, RESULT_STYLE=RESULT_STYLE,
                       CLASSIFICATION_STYLE=CLASSIFICATION_STYLE, CHECK_LABEL=CHECK_LABEL,
                       STATE_LABELS=STATE_LABELS, HUMAN_WAIT=HUMAN_WAIT)
