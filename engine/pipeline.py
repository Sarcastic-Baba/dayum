"""Stages before the shortlist: parse the request, discover suppliers, run the 7 checks.

Discovery and the checks are wired up in E3. Until then they report honestly that nothing
was verified (cannot_verify) — the safe answer, never a guess.
"""

from . import io_format, ledger, llm
from .voice import PROMPT, Spoken, guard

CHECK_NAMES = ("real_active", "manufacturer", "duplicate", "spec_match", "price",
               "distance", "fraud_risk")


def _from_free_text(ctx, text):
    """Extract the fields from free text with the voice intake's extractor and no-guess guard:
    any number or place not actually in the text is dropped (it becomes an assumption)."""
    c = ctx.cfg.get("llm") or {}
    sp, usage = llm.extract(PROMPT.format(words=text), Spoken, c.get("models", ["gemini-flash-lite-latest"]),
                            c.get("timeout_s", 20))
    ledger.record_cost(ctx.db, ctx.rid, "parse_spec", "llm", usage["tokens_in"] + usage["tokens_out"],
                       ledger.llm_cost_inr(ctx.cfg, usage["tokens_in"], usage["tokens_out"]))
    sp = guard(sp, text)
    return {"item": sp.item, "quantity_per_month": sp.quantity_per_month,
            "delivery_location": sp.delivery_location, "needed_by": sp.needed_by,
            "spec": {k: v for k, v in {"ply": sp.ply, "size_in": sp.size_in, "material": sp.material,
                                       "printing": sp.printing}.items() if v is not None}}


def parse_spec(ctx):
    raw = dict(ctx.req["input_json"])
    text = io_format.free_text(raw)
    if text:  # structured fields the user filled in win over anything extracted
        found = _from_free_text(ctx, text)
        spec = {**found.pop("spec"), **{k: v for k, v in (raw.get("spec") or {}).items() if v}}
        raw = {**found, **{k: v for k, v in raw.items() if v not in (None, "", [], {})}, "spec": spec}
    return io_format.normalize_input(raw)


def discover(ctx):
    return []  # E3: Crustdata / grounded web search -> candidate rows


def run_checks(ctx):
    for c in ctx.db.select("candidates", {"request_id": ctx.rid}):
        for name in CHECK_NAMES:
            ctx.db.insert("checks", {
                "request_id": ctx.rid, "supplier_id": c["supplier_id"], "check_name": name,
                "result": "cannot_verify", "explanation": "check not implemented yet",
                "missing": "implementation"}, conflict=["request_id", "supplier_id", "check_name"])
