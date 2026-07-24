# Business case — automating inbound document capture in Accounts Payable

*Worked example. The company is fictional and the operational figures are
estimates, labelled as such throughout. The extraction behaviour, confidence
scoring, and totals cross-check referenced below are real and reproducible from
this repository's samples.*

## Situation

**Kessler Industrieteile GmbH** is a mid-size industrial-parts distributor. Its
three-person AP & order-desk team keys inbound business documents into the ERP
by hand all day: supplier **invoices**, **order confirmations**, and **delivery
notes** arrive as PDFs and email bodies. A clerk opens each one, reads it,
retypes the header fields (document number, dates, currency, parties), keys the
line-item table, and eyeballs the stated totals against the lines before posting.

It works, but it is slow, it does not scale with volume, and a mistyped figure
is only caught downstream — as a payment dispute, a wrong stock receipt, or a
supplier chasing an unpaid invoice.

## Problem, quantified

Assumptions (synthetic, chosen to be conservative and easy to adjust):

| Assumption | Value |
|---|---|
| Inbound documents / year | 60,000 (~250 per business day) |
| Manual handling time / document | 4 minutes (open, read, key header + lines, check totals) |
| Fully-loaded labour rate | €32 / hour |
| Manual keying error rate | 3% of documents |
| Fully-loaded cost per error | €25 (correction time + downstream dispute/rework) |

Arithmetic:

- **Hours/year keying:** 60,000 × 4 min ÷ 60 = **4,000 h/yr**
- **Labour cost:** 4,000 h × €32 = **€128,000/yr**
- **Errored documents:** 3% × 60,000 = **1,800 docs/yr**
- **Error cost:** 1,800 × €25 = **€45,000/yr**
- **Total cost of the manual process ≈ €173,000/yr**

On top of the euros: because keying runs as a same-week backlog rather than
same-day, invoices are posted late, which lengthens **invoice-to-post latency**,
puts early-payment discounts (e.g. 2% / 10 days) at risk, and adds noise to DSO.

## Solution

`doc-extract-agent` turns the "unstructured document in, structured record out"
step into a deterministic pipeline. Five stages run per document — `detect`,
`header`, `line_items`, `totals`, `confidence` — each emitting a trace event, and
each field carries its own confidence score. Extraction is **sub-second** and
runs offline with no API key; the same pipeline can be routed through a real LLM
provider (`DOCEXTRACT_PROVIDER=anthropic`) without changing the stages, trace, or
validation logic.

The load-bearing part for AP is the **totals cross-check**: the engine sums the
parsed line items and compares that to the stated subtotal/total. When they
reconcile (within a small tolerance) the matched figure is promoted to 0.99
confidence and the document is flagged `validated`.

### Confidence gating (real behaviour, straight-through vs. review)

The gate is what makes this safe to trust with posting: only documents that
clear it post automatically; everything else routes to a clerk.

- **Straight-through (auto-post):** overall confidence ≥ 0.85 **and** totals
  cross-validated.
- **Review (human-in-the-loop):** anything below the threshold, or where the
  totals do not reconcile.

This is not aspirational — it is how the shipped samples actually score:

| Sample | doc_type | Overall confidence | Totals validated | Outcome |
|---|---|---|---|---|
| `invoice.txt` | invoice | 0.94 | ✅ yes | straight-through |
| `rfq_email.txt` | rfq | 0.86 | — (no totals) | review |
| `delivery_note.txt` | delivery_note | 0.86 | — (no totals) | review |

The invoice reconciles (line items sum to the €105.00 subtotal) and clears the
gate; the RFQ and delivery note carry no totals to cross-check, so they route to
a human — exactly the conservative default AP needs.

## Impact / ROI

Modeled outcome (estimates; assumes 60% of documents clear the gate and post
straight-through, the rest get a pre-filled ~45-second review):

- **Handling time per document:** ~4 minutes → **under a second** of extraction
  plus selective review.
- **Hours/year:** 4,000 h → **~450 h** (audit + reviewed tail) — roughly
  **3,550 hours freed**.
- **Labour saving:** €128,000 → ~€14,400 → **≈ €114,000/yr**.
- **Error reduction:** cross-validation blocks total mismatches before posting;
  residual error rate ~0.9% → error cost €45,000 → ~€13,500, **≈ €31,500/yr saved**.
- **Total modeled benefit ≈ €145,000/yr**, of which **~€110k/yr** is the
  conservative labour-only figure used as the headline.
- **Payback:** against an estimated one-off rollout of €30,000 (ERP integration,
  review UI, provider wiring) plus €500/mo running cost, payback on the labour
  saving alone is **~3 months**.

Faster posting also shortens invoice-to-post latency and protects early-payment
discounts — upside noted but deliberately left out of the headline number.

## Stakeholders & use case

- **AP / order-desk clerks** — stop retyping; confirm the pre-filled tail.
- **AP team lead** — owns the confidence threshold and the straight-through rate.
- **Financial controller** — wants the totals reconciled before anything posts.
- **COO / Head of Operations** — sponsors the automation budget.

Numbered workflow:

1. Document arrives (PDF/email) and its text is handed to the pipeline.
2. `detect` classifies it (invoice / delivery note / RFQ).
3. `header` extracts document number, dates, currency, and parties.
4. `line_items` parses the table into positions, quantities, prices, amounts.
5. `totals` extracts the stated totals and cross-checks them against the summed
   lines; a match flips `validated` and promotes that figure to 0.99.
6. `confidence` rolls everything into one overall score.
7. **Gate:** score ≥ 0.85 and `validated` → auto-post to ERP; otherwise route to
   a clerk with every field pre-filled and its confidence shown.
8. The clerk corrects only the flagged fields; the trace explains each decision.

## Deliverable

- This repository: the offline engine, HTTP API, and browser UI.
- [`deliverables/executive_onepager.pdf`](../deliverables/executive_onepager.pdf)
  — a one-page executive summary of the situation, quantified problem, solution,
  and ROI, generated by [`scripts/make_onepager.py`](../scripts/make_onepager.py).

---

*Honesty notes: the extraction, confidence scoring, and totals cross-check are
real and reproducible. Company, volumes, rates, error rates, and the
straight-through share are synthetic estimates chosen to be conservative and are
adjustable — swap in your own before making a decision.*
