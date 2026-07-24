"""make_onepager.py — build the executive one-pager PDF for doc-extract-agent.

Renders a single-page executive summary (situation, quantified problem,
solution, ROI, recommendation) to ``deliverables/executive_onepager.pdf`` using
matplotlib's PdfPages. The operational figures are the synthetic estimates from
``docs/BUSINESS_CASE.md``; the confidence-gate outcomes in the "Solution" panel
are pulled live from the real pipeline so the sheet can never drift from the
engine's actual behaviour.

Run:
    pip install matplotlib          # only needed for this deliverable
    python scripts/make_onepager.py

matplotlib is intentionally NOT a runtime dependency of the engine — it is used
only to build this PDF, so the core stays standard-library only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # so `docextract` imports when run as a script

import matplotlib  # noqa: E402

matplotlib.use("Agg")  # headless — no display needed
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

from docextract import extract_document  # noqa: E402

OUT = REPO / "deliverables" / "executive_onepager.pdf"

# --- Synthetic operational assumptions (mirror docs/BUSINESS_CASE.md) --------
DOCS_PER_YEAR = 60_000
MIN_PER_DOC_MANUAL = 4
WAGE = 32
ERROR_RATE = 0.03
COST_PER_ERROR = 25
STRAIGHT_THROUGH_SHARE = 0.60

INK = "#1a2332"
ACCENT = "#1f6feb"
GOOD = "#1a7f4b"
MUTED = "#5b6672"
RULE = "#d0d7de"


def gate_outcomes() -> list[tuple[str, str, float, bool, str]]:
    """Run the real pipeline on the shipped samples for the solution panel."""
    rows = []
    for name in ("invoice", "rfq_email", "delivery_note"):
        text = (REPO / "samples" / f"{name}.txt").read_text(encoding="utf-8")
        r = extract_document(text)
        validated = bool(r["totals"].get("validated"))
        conf = float(r["confidence"])
        clears = conf >= 0.85 and validated
        outcome = "straight-through" if clears else "review"
        rows.append((f"{name}.txt", str(r["doc_type"]), conf, validated, outcome))
    return rows


def compute_impact() -> dict:
    manual_hours = DOCS_PER_YEAR * MIN_PER_DOC_MANUAL / 60
    manual_labour = manual_hours * WAGE
    errored = DOCS_PER_YEAR * ERROR_RATE
    error_cost = errored * COST_PER_ERROR
    # after automation: audit on straight-through + ~45s review on the tail
    audit_h = STRAIGHT_THROUGH_SHARE * DOCS_PER_YEAR * 0.10 * 30 / 3600
    review_h = (1 - STRAIGHT_THROUGH_SHARE) * DOCS_PER_YEAR * 45 / 3600
    new_hours = round(audit_h + review_h + 100)  # + overhead buffer
    new_labour = new_hours * WAGE
    labour_saving = manual_labour - new_labour
    residual_error_cost = error_cost * 0.30
    error_saving = error_cost - residual_error_cost
    return {
        "manual_hours": manual_hours,
        "manual_labour": manual_labour,
        "error_cost": error_cost,
        "new_hours": new_hours,
        "labour_saving": labour_saving,
        "error_saving": error_saving,
        "total_benefit": labour_saving + error_saving,
        "hours_freed": manual_hours - new_hours,
    }


def eur(x: float) -> str:
    return f"€{x:,.0f}"


def build() -> None:
    impact = compute_impact()
    gate = gate_outcomes()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(OUT) as pdf:
        fig = plt.figure(figsize=(8.27, 11.69))  # A4 portrait
        fig.patch.set_facecolor("white")
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

        def text(x, y, s, size=10, color=INK, weight="normal", style="normal", ha="left"):
            ax.text(x, y, s, fontsize=size, color=color, weight=weight,
                    style=style, ha=ha, va="top", transform=ax.transAxes)

        def rule(y):
            ax.plot([0.07, 0.93], [y, y], color=RULE, lw=0.8, transform=ax.transAxes)

        # Header
        text(0.07, 0.965, "Executive one-pager", 22, INK, "bold")
        text(0.07, 0.930, "Automating inbound document capture in Accounts Payable",
             13, ACCENT, "bold")
        text(0.07, 0.908, "doc-extract-agent  ·  Kessler Industrieteile GmbH (worked example)",
             9.5, MUTED)
        rule(0.895)

        # Headline metric band
        text(0.07, 0.878, "≈ " + eur(110_000) + " / yr", 26, GOOD, "bold")
        text(0.07, 0.838, "of AP capacity freed — handling per document drops from "
             "~4 minutes to under a second.", 10.5, INK)

        # Situation
        y = 0.805
        text(0.07, y, "Situation", 12, ACCENT, "bold")
        text(0.07, y - 0.028,
             "A three-person AP & order-desk team keys ~60,000 inbound invoices, order\n"
             "confirmations, and delivery notes into the ERP by hand each year — slow,\n"
             "unscalable, and mistyped figures surface only later as disputes and rework.",
             10, INK)

        # Quantified problem
        y = 0.720
        text(0.07, y, "The problem, quantified", 12, ACCENT, "bold")
        prob = [
            ("Documents / year", "60,000  (~250 / business day)"),
            ("Manual handling", f"4 min/doc  →  {impact['manual_hours']:,.0f} h/yr  =  "
             f"{eur(impact['manual_labour'])}/yr labour"),
            ("Keying errors", f"3% × 60,000 = 1,800 docs  →  {eur(impact['error_cost'])}/yr"),
            ("Cost of the manual process", f"≈ {eur(impact['manual_labour'] + impact['error_cost'])}/yr"),
        ]
        yy = y - 0.026
        for label, val in prob:
            text(0.09, yy, "•", 10, MUTED)
            text(0.12, yy, label + ":", 10, INK, "bold")
            text(0.42, yy, val, 10, INK)
            yy -= 0.024

        # Solution + live gate table
        y = 0.595
        text(0.07, y, "Solution — deterministic pipeline with a confidence gate", 12, ACCENT, "bold")
        text(0.07, y - 0.026,
             "Five traced stages per document (detect → header → line_items → totals →\n"
             "confidence). The totals cross-check sums the parsed lines against the stated\n"
             "total; only documents scoring ≥ 0.85 with validated totals auto-post — the rest\n"
             "route to a clerk. Live outcomes on the shipped samples:", 10, INK)

        # gate table
        ty = y - 0.116
        cols = [0.09, 0.30, 0.47, 0.63, 0.78]
        heads = ["Sample", "doc_type", "confidence", "validated", "outcome"]
        for cx, h in zip(cols, heads, strict=True):
            text(cx, ty, h, 9, MUTED, "bold")
        ty -= 0.020
        rule(ty + 0.008)
        for sample, dtype, conf, validated, outcome in gate:
            text(cols[0], ty, sample, 9, INK)
            text(cols[1], ty, dtype, 9, INK)
            text(cols[2], ty, f"{conf:.2f}", 9, INK)
            text(cols[3], ty, "yes" if validated else "—", 9, INK)
            oc_color = GOOD if outcome == "straight-through" else MUTED
            text(cols[4], ty, outcome, 9, oc_color, "bold")
            ty -= 0.020

        # Impact / ROI
        y = 0.385
        text(0.07, y, "Impact / ROI  (modeled estimate)", 12, ACCENT, "bold")
        roi = [
            ("Hours/year", f"{impact['manual_hours']:,.0f} h  →  ~{impact['new_hours']:,} h   "
             f"(≈ {impact['hours_freed']:,.0f} h freed)"),
            ("Labour saving", f"≈ {eur(impact['labour_saving'])}/yr"),
            ("Error reduction", f"≈ {eur(impact['error_saving'])}/yr"),
            ("Total benefit", f"≈ {eur(impact['total_benefit'])}/yr"),
            ("Payback", "~3 months on labour alone vs. an est. €30k rollout + €500/mo"),
        ]
        yy = y - 0.028
        for label, val in roi:
            bold = label in ("Total benefit",)
            text(0.09, yy, "•", 10, MUTED)
            text(0.12, yy, label + ":", 10, INK, "bold")
            text(0.40, yy, val, 10, GOOD if bold else INK, "bold" if bold else "normal")
            yy -= 0.026

        # Recommendation
        y = 0.215
        rule(y + 0.020)
        text(0.07, y, "Recommendation", 12, ACCENT, "bold")
        text(0.07, y - 0.028,
             "Pilot the pipeline on live invoices at a 0.85 gate with human review on the tail.\n"
             "Auto-post the ~60% that reconcile, measure the true straight-through rate, then\n"
             "widen coverage. Labour payback lands inside a quarter; error and DSO gains follow.",
             10, INK)

        # Footer
        text(0.07, 0.055,
             "Estimates are labelled; company, volumes, and rates are synthetic and adjustable. "
             "Extraction, confidence\nscoring, and the totals cross-check are real and reproducible "
             "from this repository. See docs/BUSINESS_CASE.md.",
             8, MUTED, style="italic")

        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

    print(f"Wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    build()
