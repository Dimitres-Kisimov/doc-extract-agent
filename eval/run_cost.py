"""Price the measured gate policies in euros and write ``eval/cost_results.json``.

Usage::

    python -m eval.run_cost

Reads the measured operating points from ``eval/results.json`` (run
``python -m eval.run_eval`` first if it is missing) and the cost parameters
documented in ``docs/BUSINESS_CASE.md``, prices every gating policy the
harness measured — manual keying, auto-post everything, the confidence gate
alone, the gate plus business-rule validation, the strictest 100%-precision
threshold, and review-everything — and reports the break-even precision
above which auto-posting a document pays at all.

Prints an ASCII report and writes ``eval/cost_results.json``. Fully
deterministic (no clock, no RNG): the file is byte-identical run to run.
The euros are **modeled** (business-case parameters), applied to **measured**
outcomes; see the NOTE at the end of the report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python eval/run_cost.py` from anywhere
    sys.path.insert(0, str(ROOT))

from docextract.costmodel import POLICY_ORDER, cost_report  # noqa: E402

RESULTS_PATH = ROOT / "eval" / "results.json"
COST_RESULTS_PATH = ROOT / "eval" / "cost_results.json"


def _eur(value: float) -> str:
    return f"{value:,.2f}"


def format_cost_report(report: dict) -> str:
    """Render the cost report as the same ASCII style ``run_eval`` uses."""
    lines: list[str] = []
    add = lines.append
    p = report["parameters"]
    be = report["break_even"]

    add("== doc-extract-agent cost model (modeled EUR over the measured operating points) ==")
    add(f"Operating points: eval/results.json ({report['source_documents']} labelled documents); "
        "cost parameters: docs/BUSINESS_CASE.md")
    add(f"volume {p['annual_volume']:,.0f} docs/yr, labour EUR {p['labour_rate_per_hour']:.2f}/h, "
        f"manual keying {p['manual_minutes_per_doc']:.1f} min/doc "
        f"(error rate {p['manual_error_rate']:.1%}), "
        f"silent error EUR {p['silent_error_cost']:.2f}, "
        f"pre-filled review {p['review_seconds_per_doc']:.0f} s "
        f"(EUR {be['review_cost_per_doc']:.2f}/doc)")
    add("")

    add("-- Policies (measured mix priced per document and scaled to annual volume) --")
    add(f"{'policy':<24}{'auto':>6}{'errors':>8}{'review':>8}{'EUR/doc':>10}{'EUR/yr':>15}{'vs manual':>15}")
    for name in POLICY_ORDER:
        policy = report["policies"].get(name)
        if policy is None:
            continue
        if name == "manual_keying":
            add(f"{name:<24}{'-':>6}{'-':>8}{'-':>8}{policy['cost_per_doc']:>10.4f}"
                f"{_eur(policy['annual_cost']):>15}{'-':>15}")
        else:
            add(f"{name:<24}{policy['auto_post']:>6}{policy['auto_post_errors']:>8}{policy['review']:>8}"
                f"{policy['cost_per_doc']:>10.4f}{_eur(policy['annual_cost']):>15}"
                f"{policy['annual_saving_vs_manual']:>+15,.2f}")
    strict = report["policies"].get("strict_gate_100pct")
    if strict is not None:
        add(f"strict_gate_100pct posts only at confidence >= {strict['threshold']:.4f} "
            "(lowest threshold measured at 100% auto-post precision on this set)")
    cheapest = report["cheapest_policy"]
    add(f"cheapest policy on this set: {cheapest} "
        f"(EUR {_eur(report['policies'][cheapest]['annual_cost'])}/yr, modeled)")
    add("")

    add(f"-- Break-even: when does skipping the {p['review_seconds_per_doc']:.0f}-second review pay? --")
    add(f"auto-posting a document saves the EUR {be['review_cost_per_doc']:.2f} review and risks "
        f"EUR {be['silent_error_cost']:.2f} per silent error")
    add(f"-> auto-post pays only above {be['auto_post_precision_required']:.1%} precision")
    add(f"{'policy':<24}{'measured precision':>20}{'pays?':>7}   max error cost to break even")
    for name, slot in be["measured"].items():
        max_cost = ("unbounded" if slot["max_silent_error_cost"] is None
                    else f"EUR {slot['max_silent_error_cost']:,.2f}")
        pays = "yes" if slot["pays_at_this_precision"] else "no"
        add(f"{name:<24}{slot['auto_post_precision']:>20.1%}{pays:>7}   {max_cost}")
    if strict is not None and "strict_gate_100pct" in be["measured"]:
        add(f"(caution: the strict gate's 100% rests on {strict['auto_post']} auto-posted "
            f"documents - far too small a sample to establish "
            f">={be['auto_post_precision_required']:.1%} precision)")
    add("")

    curve = report["sweep_cost_curve"]
    add("-- Cost curve over the gate-threshold sweep (rule: confidence >= t AND totals validated) --")
    add(f"{'threshold':>10}{'auto':>6}{'errors':>8}{'EUR/doc':>10}{'EUR/yr':>15}")
    for point in curve["points"]:
        add(f"{point['threshold']:>10.4f}{point['auto_post']:>6}{point['auto_post_errors']:>8}"
            f"{point['cost_per_doc']:>10.4f}{_eur(point['annual_cost']):>15}")
    if curve["cheapest"]:
        add(f"cost-minimising threshold on this set: {curve['cheapest']['threshold']:.4f} "
            f"(EUR {_eur(curve['cheapest']['annual_cost'])}/yr)")
    add("")

    add("NOTE: modeled, not measured - business-case cost parameters applied to measured outcomes")
    add("on a small synthetic set that over-represents edge cases by design; reviewed documents")
    add("are assumed corrected within the pre-filled confirm; annual figures are an illustrative")
    add("scaling of the measured mix, not a forecast.")
    return "\n".join(lines)


def run(
    results_path: Path | str = RESULTS_PATH,
    out_path: Path | str = COST_RESULTS_PATH,
) -> tuple[dict, str]:
    """Build the cost report from ``results_path`` and write it to ``out_path``."""
    results_path = Path(results_path)
    if not results_path.exists():
        raise SystemExit(
            f"{results_path} not found - run `python -m eval.run_eval` first to measure "
            "the operating points the cost model prices"
        )
    summary = json.loads(results_path.read_text(encoding="utf-8"))
    report = cost_report(summary)
    text = format_cost_report(report)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return report, text


def main() -> None:
    _report, text = run()
    print(text)
    print(f"\ncost results written to {COST_RESULTS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
