# doc-extract-agent

**Paste a business document → get structured data back, live.** An interactive,
agentic document-intelligence engine that turns an RFQ email, invoice, or
delivery note into clean fields, line items, and validated totals — with a
step-by-step trace and a per-field confidence score.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![No dependencies](https://img.shields.io/badge/core-stdlib--only-success)
![License](https://img.shields.io/badge/license-MIT-green)
![Design](https://img.shields.io/badge/design-agentic%20pipeline-7c5cff)

> **Unstructured document → structured data, interactively.** This is the core
> Data & AI automation loop: classify a document, pull the fields that matter,
> cross-check them, and hand back machine-readable output a downstream workflow
> can act on — with the reasoning visible at every step.

---

## Run in 30 seconds

No dependencies, no API key, no build step. Just Python's standard library.

```bash
git clone <this-repo> doc-extract-agent
cd doc-extract-agent
python -m docextract.server
```

Then open **http://127.0.0.1:8000** in your browser, pick a sample from the
dropdown (or paste your own document), and hit **Extract**.

![screenshot placeholder](docs/screenshot.png)
<!-- Add a screenshot of the UI here: left = document input, right = live pipeline
     trace, bottom = structured fields + line-item table with confidence badges. -->

## What it does

Drop in a document and an **agentic pipeline** runs five traced stages:

| Stage | What happens |
|-------|--------------|
| `detect` | Classify the document — invoice / delivery note / RFQ. |
| `header` | Extract parties, dates, currency, document & reference numbers. |
| `line_items` | Parse the tabular line items (position, description, qty, unit, price, amount). |
| `totals` | Extract stated totals and **cross-validate** them against the summed line items. |
| `confidence` | Aggregate a single overall confidence score in `[0, 1]`. |

Every stage emits a trace event, so you watch the "agent" reason in real time.
Results export as **JSON** (full structure) or **CSV** (line items).

### API

The UI is backed by a tiny `http.server`:

```
POST /extract   Body: {"text": "<document text>"}
                → {"doc_type", "fields", "line_items", "totals", "confidence", "trace"}
```

```bash
curl -s http://127.0.0.1:8000/extract \
  -H 'Content-Type: application/json' \
  -d '{"text": "INVOICE\nInvoice Number: INV-1\nTotal: 10.00"}'
```

Or use the engine directly:

```python
from docextract import extract_document

result = extract_document(open("samples/invoice.txt").read())
print(result["doc_type"])                 # "invoice"
print(result["totals"]["validated"])      # True
```

## What it demonstrates

- **Agentic pipeline design** — ordered, single-responsibility stages that each
  emit a trace event, so the system's reasoning is observable and debuggable.
- **Document AI / IDP** — turning unstructured RFQs, invoices, and delivery
  notes into validated, structured records.
- **Provider abstraction** — a pluggable `LLMProvider` interface with an offline
  `MockProvider` default and an opt-in `AnthropicProvider`. Swap the provider,
  not the pipeline, to go from heuristic demo to a real model.
- **Confidence & validation** — per-field scores plus a totals cross-check that
  boosts confidence when the numbers reconcile and flags them when they don't.
- **Stdlib-only web server** — the whole app (server + API + static UI) runs on
  `http.server` with zero third-party packages.
- **Interactive, offline UI** — vanilla JS, no CDN, no build, with a live trace,
  color-coded confidence badges, and JSON/CSV export.
- **Testing discipline** — a `pytest` suite over the three samples plus
  robustness tests that feed the pipeline garbage and assert it never crashes.

## Limitations (honest)

- **The extraction is a heuristic mock, not a real LLM.** It uses regex and
  column-aware table parsing tuned for clean, well-structured documents. It is
  deliberately deterministic so the demo runs offline with no key.
- Messy scans, OCR noise, multi-page tables, unusual layouts, and languages
  beyond the sample style will degrade or miss fields.
- **For production, swap the provider.** The pipeline is already built around
  `LLMProvider`; set `DOCEXTRACT_PROVIDER=anthropic` (and `ANTHROPIC_API_KEY`)
  and route each stage's extraction through a real model. The stages, trace,
  confidence model, and validation logic stay exactly the same.

## Tests

```bash
pip install pytest ruff
ruff check .
pytest -q
```

## About this project — Dimitres Kisimov

Built to demonstrate the skills behind **document automation for a Data & AI
role** — specifically the Würth *"Data & AI — (Agentic) Automation with Low-code
Platforms"* context, where agentic document processing (unstructured input →
structured, validated output that a low-code workflow can consume) is the
central task.

It complements a sibling project, *agentic-automation-lab*; this repo is the
**"unstructured document → structured data, interactively"** piece.

Skills on show:

- Designing **agentic pipelines** with observable, traced reasoning.
- **Intelligent Document Processing** — classification, field/line-item
  extraction, and total validation.
- Clean **provider abstraction** so a heuristic prototype upgrades to a real LLM
  without rework.
- Shipping a **dependency-free, offline** full-stack tool (server + API + UI).
- **Testing and CI** discipline — linting, unit tests, and a live server smoke
  test on multiple Python versions.

## License

MIT © 2026 Dimitres Kisimov — see [LICENSE](LICENSE).
