"""Stages before the shortlist: parse the request, discover suppliers, run the 7 checks.

Discovery and the checks are wired up in E3. Until then they report honestly that nothing
was verified (cannot_verify) — the safe answer, never a guess.
"""

from . import io_format

CHECK_NAMES = ("real_active", "manufacturer", "duplicate", "spec_match", "price",
               "distance", "fraud_risk")


def parse_spec(ctx):
    raw = ctx.req["input_json"]
    if io_format.is_free_text(raw):
        raise NotImplementedError("free-text requests need LLM extraction (E3)")
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
