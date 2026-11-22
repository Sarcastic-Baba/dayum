"""E1 reality probe: what do keyless live sources actually return for supplier sourcing?

Runs Gemini grounded search (URL finder only), fetches the supplier pages it points to,
and measures per candidate which of the 7 checks could pass from what is really on the
page. Also probes Nominatim, RDAP (domain age) and whether GST / MCA registries can be
read without a captcha. Prints a summary and writes raw results to a JSON file.

Usage:  python scripts/probe.py [output.json]
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

UA = "BoxSource/0.1 (ContextCon hackathon prototype)"
MIN_RECORD_DATE = datetime(2026, 9, 1, tzinfo=timezone.utc)
QUERIES = [
    "5 ply corrugated box manufacturer Manesar Haryana",
    "corrugated box manufacturer Gurgaon factory",
    "corrugated packaging boxes manufacturer Faridabad 5 ply",
]
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
DATE_RE = re.compile(
    r"\b(\d{1,2}[\s\-/](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-/,]+2026"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+2026"
    r"|2026-\d{2}-\d{2})\b",
    re.I,
)
SKIP_DOMAINS = ("indiamart.", "justdial.", "tradeindia.", "exportersindia.", "facebook.",
                "linkedin.", "youtube.", "instagram.", "wikipedia.", "google.")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"\s+", " ", unescape(" ".join(p.parts))).strip()


def gstin_checksum_ok(gstin: str) -> bool:
    """Luhn mod-36 check digit used by GSTIN (format validity only, not active status)."""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    factor, total = 2, 0
    for c in reversed(gstin[:14]):
        addend = factor * chars.index(c)
        factor = 1 if factor == 2 else 2
        total += addend // 36 + addend % 36
    return chars[(36 - total % 36) % 36] == gstin[14]


NAME_RE = r"corrugat|carton|packag"
EXCLUDE = ("mover", "courier", "logistic", "packers and")


def osm_discover(http, lat, lon, radius_m=50000):
    """Keyless discovery: OpenStreetMap features near the delivery location whose name says
    corrugated/carton/packaging. Each element carries its last-edit timestamp."""
    q = (f'[out:json][timeout:60];nwr(around:{radius_m},{lat},{lon})["name"~"{NAME_RE}",i];'
         "out center tags meta;")
    r = http.post("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=90)
    out = []
    for el in r.json().get("elements", []) if r.status_code == 200 else []:
        tags = el.get("tags", {})
        name = tags.get("name", "")
        if any(x in name.lower() for x in EXCLUDE):
            continue
        out.append({"osm": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
                    "name": name, "timestamp": el.get("timestamp"),
                    "website": tags.get("website") or tags.get("contact:website"),
                    "phone": tags.get("phone") or tags.get("contact:phone"),
                    "addr": ", ".join(v for k, v in tags.items() if k.startswith("addr:")),
                    "kind": tags.get("industrial") or tags.get("craft") or tags.get("office")
                    or tags.get("shop") or tags.get("building")})
    return {"status": r.status_code, "results": out}


def grounded_search(client, model, query):
    cfg = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0)
    prompt = (f"Search the web for: {query}. List the individual company websites you find "
              "(not directories). Company name and website only.")
    t0 = time.time()
    r = client.models.generate_content(model=model, contents=prompt, config=cfg)
    gm = r.candidates[0].grounding_metadata if r.candidates else None
    chunks = [(c.web.uri, c.web.title) for c in (gm.grounding_chunks or [])] if gm else []
    u = r.usage_metadata
    return {
        "query": query,
        "model_version": r.model_version,
        "web_search_queries": list(gm.web_search_queries or []) if gm else [],
        "chunks": chunks,
        "usage": {"prompt": u.prompt_token_count, "output": u.candidates_token_count,
                  "total": u.total_token_count},
        "seconds": round(time.time() - t0, 1),
    }


def resolve(client_http, uri):
    """Grounding URIs are redirect wrappers; read Location without fetching the target."""
    try:
        r = client_http.get(uri, follow_redirects=False)
        return r.headers.get("location") or uri
    except httpx.HTTPError as e:
        return f"ERROR {type(e).__name__}"


def analyze_page(http, url):
    out = {"url": url}
    try:
        r = http.get(url, follow_redirects=True)
    except httpx.HTTPError as e:
        out["error"] = type(e).__name__
        return out
    out["status"] = r.status_code
    out["final_url"] = str(r.url)
    out["last_modified"] = r.headers.get("last-modified")
    text = html_to_text(r.text) if "html" in r.headers.get("content-type", "") else ""
    low = text.lower()
    out["chars"] = len(text)
    out["captcha_or_block"] = any(w in low for w in ("captcha", "access denied", "are you a robot"))
    out["manufacturer_phrases"] = sorted({m for m in re.findall(
        r"(manufactur\w*|factory|plant|corrugation machine|corrugator|production capacity)", low)})
    out["trader_phrases"] = sorted({m for m in re.findall(
        r"\b(trader|dealer|distributor|stockist|wholesaler)s?\b", low)})
    out["ply_mentions"] = sorted({m for m in re.findall(r"\b(\d)\s*-?\s*ply\b", low)})
    gstins = sorted(set(GSTIN_RE.findall(text)))
    out["gstins"] = [{"gstin": g, "checksum_ok": gstin_checksum_ok(g)} for g in gstins]
    out["dates_2026"] = sorted(set(DATE_RE.findall(text)))[:10]
    out["pincodes"] = sorted(set(re.findall(r"\b(1[2-3]\d{4})\b", text)))[:5]
    out["sample"] = text[:300]
    return out


def geocode(http, q):
    r = http.get("https://nominatim.openstreetmap.org/search",
                 params={"q": q, "format": "jsonv2", "limit": 1, "countrycodes": "in"})
    time.sleep(1.1)  # Nominatim policy: max 1 request/second
    data = r.json() if r.status_code == 200 else []
    return {"q": q, "status": r.status_code,
            "hit": {k: data[0][k] for k in ("lat", "lon", "display_name")} if data else None}


def rdap(http, domain):
    try:
        r = http.get(f"https://rdap.org/domain/{domain}", follow_redirects=True)
        if r.status_code != 200:
            return {"domain": domain, "status": r.status_code}
        ev = {e.get("eventAction"): e.get("eventDate") for e in r.json().get("events", [])}
        return {"domain": domain, "status": 200, "registered": ev.get("registration"),
                "last_changed": ev.get("last changed"), "rdap_url": str(r.url)}
    except (httpx.HTTPError, ValueError) as e:
        return {"domain": domain, "error": type(e).__name__}


def registry_reachability(http):
    out = {}
    for name, url in {
        "gst_portal_search": "https://services.gst.gov.in/services/searchtp",
        "mca_master_data": "https://www.mca.gov.in/content/mca/global/en/mca/master-data/MDS.html",
    }.items():
        try:
            r = http.get(url, follow_redirects=True)
            low = r.text.lower()
            out[name] = {"status": r.status_code, "mentions_captcha": "captcha" in low,
                         "mentions_login": "login" in low, "chars": len(r.text)}
        except httpx.HTTPError as e:
            out[name] = {"error": type(e).__name__}
    return out


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "probe-results.json"
    model = "gemini-3.5-flash"
    gclient = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(
        timeout=40000, retry_options=types.HttpRetryOptions(attempts=1)))
    http = httpx.Client(timeout=20, headers={"User-Agent": UA})
    results = {"run_at": datetime.now(timezone.utc).isoformat(), "searches": [], "pages": []}

    print("== Grounded search (Gemini, URL finder only) ==")
    urls = []
    for q in QUERIES[:1]:
        try:
            s = grounded_search(gclient, model, q)
        except Exception as e:  # noqa: BLE001 — record whatever the source does
            results["searches"].append({"query": q, "error": str(e)[:200]})
            print(f"- {q!r}: FAILED {str(e)[:120]}")
            continue
        s["resolved"] = [(resolve(http, uri), title) for uri, title in s["chunks"]]
        results["searches"].append(s)
        print(f"- {q!r}: {len(s['chunks'])} chunks, model={s['model_version']}")
        for final, title in s["resolved"]:
            if final.startswith("http") and not any(d in final for d in SKIP_DOMAINS):
                urls.append(final)

    print("\n== OpenStreetMap discovery (keyless) ==")
    centre = geocode(http, "Gurgaon, Haryana")
    results["centre"] = centre
    if centre["hit"]:
        osm = osm_discover(http, centre["hit"]["lat"], centre["hit"]["lon"])
        results["osm"] = osm
        with_site = [o for o in osm["results"] if o["website"]]
        print(f"Overpass status={osm['status']}: {len(osm['results'])} named matches within 50 km, "
              f"{len(with_site)} with a website tag")
        for o in osm["results"][:25]:
            print(f"   {o['name'][:48]:48s} kind={o['kind']} edited={o['timestamp']} "
                  f"site={o['website']} phone={'yes' if o['phone'] else 'no'}")
        urls += [o["website"] if o["website"].startswith("http") else "http://" + o["website"]
                 for o in with_site]

    seen, picked = set(), []
    for u in urls:
        dom = urlparse(u).netloc.lower().removeprefix("www.")
        if dom and dom not in seen:
            seen.add(dom)
            picked.append(u)
    picked = picked[:6]

    print("\n== Supplier pages ==")
    for u in picked:
        p = analyze_page(http, u)
        p["rdap"] = rdap(http, urlparse(p.get("final_url", u)).netloc.removeprefix("www."))
        results["pages"].append(p)
        print(json.dumps({k: p.get(k) for k in (
            "final_url", "status", "chars", "captcha_or_block", "last_modified",
            "manufacturer_phrases", "trader_phrases", "ply_mentions", "gstins",
            "dates_2026", "pincodes")}, ensure_ascii=False))
        print("    rdap:", p["rdap"])

    print("\n== Nominatim ==")
    results["geocode"] = [geocode(http, q) for q in
                          ("Manesar, Haryana", "Gurgaon, Haryana")]
    for g in results["geocode"]:
        print(g)

    print("\n== Registry reachability ==")
    results["registries"] = registry_reachability(http)
    for k, v in results["registries"].items():
        print(k, v)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nRaw results -> {out_path}")


if __name__ == "__main__":
    main()
