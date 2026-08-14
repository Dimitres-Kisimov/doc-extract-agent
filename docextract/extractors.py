"""Deterministic parsing heuristics — the built-in "agent".

These functions are the reference implementation the pipeline uses when no LLM
provider is available. They are pure and side-effect free so they are easy to
unit test.

The parsing strategy is column-aware: whitespace-aligned tables are split on
runs of two-or-more spaces, which keeps multi-word descriptions intact while
isolating numeric columns.

Every extracted value also carries its **source span** — the exact characters it
was read from (see :mod:`docextract.spans`). Values the engine computed rather
than read carry no span and are listed under ``derived``.

Example::

    from docextract.extractors import detect_doc_type, parse_line_items

    detect_doc_type("INVOICE\\n...")        # ("invoice", 0.97)
    parse_line_items(invoice_text)          # [{"position": 1, ...}, ...]
"""

from __future__ import annotations

import re

from docextract import spans

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


def _first_token(text: str, tokens: tuple[str, ...]) -> tuple[int, int] | None:
    """Return the offsets of whichever of ``tokens`` appears earliest in ``text``."""
    best: tuple[int, int] | None = None
    for token in tokens:
        index = text.find(token)
        if index != -1 and (best is None or index < best[0]):
            best = (index, index + len(token))
    return best


def extract_header_fields(text: str) -> dict[str, dict[str, object]]:
    """Extract labelled header fields as ``{field: {value, confidence, span}}``.

    ``span`` is the source location the value was read from (see
    :mod:`docextract.spans`), or ``None`` when the value could not be tied to
    one. The captured characters and the value are usually identical, but not
    always: a currency inferred from a "€" symbol carries the span of the
    symbol, so ``span["text"]`` shows what was actually on the page.
    """
    fields: dict[str, dict[str, object]] = {}
    starts = spans.line_starts(text)
    for name, pattern, conf in _FIELD_PATTERNS:
        if name in fields:
            continue
        match = pattern.search(text)
        if match:
            start, end = spans.trim(text, *match.span(1))
            fields[name] = {
                "value": match.group(1).strip(),
                "confidence": conf,
                "span": spans.make_span(text, start, end, starts=starts),
            }

    # Fallback currency detection via symbols if not labelled. The span points
    # at the symbol or code the currency was *inferred* from.
    if "currency" not in fields:
        for code, tokens in (("EUR", ("€", "EUR")), ("USD", ("$", "USD"))):
            found = _first_token(text, tokens)
            if found:
                fields["currency"] = {
                    "value": code,
                    "confidence": 0.6,
                    "span": spans.make_span(text, found[0], found[1], starts=starts),
                }
                break

    # Parties: the block after a "Bill To" / "Deliver To" label is the buyer.
    buyer = _extract_party(text, r"(?:Bill To|Deliver To|Deliver to|Ship To)", starts=starts)
    if buyer:
        fields["buyer"] = {"value": buyer[0], "confidence": 0.85, "span": buyer[1]}

    # The first non-empty, non-heading line names the issuing party (seller).
    seller = _extract_seller(text, starts=starts)
    if seller:
        fields["seller"] = {"value": seller[0], "confidence": 0.75, "span": seller[1]}

    return fields


def _line_span(
    text: str, line: str, line_start: int, starts: list[int]
) -> dict[str, object] | None:
    """Span of ``line``'s stripped content, given where the raw line begins."""
    indent = len(line) - len(line.lstrip())
    start = line_start + indent
    return spans.make_span(text, start, start + len(line.strip()), starts=starts)


def _extract_party(
    text: str, label_regex: str, *, starts: list[int] | None = None
) -> tuple[str, dict[str, object] | None] | None:
    """Return the party named after a label, with its source span.

    Handles both layouts seen in real documents::

        Bill To:                  Deliver to: Acme GmbH, Musterstrasse 12, ...
        Acme GmbH

    i.e. the value may be on the following line(s) *or* inline on the same
    line as the label.
    """
    lines = text.splitlines()
    starts = spans.line_starts(text) if starts is None else starts
    block_label = re.compile(label_regex + r"\s*:?\s*$", re.I)
    inline_label = re.compile(label_regex + r"\s*:\s*(\S.*)$", re.I)
    for i, line in enumerate(lines):
        stripped = line.strip()
        inline = inline_label.match(stripped)
        if inline:
            # Offsets are relative to the stripped line; add back the indent.
            indent = len(line) - len(line.lstrip())
            start, end = spans.trim(
                text,
                starts[i] + indent + inline.start(1),
                starts[i] + indent + inline.end(1),
            )
            return inline.group(1).strip(), spans.make_span(text, start, end, starts=starts)
        if block_label.match(stripped):
            for offset, follow in enumerate(lines[i + 1:], start=i + 1):
                if follow.strip():
                    return follow.strip(), _line_span(text, follow, starts[offset], starts)
    return None


_HEADINGS = {"INVOICE", "DELIVERY NOTE", "REQUEST FOR QUOTATION", "QUOTATION"}

# Email salutations must never be mistaken for a company name.
_SALUTATION_RE = re.compile(r"^(dear|hello|hi|good\s+(?:morning|afternoon|evening)|to\s+whom)\b", re.I)
# Sentence-like letter body text (function words a company name would not contain).
_PROSE_RE = re.compile(r"\b(we|you|your|our|please|would|kindly|thanks?|regarding|following|request)\b", re.I)
# The line-items table header — reaching it means the top block held no company name.
_TABLE_HEADER_RE = re.compile(r"\b(?:description|qty|quantity|unit\s*price|amount)\b", re.I)


def _extract_seller(
    text: str, *, starts: list[int] | None = None
) -> tuple[str, dict[str, object] | None] | None:
    """Heuristically identify the issuing company near the top of the document.

    Skips headings, email headers, salutations ("Dear Sales Team,") and
    sentence-like prose. Returns ``None`` rather than guessing once the
    line-items table is reached — an absent counterparty is better than a
    wrong one. On a hit, returns the name and its source span.
    """
    starts = spans.line_starts(text) if starts is None else starts
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper() in _HEADINGS:
            continue
        if stripped.lower().startswith(("from:", "to:", "subject:", "date:")):
            continue
        if _SALUTATION_RE.match(stripped):
            continue
        # Reached the table without a plausible company line: give up honestly.
        if _TABLE_HEADER_RE.search(stripped) or _ROW_RE.match(line):
            return None
        # Letter body text is not a company name.
        if _PROSE_RE.search(stripped) or stripped.endswith("."):
            continue
        return stripped, _line_span(text, line, starts[index], starts)
    return None


def _split_columns(segment: str, base: int) -> list[tuple[str, int, int]]:
    """Split a row body on runs of 2+ spaces, keeping each column's offsets.

    Mirrors ``re.split(r"\\s{2,}", segment)`` on a stripped row body — the runs
    are maximal and the body starts with a non-space, so no empty column can
    fall out — but returns ``(token, start, end)`` with offsets in the document
    so every parsed value can point back at the characters it came from.
    """
    columns: list[tuple[str, int, int]] = []
    position = 0
    for separator in re.finditer(r"\s{2,}", segment):
        token = segment[position:separator.start()]
        if token:
            columns.append((token, base + position, base + position + len(token)))
        position = separator.end()
    tail = segment[position:]
    if tail:
        columns.append((tail, base + position, base + position + len(tail)))
    return columns


def parse_line_items(text: str) -> list[dict[str, object]]:
    """Parse tabular line items.

    Each row must start with an integer position followed by two-or-more
    spaces. Columns are split on runs of whitespace so multi-word descriptions
    stay intact. Numeric columns are interpreted as (quantity[, unit_price,
    amount]) depending on how many numbers the row contains.

    Each item carries ``spans`` — the source location of every column that was
    read off the row (``None`` for columns the row does not have) — and
    ``derived``, naming the values that were computed instead. A two-number row
    states an amount but no unit price, so the unit price is back-calculated
    from amount ÷ quantity: a real number with no place in the document, and it
    is listed under ``derived`` rather than pointed at a nearby figure.
    """
    items: list[dict[str, object]] = []
    starts = spans.line_starts(text)
    for index, line in enumerate(text.splitlines()):
        row = _ROW_RE.match(line)
        if not row:
            continue
        line_start = starts[index]
        position = int(row.group(1))
        body = row.group(2)
        stripped_body = body.strip()
        columns = _split_columns(
            stripped_body, line_start + row.start(2) + (len(body) - len(body.lstrip()))
        )

        description_columns: list[tuple[str, int, int]] = []
        numbers: list[tuple[float, int, int]] = []
        unit_column: tuple[str, int, int] | None = None
        for column in columns:
            value = _to_float(column[0])
            if value is not None:
                numbers.append((value, column[1], column[2]))
            elif column[0].lower() in _UNITS:
                unit_column = column
            else:
                description_columns.append(column)

        description = " ".join(column[0] for column in description_columns).strip()
        # A real item needs a description and at least a quantity.
        if not description or not any(c.isalpha() for c in description) or not numbers:
            continue

        item: dict[str, object] = {
            "position": position,
            "description": description,
            "quantity": numbers[0][0],
            "unit": unit_column[0] if unit_column else None,
            "unit_price": None,
            "amount": None,
        }
        item_spans: dict[str, object] = {
            "position": spans.make_span(
                text, line_start + row.start(1), line_start + row.end(1), starts=starts
            ),
            # One span from the first to the last description column: the words
            # are joined with single spaces, the source may be column-padded.
            "description": spans.make_span(
                text, description_columns[0][1], description_columns[-1][2], starts=starts
            ),
            "quantity": spans.make_span(text, numbers[0][1], numbers[0][2], starts=starts),
            "unit": spans.make_span(text, unit_column[1], unit_column[2], starts=starts)
            if unit_column
            else None,
            "unit_price": None,
            "amount": None,
        }
        derived: list[str] = []
        if len(numbers) >= 3:
            item["unit_price"] = numbers[1][0]
            item["amount"] = numbers[2][0]
            item_spans["unit_price"] = spans.make_span(
                text, numbers[1][1], numbers[1][2], starts=starts
            )
            item_spans["amount"] = spans.make_span(
                text, numbers[2][1], numbers[2][2], starts=starts
            )
        elif len(numbers) == 2:
            item["amount"] = numbers[1][0]
            item_spans["amount"] = spans.make_span(
                text, numbers[1][1], numbers[1][2], starts=starts
            )
            if numbers[0][0]:
                item["unit_price"] = round(numbers[1][0] / numbers[0][0], 4)
                derived.append("unit_price")  # computed, so it has no source span

        # Per-item confidence: highest when we have qty + price + amount.
        item["confidence"] = 0.95 if len(numbers) >= 3 else (0.85 if len(numbers) == 2 else 0.8)
        item["spans"] = item_spans
        item["derived"] = derived
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
    ``{value, confidence, span}`` when present), a ``computed_line_total``
    derived from the line items, a ``derived`` list naming the computed figures
    (which therefore have no source span), and a ``validated`` flag.
    """
    totals: dict[str, object] = {}
    starts = spans.line_starts(text)
    for name, pattern in _TOTAL_PATTERNS:
        match = pattern.search(text)
        if match:
            value = _to_float(match.group(1))
            if value is not None:
                start, end = spans.trim(text, *match.span(1))
                totals[name] = {
                    "value": value,
                    "confidence": 0.9,
                    "span": spans.make_span(text, start, end, starts=starts),
                }

    computed = sum(
        float(item["amount"])
        for item in line_items
        if isinstance(item.get("amount"), (int, float))
    )
    totals["computed_line_total"] = round(computed, 2)
    # The line-item sum is the engine's own arithmetic — it is nowhere on the
    # page, so it is named as derived instead of given a span.
    totals["derived"] = ["computed_line_total"]

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
