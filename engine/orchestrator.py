"""The state machine (PRD §8).

Every handler is idempotent and returns the next state, or its own state while it waits
(for an approval or for quotes). The state is saved after every transition, so a worker
restarted after a kill continues exactly where the killed one stopped.
"""

import time
from datetime import datetime, timezone

from . import actions, emails, ledger, output, pipeline, rank
from . import quotes as qmod
from .steps import StepFailed, crash_point, now_iso, run_step

TERMINAL = {"DONE", "HELD"}
WAITING = {"WAIT_APPROVAL_RFQ", "WAIT_QUOTES", "WAIT_APPROVAL_PO"}


class Ctx:
    def __init__(self, db, cfg, transport, request_id):
        self.db, self.cfg, self.transport, self.rid = db, cfg, transport, request_id

    @property
    def req(self):
        return self.db.select("requests", {"request_id": self.rid})[0]

    @property
    def spec(self):
        return self.req["spec_json"] or {}

    def narrative(self):
        return self.req["result_json"] or {}

    def names(self):
        return {c["supplier_id"]: c["name"]
                for c in self.db.select("candidates", {"request_id": self.rid})}

    def candidate(self, sid):
        return self.db.select("candidates", {"request_id": self.rid, "supplier_id": sid})[0]

    def set_status(self, status):
        self.db.update("requests", {"request_id": self.rid},
                       {"status": status, "updated_at": now_iso()})

    def merge_data(self, sid, fields):
        d = self.candidate(sid)["data_json"] or {}
        d.update(fields)
        self.db.update("candidates", {"request_id": self.rid, "supplier_id": sid}, {"data_json": d})


def _hold(ctx, reason, status="held"):
    ctx.set_status(status)
    output.refresh(ctx.db, ctx.cfg, ctx.rid, held_reason=reason)
    return "HELD"


# ---- intake, discovery, checks -------------------------------------------------------

def h_received(ctx):
    spec, assumptions = run_step(ctx.db, ctx.rid, "parse_spec",
                                 lambda: list(pipeline.parse_spec(ctx)))
    ctx.db.update("requests", {"request_id": ctx.rid}, {"spec_json": spec})
    output.refresh(ctx.db, ctx.cfg, ctx.rid, assumptions=assumptions)
    missing = [f for f in ("item", "delivery_location") if not spec.get(f)]
    if missing:
        return _hold(ctx, "needs confirmation before searching: " + ", ".join(missing) + " not given")
    return "SPEC_PARSED"


def h_spec_parsed(ctx):
    found = run_step(ctx.db, ctx.rid, "discover", lambda: pipeline.discover(ctx))
    for c in found:  # re-applied on a rerun; the insert ignores rows that already exist
        ctx.db.insert("candidates", {"request_id": ctx.rid, **c}, conflict=["request_id", "supplier_id"])
    if not found:
        return _hold(ctx, "no candidate suppliers found by the search", "no_verified_supplier")
    return "CANDIDATES_FOUND"


def h_found(ctx):
    pipeline.run_checks(ctx)
    return "CANDIDATES_CHECKED"


def h_checked(ctx):
    sl, why_not = rank.shortlist(ctx.db, ctx.rid)
    names = ctx.names()
    output.refresh(ctx.db, ctx.cfg, ctx.rid, shortlist=sl, not_eligible=[
        {"supplier_id": s, "name": names.get(s), "reason": r} for s, r in why_not.items()])
    if not sl:
        return _hold(ctx, "no supplier passed every eligibility check (manufacturer, real_active, "
                          "spec_match, fraud_risk); see not_eligible for each reason",
                     "no_verified_supplier")
    return "SHORTLISTED"


def h_shortlisted(ctx):
    sl, names = ctx.narrative()["shortlist"], ctx.names()
    ctx.set_status("recommended")
    output.refresh(
        ctx.db, ctx.cfg, ctx.rid,
        summary=(f"{len(sl)} verified manufacturer(s) shortlisted: "
                 f"{', '.join(names[s] for s in sl)}. RFQs drafted and waiting for approval."),
        recommendation={"stage": "shortlist", "send_rfqs_to": sl,
                        "reasoning": "Every shortlisted supplier passed the manufacturer, "
                                     "real_active, spec_match and fraud_risk checks.",
                        "caveats": ["Not yet ranked: ranking waits for the quotes in RFQ replies."]})
    return "RECOMMENDED"


# ---- RFQs ------------------------------------------------------------------------------

def h_recommended(ctx):
    spec = ctx.spec
    for sid in ctx.narrative()["shortlist"]:
        cand = ctx.candidate(sid)
        msg = actions.draft(ctx.db, ctx.rid, "send_rfq", sid,
                            lambda key, c=cand: emails.build_rfq(ctx.cfg, ctx.rid, spec, c, key))
        actions.request_approval(ctx.db, ctx.rid, "send_rfq", sid, msg)
    return "WAIT_APPROVAL_RFQ"


def _send_approved(ctx, action):
    """None while any approval is pending; otherwise the number of approved sends."""
    rows = actions.approvals(ctx.db, ctx.rid, action)
    if not rows or any(a["status"] == "pending" for a in rows):
        return None
    sent = 0
    for a in rows:
        if a["status"] == "approved":
            msg = actions.stored_draft(ctx.db, ctx.rid, action, a["supplier_id"])
            if a.get("email_body") is not None and a["email_body"] != msg["body"]:
                raise StepFailed(f"{action} to {a['supplier_id']}: the approved text differs "
                                 "from the draft, so nothing was sent")
            outcome = actions.send_once(ctx.db, ctx.transport, ctx.rid, action,
                                        a["supplier_id"], a["id"], msg)
            ledger.log_step(ctx.db, ctx.cfg, ctx.rid, action, outcome, supplier_id=a["supplier_id"])
            sent += 1
    return sent


def h_wait_rfq(ctx):
    sent = _send_approved(ctx, "send_rfq")
    if sent is None:
        return "WAIT_APPROVAL_RFQ"
    if sent == 0:
        return _hold(ctx, "every RFQ was rejected by the approver")
    output.refresh(ctx.db, ctx.cfg, ctx.rid,
                   rfqs_sent_at=ctx.narrative().get("rfqs_sent_at") or now_iso())
    return "RFQS_SENT"


def h_rfqs_sent(ctx):
    ctx.set_status("awaiting_quotes")
    return "WAIT_QUOTES"


# ---- quotes ----------------------------------------------------------------------------

def _sent_rfqs(ctx):
    """RFQs that actually went out, keyed by Message-ID and by subject token."""
    by_mid, by_token = {}, {}
    for a in actions.approvals(ctx.db, ctx.rid, "send_rfq"):
        key = actions.send_key(ctx.rid, "send_rfq", a["supplier_id"])
        if ctx.db.select("outbox", {"idem_key": key}):
            by_mid[emails.message_id(key)] = (a["supplier_id"], key)
            by_token[(ctx.rid, a["supplier_id"])] = (a["supplier_id"], key)
    return by_mid, by_token


def ingest_replies(ctx):
    """Store each matching reply once (UNIQUE message_id): a restart re-reads the inbox and
    skips replies already ingested."""
    by_mid, by_token = _sent_rfqs(ctx)
    new = 0
    for raw in ctx.transport.replies():
        p = qmod.parse_email(raw)
        hit = qmod.match_rfq(p, by_mid, by_token)
        if not hit or not p["message_id"] or ctx.db.select("quotes", {"message_id": p["message_id"]}):
            continue
        sid, key = hit
        crash_point("quote:before_insert")
        won = ctx.db.insert("quotes", {
            "request_id": ctx.rid, "supplier_id": sid, "rfq_idem_key": key,
            "message_id": p["message_id"], "received_at": p["received_at"] or now_iso(),
            "raw_eml": raw, **qmod.parse_quote(p["body"])}, conflict=["message_id"])
        crash_point("quote:after_insert")
        if won:
            new += 1
            ledger.log_step(ctx.db, ctx.cfg, ctx.rid, "quote_ingested", "ok", supplier_id=sid)
    if new:
        output.refresh(ctx.db, ctx.cfg, ctx.rid)
    return {sid for sid, _ in by_mid.values()}


def h_wait_quotes(ctx):
    asked = ingest_replies(ctx)
    replied = {q["supplier_id"] for q in ctx.db.select("quotes", {"request_id": ctx.rid})}
    if asked and asked <= replied:
        return "QUOTES_RECEIVED"
    started = ctx.narrative().get("rfqs_sent_at")
    waited = ((datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
              if started else 0)
    if replied and waited >= ctx.cfg.get("quote_wait_s", 600):
        return "QUOTES_RECEIVED"
    return "WAIT_QUOTES"


def h_quotes_received(ctx):
    ranked, unranked = rank.rank_quotes(ctx.db, ctx.cfg, ctx.rid, ctx.spec,
                                        ctx.narrative()["shortlist"])
    for r in ranked:
        ctx.merge_data(r["supplier_id"], {"est_transport_inr_per_unit": r["est_transport"],
                                          "est_landed_inr_per_unit": r["est_landed"],
                                          "rank": r["rank"]})
    names = ctx.names()
    rec = rank.recommendation(ctx.cfg, ctx.spec, ranked, unranked, names)
    if not ranked:
        output.refresh(ctx.db, ctx.cfg, ctx.rid, recommendation={**rec, "stage": "quotes"})
        return _hold(ctx, "quotes came back but none is comparable; see recommendation caveats")
    ctx.set_status("recommended")
    backup = f", backup {names[rec['backup']]}" if rec["backup"] else ""
    output.refresh(
        ctx.db, ctx.cfg, ctx.rid, recommendation={**rec, "stage": "quotes"},
        summary=(f"{len(ranked)} quote(s) compared. Recommended: {names[rec['primary']]}{backup}. "
                 f"Trial order of {rec['trial_order_qty']} waiting for PO approval."))
    return "RANKED"


# ---- purchase order --------------------------------------------------------------------

def h_ranked(ctx):
    rec = ctx.narrative()["recommendation"]
    sid = rec["primary"]
    quote = [q for q in ctx.db.select("quotes", {"request_id": ctx.rid, "supplier_id": sid},
                                      order=["id"]) if q["parsed_ok"]][-1]
    cand, spec = ctx.candidate(sid), ctx.spec
    msg = actions.draft(ctx.db, ctx.rid, "send_po", sid,
                        lambda key: emails.build_po(ctx.cfg, ctx.rid, spec, cand, quote,
                                                    rec["trial_order_qty"], key))
    actions.request_approval(ctx.db, ctx.rid, "send_po", sid, msg)
    return "WAIT_APPROVAL_PO"


def h_wait_po(ctx):
    sent = _send_approved(ctx, "send_po")
    if sent is None:
        return "WAIT_APPROVAL_PO"
    if sent == 0:
        output.refresh(ctx.db, ctx.cfg, ctx.rid, summary=(ctx.narrative().get("summary") or "")
                       + " The purchase order was rejected by the approver.")
        return "DONE"
    return "PO_SENT"


def h_po_sent(ctx):
    return "DONE"


HANDLERS = {
    "RECEIVED": h_received, "SPEC_PARSED": h_spec_parsed, "CANDIDATES_FOUND": h_found,
    "CANDIDATES_CHECKED": h_checked, "SHORTLISTED": h_shortlisted,
    "RECOMMENDED": h_recommended, "WAIT_APPROVAL_RFQ": h_wait_rfq, "RFQS_SENT": h_rfqs_sent,
    "WAIT_QUOTES": h_wait_quotes, "QUOTES_RECEIVED": h_quotes_received, "RANKED": h_ranked,
    "WAIT_APPROVAL_PO": h_wait_po, "PO_SENT": h_po_sent,
}


def over_limit(ctx):
    lim = ctx.cfg["limits"]
    if ledger.total_cost(ctx.db, ctx.rid) >= lim["cost_inr_per_request"]:
        return "cost"
    if ledger.active_seconds(ctx.db, ctx.rid) >= lim["time_s_per_request"]:
        return "time"
    return None


def advance(db, cfg, transport, request_id):
    """Drive one request until it waits or finishes; returns the state it stopped in."""
    ctx = Ctx(db, cfg, transport, request_id)
    while True:
        state = ctx.req["state"]
        if state in TERMINAL:
            return state
        which = None if state in WAITING else over_limit(ctx)
        if which:
            nxt = _hold(ctx, f"limit_reached: {which} limit hit; work stopped at {state}")
        else:
            t0 = time.monotonic()
            try:
                nxt = HANDLERS[state](ctx)
            except StepFailed as e:
                nxt = _hold(ctx, f"step_failed at {state}: {e}")
            except Exception as e:  # transient (network, parsing): log it, retry next cycle
                ledger.log_step(db, cfg, request_id, state, "error", level="error",
                                detail=f"{type(e).__name__}: {e}")
                return state
            if nxt == state:
                return state  # waiting for a human or for quotes
            ledger.log_step(db, cfg, request_id, f"{state}->{nxt}", "ok",
                            (time.monotonic() - t0) * 1000)
        db.update("requests", {"request_id": request_id}, {"state": nxt, "updated_at": now_iso()})
        output.refresh(db, cfg, request_id)
        crash_point(f"after_state:{nxt}")
