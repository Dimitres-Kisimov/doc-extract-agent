"""Tests for the field-level business-rule validation layer.

Expectations are hand-computed. Covers each rule's pass / fail / skip paths, the
severity split (a warning never forces review), IBAN mod-97 vectors, the
VAT-vs-tax-line disambiguation, determinism, the collapses-to-base-case check
(a clean invoice passes every rule) and robustness on garbage input.
"""

from __future__ import annotations

import pytest

from docextract import extract_document, validate_document
from docextract.validation import iban_is_valid, vat_country_format_ok


def _clean_invoice() -> dict:
    """A fully-specified, internally consistent invoice result (the base case)."""
    return {
        "doc_type": "invoice",
        "confidence": 0.95,
        "fields": {
            "document_number": {"value": "INV-1", "confidence": 0.95},
            "document_date": {"value": "2026-07-18", "confidence": 0.93},
            "due_date": {"value": "2026-08-17", "confidence": 0.93},
            "currency": {"value": "EUR", "confidence": 0.95},
        },
        "line_items": [
            {"position": 1, "quantity": 2, "unit": None, "unit_price": 1.5, "amount": 3.0},
            {"position": 2, "quantity": 1, "unit": None, "unit_price": 4.0, "amount": 4.0},
        ],
        "totals": {
            "subtotal": {"value": 7.0, "confidence": 0.99},
            "tax": {"value": 1.33, "confidence": 0.9},
            "total": {"value": 8.33, "confidence": 0.9},
            "computed_line_total": 7.0,
            "validated": True,
        },
    }


def _check(report: dict, rule: str, scope: str | None = None) -> dict | None:
    for check in report["checks"]:
        if check["rule"] == rule and (scope is None or check["scope"] == scope):
            return check
    return None


# --- base case (collapses to all-pass) --------------------------------------

def test_clean_invoice_passes_every_rule() -> None:
    report = validate_document(_clean_invoice())
    assert report["ok"] is True
    assert report["review_recommended"] is False
    assert report["errors"] == 0
    assert report["warnings"] == 0
    # Every applicable rule passed; no rule failed.
    assert all(c["status"] in {"pass", "skip"} for c in report["checks"])
    assert _check(report, "required_fields")["status"] == "pass"
    assert _check(report, "totals_arithmetic")["status"] == "pass"
    assert _check(report, "line_total_reconciles")["status"] == "pass"
    assert _check(report, "date_order", "due_date")["status"] == "pass"


def test_report_counts_reconcile() -> None:
    report = validate_document(_clean_invoice())
    assert report["checks_run"] + report["checks_skipped"] == len(report["checks"])
    assert report["checks_run"] == sum(c["status"] != "skip" for c in report["checks"])
    assert report["errors"] == sum(
        c["status"] == "fail" and c["severity"] == "error" for c in report["checks"]
    )


# --- line_item_math ---------------------------------------------------------

def test_line_item_math_flags_mismatch() -> None:
    result = _clean_invoice()
    result["line_items"][0]["amount"] = 5.0  # 2 x 1.5 = 3.0, not 5.0
    report = validate_document(result)
    check = _check(report, "line_item_math", "line 1")
    assert check["status"] == "fail"
    assert report["review_recommended"] is True
    assert report["errors"] == 1
    # The second, consistent row still passes.
    assert _check(report, "line_item_math", "line 2")["status"] == "pass"


def test_line_item_math_tolerates_rounding() -> None:
    # A unit price rounded to 4 dp: 3 x 3.3333 = 9.9999 vs a stated 10.00.
    result = _clean_invoice()
    result["line_items"] = [
        {"position": 1, "quantity": 3, "unit": None, "unit_price": 3.3333, "amount": 10.0}
    ]
    result["totals"]["computed_line_total"] = 10.0
    assert _check(validate_document(result), "line_item_math", "line 1")["status"] == "pass"


def test_line_item_math_skips_rows_without_all_numbers() -> None:
    result = _clean_invoice()
    result["line_items"] = [
        {"position": 1, "quantity": 500, "unit": "pcs", "unit_price": None, "amount": None}
    ]
    assert _check(validate_document(result), "line_item_math", "line 1")["status"] == "skip"


# --- totals_arithmetic ------------------------------------------------------

def test_totals_arithmetic_flags_mismatch() -> None:
    result = _clean_invoice()
    result["totals"]["total"] = {"value": 9.0, "confidence": 0.9}  # 7.0 + 1.33 != 9.0
    report = validate_document(result)
    assert _check(report, "totals_arithmetic")["status"] == "fail"
    assert report["review_recommended"] is True


def test_totals_arithmetic_skipped_when_incomplete() -> None:
    result = _clean_invoice()
    del result["totals"]["tax"]
    assert _check(validate_document(result), "totals_arithmetic")["status"] == "skip"


# --- line_total_reconciles (reuses the engine's validated flag) -------------

def test_line_total_reconcile_follows_validated_flag() -> None:
    result = _clean_invoice()
    result["totals"]["validated"] = False
    report = validate_document(result)
    assert _check(report, "line_total_reconciles")["status"] == "fail"
    assert report["review_recommended"] is True


def test_line_total_reconcile_skips_without_reference_total() -> None:
    result = _clean_invoice()
    result["totals"] = {"validated": False, "computed_line_total": 0.0}
    assert _check(validate_document(result), "line_total_reconciles")["status"] == "skip"


# --- required_fields --------------------------------------------------------

def test_required_fields_flags_silently_dropped_date() -> None:
    result = _clean_invoice()
    del result["fields"]["document_date"]  # the silent-omission scenario
    report = validate_document(result)
    check = _check(report, "required_fields")
    assert check["status"] == "fail"
    assert "document_date" in check["message"]
    assert report["review_recommended"] is True


def test_required_fields_flags_missing_monetary_total() -> None:
    result = _clean_invoice()
    result["totals"] = {"validated": False, "computed_line_total": 0.0}
    check = _check(validate_document(result), "required_fields")
    assert check["status"] == "fail"
    assert "monetary_total" in check["message"]


def test_required_fields_profiles_by_doc_type() -> None:
    # An RFQ needs only a document number; a bare one passes.
    rfq = {
        "doc_type": "rfq",
        "fields": {"document_number": {"value": "RFQ-9", "confidence": 0.9}},
        "line_items": [],
        "totals": {"validated": False, "computed_line_total": 0.0},
    }
    assert _check(validate_document(rfq), "required_fields")["status"] == "pass"
    # A delivery note additionally needs a date.
    dn = {
        "doc_type": "delivery_note",
        "fields": {"document_number": {"value": "DN-9", "confidence": 0.9}},
        "line_items": [],
        "totals": {"validated": False, "computed_line_total": 0.0},
    }
    check = _check(validate_document(dn), "required_fields")
    assert check["status"] == "fail"
    assert "document_date" in check["message"]


def test_required_fields_skips_unknown_type() -> None:
    result = {"doc_type": "unknown", "fields": {}, "line_items": [],
              "totals": {"validated": False, "computed_line_total": 0.0}}
    assert _check(validate_document(result), "required_fields")["status"] == "skip"


# --- date_order -------------------------------------------------------------

def test_date_order_flags_due_before_document_date() -> None:
    result = _clean_invoice()
    result["fields"]["due_date"] = {"value": "2026-07-17", "confidence": 0.93}  # before 07-18
    report = validate_document(result)
    assert _check(report, "date_order", "due_date")["status"] == "fail"
    assert report["review_recommended"] is True


def test_date_order_skips_non_iso_date() -> None:
    result = _clean_invoice()
    result["fields"]["due_date"] = {"value": "17.08.2026", "confidence": 0.93}
    assert _check(validate_document(result), "date_order", "due_date")["status"] == "skip"


# --- iban_checksum ----------------------------------------------------------

@pytest.mark.parametrize(
    "iban",
    ["DE89370400440532013000", "GB82WEST12345698765432",
     "FR1420041010050500013M02606", "NL91ABNA0417164300"],
)
def test_iban_is_valid_accepts_known_good(iban: str) -> None:
    assert iban_is_valid(iban) is True


def test_iban_is_valid_rejects_corruption_and_junk() -> None:
    assert iban_is_valid("DE89370400440532013001") is False  # one digit changed
    assert iban_is_valid("DE89") is False
    assert iban_is_valid("not an iban") is False


def test_iban_checksum_check_over_text() -> None:
    result = _clean_invoice()
    ok = validate_document(result, text="Remit to IBAN: DE89 3704 0044 0532 0130 00")
    assert _check(ok, "iban_checksum")["status"] == "pass"
    bad = validate_document(result, text="Remit to IBAN: DE89 3704 0044 0532 0130 01")
    assert _check(bad, "iban_checksum")["status"] == "fail"
    assert bad["review_recommended"] is True
    # No text -> the check is not applicable at all.
    assert _check(validate_document(result), "iban_checksum") is None


# --- vat_format -------------------------------------------------------------

def test_vat_format_known_country() -> None:
    assert vat_country_format_ok("DE123456789") is True
    assert vat_country_format_ok("DE12345") is False
    assert vat_country_format_ok("XY123456789") is None  # unknown country: not judged


def test_vat_format_is_a_warning_not_a_review_trigger() -> None:
    # A malformed VAT id fails as a *warning*; an otherwise-clean doc still posts.
    report = validate_document(_clean_invoice(), text="VAT ID: DE12345")
    assert _check(report, "vat_format")["status"] == "fail"
    assert report["warnings"] == 1
    assert report["errors"] == 0
    assert report["review_recommended"] is False  # warnings never force review


def test_vat_label_not_confused_with_tax_amount_line() -> None:
    # "VAT (19%): 19.95" is a tax amount, not a VAT id — it must not be validated.
    report = validate_document(_clean_invoice(), text="VAT (19%): 19.95")
    check = _check(report, "vat_format")
    assert check["status"] == "skip"
    assert "no labelled VAT id" in check["message"]


# --- determinism and robustness ---------------------------------------------

def test_validation_is_deterministic() -> None:
    result = _clean_invoice()
    assert validate_document(result) == validate_document(result)


def test_validation_never_crashes_on_garbage() -> None:
    assert validate_document({}) is not None
    for text in ["", "!@#$%^&*", "🙂 emoji", "12  \n 34  Total: nope"]:
        report = validate_document(extract_document(text))
        assert set(report) >= {"checks", "errors", "warnings", "ok", "review_recommended"}
        assert report["errors"] >= 0


def test_pipeline_attaches_validation_report() -> None:
    result = extract_document(
        "INVOICE\nInvoice Number: INV-1\nInvoice Date: 2026-07-01\nCurrency: EUR\n"
        "1  Widget  2  3.00  6.00\nSubtotal: 6.00\nVAT (19%): 1.14\nTotal: 7.14"
    )
    assert "validation" in result
    assert result["validation"]["ok"] is True
    # And a validate stage event is in the trace.
    assert any(event["stage"] == "validate" for event in result["trace"])
