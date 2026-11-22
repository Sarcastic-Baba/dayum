# What the live sources actually return (E1 probe, 22 Nov 2026)

Reproduce with `python scripts/probe.py out.json`. This is evidence, not assumption: every
row below is what a real call returned on 22 Nov 2026.

| Source | What came back | Usable for |
|---|---|---|
| Gemini + Google Search grounding (`gemini-3.5-flash`, `gemini-flash-latest`) | **429 RESOURCE_EXHAUSTED** — "You exceeded your current quota" — on every model | ✗ supplier discovery is blocked on this key's plan |
| Gemini plain generation | `gemini-flash-lite-latest` / `gemini-3.1-flash-lite`: correct structured extraction in ~1–3 s. `gemini-3.5-flash`: correct but 8–22 s (heavy thinking). `gemini-flash-latest`, `gemini-3.8-flash`: **503** overloaded at times. `gemini-2.5-*`: **404**, retired for new users | ✓ extraction (the voice intake uses the two lite models) |
| Gemini request deadline | **400** "Minimum allowed deadline is 10s" for shorter client timeouts | short caps are enforced client-side in `engine/llm.py` |
| OpenStreetMap Overpass (`name ~ corrugat\|carton\|packag`, 50 km around Gurgaon) | 0 matches on one run; a 90 s read timeout on the next | ✗ too sparse and too slow for discovery |
| OpenStreetMap Nominatim | works: Manesar → 28.3247, 76.9264; Gurugram → 28.4646, 77.0299 | ✓ the distance check |
| GST portal taxpayer search | page loads (JS app); the actual search needs a captcha | ✗ no captcha bypass, by rule |
| MCA master data | **403** (blocked for automated clients) | ✗ |
| RDAP domain age | not exercised — no supplier domains were found to look up | pending |
| Crustdata | key received 22 Nov; not yet probed | next: discovery + company data |

## What this means

- **Discovery needs Crustdata** (or another search-API key, or billing enabled on the Gemini
  key for grounded search). Until then the engine finds no candidates and every request ends
  honestly as `no_verified_supplier`.
- **`fraud_risk` will mostly be `cannot_verify`** unless a source exposes registration
  status: GST and MCA can't be read without a captcha or login. Check whether Crustdata's
  company data carries it.
- **`real_active` needs a record dated on or after 1 Sep 2026**; small supplier sites rarely
  carry dates, so expect `cannot_verify` there too unless Crustdata supplies a dated record.
