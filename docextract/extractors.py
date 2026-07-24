"""Deterministic parsing heuristics — the built-in "agent".

These functions are the reference implementation the pipeline uses when no LLM
provider is available. They are pure and side-effect free so they are easy to
unit test.

The parsing strategy is column-aware: whitespace-aligned tables are split on
runs of two-or-more spaces, which keeps multi-word descriptions intact while
isolating numeric columns.

Example::

    from docextract.extractors import detect_doc_type, parse_line_items

    detect_doc_type("INVOICE\\n...")        # ("invoice", 0.97)
    parse_line_items(invoice_text)          # [{"position": 1, ...}, ...]
"""

from __future__ import annotations

import re

# Units we recognise so they are not mistaken for a description or a number.
_UNITS = {"pcs", "pc", "kg", "g", "box", "boxes", "m", "mm", "set", "sets", "unit", "units", "l", "ea"}

# A token is "numeric" if it is made of digits, dots, commas (thousands/decimal).
_NUMERIC_RE = re.compile(r"^[+-]?[\d][\d.,]*$")

# A candidate line-item row: starts with an integer position, then 2+ spaces.
_ROW_RE = re.compile(r"^\s*(\d{1,3})\s{2,}(\S.*)$")


def _to_float(token: str) -> float | None:
    """Parse a European/US formatted number, or return ``None``.

    Handles ``1000``, ``1.234,56`` (EU) and ``1,234.56`` (US) heuristically.
    """
    if not _NUMERIC_RE.match(token):
        return None
    t = token
    if "," in t and "." in t:
        # The rightmost separator is the decimal separator.
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")  # EU: 1.234,56
        else:
            t = t.replace(",", "")  # US: 1,234.56
    elif "," in t:
        # Ambiguous single comma; treat as decimal if it looks like one.
        t = t.replace(",", ".") if re.match(r"^\d{1,3},\d{1,2}$", t) else t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def detect_doc_type(text: str) -> tuple[str, float]:
    """Classify the document. Returns ``(doc_type, confidence)``.

    ``doc_type`` is one of ``invoice``, ``delivery_note``, ``rfq`` or ``unknown``.
    """
    upper = text.upper()
    # Strong signals first (labelled identifiers), then weaker keyword signals.
    if "DELIVERY NOTE" in upper or "DELIVERY NOTE NO" in upper or re.search(r"\bDN-\d", upper):
        return "delivery_note", 0.97
    if "INVOICE" in upper or "INVOICE NUMBER" in upper or re.search(r"\bINV-\d", upper):
        return "invoice", 0.97
    if "REQUEST FOR QUOTATION" in upper or "RFQ" in upper or "QUOTATION" in upper:
        return "rfq", 0.95
    return "unknown", 0.3


# Ordered label patterns: (canonical_field, regex, confidence).
_FIELD_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("document_number", re.compile(r"(?:Invoice Number|Delivery Note No)\s*[:#]?\s*([A-Z]{2,4}-\d[\w-]*)", re.I), 0.95),
    # Fallback: a bare RFQ/INV/DN identifier anywhere (e.g. in an email subject).
    ("document_number", re.compile(r"\b((?:RFQ|INV|DN)-\d[\w-]*)", re.I), 0.9),
    ("document_date", re.compile(r"(?:Invoice Date|Delivery Date|Date)\s*:\s*(\d{4}-\d{2}-\d{2})", re.I), 0.93),
    ("due_date", re.compile(r"Due Date\s*:\s*(\d{4}-\d{2}-\d{2})", re.I), 0.93),
    ("requested_delivery_date", re.compile(r"Requested delivery(?: date)?\s*:\s*(\d{4}-\d{2}-\d{2})", re.I), 0.9),
    ("order_reference", re.compile(r"Order Reference\s*:\s*([A-Z]{2,4}-?\d[\w-]*)", re.I), 0.9),
    ("currency", re.compile(r"Currency\s*:\s*([A-Z]{3})", re.I), 0.95),
]


def extract_header_fields(text: str) -> dict[str, dict[str, object]]:
    """Extract labelled header fields as ``{field: {value, confidence}}``."""
    fields: dict[str, dict[str, object]] = {}
    for name, pattern, conf in _FIELD_PATTERNS:
        if name in fields:
            continue
        match = pattern.search(text)
        if match:
            fields[name] = {"value": match.group(1).strip(), "confidence": conf}

    # Fallback currency detection via symbols if not labelled.
    if "currency" not in fields:
        if "€" in text or "EUR" in text:
            fields["currency"] = {"value": "EUR", "confidence": 0.6}
        elif "$" in text or "USD" in text:
            fields["currency"] = {"value": "USD", "confidence": 0.6}

    # Parties: the block after a "Bill To" / "Deliver To" label is the buyer.
    buyer = _extract_party(text, r"(?:Bill To|Deliver To|Deliver to|Ship To)")
    if buyer:
        fields["buyer"] = {"value": buyer, "confidence": 0.85}

    # The first non-empty, non-heading line names the issuing party (seller).
    seller = _extract_seller(text)
    if seller:
        fields["seller"] = {"value": seller, "confidence": 0.75}

    return fields


def _extract_party(text: str, label_regex: str) -> str | None:
    """Return the first line following a party label block."""
    lines = text.splitlines()
    label = re.compile(label_regex + r"\s*:?\s*$", re.I)
    for i, line in enumerate(lines):
        if label.match(line.strip()):
            for follow in lines[i + 1:]:
                if follow.strip():
                    return follow.strip()
    return None


_HEADINGS = {"INVOICE", "DELIVERY NOTE", "REQUEST FOR QUOTATION", "QUOTATION"}


def _extract_seller(text: str) -> str | None:
    """Heuristically identify the issuing company (first real line)."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper() in _HEADINGS:
            continue
        if stripped.lower().startswith(("from:", "to:", "subject:", "date:")):
            continue
        return stripped
    return None


def parse_line_items(text: str) -> list[dict[str, object]]:
    """Parse tabular line items.

    Each row must start with an integer position followed by two-or-more
    spaces. Columns are split on runs of whitespace so multi-word descriptions
    stay intact. Numeric columns are interpreted as (quantity[, unit_price,
    amount]) depending on how many numbers the row contains.
    """
    items: list[dict[str, object]] = []
    for line in text.splitlines():
        row = _ROW_RE.match(line)
        if not row:
            continue
        position = int(row.group(1))
        tokens = re.split(r"\s{2,}", row.group(2).strip())

        desc_parts: list[str] = []
        numbers: list[float] = []
        unit: str | None = None
        for token in tokens:
            value = _to_float(token)
            if value is not None:
                numbers.append(value)
            elif token.lower() in _UNITS:
                unit = token
            else:
                desc_parts.append(token)

        description = " ".join(desc_parts).strip()
        # A real item needs a description and at least a quantity.
        if not description or not any(c.isalpha() for c in description) or not numbers:
            continue

        item: dict[str, object] = {
            "position": position,
            "description": description,
            "quantity": numbers[0],
            "unit": unit,
            "unit_price": None,
            "amount": None,
        }
        if len(numbers) >= 3:
            item["unit_price"] = numbers[1]
            item["amount"] = numbers[2]
        elif len(numbers) == 2:
            item["amount"] = numbers[1]
            if numbers[0]:
                item["unit_price"] = round(numbers[1] / numbers[0], 4)

        # Per-item confidence: highest when we have qty + price + amount.
        item["confidence"] = 0.95 if len(numbers) >= 3 else (0.85 if len(numbers) == 2 else 0.8)
        items.append(item)
    return items


_TOTAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("subtotal", re.compile(r"Subtotal\s*:?\s*([\d.,]+)", re.I)),
    # Require a colon before the amount so "(19%)" in "VAT (19%): 19.95" is skipped.
    ("tax", re.compile(r"(?:VAT|Tax)[^:\n]*:\s*([\d.,]+)", re.I)),
    ("total", re.compile(r"(?<!Sub)(?<!sub)\bTotal\s*:?\s*([\d.,]+)", re.I)),
]


def extract_totals(
    text: str, line_items: list[dict[str, object]]
) -> dict[str, object]:
    """Extract stated totals and validate them against the line items.

    Returns a dict with ``subtotal`` / ``tax`` / ``total`` (each
    ``{value, confidence}`` when present), a ``computed_line_total`` derived from
    the line items, and a ``validated`` flag.
    """
    totals: dict[str, object] = {}
    for name, pattern in _TOTAL_PATTERNS:
        match = pattern.search(text)
        if match:
            value = _to_float(match.group(1))
            if value is not None:
                totals[name] = {"value": value, "confidence": 0.9}

    computed = sum(
        float(item["amount"])
        for item in line_items
        if isinstance(item.get("amount"), (int, float))
    )
    totals["computed_line_total"] = round(computed, 2)

    # Validate: does the line-item sum match the stated subtotal (or total)?
    validated = False
    reference = totals.get("subtotal") or totals.get("total")
    if reference and computed:
        stated = float(reference["value"])  # type: ignore[index]
        if abs(stated - computed) <= max(0.02, stated * 0.01):
            validated = True
            # Boost confidence of the matched figure — it is cross-checked.
            reference["confidence"] = 0.99  # type: ignore[index]
        else:
            reference["confidence"] = 0.5  # type: ignore[index]
    totals["validated"] = validated
    return totals
