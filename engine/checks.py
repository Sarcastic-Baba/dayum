"""The 7 checks (background-check-criteria.md), computed per supplier from three kinds of
source: the dated Crustdata company record, the supplier's own web page, and geocoding.

The LLM only copies sentences from the page (verbatim). Code keeps a sentence as evidence
only if it is found word for word in the stored page text (the quote rule); every verdict
below is plain code over that verified evidence. Nothing settles a check -> cannot_verify.
"""

import re
from datetime import date

from pydantic import BaseModel

from . import llm
from .sources import geocode_place, haversine_km

GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
NAME_SUFFIX_RE = re.compile(r"\b(pvt|private|ltd|limited|llp|industries|industry|enterprises?|"
                            r"co|company|and|&|the)\b\.?", re.I)


class PriceQuote(BaseModel):
    quote: str
    price_inr: float | None = None
    unit: str | None = None            # box | piece | kg | ...
    ply: int | None = None


class PageQuotes(BaseModel):
    manufacturing: list[str] = []      # says it manufactures the item / factory / plant / machinery / capacity
    trading: list[str] = []            # calls itself a trader / dealer / distributor / supplier / wholesaler
    ply: list[str] = []                # names the ply counts it makes
    custom_sizes: list[str] = []       # sizes made to order
    prices: list[PriceQuote] = []
    address: str | None = None


PROMPT = ("You are reading one supplier's web page. Copy sentences from it VERBATIM, exactly as "
          "written (no paraphrasing, no fixing typos), into these lists: manufacturing = sentences "
          "saying the company manufactures the product, or mentioning its factory, plant, machinery "
          "or production capacity; trading = sentences calling it a trader, dealer, distributor, "
          "supplier or wholesaler; ply = sentences naming ply counts of boxes it makes; "
          "custom_sizes = sentences saying sizes are custom or made to order; prices = sentences "
          "stating a price (with price_inr, unit and ply if stated); address = its address as "
          "written. Leave a list empty if the page says nothing about it.\n\nPAGE:\n{page}")


def norm(t):
    t = (t or "").lower().replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t).strip()


def verified(quote, page_text):
    """The quote rule: a fact counts only if its sentence is in the stored page text."""
    q = norm(quote)
    return len(q) >= 8 and q in norm(page_text)


def gstin_checksum_ok(g):
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    factor, total = 2, 0
    for c in reversed(g[:14]):
        addend = factor * chars.index(c)
        factor = 1 if factor == 2 else 2
        total += addend // 36 + addend % 36
    return chars[(36 - total % 36) % 36] == g[14]


def base_name(name):
    return re.sub(r"[^a-z0-9]+", " ", NAME_SUFFIX_RE.sub(" ", (name or "").lower())).strip()


def _ev(fact, url, kind, quote, record_date):
    return {"fact": fact, "source_url": url, "source_type": kind, "field_or_quote": quote[:600],
            "record_date": record_date}


def extract(ctx, page_text):
    """LLM copies sentences; only those found verbatim on the page survive. No database
    access here, so pages can be extracted in parallel. -> (quotes, usage)."""
    c = ctx.cfg.get("llm") or {}
    q, usage = llm.extract(PROMPT.format(page=page_text[:12000]), PageQuotes, c["models"],
                           c.get("timeout_s", 20))
    keep = lambda xs: list(dict.fromkeys(x for x in xs if verified(x, page_text)))  # noqa: E731
    return {"manufacturing": keep(q.manufacturing), "trading": keep(q.trading),
            "ply": keep(q.ply), "custom_sizes": keep(q.custom_sizes),
            "prices": [p for p in q.prices if verified(p.quote, page_text)],
            "address": q.address if q.address and verified(q.address, page_text) else None}, usage


# ---- the seven checks: each returns (result, explanation, missing, evidence[]) ------------

def real_active(ctx, cand, page):
    d = cand["data_json"] or {}
    updated, cutoff = (d.get("crustdata_updated_at") or "")[:10], str(ctx.cfg["min_record_date"])
    rec = _ev(f"Company record for {cand['name']} exists, last refreshed {updated}",
              f"https://api.crustdata.com/company/search#id={d.get('crustdata_company_id')}",
              "api_response", f"metadata.updated_at = {d.get('crustdata_updated_at')}", updated or None)
    live = page and page.get("ok")
    if updated and updated >= cutoff and live:
        site = _ev(f"Its website answered on {page['fetched']}", page["url"], "web_page",
                   f"HTTP {page['status']}: {page['title'][:120]}", page["fetched"])
        return "pass", (f"Company record refreshed {updated} (after {cutoff}) and its own website "
                        f"is live as of {page['fetched']}."), None, [rec, site]
    if updated and updated < cutoff:
        return "cannot_verify", f"Company record last refreshed {updated}, before {cutoff}.", \
            "a record dated on or after the cut-off", [rec]
    return "cannot_verify", "No dated record plus a live website to show it is active.", \
        "a dated record and a reachable website", [rec] if updated else []


def manufacturer(ctx, cand, page, q):
    if not page or not page.get("ok"):
        return "cannot_verify", "Its own website could not be read, so only profile data is " \
            "available; manufacturing must be shown on its own site.", "its own website", []
    mfg, need = q["manufacturing"], ctx.cfg["manufacturer_min_signals"]
    ev = [_ev("Manufacturing signal on its own site", page["url"], "web_page", s, page["fetched"])
          for s in mfg]
    if len(mfg) >= need:
        return "pass", f"{len(mfg)} manufacturing signals on its own site (need {need}).", None, ev
    if q["trading"] and not mfg:
        tr = [_ev("Describes itself as a trader", page["url"], "web_page", s, page["fetched"])
              for s in q["trading"]]
        return "fail", "Its own site calls it a trader/dealer, with no manufacturing signal.", None, tr
    return "cannot_verify", f"{len(mfg)} manufacturing signal(s) on its own site; {need} needed.", \
        f"{need - len(mfg)} more manufacturing signal(s)", ev


def spec_match(ctx, cand, page, q):
    want = (ctx.spec.get("spec") or {}).get("ply")
    if not page or not page.get("ok"):
        return "cannot_verify", "Its own website could not be read.", "a statement of the ply it makes", []
    if not want:
        return "cannot_verify", "The request does not give a ply.", "the requested ply", []
    ply_re = re.compile(rf"\b{want}\s*-?\s*ply\b", re.I)
    hits = [s for s in q["ply"] if ply_re.search(s)]
    if hits:
        ev = [_ev(f"Makes {want}-ply boxes", page["url"], "web_page", s, page["fetched"]) for s in hits]
        sizes = (" Custom sizes: " + q["custom_sizes"][0]) if q["custom_sizes"] else ""
        return "pass", f"Its site states it makes {want}-ply boxes.{sizes}", None, ev
    only = [s for s in q["ply"] if re.search(r"\bonly\b", s, re.I)]
    if only:
        ev = [_ev("States a limited ply range", page["url"], "web_page", s, page["fetched"]) for s in only]
        return "fail", f"Its site limits the ply range and does not include {want}-ply.", None, ev
    return "cannot_verify", f"Its site does not say whether it makes {want}-ply boxes.", \
        f"a statement that it makes {want}-ply", []


def price(ctx, cand, page, q):
    want = (ctx.spec.get("spec") or {}).get("ply")
    for p in q["prices"]:
        unit = (p.unit or "").lower()
        if p.price_inr and unit in ("box", "boxes", "piece", "pcs", "pc", "unit", "carton") \
                and (not want or not p.ply or p.ply == want):
            ev = [_ev(f"Listed price INR {p.price_inr} per {unit}", page["url"], "web_page", p.quote,
                      page["fetched"])]
            return "pass", f"Listed INR {p.price_inr} per {unit} — a pre-RFQ hint only.", None, ev, \
                {"listed_price_inr_per_unit": p.price_inr, "price_basis": "unknown"}
    if any((p.unit or "").lower() in ("kg", "kgs") for p in q["prices"]):
        return "cannot_verify", "Its price is per kg and the box weight is not sourced.", \
            "a per-box price", [], {}
    return "cannot_verify", "No comparable price is published; the RFQ will ask for one.", \
        "a published per-box price", [], {}


def distance(ctx, cand, page, q, delivery):
    d = cand["data_json"] or {}
    where = q["address"] or d.get("hq") or cand["address"]
    g = geocode_place(ctx, where) if where else None
    if not (g and delivery):
        return "cannot_verify", "The supplier's address could not be located.", "a locatable address", \
            [], {}
    km = round(haversine_km(g, delivery) * ctx.cfg["road_factor"], 1)
    rate, per = ctx.cfg.get("transport_inr_per_km_per_load") or 0, ctx.cfg.get("boxes_per_load") or 0
    est = round(km * rate / per, 2) if rate > 0 and per > 0 else None
    ev = [_ev(f"Located at {g['display_name']}", g["source_url"], "api_response",
              f"lat={g['lat']}, lon={g['lon']} for '{where[:120]}'", None)]
    return "pass", (f"About {km} km by road to {ctx.spec.get('delivery_location')} — an estimate "
                    f"(straight line × {ctx.cfg['road_factor']}), not a sourced fact."), None, ev, \
        {"distance_km": km, "est_transport_inr_per_unit": est}


def fraud_risk(ctx, cand, page):
    text = (page or {}).get("text") or ""
    gstins = sorted(set(GSTIN_RE.findall(text)))
    if not gstins:
        return "cannot_verify", "No GSTIN on its site, and GST/MCA registries are not accessible.", \
            "an authoritative registration record", []
    g = gstins[0]
    ev = [_ev(f"GSTIN {g} shown on its site", page["url"], "web_page", g, page["fetched"])]
    if not gstin_checksum_ok(g):
        return "cannot_verify", f"GSTIN {g} on its site fails its check digit (possibly a typo); " \
            "active status can't be checked.", "an authoritative registration record", ev
    return "cannot_verify", f"GSTIN {g} on its site is well-formed, but its active status needs the " \
        "GST portal (captcha), so it can't be confirmed.", "the GST portal's active status", ev


def duplicate(ctx, cand, lookalikes):
    others = lookalikes.get(cand["supplier_id"]) or []
    if others:
        return "pass", ("Similar name to " + ", ".join(others) + " but a different website — kept "
                        "separate, never merged on name alone."), None, [], ["lookalike_name"]
    return "pass", "No other candidate shares its website.", None, [], []
