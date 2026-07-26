"""Tests for the extraction engine against the three shipped samples."""

from __future__ import annotations

import pathlib

import pytest

from docextract import extract_document

SAMPLES_DIR = pathlib.Path(__file__).resolve().parent.parent / "samples"


def _load(name: str) -> str:
    return (SAMPLES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def rfq() -> dict:
    return extract_document(_load("rfq_email.txt"))


@pytest.fixture
def invoice() -> dict:
    return extract_document(_load("invoice.txt"))


@pytest.fixture
def delivery() -> dict:
    return extract_document(_load("delivery_note.txt"))


# --- document type detection ------------------------------------------------

def test_rfq_doc_type(rfq: dict) -> None:
    assert rfq["doc_type"] == "rfq"


def test_invoice_doc_type(invoice: dict) -> None:
    assert invoice["doc_type"] == "invoice"


def test_delivery_doc_type(delivery: dict) -> None:
    assert delivery["doc_type"] == "delivery_note"


# --- key header fields ------------------------------------------------------

def test_invoice_key_fields(invoice: dict) -> None:
    fields = invoice["fields"]
    assert fields["document_number"]["value"] == "INV-2026-8842"
    assert fields["document_date"]["value"] == "2026-07-18"
    assert fields["due_date"]["value"] == "2026-08-17"
    assert fields["currency"]["value"] == "EUR"
    assert fields["buyer"]["value"].startswith("Acme Industrial")


def test_delivery_reference_field(delivery: dict) -> None:
    assert delivery["fields"]["document_number"]["value"] == "DN-2026-3310"
    assert delivery["fields"]["order_reference"]["value"] == "RFQ-2026-0417"


def test_rfq_currency_and_number(rfq: dict) -> None:
    assert rfq["fields"]["currency"]["value"] == "EUR"
    assert rfq["fields"]["document_number"]["value"] == "RFQ-2026-0417"


# --- parties (regression: the RFQ salutation must never become the seller) --

def test_rfq_buyer_is_requesting_company(rfq: dict) -> None:
    # "Deliver to: Acme Industrial GmbH, ..." carries the value inline on the
    # label line — it must still be picked up as the buyer.
    assert rfq["fields"]["buyer"]["value"].startswith("Acme Industrial GmbH")


def test_rfq_seller_is_not_the_salutation(rfq: dict) -> None:
    # The email body holds no issuing company name near the top; better to
    # report no seller than "Dear Sales Team,".
    assert "seller" not in rfq["fields"]


def test_invoice_and_delivery_seller(invoice: dict, delivery: dict) -> None:
    assert invoice["fields"]["seller"]["value"] == "Example Supplier Handels GmbH"
    assert delivery["fields"]["seller"]["value"] == "Example Supplier Handels GmbH"


# --- line item counts -------------------------------------------------------

@pytest.mark.parametrize("fixture_name", ["rfq", "invoice", "delivery"])
def test_three_line_items(fixture_name: str, request: pytest.FixtureRequest) -> None:
    result = request.getfixturevalue(fixture_name)
    assert len(result["line_items"]) == 3


def test_invoice_line_item_values(invoice: dict) -> None:
    first = invoice["line_items"][0]
    assert first["position"] == 1
    assert first["quantity"] == 500
    assert first["unit_price"] == 0.12
    assert first["amount"] == 60.0
    assert "Hex bolt" in first["description"]


def test_rfq_line_item_has_unit_no_price(rfq: dict) -> None:
    item = rfq["line_items"][0]
    assert item["unit"] == "pcs"
    assert item["quantity"] == 500
    assert item["amount"] is None  # RFQ has no prices


# --- totals validation ------------------------------------------------------

def test_invoice_totals_validate(invoice: dict) -> None:
    totals = invoice["totals"]
    assert totals["subtotal"]["value"] == 105.0
    assert totals["tax"]["value"] == 19.95  # not the "19" from "VAT (19%)"
    assert totals["total"]["value"] == 124.95
    assert totals["computed_line_total"] == 105.0
    assert totals["validated"] is True
    # Cross-validated figure gets a confidence boost.
    assert totals["subtotal"]["confidence"] >= 0.98


def test_delivery_has_no_monetary_totals(delivery: dict) -> None:
    # A delivery note lists quantities but no priced totals to validate.
    assert delivery["totals"]["validated"] is False


# --- confidence bounds ------------------------------------------------------

@pytest.mark.parametrize("fixture_name", ["rfq", "invoice", "delivery"])
def test_confidence_in_range(fixture_name: str, request: pytest.FixtureRequest) -> None:
    result = request.getfixturevalue(fixture_name)
    assert 0.0 <= result["confidence"] <= 1.0
    for field in result["fields"].values():
        assert 0.0 <= field["confidence"] <= 1.0
    for item in result["line_items"]:
        assert 0.0 <= item["confidence"] <= 1.0
