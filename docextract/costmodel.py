"""The extraction-error cost model — the gate priced in euros.

The evaluation harness measures *how often* each gating policy posts a wrong
document (``eval/run_eval.py``); the business case documents *what a document
costs* to key, review, and correct (``docs/BUSINESS_CASE.md``). This module
joins the two: it prices each policy's measured operating point in euros, so
"the combined gate lifts precision from 70% to 87.5%" becomes an annual figure
a controller can argue with — and, just as honestly, "at these rates
auto-posting pays only above 98.4% precision, which no measured policy
credibly reaches".

The model is deliberately small — three cost mechanisms, nothing hidden:

* a document keyed **by hand** costs keying labour plus an expected
  keying-error cost (the manual baseline from the business case);
* a document routed to **review** costs the pre-filled confirm (review
  labour) and is assumed corrected there — no residual error cost;
* an **auto-posted** document costs nothing when the extraction was fully
  correct and the silent-error figure (correction + downstream dispute)
  when it was not.

That yields one closed-form number per policy::

    cost/doc = review_share x review_cost + silent_error_share x error_cost

and one break-even: auto-posting a document saves the review cost and risks
``(1 - precision) x error_cost``, so it pays only when precision exceeds
``1 - review_cost / error_cost`` — 98.4% at the default EUR 0.40 review
against the EUR 25 silent error.

Everything here is **modeled, not measured**: the parameters are the business
case's synthetic estimates, and the operating points come from a small
synthetic document set that deliberately over-represents edge cases. The
arithmetic is exact as computed; the euros are illustrative. All functions are
pure and deterministic (no clock, no RNG).

Example::

    import json
    from docextract.costmodel import cost_report

    summary = json.load(open("eval/results.json", encoding="utf-8"))
    report = cost_report(summary)
    report["policies"]["gate_plus_validation"]["annual_cost"]  # 72444.44
    report["break_even"]["auto_post_precision_required"]       # 0.984
"""

from __future__ import annotations

# The cost parameters documented in docs/BUSINESS_CASE.md — synthetic,
# conservative estimates, every one of them overridable per call.
DEFAULT_COST_PARAMS: dict[str, float] = {
    "annual_volume": 60000,          # inbound documents per year
    "labour_rate_per_hour": 32.0,    # fully-loaded clerk rate, EUR
    "manual_minutes_per_doc": 4.0,   # open, read, key header + lines, check totals
    "manual_error_rate": 0.03,       # share of hand-keyed documents with an error
    "silent_error_cost": 25.0,       # correction + downstream dispute, EUR per error
    "review_seconds_per_doc": 45.0,  # pre-filled review confirm
}

# Canonical policy order for reports (JSON files sort keys; tables should not).
POLICY_ORDER = (
    "manual_keying",
    "auto_post_everything",
    "gate_only",
    "gate_plus_validation",
    "strict_gate_100pct",
    "review_everything",
)


def _resolve_params(params: dict[str, float] | None) -> dict[str, float]:
    """Merge ``params`` over the defaults and validate every value."""
    if params is None:
        return dict(DEFAULT_COST_PARAMS)
    unknown = sorted(set(params) - set(DEFAULT_COST_PARAMS))
    if unknown:
        raise ValueError(f"unknown cost parameter(s): {', '.join(unknown)}")
    merged = {**DEFAULT_COST_PARAMS, **params}
    for name, value in merged.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"cost parameter {name} must be a number, got {value!r}")
        if value < 0:
            raise ValueError(f"cost parameter {name} must be >= 0, got {value!r}")
    if merged["manual_error_rate"] > 1:
        raise ValueError("manual_error_rate is a share of documents and must be <= 1")
    return merged


def review_cost_per_doc(params: dict[str, float] | None = None) -> float:
    """Cost of one pre-filled review confirm (labour only), unrounded."""
    p = _resolve_params(params)
    return p["review_seconds_per_doc"] / 3600.0 * p["labour_rate_per_hour"]


def manual_cost(params: dict[str, float] | None = None) -> dict[str, float]:
    """The manual-keying baseline: every document hand-keyed, no automation.

    Returns per-document and annual figures split into keying labour and the
    expected keying-error cost — the arithmetic in ``docs/BUSINESS_CASE.md``
    (EUR 2.8833/doc, EUR 173,000/yr at the defaults), computed rather than
    quoted.
    """
    p = _resolve_params(params)
    keying_hours_per_doc = p["manual_minutes_per_doc"] / 60.0
    per_doc_labour = keying_hours_per_doc * p["labour_rate_per_hour"]
    per_doc_errors = p["manual_error_rate"] * p["silent_error_cost"]
    volume = p["annual_volume"]
    return {
        "cost_per_doc": round(per_doc_labour + per_doc_errors, 4),
        "cost_per_doc_keying_labour": round(per_doc_labour, 4),
        "cost_per_doc_keying_errors": round(per_doc_errors, 4),
        "annual_cost": round((per_doc_labour + per_doc_errors) * volume, 2),
        "annual_labour_cost": round(per_doc_labour * volume, 2),
        "annual_error_cost": round(per_doc_errors * volume, 2),
        "annual_keying_hours": round(keying_hours_per_doc * volume, 2),
    }


def policy_cost(
    documents: int,
    auto_post: int,
    auto_post_errors: int,
    params: dict[str, float] | None = None,
) -> dict[str, object]:
    """Price one gating policy from its measured operating point.

    Args:
        documents: Size of the measured set the shares are taken from.
        auto_post: Documents the policy auto-posts.
        auto_post_errors: Auto-posted documents that were *not* fully correct —
            each becomes a silent error at ``silent_error_cost``.
        params: Cost-parameter overrides (see :data:`DEFAULT_COST_PARAMS`).

    Returns:
        Counts, the per-document cost split into review labour and silent
        errors, and the figures scaled to ``annual_volume`` (annual cost,
        review hours, silent errors). ``auto_post_precision`` is ``None``
        when nothing auto-posts.
    """
    p = _resolve_params(params)
    if documents <= 0:
        raise ValueError("documents must be positive - an empty set has no cost per document")
    if not 0 <= auto_post <= documents:
        raise ValueError(f"auto_post must be within [0, {documents}], got {auto_post}")
    if not 0 <= auto_post_errors <= auto_post:
        raise ValueError(
            f"auto_post_errors must be within [0, auto_post={auto_post}], got {auto_post_errors}"
        )
    review = documents - auto_post
    review_share = review / documents
    error_share = auto_post_errors / documents
    per_doc_review = review_share * review_cost_per_doc(p)
    per_doc_errors = error_share * p["silent_error_cost"]
    per_doc = per_doc_review + per_doc_errors
    volume = p["annual_volume"]
    precision = round(1.0 - auto_post_errors / auto_post, 4) if auto_post else None
    return {
        "documents": documents,
        "auto_post": auto_post,
        "auto_post_errors": auto_post_errors,
        "review": review,
        "auto_post_precision": precision,
        "cost_per_doc": round(per_doc, 4),
        "cost_per_doc_review_labour": round(per_doc_review, 4),
        "cost_per_doc_silent_errors": round(per_doc_errors, 4),
        "annual_cost": round(per_doc * volume, 2),
        "annual_review_hours": round(review_share * volume * p["review_seconds_per_doc"] / 3600.0, 2),
        "annual_silent_errors": round(error_share * volume, 2),
    }


def break_even_precision(params: dict[str, float] | None = None) -> float:
    """The auto-post precision above which skipping the review pays.

    Auto-posting a document saves one review confirm and risks
    ``(1 - precision) x silent_error_cost``, so the break-even is
    ``1 - review_cost / error_cost`` (clamped to 0 when errors are cheaper
    than reviews — then auto-posting always pays).
    """
    p = _resolve_params(params)
    if p["silent_error_cost"] <= 0:
        return 0.0  # errors are free: auto-posting always pays
    return round(max(0.0, 1.0 - review_cost_per_doc(p) / p["silent_error_cost"]), 4)


def break_even_error_cost(
    precision: float, params: dict[str, float] | None = None
) -> float | None:
    """The silent-error cost below which a policy at ``precision`` pays.

    The dual of :func:`break_even_precision`: at a measured precision ``q``,
    auto-posting beats reviewing only while ``(1 - q) x error_cost`` stays
    under the review cost, i.e. while the error cost is under
    ``review_cost / (1 - q)``. Returns ``None`` for ``precision == 1.0``
    (a perfect poster tolerates any error cost).
    """
    if not 0.0 <= precision <= 1.0:
        raise ValueError(f"precision must be within [0, 1], got {precision}")
    p = _resolve_params(params)
    if precision == 1.0:
        return None
    return round(review_cost_per_doc(p) / (1.0 - precision), 4)


def cost_report(
    summary: dict[str, object], params: dict[str, float] | None = None
) -> dict[str, object]:
    """Price every gating policy measured by the evaluation harness.

    Consumes an evaluation ``summary`` (the ``eval/results.json`` shape —
    nothing is re-extracted or re-scored) and returns:

    * ``policies`` — the manual baseline plus five automated policies at
      their *measured* operating points: auto-post everything (the reckless
      bound), the confidence gate alone, the gate combined with the
      business-rule validation layer, the strictest 100%-precision threshold
      from the sweep (omitted when the sweep found none), and
      review-everything (pre-fill all, auto-post nothing). Each carries its
      annual saving against the manual baseline (negative = worse than
      manual).
    * ``cheapest_policy`` — the lowest annual cost (first in
      :data:`POLICY_ORDER` on a tie).
    * ``break_even`` — the precision auto-posting must exceed to pay at all,
      and per measured policy whether it clears that bar plus the highest
      silent-error cost it could tolerate.
    * ``sweep_cost_curve`` — the gate-threshold sweep priced point by point,
      with the cost-minimising threshold (lowest threshold on a tie).
    """
    p = _resolve_params(params)
    documents = int(summary["documents_scored"])  # type: ignore[arg-type]
    gate: dict = summary["gate"]  # type: ignore[assignment]
    combined: dict = summary["validation"]["combined_gate"]  # type: ignore[index]
    fully_correct = int(summary["failure_modes"]["fully_correct_documents"])  # type: ignore[index]

    manual = manual_cost(p)
    policies: dict[str, dict] = {
        "manual_keying": manual,
        "auto_post_everything": policy_cost(documents, documents, documents - fully_correct, p),
        "gate_only": policy_cost(
            documents, gate["auto_post"], gate["auto_post"] - gate["auto_post_fully_correct"], p
        ),
        "gate_plus_validation": policy_cost(
            documents,
            combined["combined_auto_post"],
            combined["combined_auto_post"] - combined["combined_auto_post_fully_correct"],
            p,
        ),
    }
    best = summary["sweep"].get("min_threshold_for_100pct")  # type: ignore[union-attr]
    if best:
        strict = policy_cost(
            documents, best["auto_post"], best["auto_post"] - best["fully_correct"], p
        )
        strict["threshold"] = best["threshold"]
        policies["strict_gate_100pct"] = strict
    policies["review_everything"] = policy_cost(documents, 0, 0, p)

    for name, policy in policies.items():
        if name != "manual_keying":
            policy["annual_saving_vs_manual"] = round(
                manual["annual_cost"] - policy["annual_cost"], 2
            )

    # min() keeps the first minimum in iteration (= POLICY_ORDER) order.
    cheapest = min(policies, key=lambda name: policies[name]["annual_cost"])

    required = break_even_precision(p)
    measured: dict[str, dict] = {}
    for name in ("auto_post_everything", "gate_only", "gate_plus_validation", "strict_gate_100pct"):
        policy = policies.get(name)
        if policy is None or policy["auto_post_precision"] is None:
            continue
        precision = float(policy["auto_post_precision"])  # type: ignore[arg-type]
        measured[name] = {
            "auto_post_precision": precision,
            "pays_at_this_precision": precision >= required,
            "max_silent_error_cost": break_even_error_cost(precision, p),
        }
    break_even = {
        "review_cost_per_doc": round(review_cost_per_doc(p), 4),
        "silent_error_cost": p["silent_error_cost"],
        "auto_post_precision_required": required,
        "measured": measured,
    }

    curve: list[dict] = []
    for point in summary["sweep"]["points"]:  # type: ignore[index]
        errors = point["auto_post"] - point["fully_correct"]
        priced = policy_cost(documents, point["auto_post"], errors, p)
        curve.append({
            "threshold": point["threshold"],
            "auto_post": point["auto_post"],
            "auto_post_errors": errors,
            "cost_per_doc": priced["cost_per_doc"],
            "annual_cost": priced["annual_cost"],
        })
    # min() keeps the first minimum; the sweep is threshold-ascending, so a
    # cost tie resolves to the *lowest* (highest-volume) threshold.
    cheapest_point = min(curve, key=lambda c: c["annual_cost"]) if curve else None

    return {
        "source_documents": documents,
        "parameters": dict(sorted(p.items())),
        "policies": policies,
        "cheapest_policy": cheapest,
        "break_even": break_even,
        "sweep_cost_curve": {"points": curve, "cheapest": cheapest_point},
    }
