"""Field-level business-rule validation — the auto-post safety net.

The confidence gate (:mod:`docextract.gate`) answers *"how sure is the parser?"*.
This module answers a different, complementary question: *"is the extracted
record internally consistent and complete enough to post?"* — the check an AP
clerk performs by eye before keying an invoice into the ERP.

It consumes an extraction ``result`` (and, optionally, the raw document text for
the format checks) and re-derives nothing: the arithmetic rules read the numbers
the pipeline already parsed, and the reconciliation rule reuses the engine's own
``totals.validated`` signal. The rules are:

===========================  ========  =======================================
rule                         severity  what it asserts
===========================  ========  =======================================
``required_fields``          error     the mandatory header fields for the
                                       document type were extracted (an invoice
                                       needs a number, a date, a currency and a
                                       monetary total)
``line_total_reconciles``    error     the summed line items match the stated
                                       subtotal/total (reuses ``validated``)
``totals_arithmetic``        error     ``subtotal + tax == total``
``line_item_math``           error     ``quantity * unit_price == amount`` per row
``date_order``               error     a due / requested date is not before the
                                       document date
``iban_checksum``            error     a labelled IBAN passes the ISO 13616
                                       mod-97 check
``vat_format``               warning   a labelled VAT id matches its country's
                                       format (a shape check, not a VIES lookup)
===========================  ========  =======================================

A rule reports ``pass`` / ``fail`` / ``skip`` (skip = not applicable, e.g. no
totals to reconcile). ``review_recommended`` is ``True`` when any *error*-severity
rule failed; warnings annotate but never force review.

The point this makes, which confidence cannot: a field the parser *silently
drops* lowers no confidence average, but ``required_fields`` turns that omission
into an explicit, gate-visible signal — see ``eval/run_eval.py`` for the measured
effect on the labelled set.

The evaluation is pure and deterministic (no clock, no RNG).

Example::

    from docextract import extract_document, validate_document

    result = extract_document(open("samples/invoice.txt").read())
    report = validate_document(result)
    assert report["ok"] is True            # a clean invoice passes every rule
    assert report["review_recommended"] is False
"""

from __future__ import annotations

import datetime
import re

# --- rule configuration -----------------------------------------------------

# Header fields that must be present (extracted) for each document type. An
# invoice additionally needs a monetary total, handled separately below.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "invoice": ("document_number", "document_date", "currency"),
    "delivery_note": ("document_number", "document_date"),
    "rfq": ("document_number",),
}
# Document types for which a stated subtotal or total is mandatory to post.
_TYPES_NEEDING_TOTAL = frozenset({"invoice"})

# Date fields that must not precede the document date, in report order.
_DATE_AFTER_DOCUMENT = ("due_date", "requested_delivery_date")

# A labelled IBAN: "IBAN: DE89 3704 0044 0532 0130 00" (spaces tolerated).
_IBAN_RE = re.compile(r"IBAN\s*[:#]?\s*([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){10,30})", re.I)

# A labelled VAT identifier — anchored so the "VAT (19%):" *tax amount* line is
# never mistaken for a VAT id (the captured value must start with a country code).
_VAT_RE = re.compile(
    r"(?:VAT(?:\s*(?:ID|No\.?|Number|Reg(?:\.?|istration)?))?|USt-?IdNr\.?|Tax\s*ID)"
    r"\s*[:#]?\s*([A-Z]{2}[A-Z0-9 ]{2,14})",
    re.I,
)

# Per-country EU VAT formats (country code + national part). A shape check only.
_VAT_FORMATS: dict[str, re.Pattern[str]] = {
    "AT": re.compile(r"^ATU\d{8}$"),
    "BE": re.compile(r"^BE0\d{9}$"),
    "CZ": re.compile(r"^CZ\d{8,10}$"),
    "DE": re.compile(r"^DE\d{9}$"),
    "DK": re.compile(r"^DK\d{8}$"),
    "ES": re.compile(r"^ES[A-Z0-9]\d{7}[A-Z0-9]$"),
    "FI": re.compile(r"^FI\d{8}$"),
    "FR": re.compile(r"^FR[A-Z0-9]{2}\d{9}$"),
    "IE": re.compile(r"^IE(?:\d{7}[A-W]{1,2}|\d[A-Z0-9+*]\d{5}[A-W])$"),
    "IT": re.compile(r"^IT\d{11}$"),
    "LU": re.compile(r"^LU\d{8}$"),
    "NL": re.compile(r"^NL\d{9}B\d{2}$"),
    "PL": re.compile(r"^PL\d{10}$"),
    "PT": re.compile(r"^PT\d{9}$"),
    "SE": re.compile(r"^SE\d{12}$"),
}


# --- small helpers ----------------------------------------------------------

def _is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _field_present(fields: dict[str, object], name: str) -> bool:
    return isinstance(fields.get(name), dict)


def _field_value(fields: dict[str, object], name: str) -> object:
    entry = fields.get(name)
    return entry.get("value") if isinstance(entry, dict) else None


def _total_value(totals: dict[str, object], key: str) -> float | None:
    entry = totals.get(key)
    if isinstance(entry, dict) and _is_num(entry.get("value")):
        return float(entry["value"])  # type: ignore[arg-type]
    return None


def _money(value: float) -> str:
    """Format a number for a deterministic, platform-independent message."""
    return f"{value:.2f}"


def _parse_iso(value: object) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def iban_is_valid(iban: str) -> bool:
    """Return ``True`` if ``iban`` passes the ISO 13616 mod-97 checksum.

    Spaces are ignored. The first four characters are moved to the end and each
    letter is replaced by its position value (``A`` = 10 … ``Z`` = 35); the
    resulting integer is valid iff it is congruent to 1 modulo 97.
    """
    compact = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        numeric = "".join(str(int(char, 36)) for char in rearranged)
    except ValueError:  # pragma: no cover - guarded by the regex above
        return False
    return int(numeric) % 97 == 1


def vat_country_format_ok(vat: str) -> bool | None:
    """Validate a VAT id's *shape* for its country.

    Returns ``True``/``False`` for the countries whose format is known, or
    ``None`` when the country code is not in the table (unknown → not judged,
    rather than a false warning).
    """
    compact = re.sub(r"\s+", "", vat).upper()
    pattern = _VAT_FORMATS.get(compact[:2])
    if pattern is None:
        return None
    return bool(pattern.fullmatch(compact))


# --- the validator ----------------------------------------------------------

def validate_document(result: dict[str, object], *, text: str = "") -> dict[str, object]:
    """Run the business rules over an extraction ``result``.

    Args:
        result: A result dict as returned by :func:`docextract.extract_document`.
        text: Optional raw document text; enables the IBAN and VAT checks, which
            read a labelled value straight from the source. When empty those two
            checks are skipped (they never apply to a bare result).

    Returns:
        A report dict with ``checks`` (an ordered list of
        ``{rule, scope, status, severity, message}``), the ``errors`` and
        ``warnings`` failure counts, ``checks_run`` / ``checks_skipped``, an
        ``ok`` flag (no error-severity failures) and ``review_recommended``
        (the inverse — errors route a document to a human even if the confidence
        gate would have passed it).
    """
    checks: list[dict[str, object]] = []

    def record(rule: str, scope: str, status: str, severity: str, message: str) -> None:
        checks.append(
            {"rule": rule, "scope": scope, "status": status,
             "severity": severity, "message": message}
        )

    doc_type = result.get("doc_type")
    fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    items = result.get("line_items") if isinstance(result.get("line_items"), list) else []
    assert isinstance(fields, dict) and isinstance(totals, dict) and isinstance(items, list)

    # 1. required_fields — mandatory header fields (and, for invoices, a total).
    required = _REQUIRED_FIELDS.get(str(doc_type))
    if required is None:
        record("required_fields", "document", "skip", "error",
                f"no required-field profile for document type '{doc_type}'")
    else:
        missing = [name for name in required if not _field_present(fields, name)]
        if str(doc_type) in _TYPES_NEEDING_TOTAL and not any(
            isinstance(totals.get(key), dict) for key in ("subtotal", "total")
        ):
            missing.append("monetary_total")
        if missing:
            record("required_fields", "document", "fail", "error",
                    f"missing mandatory field(s): {', '.join(missing)}")
        else:
            record("required_fields", "document", "pass", "error",
                    f"all mandatory {doc_type} fields present")

    # 2. line_total_reconciles — reuse the engine's own cross-validation signal.
    has_reference = any(isinstance(totals.get(key), dict) for key in ("subtotal", "total"))
    computed = totals.get("computed_line_total")
    if has_reference and _is_num(computed) and float(computed) != 0.0:  # type: ignore[arg-type]
        if totals.get("validated"):
            record("line_total_reconciles", "totals", "pass", "error",
                    f"line items sum to the stated total ({_money(float(computed))})")  # type: ignore[arg-type]
        else:
            record("line_total_reconciles", "totals", "fail", "error",
                    f"line items sum to {_money(float(computed))} but do not match the stated total")  # type: ignore[arg-type]
    else:
        record("line_total_reconciles", "totals", "skip", "error",
                "no stated total and line-item sum to reconcile")

    # 3. totals_arithmetic — subtotal + tax == total.
    subtotal = _total_value(totals, "subtotal")
    tax = _total_value(totals, "tax")
    total = _total_value(totals, "total")
    if subtotal is not None and tax is not None and total is not None:
        tolerance = max(0.02, abs(total) * 0.01)
        if abs(subtotal + tax - total) <= tolerance:
            record("totals_arithmetic", "totals", "pass", "error",
                    f"{_money(subtotal)} + {_money(tax)} = {_money(total)}")
        else:
            record("totals_arithmetic", "totals", "fail", "error",
                    f"{_money(subtotal)} + {_money(tax)} != stated total {_money(total)}")
    else:
        record("totals_arithmetic", "totals", "skip", "error",
                "need subtotal, tax and total to check the arithmetic")

    # 4. line_item_math — quantity * unit_price == amount, per row (in order).
    for item in items:
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        scope = f"line {position}" if position is not None else "line"
        qty, unit_price, amount = item.get("quantity"), item.get("unit_price"), item.get("amount")
        if _is_num(qty) and _is_num(unit_price) and _is_num(amount):
            expected = float(qty) * float(unit_price)  # type: ignore[arg-type]
            tolerance = max(0.02, abs(float(amount)) * 0.01)  # type: ignore[arg-type]
            if abs(expected - float(amount)) <= tolerance:  # type: ignore[arg-type]
                record("line_item_math", scope, "pass", "error",
                        f"{qty} x {_money(float(unit_price))} = {_money(float(amount))}")  # type: ignore[arg-type]
            else:
                record("line_item_math", scope, "fail", "error",
                        f"{qty} x {_money(float(unit_price))} = {_money(expected)}, "  # type: ignore[arg-type]
                        f"but amount is {_money(float(amount))}")  # type: ignore[arg-type]
        else:
            record("line_item_math", scope, "skip", "error",
                    "row lacks quantity, unit price and amount")

    # 5. date_order — a due / requested date must not precede the document date.
    document_date = _parse_iso(_field_value(fields, "document_date"))
    for later_name in _DATE_AFTER_DOCUMENT:
        if not _field_present(fields, later_name):
            continue
        later_date = _parse_iso(_field_value(fields, later_name))
        if document_date is None or later_date is None:
            record("date_order", later_name, "skip", "error",
                    "a compared date is missing or not ISO-formatted")
        elif later_date >= document_date:
            record("date_order", later_name, "pass", "error",
                    f"{later_name} {later_date} is on/after document date {document_date}")
        else:
            record("date_order", later_name, "fail", "error",
                    f"{later_name} {later_date} precedes document date {document_date}")

    # 6. iban_checksum — a labelled IBAN must pass ISO 13616 mod-97.
    iban_match = _IBAN_RE.search(text) if text else None
    if iban_match:
        raw_iban = iban_match.group(1).strip()
        compact = raw_iban.replace(" ", "").upper()
        if iban_is_valid(raw_iban):
            record("iban_checksum", "payment", "pass", "error",
                    f"IBAN {compact} passes the mod-97 checksum")
        else:
            record("iban_checksum", "payment", "fail", "error",
                    f"IBAN {compact} fails the mod-97 checksum")
    elif text:
        record("iban_checksum", "payment", "skip", "error", "no labelled IBAN found")

    # 7. vat_format — a labelled VAT id should match its country's shape.
    vat_match = _VAT_RE.search(text) if text else None
    if vat_match:
        raw_vat = vat_match.group(1).strip()
        compact = re.sub(r"\s+", "", raw_vat).upper()
        verdict = vat_country_format_ok(raw_vat)
        if verdict is None:
            record("vat_format", "party", "skip", "warning",
                    f"VAT id {compact}: country not in the format table")
        elif verdict:
            record("vat_format", "party", "pass", "warning",
                    f"VAT id {compact} matches the {compact[:2]} format")
        else:
            record("vat_format", "party", "fail", "warning",
                    f"VAT id {compact} does not match the {compact[:2]} format")
    elif text:
        record("vat_format", "party", "skip", "warning", "no labelled VAT id found")

    errors = sum(1 for c in checks if c["status"] == "fail" and c["severity"] == "error")
    warnings = sum(1 for c in checks if c["status"] == "fail" and c["severity"] == "warning")
    run = sum(1 for c in checks if c["status"] != "skip")
    skipped = sum(1 for c in checks if c["status"] == "skip")
    return {
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "checks_run": run,
        "checks_skipped": skipped,
        "ok": errors == 0,
        "review_recommended": errors > 0,
    }
