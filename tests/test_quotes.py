"""Reply parsing: read the quoted terms deterministically, never guess a price."""

from engine.quotes import own_text, parse_email, parse_quote


def test_rupee_sign_survives_email_parsing():
    """Regression: parsing a str mangled ₹ into the text '\\u20b9', hiding the price."""
    raw = ("From: seller@example.invalid\nMessage-ID: <r1@example.invalid>\n"
           "Content-Type: text/plain; charset=utf-8\n\nPrice: ₹39.50/box (excluding GST)\n")
    p = parse_email(raw)
    assert "₹39.50" in p["body"]
    assert parse_quote(p["body"])["price_inr_per_unit"] == 39.50


def test_reads_price_gst_moq_lead_time_and_ply():
    q = parse_quote("Hi,\nOur price is Rs. 37.20 per box ex-GST for 5 ply.\n"
                    "MOQ: 10,000 boxes\nLead time: 2 weeks\n")
    assert q["price_inr_per_unit"] == 37.20 and q["gst_basis"] == "ex-GST"
    assert q["moq"] == 10000 and q["lead_time_days"] == 14 and q["ply"] == 5
    assert q["source_quote"] == "Our price is Rs. 37.20 per box ex-GST for 5 ply."
    assert q["parsed_ok"] is True


def test_rupee_symbol_and_inclusive_gst():
    q = parse_quote("Price: ₹44/box inclusive of GST")
    assert q["price_inr_per_unit"] == 44.0 and q["gst_basis"] == "incl-GST"


def test_quoted_rfq_text_is_ignored():
    body = ("Sorry, we only make 3 ply.\n\nOn Sun, 22 Nov 2026, BoxSource wrote:\n"
            "> Price per box in INR, stating whether it is ex-GST or including GST\n")
    assert "ex-GST" not in own_text(body)
    q = parse_quote(body)
    assert q["parsed_ok"] is False and q["price_inr_per_unit"] is None
    assert q["gst_basis"] == "unknown" and q["ply"] == 3


def test_no_price_means_not_parsed_not_guessed():
    q = parse_quote("We will send our best rate tomorrow. MOQ 5000.")
    assert q["parsed_ok"] is False and q["price_inr_per_unit"] is None and q["moq"] == 5000
