"""Stages before the shortlist: parse the request, discover suppliers (Crustdata company
search), fetch each supplier's own site (Crustdata web fetch), and run the 7 checks.
Each supplier's checks are one idempotent step, so a killed run never duplicates evidence."""

import hashlib
import re
from datetime import date

from concurrent.futures import ThreadPoolExecutor

from . import checks, io_format, ledger, llm, sources
from .steps import get_step, idem_key, run_step
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


def _domain(url):
    d = re.sub(r"^https?://", "", (url or "").strip().lower()).split("/")[0]
    return d.removeprefix("www.")


def discover(ctx):
    """Crustdata company search near the delivery location -> candidate rows, one per website
    (the same domain twice is the same supplier). Each row keeps its dated company record."""
    spec, d = ctx.spec, ctx.cfg["discovery"]
    query = d["query"].format(item=spec.get("item") or "corrugated box",
                              location=spec.get("delivery_location") or "")
    rows, seen = [], set()
    for c in sources.search_companies(ctx, query, ctx.cfg["max_candidates"]):
        b, meta = c.get("basic_info") or {}, c.get("metadata") or {}
        dom = _domain(b.get("primary_domain") or b.get("website"))
        if dom and any(x in dom for x in d["not_own_site"]):
            dom = ""  # a directory / share link is not the supplier's own website
        if dom and dom in seen:
            continue
        seen.add(dom)
        hq = (c.get("locations") or {}).get("headquarters")
        rows.append({"supplier_id": f"sup_{len(rows) + 1:02d}", "name": b.get("name") or "unknown",
                     "website": f"https://{dom}" if dom else None,
                     "address": hq if isinstance(hq, str) else None,
                     "data_json": {"crustdata_company_id": c.get("crustdata_company_id"),
                                   "crustdata_updated_at": meta.get("updated_at"),
                                   "description": b.get("description"),
                                   "hq": hq if isinstance(hq, str) else None}})
    return rows


def _pages(ctx, cands):
    urls = [c["website"] for c in cands if c["website"]]
    got = sources.fetch_pages(ctx, urls) if urls else {}
    out = {}
    for url, p in got.items():
        ok = bool(p.get("success")) and (p.get("http_status") or 0) < 400
        text = sources.html_to_text(p.get("content") or "")
        out[url] = {"url": p.get("url") or url, "ok": ok, "status": p.get("http_status"),
                    "title": p.get("title") or "", "text": text,
                    "fetched": date.fromtimestamp(p["timestamp"]).isoformat() if p.get("timestamp")
                    else date.today().isoformat()}
        if text:  # every fetched page is kept, so each quote can be re-checked later
            ctx.db.insert("pages", {"url": out[url]["url"], "text": text,
                                    "content_hash": hashlib.sha256(text.encode()).hexdigest()},
                          conflict=["url", "content_hash"])
    return out


def _lookalikes(cands):
    by_name = {}
    for c in cands:
        by_name.setdefault(checks.base_name(c["name"]), []).append(c)
    return {c["supplier_id"]: [o["name"] for o in group if o is not c]
            for group in by_name.values() if len(group) > 1 for c in group}


EMPTY = {"manufacturing": [], "trading": [], "ply": [], "custom_sizes": [], "prices": [],
         "address": None}


def _check_one(ctx, cand, page, delivery, lookalikes, extracted):
    """All 7 checks for one supplier. Replaces any half-written rows from a killed run."""
    rid, sid = ctx.rid, cand["supplier_id"]
    ctx.db.delete("evidence", {"request_id": rid, "supplier_id": sid})
    ctx.db.delete("checks", {"request_id": rid, "supplier_id": sid})
    q = EMPTY
    if extracted:  # (quotes, usage) from the parallel extraction
        q, usage = extracted
        ledger.record_cost(ctx.db, rid, "checks_extract", "llm",
                           usage["tokens_in"] + usage["tokens_out"],
                           ledger.llm_cost_inr(ctx.cfg, usage["tokens_in"], usage["tokens_out"]))
    data, results = dict(cand["data_json"] or {}), {}
    out = {"real_active": checks.real_active(ctx, cand, page),
           "manufacturer": checks.manufacturer(ctx, cand, page, q),
           "spec_match": checks.spec_match(ctx, cand, page, q),
           "fraud_risk": checks.fraud_risk(ctx, cand, page)}
    *out["price"], extra = checks.price(ctx, cand, page, q)
    data.update(extra)
    *out["distance"], extra = checks.distance(ctx, cand, page, q, delivery)
    data.update(extra)
    *out["duplicate"], flags = checks.duplicate(ctx, cand, lookalikes)
    data["flags"] = flags
    ev_rows, check_rows = [], []
    for name, (result, explanation, missing, evidence) in out.items():
        ev_rows += [{"request_id": rid, "supplier_id": sid, "check_name": name, **e} for e in evidence]
        check_rows.append({"request_id": rid, "supplier_id": sid, "check_name": name,
                           "result": result, "explanation": explanation, "missing": missing})
        results[name] = result
    ctx.db.insert_many("evidence", ev_rows)  # two round trips instead of one per row
    ctx.db.insert_many("checks", check_rows)
    m = out["manufacturer"][0]
    ctx.db.update("candidates", {"request_id": rid, "supplier_id": sid}, {
        "classification": "manufacturer" if m == "pass" else "trader" if m == "fail" else "cannot_verify",
        "data_json": data})
    return results


def run_checks(ctx):
    cands = [c for c in ctx.db.select("candidates", {"request_id": ctx.rid}, order=["supplier_id"])
             if not c["merged_into"]]
    pages = run_step(ctx.db, ctx.rid, "fetch_pages", lambda: _pages(ctx, cands))
    delivery = sources.geocode_place(ctx, ctx.spec.get("delivery_location"))
    lookalikes = _lookalikes(cands)
    pending = [c for c in cands if (get_step(ctx.db, idem_key(ctx.rid, "step", f"checks:{c['supplier_id']}"))
                                    or {}).get("status") != "done"]
    readable = {c["supplier_id"]: p["text"] for c in pending
                if (p := pages.get(c["website"])) and p["ok"] and p["text"]}
    extracted = {}
    with ThreadPoolExecutor(max_workers=6) as pool:  # page extraction is pure LLM work
        futures = {sid: pool.submit(checks.extract, ctx, text) for sid, text in readable.items()}
    for sid, fut in futures.items():
        try:
            extracted[sid] = fut.result()
        except llm.LLMUnavailable:
            extracted[sid] = None  # that supplier's page checks become cannot_verify
    for c in pending:
        run_step(ctx.db, ctx.rid, f"checks:{c['supplier_id']}",
                 lambda c=c: _check_one(ctx, c, pages.get(c["website"]), delivery, lookalikes,
                                        extracted.get(c["supplier_id"])))
