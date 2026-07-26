"""Deterministic generator for the labelled evaluation set.

Produces ``eval/dataset/<id>.txt`` (the document) and ``<id>.json`` (ground
truth) for 27 synthetic-but-realistic business documents: invoices, RFQ
emails and delivery notes, with deliberate edge cases (missing totals,
mismatched totals, multi-currency mentions, non-ISO date formats, EU
thousands separators, unicode company names, noisy phrasing, partial
deliveries).

Ground truth records what a *correct* extraction should produce — dates are
normalised to ISO, party names exclude addresses, and figures are the values
as printed on the document. Where the heuristic extractor cannot recover a
value, that is an honest miss for the evaluation to measure, not something
the ground truth papers over.

The generation is fully deterministic: a fixed seed drives the only random
choices (document-number suffixes), so ``python -m eval.make_dataset``
always reproduces the committed set byte for byte.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent / "dataset"
SEED = 20260726


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_money(value: float, style: str = "plain") -> str:
    """Format ``value`` with 2 decimals in ``plain``/``us``/``eu`` style."""
    s = f"{value:,.2f}"  # US style: 1,234.56
    if style == "eu":
        s = s.replace(",", "@").replace(".", ",").replace("@", ".")
    elif style == "plain":
        s = s.replace(",", "")
    return s


def fmt_qty(qty: float, override: str | None = None) -> str:
    """Format a quantity; an explicit ``override`` string wins (edge cases)."""
    if override is not None:
        return override
    return f"{qty:g}"


def _table_row(pos: int, desc: str, cols: list[str]) -> str:
    """One whitespace-aligned table row (columns separated by 2+ spaces)."""
    line = f"{pos:<6}{desc:<36}{cols[0]:>8}"
    for col in cols[1:]:
        line += f"    {col:>12}"
    return line


def _item_amount(item: dict) -> float:
    return round(item["qty"] * item["price"], 2)


def _sum_amounts(items: list[dict]) -> float:
    return round(sum(_item_amount(i) for i in items), 2)


# ---------------------------------------------------------------------------
# Renderers (spec -> document text)
# ---------------------------------------------------------------------------

def render_invoice(spec: dict) -> str:
    lines: list[str] = ["INVOICE", ""]
    lines += spec["seller"] + [""]
    lines += ["Bill To:"] + spec["buyer"] + [""]
    lines += [
        f"Invoice Number: {spec['number']}",
        f"Invoice Date: {spec['date'][1]}",
        f"Due Date: {spec['due'][1]}",
    ]
    if spec.get("order_ref"):
        lines.append(f"Order Reference: {spec['order_ref']}")
    if spec["currency_mode"] == "label":
        lines.append(f"Currency: {spec['currency']}")
    lines.append("")

    style = spec.get("numfmt", "plain")
    columns = spec.get("columns", "qty_price_amount")
    if columns == "qty_price_amount":
        header_cols = ["Qty", "Unit Price", "Amount"]
    else:  # qty_price
        header_cols = ["Qty", "Unit Price"]
    lines.append(_table_row_header(header_cols))
    for pos, item in enumerate(spec["items"], start=1):
        qty = fmt_qty(item["qty"], item.get("qty_render"))
        cols = [qty, fmt_money(item["price"], style)]
        if columns == "qty_price_amount":
            cols.append(fmt_money(_item_amount(item), style))
        lines.append(_table_row(pos, item["desc"], cols))
    lines.append("")

    totals_mode = spec["totals_mode"]
    subtotal = _sum_amounts(spec["items"])
    if totals_mode == "subtotal_vat_total":
        tax = round(subtotal * spec["tax_rate"], 2)
        total = round(subtotal + tax, 2)
        lines += _totals_block(spec, subtotal, tax, total, style)
    elif totals_mode == "total_only":
        lines.append(f"Total:         {fmt_money(subtotal, style)}")
    elif totals_mode == "stated":
        stated = spec["stated_totals"]
        lines += _totals_block(spec, stated["subtotal"], stated["tax"], stated["total"], style)
    # totals_mode == "none": no totals section at all.
    lines.append("")
    lines += spec.get("notes", [])
    return "\n".join(lines).rstrip() + "\n"


def _table_row_header(cols: list[str]) -> str:
    line = f"{'Line':<6}{'Description':<36}{cols[0]:>8}"
    for col in cols[1:]:
        line += f"    {col:>12}"
    return line


def _totals_block(spec: dict, subtotal: float, tax: float, total: float, style: str) -> list[str]:
    tax_label = spec.get("tax_label", "VAT (19%)")
    return [
        f"Subtotal:      {fmt_money(subtotal, style)}",
        f"{tax_label}:     {fmt_money(tax, style)}",
        f"Total:         {fmt_money(total, style)}",
    ]


def render_rfq(spec: dict) -> str:
    lines: list[str] = [
        f"From: {spec['from']}",
        f"To: {spec['to']}",
        f"Subject: {spec['subject']}",
    ]
    if spec.get("email_date"):
        lines.append(f"Date: {spec['email_date']}")
    lines += ["", spec["salutation"], ""]
    lines += spec["prose"] + [""]

    lines.append(f"{'Pos':<6}{'Description':<36}{'Qty':>8}    Unit")
    for pos, item in enumerate(spec["items"], start=1):
        lines.append(_table_row(pos, item["desc"], [fmt_qty(item["qty"]), item["unit"]]))
    lines.append("")

    if spec.get("deliver_inline"):
        lines.append(f"Deliver to: {spec['deliver_inline']}")
    else:
        lines += ["Deliver to:"] + spec["deliver_to"]
    if spec.get("requested"):
        lines.append(f"Requested delivery date: {spec['requested'][1]}")
    if spec["currency_mode"] == "label":
        lines.append(f"Currency: {spec['currency']}")
    elif spec["currency_mode"] == "mention":
        lines.append(spec["currency_mention"])
    lines.append("")
    lines += spec["signature"]
    return "\n".join(lines).rstrip() + "\n"


def render_delivery_note(spec: dict) -> str:
    lines: list[str] = ["DELIVERY NOTE", ""]
    lines += spec["seller"] + [""]
    lines += ["Deliver To:"] + spec["buyer"] + [""]
    lines += [
        f"Delivery Note No: {spec['number']}",
        f"Delivery Date: {spec['date'][1]}",
    ]
    if spec.get("order_ref"):
        lines.append(f"Order Reference: {spec['order_ref']}")
    lines.append("")

    lines.append(f"{'Pos':<6}{'Description':<36}{'Qty':>8}    Unit")
    for pos, item in enumerate(spec["items"], start=1):
        qty = fmt_qty(item["qty"], item.get("qty_render"))
        lines.append(_table_row(pos, item["desc"], [qty, item["unit"]]))
    lines.append("")
    lines += spec.get("notes", [])
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Ground truth (spec -> label JSON)
# ---------------------------------------------------------------------------

def build_ground_truth(spec: dict) -> dict:
    kind = spec["kind"]
    fields: dict[str, str] = {}
    items: list[dict] = []
    totals: dict[str, float] = {}

    if kind == "invoice":
        fields["document_number"] = spec["number"]
        fields["document_date"] = spec["date"][0]
        fields["due_date"] = spec["due"][0]
        if spec.get("order_ref"):
            fields["order_reference"] = spec["order_ref"]
        if spec.get("currency"):
            fields["currency"] = spec["currency"]
        fields["buyer"] = spec["buyer"][0]
        fields["seller"] = spec["seller"][0]
        columns = spec.get("columns", "qty_price_amount")
        for pos, item in enumerate(spec["items"], start=1):
            items.append(
                {
                    "position": pos,
                    "description": item["desc"],
                    "quantity": item["qty"],
                    "unit": None,
                    "unit_price": item["price"],
                    "amount": _item_amount(item) if columns == "qty_price_amount" else None,
                }
            )
        subtotal = _sum_amounts(spec["items"])
        if spec["totals_mode"] == "subtotal_vat_total":
            tax = round(subtotal * spec["tax_rate"], 2)
            totals = {"subtotal": subtotal, "tax": tax, "total": round(subtotal + tax, 2)}
        elif spec["totals_mode"] == "total_only":
            totals = {"total": subtotal}
        elif spec["totals_mode"] == "stated":
            totals = dict(spec["stated_totals"])

    elif kind == "rfq":
        fields["document_number"] = spec["number"]
        if spec.get("email_date"):
            fields["document_date"] = spec["email_date"]
        if spec.get("requested"):
            fields["requested_delivery_date"] = spec["requested"][0]
        if spec.get("currency"):
            fields["currency"] = spec["currency"]
        fields["buyer"] = spec["buyer_truth"]
        for pos, item in enumerate(spec["items"], start=1):
            items.append(
                {
                    "position": pos,
                    "description": item["desc"],
                    "quantity": item["qty"],
                    "unit": item["unit"],
                    "unit_price": None,
                    "amount": None,
                }
            )

    else:  # delivery_note
        fields["document_number"] = spec["number"]
        fields["document_date"] = spec["date"][0]
        if spec.get("order_ref"):
            fields["order_reference"] = spec["order_ref"]
        fields["buyer"] = spec["buyer"][0]
        fields["seller"] = spec["seller"][0]
        for pos, item in enumerate(spec["items"], start=1):
            items.append(
                {
                    "position": pos,
                    "description": item["desc"],
                    "quantity": item["qty"],
                    "unit": item["unit"],
                    "unit_price": None,
                    "amount": None,
                }
            )

    return {
        "id": spec["id"],
        "doc_type": {"invoice": "invoice", "rfq": "rfq", "delivery_note": "delivery_note"}[kind],
        "edge": spec["edge"],
        "fields": fields,
        "line_items": items,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# The 27 document specs
# ---------------------------------------------------------------------------

def _it(desc: str, qty: float, price: float | None = None, unit: str | None = None,
        qty_render: str | None = None) -> dict:
    item: dict = {"desc": desc, "qty": qty}
    if price is not None:
        item["price"] = price
    if unit is not None:
        item["unit"] = unit
    if qty_render is not None:
        item["qty_render"] = qty_render
    return item


def build_specs(rng: random.Random) -> list[dict]:
    """The full document list. Order is fixed; rng only feeds number suffixes."""

    def num(prefix: str) -> str:
        return f"{prefix}-2026-{rng.randint(1000, 9999)}"

    specs: list[dict] = []

    # ---------------- Invoices (13) ----------------
    inv_notes = ["Payment terms: 30 days net. Please reference the invoice number with payment."]

    specs.append({
        "id": "inv01_clean_eur", "kind": "invoice", "edge": "clean",
        "seller": ["Nordwind Stahlhandel GmbH", "Hafenstrasse 22", "20457 Hamburg, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-18", "2026-07-18"), "due": ("2026-08-17", "2026-08-17"),
        "order_ref": "RFQ-2026-0417", "currency": "EUR", "currency_mode": "label",
        "items": [_it("Hex bolt M8x40 DIN 933", 500, 0.12), _it("Hex nut M8 DIN 934", 500, 0.05),
                  _it("Washer M8 DIN 125", 1000, 0.02)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.19, "notes": inv_notes,
    })
    specs.append({
        "id": "inv02_clean_usd", "kind": "invoice", "edge": "us_number_format",
        "seller": ["Great Lakes Fastener Inc", "2200 Superior Ave", "Cleveland, OH 44114, USA"],
        "buyer": ["Midwest Assembly LLC", "480 Commerce Dr", "Toledo, OH 43604, USA"],
        "number": num("INV"), "date": ("2026-06-30", "2026-06-30"), "due": ("2026-07-30", "2026-07-30"),
        "order_ref": "RFQ-2026-1188", "currency": "USD", "currency_mode": "label", "numfmt": "us",
        "items": [_it("Bearing 6204-2RS", 1250, 2.40, qty_render="1,250"),
                  _it("Cable tie 300mm black", 10000, 0.03, qty_render="10,000"),
                  _it("Pallet EUR 1200x800", 16, 9.50)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.08, "tax_label": "Sales Tax (8%)",
        "notes": ["Payment terms: net 30."],
    })
    specs.append({
        "id": "inv03_eu_numbers", "kind": "invoice", "edge": "eu_decimal_comma",
        "seller": ["Rheinland Metallbau AG", "Severinstrasse 5", "50678 Koeln, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-02", "2026-07-02"), "due": ("2026-08-01", "2026-08-01"),
        "order_ref": "RFQ-2026-0522", "currency": "EUR", "currency_mode": "label", "numfmt": "eu",
        "items": [_it("Steel angle 40x40x4 S235", 120, 3.85), _it("Machine oil ISO VG 46", 60, 4.20)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.19, "notes": inv_notes,
    })
    specs.append({
        "id": "inv04_unicode_names", "kind": "invoice", "edge": "unicode_names",
        "seller": ["Müller & Söhne GmbH", "Bahnhofstraße 3", "90402 Nürnberg, Germany"],
        "buyer": ["Årdal Verft AS", "Kaivegen 17", "6884 Øvre Årdal, Norway"],
        "number": num("INV"), "date": ("2026-07-10", "2026-07-10"), "due": ("2026-08-09", "2026-08-09"),
        "order_ref": "RFQ-2026-0733", "currency": "EUR", "currency_mode": "label",
        "items": [_it("Hex bolt M8x40 DIN 933", 800, 0.12), _it("Bearing 6204-2RS", 40, 2.40)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.19, "notes": inv_notes,
    })
    specs.append({
        "id": "inv05_total_only", "kind": "invoice", "edge": "no_subtotal_line",
        "seller": ["Baltic Seals OU", "Peterburi tee 46", "11415 Tallinn, Estonia"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-21", "2026-07-21"), "due": ("2026-08-20", "2026-08-20"),
        "order_ref": "RFQ-2026-0611", "currency": "EUR", "currency_mode": "label",
        "items": [_it("Sealing ring NBR 25x35x7", 120, 0.55), _it("Cable tie 300mm black", 1000, 0.03)],
        "totals_mode": "total_only", "notes": ["Reverse charge - VAT to be accounted for by the recipient."],
    })
    specs.append({
        "id": "inv06_single_item", "kind": "invoice", "edge": "clean",
        "seller": ["Hanse Schmierstoffe GmbH", "Am Kai 9", "28217 Bremen, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-05", "2026-07-05"), "due": ("2026-08-04", "2026-08-04"),
        "order_ref": "RFQ-2026-0489", "currency": "EUR", "currency_mode": "label",
        "items": [_it("Machine oil ISO VG 46", 30, 4.20)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.19, "notes": inv_notes,
    })
    specs.append({
        "id": "inv07_discount_row", "kind": "invoice", "edge": "negative_discount_line",
        "seller": ["Verpackung Direkt GmbH", "Industriering 44", "68199 Mannheim, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-12", "2026-07-12"), "due": ("2026-08-11", "2026-08-11"),
        "order_ref": "RFQ-2026-0655", "currency": "EUR", "currency_mode": "label",
        "items": [_it("Corrugated box 400x300x200", 200, 0.85), _it("Pallet EUR 1200x800", 10, 9.50),
                  _it("Cable tie 300mm black", 500, 0.03), _it("Early payment discount", 1, -25.00)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.19, "notes": inv_notes,
    })
    specs.append({
        "id": "inv08_weird_dates", "kind": "invoice", "edge": "non_iso_dates",
        "seller": ["Schwarzwald Drehteile GmbH", "Talstrasse 8", "78120 Furtwangen, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-18", "18.07.2026"), "due": ("2026-08-17", "17.08.2026"),
        "order_ref": "RFQ-2026-0702", "currency": "EUR", "currency_mode": "label",
        "items": [_it("Hex bolt M8x40 DIN 933", 300, 0.12), _it("Hex nut M8 DIN 934", 300, 0.05)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.19, "notes": inv_notes,
    })
    specs.append({
        "id": "inv09_multicurrency", "kind": "invoice", "edge": "multi_currency_mention",
        "seller": ["Atlantic Bearing Supply Inc", "77 Harbor Blvd", "Boston, MA 02110, USA"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-15", "2026-07-15"), "due": ("2026-08-14", "2026-08-14"),
        "order_ref": "RFQ-2026-0641", "currency": "USD", "currency_mode": "none",
        "items": [_it("Bearing 6204-2RS", 100, 2.40), _it("Sealing ring NBR 25x35x7", 200, 0.55)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.07, "tax_label": "Sales Tax (7%)",
        "notes": ["All prices in USD; EUR settlement available on request."],
    })
    specs.append({
        "id": "inv10_eu_thousands", "kind": "invoice", "edge": "eu_thousands_qty",
        "seller": ["Donau Schrauben AG", "Werftstrasse 2", "93059 Regensburg, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-08", "2026-07-08"), "due": ("2026-08-07", "2026-08-07"),
        "order_ref": "RFQ-2026-0598", "currency": "EUR", "currency_mode": "label", "numfmt": "eu",
        "items": [_it("Hex bolt M8x40 DIN 933", 1000, 0.12, qty_render="1.000"),
                  _it("Washer M8 DIN 125", 2500, 0.02, qty_render="2.500")],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.19, "notes": inv_notes,
    })
    specs.append({
        "id": "inv11_missing_totals", "kind": "invoice", "edge": "missing_totals",
        "seller": ["Alpen Profile GmbH", "Gewerbepark 12", "87700 Memmingen, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-19", "2026-07-19"), "due": ("2026-08-18", "2026-08-18"),
        "order_ref": "RFQ-2026-0688", "currency": "EUR", "currency_mode": "label",
        "items": [_it("Steel angle 40x40x4 S235", 80, 3.85), _it("Machine oil ISO VG 46", 20, 4.20)],
        "totals_mode": "none",
        "notes": ["Totals page to follow separately with the monthly statement."],
    })
    specs.append({
        "id": "inv12_totals_mismatch", "kind": "invoice", "edge": "stated_totals_mismatch",
        "seller": ["Nordwind Stahlhandel GmbH", "Hafenstrasse 22", "20457 Hamburg, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-22", "2026-07-22"), "due": ("2026-08-21", "2026-08-21"),
        "order_ref": "RFQ-2026-0417", "currency": "EUR", "currency_mode": "label",
        "items": [_it("Hex bolt M8x40 DIN 933", 500, 0.12), _it("Hex nut M8 DIN 934", 500, 0.05),
                  _it("Washer M8 DIN 125", 1000, 0.02)],
        "totals_mode": "stated",
        "stated_totals": {"subtotal": 150.00, "tax": 28.50, "total": 178.50},
        "notes": inv_notes,
    })
    specs.append({
        "id": "inv13_no_amount_column", "kind": "invoice", "edge": "qty_price_only_columns",
        "seller": ["Werra Kleinteile GmbH", "Muehlweg 7", "36266 Heringen, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("INV"), "date": ("2026-07-24", "2026-07-24"), "due": ("2026-08-23", "2026-08-23"),
        "order_ref": "RFQ-2026-0719", "currency": "EUR", "currency_mode": "label",
        "columns": "qty_price",
        "items": [_it("Hex bolt M8x40 DIN 933", 400, 0.12), _it("Hex nut M8 DIN 934", 400, 0.05),
                  _it("Washer M8 DIN 125", 800, 0.02)],
        "totals_mode": "subtotal_vat_total", "tax_rate": 0.19, "notes": inv_notes,
    })

    # ---------------- RFQs (8) ----------------
    rfq_sig = ["Best regards,", "Maria Schmidt", "Procurement Department", "Acme Industrial GmbH"]
    rfq_prose = [
        "We would like to request a quotation for the following items. Please provide",
        "your best pricing and lead times at your earliest convenience.",
    ]

    specs.append({
        "id": "rfq01_clean", "kind": "rfq", "edge": "clean",
        "from": "procurement@acme-industrial.example", "to": "sales@nordwind-stahl.example",
        "number": "RFQ-2026-0801", "subject": "Request for Quotation - RFQ-2026-0801",
        "email_date": "2026-07-20", "salutation": "Dear Sales Team,", "prose": rfq_prose,
        "items": [_it("Hex bolt M8x40 DIN 933", 500, unit="pcs"), _it("Hex nut M8 DIN 934", 500, unit="pcs"),
                  _it("Washer M8 DIN 125", 1000, unit="pcs")],
        "deliver_to": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "buyer_truth": "Acme Industrial GmbH",
        "requested": ("2026-08-10", "2026-08-10"), "currency": "EUR", "currency_mode": "label",
        "signature": rfq_sig,
    })
    specs.append({
        "id": "rfq02_unicode_contact", "kind": "rfq", "edge": "unicode_names",
        "from": "compras@garcia-componentes.example", "to": "ventas@iberica-tornillos.example",
        "number": "RFQ-2026-0815", "subject": "Request for Quotation - RFQ-2026-0815",
        "email_date": "2026-07-21", "salutation": "Dear Ms. García,",
        "prose": ["We kindly request your quotation for the positions below."],
        "items": [_it("Sealing ring NBR 25x35x7", 300, unit="pcs"), _it("Bearing 6204-2RS", 60, unit="pcs")],
        "deliver_to": ["Fábrica de Componentes García S.L.", "Calle Mayor 8", "28013 Madrid, Spain"],
        "buyer_truth": "Fábrica de Componentes García S.L.",
        "requested": ("2026-08-12", "2026-08-12"), "currency": "EUR", "currency_mode": "mention",
        "currency_mention": "Please quote all prices in EUR including packaging.",
        "signature": ["Saludos cordiales,", "Ilinca Pîrvu", "Compras"],
    })
    specs.append({
        "id": "rfq03_noisy_no_header_date", "kind": "rfq", "edge": "noisy_phrasing",
        "from": "office@baltika-assembly.example", "to": "sales@baltic-seals.example",
        "number": "RFQ-2026-0822", "subject": "urgent quote needed pls - RFQ-2026-0822",
        "email_date": None, "salutation": "Hi,",
        "prose": [
            "Hope you are doing well. We are a bit under pressure here, so apologies for",
            "the short notice. Could you send us your best price for the parts below?",
            "Our line stops next month if we do not get these ordered, thanks a lot.",
        ],
        "items": [_it("Sealing ring NBR 25x35x7", 250, unit="pcs"), _it("Cable tie 300mm black", 2000, unit="pcs")],
        "deliver_to": ["Baltika Assembly OU", "Harju 4", "10130 Tallinn, Estonia"],
        "buyer_truth": "Baltika Assembly OU",
        "requested": ("2026-09-15", "2026-09-15"), "currency": "EUR", "currency_mode": "label",
        "signature": ["Thanks,", "Priit Kask"],
    })
    specs.append({
        "id": "rfq04_weird_date", "kind": "rfq", "edge": "non_iso_dates",
        "from": "purchasing@midwest-assembly.example", "to": "sales@greatlakes-fastener.example",
        "number": "RFQ-2026-0830", "subject": "Request for Quotation - RFQ-2026-0830",
        "email_date": "2026-08-28", "salutation": "Dear Sales Team,", "prose": rfq_prose,
        "items": [_it("Bearing 6204-2RS", 500, unit="pcs"), _it("Pallet EUR 1200x800", 24, unit="pcs")],
        "deliver_to": ["Midwest Assembly LLC", "480 Commerce Dr", "Toledo, OH 43604, USA"],
        "buyer_truth": "Midwest Assembly LLC",
        "requested": ("2026-09-01", "01/09/2026"), "currency": "USD", "currency_mode": "label",
        "signature": ["Regards,", "Dana Whitfield", "Purchasing"],
    })
    specs.append({
        "id": "rfq05_no_currency", "kind": "rfq", "edge": "no_currency_stated",
        "from": "einkauf@alpen-profile.example", "to": "vertrieb@donau-schrauben.example",
        "number": "RFQ-2026-0841", "subject": "Request for Quotation - RFQ-2026-0841",
        "email_date": "2026-07-23", "salutation": "Dear team,",
        "prose": ["Please quote the following positions with your standard conditions."],
        "items": [_it("Hex bolt M8x40 DIN 933", 1500, unit="pcs"), _it("Washer M8 DIN 125", 3000, unit="pcs")],
        "deliver_to": ["Alpen Profile GmbH", "Gewerbepark 12", "87700 Memmingen, Germany"],
        "buyer_truth": "Alpen Profile GmbH",
        "requested": ("2026-08-20", "2026-08-20"), "currency": None, "currency_mode": "none",
        "signature": ["Mit freundlichen Gruessen,", "Jonas Beck", "Einkauf"],
    })
    specs.append({
        "id": "rfq06_multicurrency", "kind": "rfq", "edge": "multi_currency_mention",
        "from": "procurement@acme-industrial.example", "to": "sales@atlantic-bearing.example",
        "number": "RFQ-2026-0850", "subject": "Request for Quotation - RFQ-2026-0850",
        "email_date": "2026-07-24", "salutation": "Dear Sales Team,", "prose": rfq_prose,
        "items": [_it("Bearing 6204-2RS", 200, unit="pcs")],
        "deliver_to": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "buyer_truth": "Acme Industrial GmbH",
        "requested": ("2026-08-25", "2026-08-25"), "currency": None, "currency_mode": "mention",
        "currency_mention": "You may quote in either EUR or USD, whichever is simpler for you.",
        "signature": rfq_sig,
    })
    specs.append({
        "id": "rfq07_mixed_units", "kind": "rfq", "edge": "mixed_units",
        "from": "procurement@acme-industrial.example", "to": "sales@hanse-schmierstoffe.example",
        "number": "RFQ-2026-0860", "subject": "Request for Quotation - RFQ-2026-0860",
        "email_date": "2026-07-25", "salutation": "Dear Sales Team,", "prose": rfq_prose,
        "items": [_it("Machine oil ISO VG 46", 12.5, unit="l"), _it("Corrugated box 400x300x200", 40, unit="boxes"),
                  _it("Steel angle 40x40x4 S235", 60, unit="m")],
        "deliver_to": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "buyer_truth": "Acme Industrial GmbH",
        "requested": ("2026-08-30", "2026-08-30"), "currency": "EUR", "currency_mode": "label",
        "signature": rfq_sig,
    })
    specs.append({
        "id": "rfq08_inline_deliver_to", "kind": "rfq", "edge": "inline_deliver_to",
        "from": "office@baltika-assembly.example", "to": "sales@verpackung-direkt.example",
        "number": "RFQ-2026-0871", "subject": "Request for Quotation - RFQ-2026-0871",
        "email_date": "2026-07-26", "salutation": "Dear Sales Team,", "prose": rfq_prose,
        "items": [_it("Corrugated box 400x300x200", 600, unit="boxes"), _it("Pallet EUR 1200x800", 12, unit="pcs")],
        "deliver_inline": "Baltika Assembly OU, Harju 4, 10130 Tallinn, Estonia",
        "buyer_truth": "Baltika Assembly OU",
        "requested": ("2026-09-05", "2026-09-05"), "currency": "EUR", "currency_mode": "label",
        "signature": ["Thanks,", "Priit Kask"],
    })

    # ---------------- Delivery notes (6) ----------------
    dn_notes = ["Goods received in good condition. Please sign and return one copy."]

    specs.append({
        "id": "dn01_clean", "kind": "delivery_note", "edge": "clean",
        "seller": ["Nordwind Stahlhandel GmbH", "Hafenstrasse 22", "20457 Hamburg, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("DN"), "date": ("2026-07-20", "2026-07-20"), "order_ref": "RFQ-2026-0417",
        "items": [_it("Hex bolt M8x40 DIN 933", 500, unit="pcs"), _it("Hex nut M8 DIN 934", 500, unit="pcs"),
                  _it("Washer M8 DIN 125", 1000, unit="pcs")],
        "notes": ["Total packages: 3", "Total weight: 45 kg", ""] + dn_notes,
    })
    specs.append({
        "id": "dn02_unicode_buyer", "kind": "delivery_note", "edge": "unicode_names",
        "seller": ["Iberica Tornillos SA", "Poligono Sur 21", "41007 Sevilla, Spain"],
        "buyer": ["Construções Navais do Tejo S.A.", "Rua do Arsenal 14", "1100-038 Lisboa, Portugal"],
        "number": num("DN"), "date": ("2026-07-17", "2026-07-17"), "order_ref": "RFQ-2026-0733",
        "items": [_it("Hex bolt M8x40 DIN 933", 800, unit="pcs"), _it("Bearing 6204-2RS", 40, unit="pcs")],
        "notes": dn_notes,
    })
    specs.append({
        "id": "dn03_no_order_ref", "kind": "delivery_note", "edge": "missing_order_reference",
        "seller": ["Verpackung Direkt GmbH", "Industriering 44", "68199 Mannheim, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("DN"), "date": ("2026-07-14", "2026-07-14"), "order_ref": None,
        "items": [_it("Corrugated box 400x300x200", 200, unit="boxes"), _it("Pallet EUR 1200x800", 10, unit="pcs")],
        "notes": dn_notes,
    })
    specs.append({
        "id": "dn04_weird_date", "kind": "delivery_note", "edge": "non_iso_dates",
        "seller": ["Baltic Seals OU", "Peterburi tee 46", "11415 Tallinn, Estonia"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("DN"), "date": ("2026-07-21", "21 July 2026"), "order_ref": "RFQ-2026-0611",
        "items": [_it("Sealing ring NBR 25x35x7", 120, unit="pcs"), _it("Cable tie 300mm black", 1000, unit="pcs")],
        "notes": dn_notes,
    })
    specs.append({
        "id": "dn05_zero_qty_backorder", "kind": "delivery_note", "edge": "zero_quantity_row",
        "seller": ["Werra Kleinteile GmbH", "Muehlweg 7", "36266 Heringen, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("DN"), "date": ("2026-07-23", "2026-07-23"), "order_ref": "RFQ-2026-0719",
        "items": [_it("Hex bolt M8x40 DIN 933", 400, unit="pcs"), _it("Hex nut M8 DIN 934", 400, unit="pcs"),
                  _it("Washer M8 DIN 125", 0, unit="pcs")],
        "notes": ["Position 3 backordered; balance follows next week.", ""] + dn_notes,
    })
    specs.append({
        "id": "dn06_partial_delivery", "kind": "delivery_note", "edge": "partial_delivery_note_in_qty",
        "seller": ["Rheinland Metallbau AG", "Severinstrasse 5", "50678 Koeln, Germany"],
        "buyer": ["Acme Industrial GmbH", "Musterstrasse 12", "74653 Kunzelsau, Germany"],
        "number": num("DN"), "date": ("2026-07-22", "2026-07-22"), "order_ref": "RFQ-2026-0522",
        "items": [_it("Steel angle 40x40x4 S235", 480, unit="m", qty_render="480 of 500"),
                  _it("Machine oil ISO VG 46", 60, unit="l")],
        "notes": ["Remaining 20 m to follow with the next shipment.", ""] + dn_notes,
    })

    return specs


_RENDERERS = {"invoice": render_invoice, "rfq": render_rfq, "delivery_note": render_delivery_note}


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate(out_dir: Path | str = DATASET_DIR) -> list[str]:
    """Write the full dataset into ``out_dir``; returns the document ids."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    ids: list[str] = []
    for spec in build_specs(rng):
        text = _RENDERERS[spec["kind"]](spec)
        truth = build_ground_truth(spec)
        (out / f"{spec['id']}.txt").write_text(text, encoding="utf-8", newline="\n")
        with open(out / f"{spec['id']}.json", "w", encoding="utf-8", newline="\n") as fh:
            json.dump(truth, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        ids.append(spec["id"])
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the labelled evaluation dataset.")
    parser.add_argument("--out", default=str(DATASET_DIR), help="output directory")
    args = parser.parse_args()
    ids = generate(args.out)
    kinds = {"inv": 0, "rfq": 0, "dn0": 0}
    for doc_id in ids:
        kinds[doc_id[:3]] += 1
    print(
        f"wrote {len(ids)} documents to {args.out} "
        f"(invoices {kinds['inv']}, rfqs {kinds['rfq']}, delivery notes {kinds['dn0']})"
    )


if __name__ == "__main__":
    main()
