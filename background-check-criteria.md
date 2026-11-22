# BoxSource — Supplier Background Check Criteria

Every supplier the agent finds goes through 7 checks. Each check returns **pass**, **fail** or **cannot_verify**, and every fact behind it needs a source, the exact supporting quote and a date.

## Rules for every check

- **Proof:** a fact counts only if the page or API response is stored and the exact supporting sentence is found in it (text normalized: lowercase, whitespace collapsed, quotes and dashes normalized). If the quote isn't found, the fact is discarded.
- **Freshness:** facts about the supplier's *current* state (active, price) must have `record_date >= 2026-09-01` (`config.min_record_date`). Older evidence → `cannot_verify` (stale).
- **No source:** if nothing settles a check → `cannot_verify`, and the output names what is missing. Never guess.

## The 7 checks

### 1. real_active: is it real and active?
| Result | Criteria |
|---|---|
| pass | Company exists (company data record, or own website with an address or registration number) **and** shows recent activity (an updated site, listing, news or job post dated ≥ 1 Sep 2026) |
| fail | Sourced evidence of closure or shutdown |
| cannot_verify | Exists but no recent activity, or no trace found |

### 2. manufacturer: manufacturer or trader?
| Result | Criteria |
|---|---|
| pass (manufacturer) | At least `config.manufacturer_min_signals` (default **2**) sourced signals from: own site says it manufactures the item; factory or plant address given; machinery or capacity described (e.g. a corrugation plant); registration type stated as manufacturing |
| fail (trader) | Calls itself a trader, dealer, distributor or supplier, with no manufacturing signal |
| cannot_verify | Appears only on listing sites, with no website of its own |

Manufacturing signals must come from the supplier's **own** site or an official record, not from a listing page.

### 3. duplicate: duplicate or lookalike?
| Situation | Criteria | Action |
|---|---|---|
| Same supplier | Same website domain, GSTIN or phone number | Merge into one candidate and keep all evidence |
| Lookalike | Similar name but different domain or address | Keep separate and flag `lookalike_name`. **Never merge on name alone.** |
| Existing supplier | Already in the client's supplier records | Note it; don't treat it as new |

### 4. spec_match: does it make what we asked for?
| Result | Criteria |
|---|---|
| pass | Sourced evidence of the right ply count and material. For size, "custom sizes available" counts if ply matches (`config.accept_custom_size_statement`) |
| fail | Contradiction (e.g. "only 3-ply") |
| cannot_verify | No statement either way |

### 5. price: is the price known and comparable?
| Result | Criteria |
|---|---|
| pass | Listed price for the same ply and a comparable size, per box, dated ≥ 1 Sep 2026; MOQ and GST basis recorded where shown |
| cannot_verify | No price published; price per kg with no sourced box weight; price for a different spec; price dated before 1 Sep 2026 |

Here `price` is the supplier's *listed/published* price, used only to pre-sort the shortlist. A supplier with `cannot_verify` listed price still receives an RFQ and is ranked normally once its **quoted** price comes back.

### 6. distance: how far is it?
- Geocode the supplier address and the delivery location (OpenStreetMap Nominatim, cached).
- `distance_km = haversine × config.road_factor`
- `est_transport_inr_per_unit = distance_km × config.transport_inr_per_km_per_load / config.boxes_per_load`
- These are **estimates from the client's own rates**, labelled as estimates in the output and never presented as sourced facts.
- No locatable address → `cannot_verify`.

### 7. fraud_risk: any sourced sign of fraud, shell or scam behaviour?

Confirms the supplier is a genuine trading entity, not a fraudulent or shell operator. Every signal must be sourced and dated; **never** output a bare "scam" label on suspicion alone — a wrong fraud call costs more than `cannot_verify`.

| Result | Criteria |
|---|---|
| pass (low risk) | An authoritative identity record is **active** and consistent with the site — e.g. GSTIN shown active on the GST portal, or company/LLP status active (not struck-off or dormant) on MCA — **and** no red flag below |
| fail (high risk) | A sourced, dated red flag: GSTIN or company status cancelled, struck-off or not found; site identity (name, GSTIN, address) contradicts the official record; a credible fraud or complaint report, or news naming this company; a payee/bank name that does not match the legal entity |
| cannot_verify | No authoritative record found, or records exist but nothing confirms or contradicts legitimacy (the safe default) |

Sources: GST portal (GSTIN status), MCA (company/LLP status), domain age via WHOIS (a very new domain is a weak signal, noted not decisive), and Crustdata/web search for complaints or fraud news. A supplier that fails `fraud_risk` is never recommended, ranked or sent an RFQ.

## From checks to decision (plain code, no LLM)

| Status | Rule |
|---|---|
| Eligible | manufacturer = pass **and** real_active = pass **and** spec_match = pass **and** fraud_risk = pass **and** not a duplicate of another candidate |
| Shortlist (pre-RFQ) | All eligible candidates, pre-sorted by **listed** price where published (`ex-GST per box + est. transport`). Listed price is only a hint and a caveat — never the final basis |
| RFQ sent | After approval, an RFQ goes to **every** shortlisted supplier, including those with no listed price |
| Ranked (final) | Among suppliers that **returned a valid, comparable quote**, sorted by `quoted price (ex-GST per box) + est. transport per box`; ties broken by distance |
| Recommendation | Primary = rank 1, backup = rank 2 (by returned quote), trial order = `ceil(monthly qty × config.trial_order_fraction)` (default 10%) |
| Awaiting quotes | RFQs sent, no reply yet → status `awaiting_quotes`; the dashboard shows pending RFQs. Suppliers that never reply are listed, not ranked |
| No eligible supplier | Status `no_verified_supplier`, with what was searched and why each candidate failed |
| Limit reached | Status `held`, reason `limit_reached`, unfinished checks listed |

## Tuning note

The **2 manufacturing signals** bar is strict. Many real small factories have thin websites and will come back `cannot_verify`. That is the safe side, because a wrong answer costs more than `cannot_verify`. If the M0 probe shows almost no supplier passes, set `manufacturer_min_signals: 1` in `config.yaml`, but keep requiring the quote from the supplier's own site.
