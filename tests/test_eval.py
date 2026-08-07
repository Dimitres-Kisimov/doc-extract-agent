"""Tests for the evaluation harness: dataset generation, scorer, gate stats."""

from __future__ import annotations

import filecmp
import json
import pathlib

from eval.make_dataset import DATASET_DIR, generate
from eval.run_eval import (
    FIELD_FAILURE_MODES,
    calibration,
    calibration_from_predictions,
    evaluate,
    failure_modes,
    score_document,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _snapshot(directory: pathlib.Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


def test_dataset_generation_is_deterministic(tmp_path: pathlib.Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    ids_a = generate(a)
    ids_b = generate(b)
    assert ids_a == ids_b
    assert len(ids_a) == 27
    assert _snapshot(a) == _snapshot(b)


def test_committed_dataset_matches_regeneration(tmp_path: pathlib.Path) -> None:
    # The committed eval/dataset must be exactly what the seeded script produces.
    fresh = tmp_path / "fresh"
    generate(fresh)
    committed = _snapshot(DATASET_DIR)
    regenerated = _snapshot(fresh)
    assert sorted(committed) == sorted(regenerated)
    mismatched = [name for name in committed if committed[name] != regenerated[name]]
    assert mismatched == [], f"committed dataset out of date for: {mismatched}"
    # filecmp double-check on one pair to guard the snapshot helper itself.
    sample = next(iter(committed))
    assert filecmp.cmp(DATASET_DIR / sample, fresh / sample, shallow=False)


def _hand_truth() -> dict:
    return {
        "id": "hand01",
        "doc_type": "invoice",
        "edge": "hand_checked",
        "fields": {"currency": "EUR", "document_number": "INV-1"},
        "line_items": [
            {"position": 1, "description": "Widget A", "quantity": 2,
             "unit": None, "unit_price": 1.50, "amount": 3.00},
            {"position": 2, "description": "Widget B", "quantity": 1,
             "unit": None, "unit_price": 4.00, "amount": 4.00},
        ],
        "totals": {"total": 7.00},
    }


def _hand_result() -> dict:
    return {
        "doc_type": "invoice",
        "confidence": 0.9,
        "fields": {
            "currency": {"value": "EUR", "confidence": 0.95},
            "document_number": {"value": "INV-1", "confidence": 0.95},
        },
        "line_items": [
            {"position": 1, "description": "Widget A", "quantity": 2.0,
             "unit": None, "unit_price": 1.5, "amount": 3.0, "confidence": 0.95},
            {"position": 2, "description": "Widget B", "quantity": 1.0,
             "unit": None, "unit_price": 4.0, "amount": 4.0, "confidence": 0.95},
        ],
        "totals": {"total": {"value": 7.0, "confidence": 0.99},
                   "computed_line_total": 7.0, "validated": True},
    }


def test_scorer_on_hand_checked_correct_pair() -> None:
    record = score_document(_hand_truth(), _hand_result())
    assert record["fully_correct"]
    assert record["errors"] == []
    assert record["items"] == {"tp": 2, "fp": 0, "fn": 0, "expected": 2}
    assert record["fields"]["currency"]["match"]
    assert record["totals"]["total"]["match"]


def test_scorer_flags_hand_checked_errors() -> None:
    result = _hand_result()
    result["fields"]["currency"]["value"] = "USD"  # wrong field value
    result["line_items"][0]["amount"] = 3.05  # outside the +/-0.01 tolerance
    result["totals"]["total"]["value"] = 7.005  # inside the tolerance: still a match
    record = score_document(_hand_truth(), result)
    assert not record["fully_correct"]
    assert not record["fields"]["currency"]["match"]
    assert record["totals"]["total"]["match"]
    assert record["items"] == {"tp": 1, "fp": 1, "fn": 1, "expected": 2}
    assert any("currency" in e for e in record["errors"])
    assert any("Widget A" in e for e in record["errors"])


def test_eval_runs_end_to_end_on_committed_set() -> None:
    summary = evaluate()
    assert summary["documents_scored"] == 27
    assert summary["field_accuracy"]["doc_type"]["accuracy"] == 1.0
    assert 0.0 < summary["line_items"]["f1"] <= 1.0
    # The committed results.json must match a fresh run (the eval is deterministic).
    committed = json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))
    fresh = json.loads(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    assert committed == fresh


def test_gate_stats_are_consistent_with_per_document_results() -> None:
    summary = evaluate()
    docs = summary["documents"]
    gate = summary["gate"]
    auto = [d for d in docs if d["disposition"] == "auto_post"]
    review = [d for d in docs if d["disposition"] == "review"]
    assert gate["auto_post"] + gate["review"] == len(docs)
    assert gate["auto_post"] == len(auto)
    assert gate["auto_post_fully_correct"] == sum(d["fully_correct"] for d in auto)
    if auto:
        assert gate["auto_post_precision"] == round(
            gate["auto_post_fully_correct"] / gate["auto_post"], 4
        )
    assert gate["review_with_extraction_errors"] == sum(not d["fully_correct"] for d in review)
    assert sorted(gate["auto_post_misses"]) == sorted(
        d["id"] for d in auto if not d["fully_correct"]
    )


def test_failure_modes_partition_the_imperfect_documents() -> None:
    summary = evaluate()
    breakdown = summary["failure_modes"]
    imperfect = {d["id"] for d in summary["documents"] if not d["fully_correct"]}
    attributed = {
        doc_id for slot in breakdown["modes"].values() for doc_id in slot["documents"]
    }
    # Every imperfect document is attributed to a mode and every attributed
    # document is genuinely imperfect: the modes cover exactly the failures.
    assert attributed == imperfect
    assert set(breakdown["imperfect_ids"]) == imperfect
    assert breakdown["imperfect_documents"] == len(imperfect)
    assert breakdown["fully_correct_documents"] + breakdown["imperfect_documents"] == len(
        summary["documents"]
    )


def test_failure_mode_field_occurrences_reconcile_with_field_accuracy() -> None:
    summary = evaluate()
    modes = summary["failure_modes"]["modes"]
    fields = summary["field_accuracy"]["fields"]
    field_misses = sum(slot["expected"] - slot["correct"] for slot in fields.values())
    spurious = sum(slot["spurious"] for slot in fields.values())
    field_occ = sum(
        slot["occurrences"] for name, slot in modes.items() if name in FIELD_FAILURE_MODES
    )
    # Header-field failure modes account for exactly the field-level misses
    # plus spurious extractions counted in the accuracy table — nothing lost,
    # nothing double-counted.
    assert field_occ == field_misses + spurious


def test_measured_failure_mode_headline_on_committed_set() -> None:
    summary = evaluate()
    modes = summary["failure_modes"]["modes"]
    # The honest, measured headline: silent non-ISO date drops are the top mode.
    assert set(modes["date_not_parsed"]["documents"]) == {
        "dn04_weird_date", "inv08_weird_dates", "rfq04_weird_date"
    }
    assert modes["date_not_parsed"]["occurrences"] == 4
    # Line-item column misreads hit exactly the two column-format edge cases.
    assert set(modes["line_item_values_wrong"]["documents"]) == {
        "inv10_eu_thousands", "inv13_no_amount_column"
    }
    # The over-capture symptom is the inline delivery address bleeding into buyer.
    assert modes["party_over_capture"]["documents"] == ["rfq08_inline_deliver_to"]


def test_failure_modes_on_hand_checked_pair() -> None:
    # A clean pair yields no modes; a broken field is bucketed by symptom.
    clean = score_document(_hand_truth(), _hand_result())
    assert failure_modes([clean])["modes"] == {}
    broken = _hand_result()
    broken["fields"]["currency"]["value"] = "USD"  # wrong currency value
    record = score_document(_hand_truth(), broken)
    modes = failure_modes([record])["modes"]
    assert "currency_misinferred" in modes
    assert modes["currency_misinferred"]["documents"] == ["hand01"]


def test_threshold_sweep_finding_holds() -> None:
    summary = evaluate()
    best = summary["sweep"]["min_threshold_for_100pct"]
    assert best is not None, "expected some threshold to reach 100% on this set"
    threshold = best["threshold"]
    auto = [
        d for d in summary["documents"]
        if d["totals_validated"] and d["confidence"] >= threshold
    ]
    assert len(auto) == best["auto_post"] > 0
    assert all(d["fully_correct"] for d in auto)
    # And the default threshold really is below 100% on this set (the honest bit).
    assert summary["gate"]["auto_post_precision"] < 1.0


# --- confidence calibration -------------------------------------------------

def test_calibration_math_on_constructed_predictions() -> None:
    # A two-prediction set exercises every metric with hand-verifiable numbers:
    # one correct and one wrong, both stated at 0.9 -> empirical accuracy 0.5.
    cal = calibration_from_predictions([
        {"source": "field", "confidence": 0.9, "correct": True},
        {"source": "field", "confidence": 0.9, "correct": False},
    ])
    assert cal["predictions"] == 2
    assert cal["correct"] == 1
    assert cal["accuracy"] == 0.5
    assert cal["mean_confidence"] == 0.9
    assert cal["calibration_gap"] == 0.4  # over-confident: stated 0.9, real 0.5
    # Brier = ((0.9-1)^2 + (0.9-0)^2) / 2 = (0.01 + 0.81) / 2
    assert cal["brier_score"] == 0.41
    assert cal["ece"] == 0.4  # single bin at 0.9, |0.5 - 0.9| weighted by all
    assert cal["mce"] == 0.4
    assert cal["reliability"] == [
        {"confidence": 0.9, "count": 2, "correct": 1, "accuracy": 0.5, "gap": 0.4}
    ]


def test_calibration_empty_is_safe() -> None:
    cal = calibration_from_predictions([])
    assert cal["predictions"] == 0
    assert cal["accuracy"] is None
    assert cal["ece"] is None
    assert cal["reliability"] == []
    assert cal["by_source"] == {}


def test_calibration_reconciles_with_pooled_predictions() -> None:
    summary = evaluate()
    docs = summary["documents"]
    cal = summary["calibration"]
    pooled = [p for d in docs for p in d["predictions"]]
    # The aggregate is exactly the pool of every document's scored predictions.
    assert cal["predictions"] == len(pooled)
    assert cal["correct"] == sum(int(p["correct"]) for p in pooled)
    assert calibration(docs) == cal
    # Metrics stay in their valid ranges.
    for key in ("accuracy", "mean_confidence", "ece", "mce", "brier_score"):
        assert 0.0 <= cal[key] <= 1.0
    # The reliability table partitions the pool: counts and corrects reconcile,
    # and each row's empirical accuracy is exactly correct/count.
    assert sum(r["count"] for r in cal["reliability"]) == cal["predictions"]
    assert sum(r["correct"] for r in cal["reliability"]) == cal["correct"]
    for row in cal["reliability"]:
        assert row["accuracy"] == round(row["correct"] / row["count"], 4)
    # The per-source split also partitions the pool.
    assert sum(s["predictions"] for s in cal["by_source"].values()) == cal["predictions"]
    assert sum(s["correct"] for s in cal["by_source"].values()) == cal["correct"]


def test_calibration_headline_numbers_on_committed_set() -> None:
    summary = evaluate()
    cal = summary["calibration"]
    # Measured headline: 251 extracted predictions, 96% accurate but only 88.3%
    # mean stated confidence -> the hand-picked constants are UNDER-confident.
    assert cal["predictions"] == 251
    assert cal["correct"] == 241
    assert cal["accuracy"] == 0.9602
    assert cal["mean_confidence"] == 0.8835
    assert cal["calibration_gap"] == -0.0767
    assert cal["calibration_gap"] < 0  # net under-confident on this set
    assert cal["ece"] == 0.0878
    assert cal["mce"] == 0.5
    assert cal["brier_score"] == 0.0444
    # The committed results.json deliverable carries the same block.
    committed = json.loads((ROOT / "eval" / "results.json").read_text(encoding="utf-8"))
    assert committed["calibration"] == cal


def test_calibration_currency_symbol_level_is_the_only_overconfident_bin() -> None:
    summary = evaluate()
    reliability = summary["calibration"]["reliability"]
    over = [row for row in reliability if row["gap"] > 0]
    # Exactly one stated-confidence level is over-confident: the 0.60 currency
    # symbol/keyword fallback, empirically right only 25% of the time here.
    assert len(over) == 1
    assert over[0]["confidence"] == 0.6
    assert over[0]["count"] == 4
    assert over[0]["accuracy"] == 0.25
    # Every other level is at-or-below its stated confidence (under-confident).
    others = [row for row in reliability if row["confidence"] != 0.6]
    assert all(row["gap"] <= 0 for row in others)
    # The MCE is driven by the 0.50 "unreconciled total" level: those figures
    # were extracted faithfully (100% correct) but confidence was halved.
    low = next(row for row in reliability if row["confidence"] == 0.5)
    assert low["accuracy"] == 1.0
    assert summary["calibration"]["mce"] == round(abs(low["accuracy"] - low["confidence"]), 4)


def test_calibration_cannot_see_silent_omissions() -> None:
    # The honest limitation, measured: calibration covers only *extracted*
    # predictions, so extracted-prediction accuracy (96%) is far above the
    # document-level fully-correct rate (16/27) — because the dominant real
    # failure, silent non-ISO date drops, emits no confidence at all.
    summary = evaluate()
    cal = summary["calibration"]
    fully_correct_fraction = summary["failure_modes"]["fully_correct_documents"] / len(
        summary["documents"]
    )
    assert cal["accuracy"] > fully_correct_fraction
    # inv08 drops both non-ISO dates: those labelled fields yield no prediction.
    inv08 = next(d for d in summary["documents"] if d["id"] == "inv08_weird_dates")
    silently_missed = [n for n, s in inv08["fields"].items() if s["extracted"] is None]
    assert silently_missed  # the dropped dates
    field_preds = [p for p in inv08["predictions"] if p["source"] == "field"]
    extracted_fields = sum(1 for s in inv08["fields"].values() if s["extracted"] is not None)
    # One prediction per extracted field, none for the silently missed ones.
    assert len(field_preds) == extracted_fields
