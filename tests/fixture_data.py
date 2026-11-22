"""Fixture data for the tests and for the dashboard demo seed. Clearly fake: every supplier
name ends in "(fixture)" and every URL is on example.invalid. The engine never reads this."""

SPEC = {"item": "corrugated box",
        "spec": {"ply": 5, "size_in": [18, 12, 12], "material": "kraft"},
        "quantity_per_month": 50000, "delivery_location": "Gurgaon, Haryana",
        "needed_by": "2026-12-15", "budget_inr_per_unit": None}
SUPPLIERS = [("sup_01", "Alpha Packaging (fixture)", 36.0, 30),
             ("sup_02", "Beta Cartons (fixture)", None, 55)]
CHECKS = ("real_active", "manufacturer", "duplicate", "spec_match", "price", "distance",
          "fraud_risk")
QUOTE = "We manufacture 3, 5 and 7 ply corrugated boxes at our Manesar plant. (fixture)"


def seed(db, rid, evidence=False, not_eligible=False):
    """A request that has already passed discovery and the checks (state CANDIDATES_CHECKED)."""
    spec = {"request_id": rid, **SPEC}
    db.insert("requests", {"request_id": rid, "input_json": spec, "spec_json": spec,
                           "state": "CANDIDATES_CHECKED", "created_by": "cli"})
    for sid, name, listed, dist in SUPPLIERS:
        db.insert("candidates", {"request_id": rid, "supplier_id": sid, "name": name,
                                 "website": f"https://{sid}.example.invalid",
                                 "address": "Sector 8, IMT Manesar, Haryana (fixture)",
                                 "classification": "manufacturer",
                                 "data_json": {"listed_price_inr_per_unit": listed,
                                               "price_basis": "ex-GST" if listed else "unknown",
                                               "distance_km": dist}})
        for check in CHECKS:
            db.insert("checks", {"request_id": rid, "supplier_id": sid, "check_name": check,
                                 "result": "pass", "explanation": f"{check} confirmed (fixture)"})
            if evidence:
                db.insert("evidence", {
                    "request_id": rid, "supplier_id": sid, "check_name": check,
                    "fact": f"{name}: {check} (fixture fact)",
                    "source_url": f"https://{sid}.example.invalid/about",
                    "source_type": "web_page", "field_or_quote": QUOTE,
                    "record_date": "2026-10-14"})
    if not_eligible:  # an excluded supplier, so the "not eligible" list has something to show
        db.insert("candidates", {"request_id": rid, "supplier_id": "sup_03",
                                 "name": "Gamma Traders (fixture)",
                                 "website": "https://sup_03.example.invalid",
                                 "classification": "trader", "data_json": {}})
        for check in CHECKS:
            result = "fail" if check == "manufacturer" else "cannot_verify"
            db.insert("checks", {
                "request_id": rid, "supplier_id": "sup_03", "check_name": check,
                "result": result,
                "explanation": ("site describes the company as a trader and distributor, "
                                "with no manufacturing signal (fixture)") if result == "fail"
                else "no source settles this (fixture)",
                "missing": None if result == "fail" else "a dated record"})


def cleanup(db, rid):
    """Delete one request and everything under it, children first (foreign keys)."""
    ids = [a["id"] for a in db.select("approvals", {"request_id": rid})]
    if ids:
        db.delete("outbox", {"approval_id": ("in", ids)})
    for table in ("quotes", "approvals", "evidence", "checks", "steps", "costs", "logs",
                  "candidates", "requests"):
        db.delete(table, {"request_id": rid})
