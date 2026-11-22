"""Eligibility, shortlist, ranking and recommendation — plain code, no LLM (PRD §11).

Listed web prices only pre-sort the shortlist. The ranking uses the prices suppliers quote
in their RFQ replies, normalized to ex-GST per unit, plus the estimated transport cost."""

import math

ELIGIBILITY = ("manufacturer", "real_active", "spec_match", "fraud_risk")


def _checks(db, request_id):
    return {(c["supplier_id"], c["check_name"]): c
            for c in db.select("checks", {"request_id": request_id})}


def eligibility(db, request_id):
    """-> (eligible candidates, {supplier_id: reason it is not eligible})"""
    checks = _checks(db, request_id)
    ok, why_not = [], {}
    for c in db.select("candidates", {"request_id": request_id}, order=["supplier_id"]):
        sid = c["supplier_id"]
        if c["merged_into"]:
            why_not[sid] = f"duplicate of {c['merged_into']}"
            continue
        failing = [f"{n}={checks.get((sid, n), {}).get('result', 'missing')}"
                   for n in ELIGIBILITY if checks.get((sid, n), {}).get("result") != "pass"]
        if failing:
            why_not[sid] = "not eligible: " + ", ".join(failing)
        else:
            ok.append(c)
    return ok, why_not


def shortlist(db, request_id):
    eligible, why_not = eligibility(db, request_id)

    def order(c):
        d = c["data_json"] or {}
        lp, dist = d.get("listed_price_inr_per_unit"), d.get("distance_km")
        return (lp is None, lp or 0, dist if dist is not None else math.inf)

    return [c["supplier_id"] for c in sorted(eligible, key=order)], why_not


def est_transport(cfg, distance_km):
    rate = cfg.get("transport_inr_per_km_per_load") or 0
    per_load = cfg.get("boxes_per_load") or 0
    if distance_km is None or rate <= 0 or per_load <= 0:
        return None
    return round(distance_km * rate / per_load, 2)


def ex_gst_price(cfg, q):
    if q["gst_basis"] == "ex-GST":
        return q["price_inr_per_unit"]
    rate = cfg.get("gst_rate_percent")
    if q["gst_basis"] == "incl-GST" and rate:
        return round(q["price_inr_per_unit"] / (1 + rate / 100), 4)
    return None


def rank_quotes(db, cfg, request_id, spec, shortlist_ids):
    quotes = db.select("quotes", {"request_id": request_id}, order=["id"])
    cands = {c["supplier_id"]: c for c in db.select("candidates", {"request_id": request_id})}
    want_ply = (spec.get("spec") or {}).get("ply")
    ranked, unranked = [], []
    for sid in shortlist_ids:
        mine = [q for q in quotes if q["supplier_id"] == sid]
        parsed = [q for q in mine if q["parsed_ok"]]
        if not mine:
            unranked.append({"supplier_id": sid, "reason": "no reply to the RFQ"})
            continue
        if not parsed:
            unranked.append({"supplier_id": sid,
                             "reason": "reply received but no price could be read; needs a human"})
            continue
        q = parsed[-1]
        if want_ply and q["ply"] and q["ply"] != want_ply:
            unranked.append({"supplier_id": sid,
                             "reason": f"quote is for {q['ply']}-ply, requested {want_ply}-ply"})
            continue
        ex = ex_gst_price(cfg, q)
        if ex is None:
            reason = ("quote includes GST but gst_rate_percent is not configured"
                      if q["gst_basis"] == "incl-GST" else "quote does not say whether GST is included")
            unranked.append({"supplier_id": sid, "reason": reason})
            continue
        dist = (cands[sid]["data_json"] or {}).get("distance_km")
        transport = est_transport(cfg, dist)
        ranked.append({"supplier_id": sid, "quote_id": q["id"], "quoted_ex_gst": ex,
                       "est_transport": transport, "distance_km": dist,
                       "est_landed": round(ex + (transport or 0), 2)})
    ranked.sort(key=lambda r: (r["est_landed"],
                               r["distance_km"] if r["distance_km"] is not None else math.inf))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return ranked, unranked


def recommendation(cfg, spec, ranked, unranked, names):
    qty = spec.get("quantity_per_month")
    rec = {
        "primary": ranked[0]["supplier_id"] if ranked else None,
        "backup": ranked[1]["supplier_id"] if len(ranked) > 1 else None,
        "trial_order_qty": math.ceil(qty * cfg["trial_order_fraction"]) if qty else None,
        "full_monthly_qty": qty,
    }
    if ranked:
        top = ranked[0]
        rec["reasoning"] = (f"{names.get(top['supplier_id'], top['supplier_id'])} has the lowest "
                            f"estimated landed cost: INR {top['est_landed']}/unit "
                            f"(quoted INR {top['quoted_ex_gst']} ex-GST"
                            + (f" + est. transport INR {top['est_transport']}" if top["est_transport"]
                               is not None else "") + ").")
    else:
        rec["reasoning"] = "No comparable quote yet, so nothing can be ranked."
    caveats = ["Ranked on prices quoted in supplier replies; listed web prices were a pre-RFQ hint only."]
    if ranked and any(r["est_transport"] is None for r in ranked):
        caveats.append("Transport cost not included: client transport rates are not configured.")
    elif ranked:
        caveats.append("Transport cost is an estimate from client rates, not a sourced fact.")
    if unranked:
        caveats.append("Not ranked: " + "; ".join(
            f"{names.get(u['supplier_id'], u['supplier_id'])} ({u['reason']})" for u in unranked))
    budget = spec.get("budget_inr_per_unit")
    if budget and ranked and all(r["est_landed"] > budget for r in ranked):
        caveats.append(f"Every quote exceeds the budget of INR {budget}/unit; the cheapest is listed.")
    rec["caveats"] = caveats
    return rec
