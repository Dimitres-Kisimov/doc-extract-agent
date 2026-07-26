# doc-extract-agent

Picture a three-person AP & order-desk team keying ~60,000 supplier invoices, order confirmations, and delivery notes into the ERP by hand every year — four minutes a document, and a mistyped total only shows up later as a payment dispute. This is the loop that eats it: **it takes handling from ~4 minutes to under a second per document and frees on the order of €110k/yr of AP capacity** (modeled — see the business case), because only the documents that reconcile and clear a confidence gate post automatically, and the rest route to a human.

Paste a business document — an RFQ email, an invoice, a delivery note — and get structured data back: header fields, line items, and totals that are cross-checked against the line items, each with a confidence score, plus a step-by-step trace of how it got there.

![doc-extract-agent web UI — document input with sample loader, pipeline trace panel and structured-result area (landing state)](docs/img/ui.png)

This is the "unstructured document in, structured record out" loop that sits under a lot of Data & AI automation work, and I built it while learning that space for internship applications. It complements a sibling project, `agentic-automation-lab`. Everything runs on Python's standard library — no dependencies, no API key, no build step.

**Business case:** [`docs/BUSINESS_CASE.md`](docs/BUSINESS_CASE.md) — the AP scenario, the arithmetic behind the numbers above, and the confidence-gate behaviour, with a one-page [executive summary PDF](deliverables/executive_onepager.pdf).

## Running it

```bash
python -m docextract.server
```

Open http://127.0.0.1:8000, pick a sample from the dropdown or paste your own text, and hit Extract. There's also a small HTTP API:

```
POST /extract        {"text": "<document text>", "gate_threshold": 0.85}
                     →  {doc_type, fields, line_items, totals, confidence, gate, trace}

POST /extract/batch  {"documents": [{"name": "a.txt", "text": "..."}, ...], "gate_threshold": 0.85}
                     →  {results: [{name, ...same shape as /extract}], summary: {documents, auto_post, review, gate_threshold}}
```

`gate_threshold` is optional (default 0.85); a batch takes up to 50 documents and shares the 1 MB request limit.

Or call the engine directly:

```python
from docextract import evaluate_gate, extract_document

result = extract_document(open("samples/invoice.txt").read())
print(result["doc_type"])                     # "invoice"
print(result["totals"]["validated"])          # True
print(evaluate_gate(result)["disposition"])   # "auto_post"
```

## What runs when you hit Extract

A five-stage pipeline, and each stage emits a trace event so you can watch it work: `detect` classifies the document, `header` pulls parties/dates/currency/reference numbers, `line_items` parses the table, `totals` extracts the stated totals and cross-checks them against the summed line items, and `confidence` rolls everything into one score. Results copy to the clipboard (per field, or all fields as a tab-separated block that pastes into Excel/an ERP form) and export as JSON or CSV — line items only, or the full record (header fields + totals + line items) as one importable table. Ctrl+Enter extracts without leaving the textarea.

Every result is then stamped by the **confidence gate** — the heuristic rule from the business case, now implemented: auto-post requires overall confidence at or above the gate (default 85%, adjustable in the UI and per API request) *and* totals that cross-validate against the line items. Everything else is marked **needs review** with the specific reasons, and every below-gate field is flagged so a reviewer knows what to check first. A document with no monetary totals — an RFQ, a delivery note — always routes to review; flagged fields never change the disposition, they just mark where to look.

For the stack-of-emails case there's a **batch mode**: drop several `.txt`/`.eml` files onto the textarea (or pick them with "Batch files…") and they're all extracted in one request. You get a results table — file, type, document number, overall confidence, auto-post/review per document — any row opens into the full trace and result panels, and two combined exports: one CSV of full records across all documents (prefixed with source file and gate disposition) and the whole batch as JSON. `.eml` files are read as plain text; there's no MIME or quoted-reply cleanup yet.

## The two bugs that made me write real tests

The parsing looks straightforward until you actually run it against realistic samples, and two things bit me:

**"VAT (19%): 19.95" extracted the tax as 19.** My first tax regex just grabbed the first number after the VAT label, so it happily returned `19` — the percentage — instead of `19.95`, the actual amount. The fix was to require a colon before the number so the `(19%)` in parentheses gets skipped. There's now a test pinning `tax == 19.95` specifically so this can't regress.

**"Subtotal: 105.00" was being read as the Total.** A bare `\bTotal` pattern matches inside the word "Subtotal", so the total field kept picking up the subtotal value. I added a negative lookbehind (`(?<!Sub)`) so `Total` only matches when it isn't the tail of `Subtotal`. Both figures now come out separately, and the totals validation catches when the line-item sum doesn't reconcile.

The number parsing has its own share of this — it has to handle both `1.234,56` (EU) and `1,234.56` (US) and decide which separator is the decimal — which is the other place the tests earn their keep.

## Measured extraction quality

Claims like the ones above deserve numbers, so the repo ships a labelled evaluation set — 27 synthetic-but-realistic documents (13 invoices, 8 RFQ emails, 6 delivery notes) with deliberate edge cases: missing and mismatched totals, multi-currency mentions, non-ISO date formats, EU thousands separators, unicode company names, noisy phrasing, partial deliveries. It's generated deterministically (`python -m eval.make_dataset`, fixed seed, committed under `eval/dataset/`) and scored with `python -m eval.run_eval`, which writes `eval/results.json`. This is the measured output on that set:

```
== doc-extract-agent evaluation (mock pipeline) ==
Documents: 27 (delivery_note 6, invoice 13, rfq 8)

-- Document type detection --
correct 27/27 (100.0%)

-- Header fields (exact match) --
field                      expected  correct  accuracy  spurious
buyer                            27       26     96.3%         0
currency                         19       18     94.7%         2
document_date                    26       24     92.3%         1
document_number                  27       27    100.0%         0
due_date                         13       12     92.3%         0
order_reference                  18       18    100.0%         0
requested_delivery_date           8        7     87.5%         0
seller                           19       19    100.0%         0

-- Stated totals (numeric, +/-0.01) --
total                      expected  correct  accuracy  spurious
subtotal                         11       11    100.0%         0
tax                              11       11    100.0%         0
total                            12       12    100.0%         0

-- Line items (matched by description; qty/unit/price/amount must all match) --
tp 56  fp 5  fn 6  precision 91.8%  recall 90.3%  f1 91.1%

-- Per document type --
type              docs  fully correct  field acc  item f1
delivery_note        6              3      96.5%    96.3%
invoice             13              9      96.7%    83.9%
rfq                  8              4      94.6%   100.0%
all                 27             16

-- Confidence gate @ 0.85 (operating stats) --
auto-posted:       10 docs, 7 fully correct -> auto-post precision 70.0%
  posted WITH errors: inv08_weird_dates, inv09_multicurrency, inv10_eu_thousands
review-flagged:    17 docs, 8 with real extraction errors, 9 extraction-clean (held for unreconciled or absent totals)

lowest threshold with 100% auto-post precision: 0.9393 (auto-post volume drops to 4 of 27 docs)
```

The honest reading:

- **Only 16 of 27 documents come out fully correct.** Labelled identifiers (document numbers, order references, ISO dates, stated totals) are near-perfect; what fails is exactly what regexes can't see: **non-ISO dates** (`18.07.2026`, `21 July 2026`, `01/09/2026` are all silently dropped), **currency by inference** (an invoice that says "all prices in USD" in prose gets tagged `EUR`; a delivery note with no currency at all gets a spurious `EUR` because a line item is literally named "Pallet EUR 1200x800"), **EU thousands separators in quantities** (`1.000` parses as 1.0), and **prose-polluted columns** ("480 of 500" drops the row).
- **The gate's auto-post precision at the default 0.85 threshold is 70%, not 100%.** 10 documents auto-post; 3 of them carry real extraction errors. The reason is structural: a field the regex *silently misses* doesn't lower the confidence average (the score only aggregates over what *was* extracted), so a document with a dropped date can score *higher* than a clean one. Confidence catches parse uncertainty, not silent omissions.
- **What the gate does catch is real:** all 8 documents with broken line items or unreconciled/mismatched totals were held for review — the cross-validation rule (not the confidence score) did that work, including an invoice whose printed totals simply don't add up.
- **Sweeping the threshold: 0.94 reaches 100% auto-post precision on this set, but volume collapses from 10 to 4 documents.** That trade-off (and the fact that a threshold can't see silent misses at all) is the argument for the roadmap items: per-doc-type required-field checks and per-row qty x price = amount validation would catch these errors structurally instead of statistically.

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

The suite runs the three shipped samples end to end, feeds the pipeline garbage to check it never crashes, and covers the evaluation harness: dataset generation is byte-for-byte deterministic (and the committed set is verified against a fresh regeneration), the scorer is checked on a hand-verified document pair, and the gate's operating stats are recomputed from the per-document results. Regenerate the numbers any time with `python -m eval.run_eval`.

## Next

I'd like to run the same documents through the Anthropic provider and diff its output against the heuristic — partly to see where the regex quietly loses, partly to make the confidence scores mean something calibrated rather than hand-picked.

---

© 2026 Dimitres Kisimov — all rights reserved; published for portfolio review. See LICENSE.
