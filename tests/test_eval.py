"""Tests for the evaluation harness: dataset generation, scorer, gate stats."""

from __future__ import annotations

import filecmp
import json
import pathlib

from eval.make_dataset import DATASET_DIR, generate
from eval.run_eval import FIELD_FAILURE_MODES, evaluate, failure_modes, score_document

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
