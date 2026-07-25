# doc-extract-agent

Picture a three-person AP & order-desk team keying ~60,000 supplier invoices, order confirmations, and delivery notes into the ERP by hand every year — four minutes a document, and a mistyped total only shows up later as a payment dispute. This is the loop that eats it: **it takes handling from ~4 minutes to under a second per document and frees on the order of €110k/yr of AP capacity** (modeled — see the business case), because only the documents that reconcile and clear a confidence gate post automatically, and the rest route to a human.

Paste a business document — an RFQ email, an invoice, a delivery note — and get structured data back: header fields, line items, and totals that are cross-checked against the line items, each with a confidence score, plus a step-by-step trace of how it got there.

This is the "unstructured document in, structured record out" loop that sits under a lot of Data & AI automation work, and I built it while learning that space for internship applications. It complements a sibling project, `agentic-automation-lab`. Everything runs on Python's standard library — no dependencies, no API key, no build step.

**Business case:** [`docs/BUSINESS_CASE.md`](docs/BUSINESS_CASE.md) — the AP scenario, the arithmetic behind the numbers above, and the confidence-gate behaviour, with a one-page [executive summary PDF](deliverables/executive_onepager.pdf).

## Running it

```bash
python -m docextract.server
```

Open http://127.0.0.1:8000, pick a sample from the dropdown or paste your own text, and hit Extract. There's also a small HTTP API:

```
POST /extract   {"text": "<document text>"}  →  {doc_type, fields, line_items, totals, confidence, trace}
```

Or call the engine directly:

```python
from docextract import extract_document

result = extract_document(open("samples/invoice.txt").read())
print(result["doc_type"])            # "invoice"
print(result["totals"]["validated"]) # True
```

## What runs when you hit Extract

A five-stage pipeline, and each stage emits a trace event so you can watch it work: `detect` classifies the document, `header` pulls parties/dates/currency/reference numbers, `line_items` parses the table, `totals` extracts the stated totals and cross-checks them against the summed line items, and `confidence` rolls everything into one score. Results export as JSON or CSV.

## The two bugs that made me write real tests

The parsing looks straightforward until you actually run it against realistic samples, and two things bit me:

**"VAT (19%): 19.95" extracted the tax as 19.** My first tax regex just grabbed the first number after the VAT label, so it happily returned `19` — the percentage — instead of `19.95`, the actual amount. The fix was to require a colon before the number so the `(19%)` in parentheses gets skipped. There's now a test pinning `tax == 19.95` specifically so this can't regress.

**"Subtotal: 105.00" was being read as the Total.** A bare `\bTotal` pattern matches inside the word "Subtotal", so the total field kept picking up the subtotal value. I added a negative lookbehind (`(?<!Sub)`) so `Total` only matches when it isn't the tail of `Subtotal`. Both figures now come out separately, and the totals validation catches when the line-item sum doesn't reconcile.

The number parsing has its own share of this — it has to handle both `1.234,56` (EU) and `1,234.56` (US) and decide which separator is the decimal — which is the other place the tests earn their keep.

## Limitations

- The extraction is a deterministic heuristic (regex plus column-aware table parsing), not a real LLM. That's on purpose — it keeps the demo offline and reproducible — but it's tuned for clean, well-structured documents.
- Messy scans, OCR noise, multi-page tables, unusual layouts, or languages outside the sample style will drop or miss fields.
- For production, the pipeline is already built around an `LLMProvider` interface. Set `DOCEXTRACT_PROVIDER=anthropic` (plus `ANTHROPIC_API_KEY`) and each stage routes its extraction through a real model — the stages, trace, confidence, and validation logic stay put.

## Tests

```bash
pip install pytest ruff
ruff check .
pytest -q
```

The suite runs the three shipped samples end to end and also feeds the pipeline garbage to check it never crashes.

## Next

I'd like to run the same documents through the Anthropic provider and diff its output against the heuristic — partly to see where the regex quietly loses, partly to make the confidence scores mean something calibrated rather than hand-picked.

---

© 2026 Dimitres Kisimov — all rights reserved; published for portfolio review. See LICENSE.
