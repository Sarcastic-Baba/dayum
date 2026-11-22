"""Live sources: Crustdata (company search = discovery, web fetch = supplier pages) and
OpenStreetMap Nominatim (geocoding). Every paid call is cached as a step, so a restart never
pays twice, and recorded in the cost ledger when it actually runs."""

import math
import os
import re
import time
from html import unescape
from html.parser import HTMLParser

import httpx

from .ledger import record_cost
from .steps import cached

CRUSTDATA = "https://api.crustdata.com"
UA = "BoxSource/0.1 (ContextCon hackathon prototype)"


def _crustdata(ctx, path, body, kind):
    def call():
        headers = {"authorization": f"Bearer {os.environ['CRUSTDATA_API_KEY']}",
                   "x-api-version": "2025-11-01", "content-type": "application/json"}
        with httpx.Client(base_url=CRUSTDATA, headers=headers, timeout=90) as c:
            r = c.post(path, json=body)
            r.raise_for_status()
            record_cost(ctx.db, ctx.rid, kind, "crustdata", 1,
                        ctx.cfg["pricing"].get("crustdata_per_call", 0))
            return r.json()
    return cached(ctx.db, ctx.rid, kind, body, call)


def search_companies(ctx, query, limit):
    body = {"search": {"query": query, "mode": "hybrid"},
            "filters": {"field": "locations.country", "type": "=",
                        "value": ctx.cfg["discovery"]["country"]}, "limit": limit}
    return _crustdata(ctx, "/company/search", body, "crustdata_company_search").get("companies") or []


def fetch_pages(ctx, urls):
    """Crustdata web fetch, up to 10 URLs per call -> {requested url: page dict}."""
    out = {}
    for i in range(0, len(urls), 10):
        batch = urls[i:i + 10]
        got = _crustdata(ctx, "/web/enrich/live", {"urls": batch}, "crustdata_web_fetch") or []
        for url, page in zip(batch, got if isinstance(got, list) else []):
            out[url] = page
    return out


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


def html_to_text(html):
    p = _Text()
    p.feed(html or "")
    return re.sub(r"\s+", " ", unescape(" ".join(p.parts))).strip()


# ---- geocoding ---------------------------------------------------------------------------

_last_call = [0.0]
_MISSES = set()  # queries Nominatim had no answer for (this process) — never asked twice


def geocode(ctx, query):
    """-> {"lat", "lon", "display_name", "source_url"} or None. Cached in geo_cache;
    live calls respect Nominatim's 1 request/second policy."""
    if not query or query in _MISSES:
        return None
    hit = ctx.db.select("geo_cache", {"query": query})
    if hit:
        return hit[0]["source_json"]
    wait = 1.1 - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
    params = {"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "in"}
    r = httpx.get("https://nominatim.openstreetmap.org/search", params=params,
                  headers={"User-Agent": UA}, timeout=20)
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        _MISSES.add(query)
        return None
    g = {"lat": float(rows[0]["lat"]), "lon": float(rows[0]["lon"]),
         "display_name": rows[0]["display_name"],
         "source_url": str(httpx.URL("https://nominatim.openstreetmap.org/search", params=params))}
    ctx.db.insert("geo_cache", {"query": query, "lat": g["lat"], "lon": g["lon"], "source_json": g},
                  conflict=["query"])
    return g


def geocode_place(ctx, text):
    """Try the full address, then shorter tails ('…, Gurugram, Haryana, India')."""
    parts = [p.strip() for p in re.split(r"[,;]", text or "") if p.strip()]
    tails = [", ".join(parts[i:]) for i in range(len(parts))]
    for q in (tails[:2] + tails[-1:]) if len(tails) > 3 else tails:  # at most 3 live calls
        g = geocode(ctx, q)
        if g:
            return g
    return None


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))
