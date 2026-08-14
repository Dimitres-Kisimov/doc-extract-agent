"""Provenance tests: every span must point at the characters the value came from.

The rule these tests enforce is the one the UI depends on when it highlights a
field in the original document: a span is either *exactly* the source slice it
claims (``text[start:end] == span["text"]``, at the stated line/column) or it is
``None``. There is no third state where a value points at approximately the
right place.
"""

from __future__ import annotations

import pathlib

import pytest

from docextract import extract_document, spans

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES_DIR = ROOT / "samples"
DATASET_DIR = ROOT / "eval" / "dataset"

SPAN_KEYS = {"start", "end", "line", "col", "text"}


def _load(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_spans(result: dict) -> list[tuple[str, dict | None]]:
    """Every (label, span) slot in a result — including the ``None`` ones."""
    found: list[tuple[str, dict | None]] = []
    for name, field in result["fields"].items():
        found.append((f"field {name}", field["span"]))
    for item in result["line_items"]:
        for column, span in item["spans"].items():
            found.append((f"line {item['position']}.{column}", span))
    for key in ("subtotal", "tax", "total"):
        entry = result["totals"].get(key)
        if isinstance(entry, dict):
            found.append((f"total {key}", entry["span"]))
    return found


def _assert_span_is_real(label: str, span: dict, text: str) -> None:
    assert set(span) == SPAN_KEYS, f"{label}: unexpected span keys {sorted(span)}"
    assert 0 <= span["start"] < span["end"] <= len(text), f"{label}: offsets out of range"
    # The slice the span claims is the slice the document actually holds.
    assert text[span["start"]:span["end"]] == span["text"], f"{label}: span text mismatch"
    # ...and line/col address that same slice.
    line = text.splitlines()[span["line"] - 1]
    assert line[span["col"] - 1:span["col"] - 1 + len(span["text"])] == span["text"], (
        f"{label}: line/col does not address the span"
    )


ALL_DOCUMENTS = sorted(SAMPLES_DIR.glob("*.txt")) + sorted(DATASET_DIR.glob("*.txt"))


# --- the invariant, over every committed document ---------------------------

@pytest.mark.parametrize("path", ALL_DOCUMENTS, ids=lambda p: p.stem)
def test_every_span_addresses_its_own_source_text(path: pathlib.Path) -> None:
    text = _load(path)
    result = extract_document(text)
    checked = 0
    for label, span in _all_spans(result):
        if span is None:
            continue
        _assert_span_is_real(label, span, text)
        checked += 1
    assert checked, f"{path.name}: extracted values but located none of them"


@pytest.mark.parametrize("path", ALL_DOCUMENTS, ids=lambda p: p.stem)
def test_header_fields_and_stated_totals_are_always_located(path: pathlib.Path) -> None:
    # Header fields and stated totals are read off the page, never computed:
    # a missing span there would mean the provenance layer lost track.
    result = extract_document(_load(path))
    for name, field in result["fields"].items():
        assert field["span"] is not None, f"{path.name}: field {name} has no span"
    for key in ("subtotal", "tax", "total"):
        entry = result["totals"].get(key)
        if isinstance(entry, dict):
            assert entry["span"] is not None, f"{path.name}: total {key} has no span"


@pytest.mark.parametrize("path", ALL_DOCUMENTS, ids=lambda p: p.stem)
def test_derived_values_carry_no_span(path: pathlib.Path) -> None:
    result = extract_document(_load(path))
    for item in result["line_items"]:
        for name in item["derived"]:
            assert item[name] is not None, "a derived name must have a value"
            assert item["spans"][name] is None, (
                f"{path.name}: line {item['position']} lists {name} as derived but gave it a span"
            )
    assert result["totals"]["derived"] == ["computed_line_total"]


# --- exact placement on the shipped samples ---------------------------------

@pytest.fixture
def invoice_text() -> str:
    return _load(SAMPLES_DIR / "invoice.txt")


@pytest.fixture
def rfq_text() -> str:
    return _load(SAMPLES_DIR / "rfq_email.txt")


def test_document_number_span_is_the_identifier_not_its_label(invoice_text: str) -> None:
    span = extract_document(invoice_text)["fields"]["document_number"]["span"]
    assert span["text"] == "INV-2026-8842"
    assert span["start"] == invoice_text.index("INV-2026-8842")
    assert (span["line"], span["col"]) == (12, 17)  # "Invoice Number: " is 16 chars


def test_buyer_span_is_the_line_after_the_bill_to_label(invoice_text: str) -> None:
    span = extract_document(invoice_text)["fields"]["buyer"]["span"]
    assert span["text"] == "Acme Industrial GmbH"
    assert span["line"] == 8 and span["col"] == 1
    assert invoice_text.splitlines()[span["line"] - 2] == "Bill To:"


def test_seller_span_is_the_issuer_line(invoice_text: str) -> None:
    span = extract_document(invoice_text)["fields"]["seller"]["span"]
    assert (span["text"], span["line"]) == ("Example Supplier Handels GmbH", 3)


def test_line_item_columns_point_at_their_own_cells(invoice_text: str) -> None:
    first = extract_document(invoice_text)["line_items"][0]
    located = {name: span["text"] for name, span in first["spans"].items() if span}
    assert located == {
        "position": "1",
        "description": "Hex bolt M8x40 DIN 933",
        "quantity": "500",
        "unit_price": "0.12",
        "amount": "60.00",
    }
    # All five sit on the same source row.
    assert {first["spans"][name]["line"] for name in located} == {19}
    assert first["spans"]["unit"] is None  # this invoice has no unit column


def test_rfq_unit_column_is_located(rfq_text: str) -> None:
    first = extract_document(rfq_text)["line_items"][0]
    assert first["spans"]["unit"]["text"] == "pcs"
    assert first["spans"]["amount"] is None  # an RFQ prices nothing


def test_totals_spans_are_the_stated_figures(invoice_text: str) -> None:
    totals = extract_document(invoice_text)["totals"]
    assert [totals[key]["span"]["text"] for key in ("subtotal", "tax", "total")] == [
        "105.00",
        "19.95",  # not the "19" of "VAT (19%)"
        "124.95",
    ]
    assert totals["computed_line_total"] == 105.0  # summed, and named as derived


# --- the honest edges -------------------------------------------------------

def test_back_calculated_unit_price_is_derived_not_located() -> None:
    # A row that states qty and a price but no amount: the engine reads two
    # numbers, so the unit price it reports is arithmetic, not a source value.
    text = _load(DATASET_DIR / "inv13_no_amount_column.txt")
    first = extract_document(text)["line_items"][0]
    assert first["derived"] == ["unit_price"]
    assert first["unit_price"] is not None
    assert first["spans"]["unit_price"] is None
    assert first["spans"]["amount"]["text"] == "0.12"  # what it did read


def test_inferred_currency_span_is_the_symbol_it_was_inferred_from() -> None:
    # No "Currency:" label — the value is inferred from the symbol, so the span
    # is the symbol and span["text"] != value. That difference is the point.
    result = extract_document("INVOICE\nInvoice Number: INV-1\nTotal: € 10.00")
    currency = result["fields"]["currency"]
    assert currency["value"] == "EUR"
    assert currency["span"]["text"] == "€"


def test_european_number_span_keeps_the_source_formatting() -> None:
    text = "INVOICE\nInvoice Number: INV-1\nCurrency: EUR\nTotal: 1.234,56"
    total = extract_document(text)["totals"]["total"]
    assert total["value"] == 1234.56
    assert total["span"]["text"] == "1.234,56"


def test_offsets_are_code_points_so_astral_characters_do_not_shift_them() -> None:
    # Emoji are two UTF-16 code units but one code point; Python slices by code
    # point, and the UI is documented to index the same way.
    text = "INVOICE 🚚🚚\nInvoice Number: INV-42\n"
    span = extract_document(text)["fields"]["document_number"]["span"]
    assert text[span["start"]:span["end"]] == "INV-42"
    assert (span["line"], span["col"]) == (2, 17)


@pytest.mark.parametrize(
    "text",
    ["", "   \n\t ", "!@#$%^&*()", "\x00\x01 bytes", "🙂 emoji only 🚚", "1234567890" * 40],
)
def test_garbage_never_produces_a_broken_span(text: str) -> None:
    result = extract_document(text)
    for label, span in _all_spans(result):
        if span is not None:
            _assert_span_is_real(label, span, text)


def test_crlf_documents_are_addressed_correctly() -> None:
    text = "INVOICE\r\n\r\nInvoice Number: INV-7\r\nCurrency: EUR\r\n"
    span = extract_document(text)["fields"]["document_number"]["span"]
    assert text[span["start"]:span["end"]] == "INV-7"
    assert span["line"] == 3


# --- the span helpers themselves --------------------------------------------

def test_line_starts_indexes_every_line() -> None:
    text = "alpha\nbeta\r\ngamma"
    starts = spans.line_starts(text)
    assert [text[start:start + 5] for start in starts] == ["alpha", "beta\r", "gamma"]
    assert len(starts) == len(text.splitlines())


def test_line_starts_of_empty_text_is_a_single_origin() -> None:
    assert spans.line_starts("") == [0]


@pytest.mark.parametrize(
    ("start", "end"),
    [(5, 5), (4, 2), (-1, 3), (0, 99)],
)
def test_make_span_refuses_ranges_it_cannot_honour(start: int, end: int) -> None:
    assert spans.make_span("some text", start, end) is None


def test_trim_strips_the_whitespace_a_value_would_have_stripped() -> None:
    text = "  padded  "
    assert spans.trim(text, 0, len(text)) == (2, 8)
    assert text[2:8] == "padded"
