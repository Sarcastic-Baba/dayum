"""Kickoff input/output format <-> internal. When the kickoff kit arrives (or the format
changes at hour 3), this is the only file that should need editing."""

REQUIRED = ("item", "quantity_per_month", "delivery_location")
SPEC_FIELDS = ("ply", "size_in", "material")


def is_free_text(raw):
    return bool(raw.get("text")) and not raw.get("item")


def normalize_input(raw):
    """-> (spec, assumptions). Missing fields stay None and are listed for confirmation."""
    spec = {
        "request_id": raw.get("request_id"),
        "item": raw.get("item"),
        "spec": dict(raw.get("spec") or {}),
        "quantity_per_month": raw.get("quantity_per_month"),
        "delivery_location": raw.get("delivery_location"),
        "needed_by": raw.get("needed_by"),
        "budget_inr_per_unit": raw.get("budget_inr_per_unit"),
    }
    assumptions = [f"{f} not given — needs confirmation" for f in REQUIRED if not spec.get(f)]
    assumptions += [f"spec.{f} not given — needs confirmation"
                    for f in SPEC_FIELDS if not spec["spec"].get(f)]
    return spec, assumptions


def to_kickoff(full, cfg):
    """The screening/kickoff view: only the configured output fields, in order."""
    return {k: full.get(k) for k in cfg.get("output_fields") or full.keys()}
