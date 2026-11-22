"""The 7 checks' verdict logic on fixture pages (no network: the LLM is a fake that returns
the sentences a page 'contains' — including one it invented, which the quote rule drops)."""

from types import SimpleNamespace

from engine import checks, llm
from engine.pipeline import _lookalikes

CFG = {"min_record_date": "2026-09-01", "manufacturer_min_signals": 2, "road_factor": 1.3,
       "llm": {"models": ["fake"], "timeout_s": 1}}
PAGE_TEXT = ("Kapoor Box Factory is a manufacturer of corrugated boxes since 1972. Our plant at "
             "IMT Manesar runs two automatic corrugation lines. We make 3 ply, 5 ply and 7 ply boxes. "
             "Sizes are made to order.")


def ctx(ply=5):
    return SimpleNamespace(cfg=CFG, spec={"spec": {"ply": ply}, "delivery_location": "Gurgaon"})


def page(text=PAGE_TEXT):
    return {"url": "https://kapoorbox.example.invalid", "ok": True, "status": 200, "title": "Kapoor",
            "text": text, "fetched": "2026-11-22"}


def fake_llm(monkeypatch, **lists):
    def extract(prompt, schema, models, timeout_s, on_usage=None):
        return schema(**lists), {"tokens_in": 10, "tokens_out": 5}
    monkeypatch.setattr(llm, "extract", extract)


def test_quote_rule_drops_an_invented_sentence(monkeypatch):
    fake_llm(monkeypatch, manufacturing=[
        "Kapoor Box Factory is a manufacturer of corrugated boxes since 1972.",
        "We operate 40 factories across India."])                      # not on the page
    q, _ = checks.extract(ctx(), PAGE_TEXT)
    assert q["manufacturing"] == ["Kapoor Box Factory is a manufacturer of corrugated boxes since 1972."]


def test_manufacturer_needs_two_signals_and_trader_fails(monkeypatch):
    two = {"manufacturing": ["a manufacturer of corrugated boxes", "Our plant at IMT Manesar"],
           "trading": [], "ply": [], "custom_sizes": [], "prices": [], "address": None}
    assert checks.manufacturer(ctx(), {"name": "K"}, page(), two)[0] == "pass"
    one = {**two, "manufacturing": two["manufacturing"][:1]}
    assert checks.manufacturer(ctx(), {"name": "K"}, page(), one)[0] == "cannot_verify"
    trader = {**two, "manufacturing": [], "trading": ["We are leading traders and distributors"]}
    assert checks.manufacturer(ctx(), {"name": "T"}, page(), trader)[0] == "fail"
    assert checks.manufacturer(ctx(), {"name": "X"}, None, two)[0] == "cannot_verify"  # no own site


def test_spec_match_pass_fail_and_unknown():
    q = {"ply": ["We make 3 ply, 5 ply and 7 ply boxes."], "custom_sizes": []}
    assert checks.spec_match(ctx(5), {}, page(), q)[0] == "pass"
    only3 = {"ply": ["We manufacture only 3 ply boxes."], "custom_sizes": []}
    assert checks.spec_match(ctx(5), {}, page(), only3)[0] == "fail"
    assert checks.spec_match(ctx(5), {}, page(), {"ply": [], "custom_sizes": []})[0] == "cannot_verify"


def test_real_active_needs_a_dated_record_and_a_live_site():
    cand = {"name": "K", "data_json": {"crustdata_updated_at": "2026-11-18T06:27:04Z",
                                       "crustdata_company_id": 1}}
    assert checks.real_active(ctx(), cand, page())[0] == "pass"
    stale = {"name": "K", "data_json": {"crustdata_updated_at": "2026-05-01T00:00:00Z"}}
    assert checks.real_active(ctx(), stale, page())[0] == "cannot_verify"
    assert checks.real_active(ctx(), cand, None)[0] == "cannot_verify"


def test_no_price_is_cannot_verify_and_fraud_never_guesses():
    assert checks.price(ctx(), {}, page(), {"prices": []})[0] == "cannot_verify"
    assert checks.fraud_risk(ctx(), {}, page())[0] == "cannot_verify"  # no GSTIN, no registry


def test_lookalike_names_are_flagged_not_merged():
    cands = [{"supplier_id": "sup_01", "name": "Gurgaon Packaging Ltd."},
             {"supplier_id": "sup_02", "name": "Gurgaon Packaging Pvt Ltd"},
             {"supplier_id": "sup_03", "name": "Kapoor Box Factory"}]
    looks = _lookalikes(cands)
    assert set(looks) == {"sup_01", "sup_02"}
    assert checks.duplicate(ctx(), cands[0], looks)[4] == ["lookalike_name"]
