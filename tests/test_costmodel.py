"""Tests for the extraction-error cost model: hand-checked euros, break-evens,
collapse-to-base-case behaviour, and the committed cost report."""

from __future__ import annotations

import json
import pathlib

import pytest

from docextract.costmodel import (
    DEFAULT_COST_PARAMS,
    POLICY_ORDER,
    break_even_error_cost,
    break_even_precision,
    cost_report,
    manual_cost,
    policy_cost,
    review_cost_per_doc,
)
from eval.run_cost import run

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _committed_summary() -> dict:
    return json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))


def _committed_cost_results() -> dict:
    return json.loads((ROOT / "eval" / "cost_results.json").read_text(encoding="utf-8"))


# --- hand-checked model arithmetic -------------------------------------------

def test_review_cost_per_doc_matches_business_case() -> None:
    # 45 seconds of a EUR 32/h clerk = 45/3600 * 32 = EUR 0.40 exactly.
    assert review_cost_per_doc() == pytest.approx(0.40)


def test_manual_baseline_matches_business_case_arithmetic() -> None:
    # docs/BUSINESS_CASE.md, computed rather than quoted:
    # 4 min at EUR 32/h = EUR 2.1333/doc labour; 3% x EUR 25 = EUR 0.75/doc errors;
    # 60,000 docs/yr -> EUR 128,000 + EUR 45,000 = EUR 173,000/yr over 4,000 hours.
    manual = manual_cost()
    assert manual["cost_per_doc_keying_labour"] == 2.1333
    assert manual["cost_per_doc_keying_errors"] == 0.75
    assert manual["cost_per_doc"] == 2.8833
    assert manual["annual_labour_cost"] == 128000.0
    assert manual["annual_error_cost"] == 45000.0
    assert manual["annual_cost"] == 173000.0
    assert manual["annual_keying_hours"] == 4000.0


def test_policy_cost_hand_checked_on_the_gate_operating_point() -> None:
    # The measured gate-only point: 27 docs, 10 auto-posted, 3 of them wrong.
    # cost/doc = (17/27) x 0.40 + (3/27) x 25 = (6.80 + 75.00)/27 = 81.80/27.
    policy = policy_cost(27, 10, 3)
    assert policy["review"] == 17
    assert policy["auto_post_precision"] == 0.7
    assert policy["cost_per_doc_review_labour"] == round(17 / 27 * 0.40, 4)  # 0.2519
    assert policy["cost_per_doc_silent_errors"] == round(3 / 27 * 25.0, 4)  # 2.7778
    assert policy["cost_per_doc"] == round(81.80 / 27, 4)  # 3.0296
    assert policy["annual_cost"] == 181777.78
    # 17/27 of 60,000 docs at 45 s each = 17/27 x 750 h = 472.22 h of review.
    assert policy["annual_review_hours"] == 472.22
    assert policy["annual_silent_errors"] == 6666.67


def test_policy_cost_review_everything_is_pure_review_labour() -> None:
    policy = policy_cost(27, 0, 0)
    assert policy["auto_post_precision"] is None
    assert policy["cost_per_doc"] == 0.4
    assert policy["cost_per_doc_silent_errors"] == 0.0
    assert policy["annual_cost"] == 24000.0
    assert policy["annual_silent_errors"] == 0.0
    assert policy["annual_review_hours"] == 750.0


def test_policy_cost_rejects_impossible_operating_points() -> None:
    with pytest.raises(ValueError):
        policy_cost(0, 0, 0)  # empty set has no per-document cost
    with pytest.raises(ValueError):
        policy_cost(27, 28, 0)  # more auto-posts than documents
    with pytest.raises(ValueError):
        policy_cost(27, -1, 0)
    with pytest.raises(ValueError):
        policy_cost(27, 10, 11)  # more errors than auto-posts


def test_unknown_or_invalid_params_are_rejected() -> None:
    with pytest.raises(ValueError):
        policy_cost(27, 10, 3, {"eror_cost": 10.0})  # typo must not pass silently
    with pytest.raises(ValueError):
        policy_cost(27, 10, 3, {"labour_rate_per_hour": -1.0})
    with pytest.raises(ValueError):
        policy_cost(27, 10, 3, {"silent_error_cost": "25"})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        manual_cost({"manual_error_rate": 1.5})  # a share, not a multiplier


def test_collapse_to_base_case_free_errors_leave_only_review_labour() -> None:
    # With silent errors priced at zero the model collapses to review labour,
    # and the break-even says auto-posting always pays.
    policy = policy_cost(27, 10, 3, {"silent_error_cost": 0.0})
    assert policy["cost_per_doc"] == round(17 / 27 * 0.40, 4)
    assert policy["cost_per_doc_silent_errors"] == 0.0
    assert break_even_precision({"silent_error_cost": 0.0}) == 0.0


def test_collapse_to_base_case_review_priced_like_keying_is_the_manual_baseline() -> None:
    # Price the review at the full 4-minute keying time and remove keying
    # errors: reviewing everything must cost exactly the manual labour.
    params = {"review_seconds_per_doc": 240.0, "manual_error_rate": 0.0}
    manual = manual_cost(params)
    review_all = policy_cost(27, 0, 0, params)
    assert review_all["cost_per_doc"] == manual["cost_per_doc"] == 2.1333
    assert review_all["annual_cost"] == manual["annual_cost"] == 128000.0


def test_zero_volume_scales_annual_figures_to_zero() -> None:
    policy = policy_cost(27, 10, 3, {"annual_volume": 0})
    assert policy["annual_cost"] == 0.0
    assert policy["annual_review_hours"] == 0.0
    assert policy["annual_silent_errors"] == 0.0
    assert policy["cost_per_doc"] == round(81.80 / 27, 4)  # per-doc cost unaffected


# --- break-evens -------------------------------------------------------------

def test_break_even_precision_hand_checked() -> None:
    # Save EUR 0.40, risk EUR 25: pays only above 1 - 0.40/25 = 98.4%.
    assert break_even_precision() == 0.984
    # A free review means only a perfect poster pays.
    assert break_even_precision({"review_seconds_per_doc": 0.0}) == 1.0
    # Errors cheaper than the review: auto-posting always pays (clamped to 0).
    assert break_even_precision({"silent_error_cost": 0.2}) == 0.0


def test_break_even_error_cost_hand_checked() -> None:
    # At 87.5% precision the error cost must stay under 0.40/0.125 = EUR 3.20;
    # at 70% under 0.40/0.30 = EUR 1.33; a perfect poster tolerates anything.
    assert break_even_error_cost(0.875) == 3.2
    assert break_even_error_cost(0.7) == 1.3333
    assert break_even_error_cost(1.0) is None
    with pytest.raises(ValueError):
        break_even_error_cost(1.2)
    with pytest.raises(ValueError):
        break_even_error_cost(-0.1)


def test_break_even_is_consistent_with_the_policy_costs() -> None:
    # At exactly the break-even precision, the expected silent-error cost of an
    # auto-posted document equals the review cost it saves.
    for params in (None, {"silent_error_cost": 10.0}, {"review_seconds_per_doc": 90.0}):
        merged = {**DEFAULT_COST_PARAMS, **(params or {})}
        precision = 1.0 - review_cost_per_doc(params) / merged["silent_error_cost"]
        expected_error = (1.0 - precision) * merged["silent_error_cost"]
        assert expected_error == pytest.approx(review_cost_per_doc(params))


def test_costs_are_monotonic_in_error_price_and_error_count() -> None:
    base = policy_cost(27, 10, 3)
    dearer = policy_cost(27, 10, 3, {"silent_error_cost": 50.0})
    fewer_errors = policy_cost(27, 10, 2)
    assert dearer["cost_per_doc"] > base["cost_per_doc"]
    assert fewer_errors["cost_per_doc"] < base["cost_per_doc"]


# --- the report over the committed measured results ---------------------------

def test_cost_report_policies_pinned_on_committed_results() -> None:
    report = cost_report(_committed_summary())
    policies = report["policies"]
    assert policies["manual_keying"]["annual_cost"] == 173000.0
    assert policies["auto_post_everything"]["annual_cost"] == 611111.11
    assert policies["gate_only"]["annual_cost"] == 181777.78
    assert policies["gate_plus_validation"]["annual_cost"] == 72444.44
    assert policies["strict_gate_100pct"]["annual_cost"] == 20444.44
    assert policies["strict_gate_100pct"]["threshold"] == 0.9393
    assert policies["review_everything"]["annual_cost"] == 24000.0
    # The operating points are exactly the measured ones.
    assert policies["gate_only"]["auto_post"] == 10
    assert policies["gate_only"]["auto_post_errors"] == 3
    assert policies["gate_plus_validation"]["auto_post"] == 8
    assert policies["gate_plus_validation"]["auto_post_errors"] == 1


def test_measured_headline_gate_alone_costs_more_than_manual_keying() -> None:
    # The honest finding: at the measured 70% precision, the confidence gate
    # alone LOSES money against manual keying - 3 silent errors per 27 docs
    # outweigh the 10 skipped 45-second reviews.
    report = cost_report(_committed_summary())
    policies = report["policies"]
    assert policies["gate_only"]["annual_cost"] > policies["manual_keying"]["annual_cost"]
    assert policies["gate_only"]["annual_saving_vs_manual"] == -8777.78
    # The validation layer flips that into a six-figure saving...
    assert policies["gate_plus_validation"]["annual_saving_vs_manual"] == 100555.56
    # ...and is itself worth the whole gap between the two policies.
    assert round(
        policies["gate_only"]["annual_cost"] - policies["gate_plus_validation"]["annual_cost"], 2
    ) == 109333.34


def test_no_measured_policy_credibly_clears_the_break_even() -> None:
    report = cost_report(_committed_summary())
    measured = report["break_even"]["measured"]
    assert report["break_even"]["auto_post_precision_required"] == 0.984
    assert measured["gate_only"]["pays_at_this_precision"] is False
    assert measured["gate_only"]["max_silent_error_cost"] == 1.3333
    assert measured["gate_plus_validation"]["pays_at_this_precision"] is False
    assert measured["gate_plus_validation"]["max_silent_error_cost"] == 3.2
    # Only the strict gate clears the bar, on a 4-document sample (the caveat
    # is printed with the report and documented in the README).
    assert measured["strict_gate_100pct"]["pays_at_this_precision"] is True
    assert measured["strict_gate_100pct"]["max_silent_error_cost"] is None
    assert measured["auto_post_everything"]["pays_at_this_precision"] is False


def test_cheapest_policy_on_this_set_is_the_strict_gate() -> None:
    report = cost_report(_committed_summary())
    assert report["cheapest_policy"] == "strict_gate_100pct"
    cheapest_cost = report["policies"]["strict_gate_100pct"]["annual_cost"]
    assert all(
        policy["annual_cost"] >= cheapest_cost for policy in report["policies"].values()
    )
    # Review-everything is the second-cheapest: on this set the model's value
    # is overwhelmingly the pre-fill, not the skipped confirm.
    ranked = sorted(report["policies"].items(), key=lambda kv: kv[1]["annual_cost"])
    assert [name for name, _ in ranked[:2]] == ["strict_gate_100pct", "review_everything"]


def test_savings_reconcile_with_the_manual_baseline() -> None:
    report = cost_report(_committed_summary())
    manual_annual = report["policies"]["manual_keying"]["annual_cost"]
    for name, policy in report["policies"].items():
        if name == "manual_keying":
            assert "annual_saving_vs_manual" not in policy
            continue
        assert policy["annual_saving_vs_manual"] == round(
            manual_annual - policy["annual_cost"], 2
        )


def test_policy_counts_partition_the_measured_set() -> None:
    report = cost_report(_committed_summary())
    for name, policy in report["policies"].items():
        if name == "manual_keying":
            continue
        assert policy["auto_post"] + policy["review"] == report["source_documents"]
        assert 0 <= policy["auto_post_errors"] <= policy["auto_post"]


def test_sweep_cost_curve_reconciles_with_independent_repricing() -> None:
    summary = _committed_summary()
    report = cost_report(summary)
    curve = report["sweep_cost_curve"]
    points = summary["sweep"]["points"]
    assert len(curve["points"]) == len(points)
    for sweep_point, priced in zip(points, curve["points"], strict=True):
        expected = policy_cost(
            summary["documents_scored"],
            sweep_point["auto_post"],
            sweep_point["auto_post"] - sweep_point["fully_correct"],
        )
        assert priced["threshold"] == sweep_point["threshold"]
        assert priced["cost_per_doc"] == expected["cost_per_doc"]
        assert priced["annual_cost"] == expected["annual_cost"]
    # The cheapest point is the first minimum, i.e. the lowest such threshold.
    best_cost = min(point["annual_cost"] for point in curve["points"])
    first_best = next(p for p in curve["points"] if p["annual_cost"] == best_cost)
    assert curve["cheapest"] == first_best
    assert curve["cheapest"]["threshold"] == 0.9393


def test_cost_report_without_a_100pct_threshold_point() -> None:
    # If no threshold reaches 100% precision the strict policy is simply
    # absent - the rest of the report must not care.
    summary = _committed_summary()
    summary["sweep"]["min_threshold_for_100pct"] = None
    report = cost_report(summary)
    assert "strict_gate_100pct" not in report["policies"]
    assert "strict_gate_100pct" not in report["break_even"]["measured"]
    assert report["cheapest_policy"] == "review_everything"
    assert set(report["policies"]) == set(POLICY_ORDER) - {"strict_gate_100pct"}


def test_cost_report_is_deterministic() -> None:
    summary = _committed_summary()
    first = cost_report(summary)
    second = cost_report(_committed_summary())
    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_committed_cost_results_match_regeneration() -> None:
    # The committed eval/cost_results.json must be exactly what a fresh run of
    # the model over the committed results.json produces.
    fresh = json.loads(
        json.dumps(cost_report(_committed_summary()), ensure_ascii=False, sort_keys=True)
    )
    assert _committed_cost_results() == fresh


def test_run_cost_reproduces_the_committed_file_byte_for_byte(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "cost_results.json"
    run(out_path=out)
    assert out.read_bytes() == (ROOT / "eval" / "cost_results.json").read_bytes()


def test_run_cost_without_results_fails_with_a_helpful_message(tmp_path: pathlib.Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run(results_path=tmp_path / "missing.json", out_path=tmp_path / "out.json")
    assert "run_eval" in str(excinfo.value)
    assert not (tmp_path / "out.json").exists()


def test_format_cost_report_carries_the_headlines_and_stays_ascii(
    tmp_path: pathlib.Path,
) -> None:
    report, text = run(out_path=tmp_path / "cost_results.json")
    assert text.isascii()  # the report renders in any terminal
    assert "173,000.00" in text  # the manual baseline
    assert "181,777.78" in text  # gate alone: worse than manual
    assert "72,444.44" in text  # gate + validation
    assert "98.4% precision" in text  # the break-even
    assert "cheapest policy on this set: strict_gate_100pct" in text
    assert "NOTE: modeled, not measured" in text
    # Every policy in the report appears as a table row.
    for name in report["policies"]:
        assert name in text
