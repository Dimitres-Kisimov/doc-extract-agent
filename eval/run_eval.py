"""Run the mock pipeline over the labelled set and score it honestly.

Usage::

    python -m eval.run_eval

Scores per-field exact-match accuracy, stated-totals accuracy with a +/-0.01
numeric tolerance, line-item F1 (items matched by normalised description),
a per-document-type breakdown, and the confidence gate's operating stats:
of the documents the gate auto-posts, how many are *fully correct* (the
number that matters operationally), and of the review-flagged documents,
how many actually contained extraction errors. Also sweeps the gate
threshold to find the lowest setting that yields 100% auto-post precision
on this set, groups every imperfect document into a **failure-mode
breakdown** — each mismatch attributed to a named symptom (silent non-ISO
date drop, currency mis-inference, spurious field, party over-capture,
line-item column misparse, missed row) with the documents it affects — so
*why* the heuristic fails is measured, not asserted, and runs a **confidence
calibration** over every extracted prediction (empirical accuracy at each
stated confidence level, plus ECE, MCE and Brier score) so whether a stated
0.75 really means 75% is measured rather than assumed.

Prints an ASCII report and writes ``eval/results.json``. The run is fully
deterministic (mock provider, no timestamps in the results file).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python eval/run_eval.py` from anywhere
    sys.path.insert(0, str(ROOT))

from docextract import evaluate_gate, extract_document  # noqa: E402
from docextract.gate import DEFAULT_THRESHOLD  # noqa: E402

DATASET_DIR = ROOT / "eval" / "dataset"
RESULTS_PATH = ROOT / "eval" / "results.json"
MONEY_TOL = 0.01
STATED_TOTAL_KEYS = ("subtotal", "tax", "total")
DATE_FIELDS = frozenset({"document_date", "due_date", "requested_delivery_date"})
PARTY_FIELDS = frozenset({"buyer", "seller"})
# Modes that describe a header-field defect (as opposed to a line-item one); the
# breakdown test reconciles their occurrence count against the field-accuracy
# table (field misses + spurious extractions).
FIELD_FAILURE_MODES = frozenset({
    "date_not_parsed", "date_value_wrong", "date_spurious",
    "currency_misinferred", "currency_not_found", "currency_spurious",
    "party_over_capture", "party_value_wrong", "party_not_found",
    "field_not_found", "field_value_wrong", "field_spurious",
})


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _item_key(description: str) -> str:
    return re.sub(r"\s+", " ", description.strip().lower())


def _num_close(a: object, b: object, tol: float = MONEY_TOL) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol  # type: ignore[arg-type]


def _item_matches(gt_item: dict, pred_item: dict) -> bool:
    """A predicted item is correct if every labelled attribute matches."""
    if not _num_close(gt_item["quantity"], pred_item.get("quantity"), 1e-6):
        return False
    if (gt_item.get("unit") or None) != (pred_item.get("unit") or None):
        return False
    return _num_close(gt_item.get("unit_price"), pred_item.get("unit_price")) and _num_close(
        gt_item.get("amount"), pred_item.get("amount")
    )


def score_document(truth: dict, result: dict) -> dict:
    """Score one pipeline ``result`` against its ground ``truth``.

    Returns a record with per-field matches, stated-totals matches, line-item
    tp/fp/fn, spurious extractions, and a ``fully_correct`` verdict (every
    labelled value exact, nothing spurious) — the criterion the gate's
    auto-post precision is judged on.
    """
    errors: list[str] = []
    # Every extracted value that carries a confidence and is scored against a
    # label becomes a calibration prediction: (confidence, correct). A *silently
    # missed* field emits no confidence, so it is intentionally absent here —
    # which is exactly why calibration can never see silent omissions.
    predictions: list[dict] = []

    doc_type_ok = result.get("doc_type") == truth["doc_type"]
    if not doc_type_ok:
        errors.append(f"doc_type: expected {truth['doc_type']}, got {result.get('doc_type')}")

    # Header fields: exact string match on the extracted value.
    extracted_fields = result.get("fields") or {}
    field_scores: dict[str, dict] = {}
    for name, expected in truth["fields"].items():
        got = extracted_fields.get(name)
        got_value = str(got["value"]).strip() if isinstance(got, dict) else None
        match = got_value == str(expected).strip()
        field_scores[name] = {"expected": expected, "extracted": got_value, "match": match}
        if isinstance(got, dict):
            predictions.append(
                {"source": "field", "confidence": float(got.get("confidence") or 0.0), "correct": match}
            )
        if not match:
            errors.append(f"field {name}: expected {expected!r}, got {got_value!r}")
    spurious_fields = sorted(set(extracted_fields) - set(truth["fields"]))
    for name in spurious_fields:
        got = extracted_fields.get(name)
        if isinstance(got, dict):
            predictions.append(
                {"source": "field", "confidence": float(got.get("confidence") or 0.0), "correct": False}
            )
        errors.append(f"field {name}: spurious (not on the document)")

    # Stated totals: numeric match within +/-0.01.
    extracted_totals = result.get("totals") or {}
    totals_scores: dict[str, dict] = {}
    for key, expected in truth["totals"].items():
        got = extracted_totals.get(key)
        got_value = float(got["value"]) if isinstance(got, dict) else None
        match = _num_close(expected, got_value)
        totals_scores[key] = {"expected": expected, "extracted": got_value, "match": match}
        if isinstance(got, dict):
            predictions.append(
                {"source": "total", "confidence": float(got.get("confidence") or 0.0), "correct": match}
            )
        if not match:
            errors.append(f"total {key}: expected {expected}, got {got_value}")
    spurious_totals = sorted(
        key
        for key in STATED_TOTAL_KEYS
        if isinstance(extracted_totals.get(key), dict) and key not in truth["totals"]
    )
    for key in spurious_totals:
        got = extracted_totals.get(key)
        if isinstance(got, dict):
            predictions.append(
                {"source": "total", "confidence": float(got.get("confidence") or 0.0), "correct": False}
            )
        errors.append(f"total {key}: spurious (not stated on the document)")

    # Line items: matched by normalised description key.
    gt_by_key = {_item_key(item["description"]): item for item in truth["line_items"]}
    pred_items = [item for item in result.get("line_items") or [] if isinstance(item, dict)]
    tp = fp = 0
    matched_keys: set[str] = set()
    for pred in pred_items:
        key = _item_key(str(pred.get("description") or ""))
        gt_item = gt_by_key.get(key)
        item_correct = (
            gt_item is not None and key not in matched_keys and _item_matches(gt_item, pred)
        )
        if item_correct:
            tp += 1
            matched_keys.add(key)
        else:
            fp += 1
            errors.append(f"line item {pred.get('description')!r}: wrong or spurious")
        conf = pred.get("confidence")
        if isinstance(conf, (int, float)):
            predictions.append(
                {"source": "line_item", "confidence": float(conf), "correct": item_correct}
            )
    fn = len(gt_by_key) - len(matched_keys)
    for key, gt_item in gt_by_key.items():
        if key not in matched_keys:
            errors.append(f"line item {gt_item['description']!r}: missed or extracted wrong")

    fully_correct = not errors
    return {
        "id": truth["id"],
        "doc_type": truth["doc_type"],
        "edge": truth["edge"],
        "doc_type_correct": doc_type_ok,
        "fields": field_scores,
        "spurious_fields": spurious_fields,
        "totals": totals_scores,
        "spurious_totals": spurious_totals,
        "items": {"tp": tp, "fp": fp, "fn": fn, "expected": len(gt_by_key)},
        "predictions": predictions,
        "fully_correct": fully_correct,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Evaluation over the committed set
# ---------------------------------------------------------------------------

def evaluate(dataset_dir: Path | str = DATASET_DIR) -> dict:
    """Run the pipeline over every labelled document and aggregate scores."""
    dataset_dir = Path(dataset_dir)
    docs: list[dict] = []
    for truth_path in sorted(dataset_dir.glob("*.json")):
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        text = (dataset_dir / f"{truth['id']}.txt").read_text(encoding="utf-8")
        result = extract_document(text)
        gate = evaluate_gate(result)
        record = score_document(truth, result)
        record["confidence"] = float(result["confidence"])
        record["totals_validated"] = bool(result["totals"].get("validated"))
        record["disposition"] = gate["disposition"]
        docs.append(record)

    return {
        "dataset": str(dataset_dir.relative_to(ROOT)) if dataset_dir.is_relative_to(ROOT) else str(dataset_dir),
        "documents_scored": len(docs),
        "gate_threshold": DEFAULT_THRESHOLD,
        "field_accuracy": _field_accuracy(docs),
        "totals_accuracy": _totals_accuracy(docs),
        "line_items": _item_stats(docs),
        "by_type": _by_type(docs),
        "gate": _gate_stats(docs),
        "sweep": _sweep(docs),
        "calibration": calibration(docs),
        "failure_modes": failure_modes(docs),
        "documents": docs,
    }


def _field_accuracy(docs: list[dict]) -> dict:
    per_field: dict[str, dict] = {}
    for doc in docs:
        for name, score in doc["fields"].items():
            slot = per_field.setdefault(name, {"expected": 0, "correct": 0, "spurious": 0})
            slot["expected"] += 1
            slot["correct"] += int(score["match"])
        for name in doc["spurious_fields"]:
            per_field.setdefault(name, {"expected": 0, "correct": 0, "spurious": 0})["spurious"] += 1
    for slot in per_field.values():
        slot["accuracy"] = round(slot["correct"] / slot["expected"], 4) if slot["expected"] else None
    doc_type_correct = sum(d["doc_type_correct"] for d in docs)
    return {
        "doc_type": {"expected": len(docs), "correct": doc_type_correct,
                     "accuracy": round(doc_type_correct / len(docs), 4)},
        "fields": dict(sorted(per_field.items())),
    }


def _totals_accuracy(docs: list[dict]) -> dict:
    per_key: dict[str, dict] = {}
    for doc in docs:
        for key, score in doc["totals"].items():
            slot = per_key.setdefault(key, {"expected": 0, "correct": 0, "spurious": 0})
            slot["expected"] += 1
            slot["correct"] += int(score["match"])
        for key in doc["spurious_totals"]:
            per_key.setdefault(key, {"expected": 0, "correct": 0, "spurious": 0})["spurious"] += 1
    for slot in per_key.values():
        slot["accuracy"] = round(slot["correct"] / slot["expected"], 4) if slot["expected"] else None
    return dict(sorted(per_key.items()))


def _prf(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 4),
            "recall": round(recall, 4), "f1": round(f1, 4)}


def _item_stats(docs: list[dict]) -> dict:
    tp = sum(d["items"]["tp"] for d in docs)
    fp = sum(d["items"]["fp"] for d in docs)
    fn = sum(d["items"]["fn"] for d in docs)
    return _prf(tp, fp, fn)


def _by_type(docs: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for doc_type in sorted({d["doc_type"] for d in docs}):
        subset = [d for d in docs if d["doc_type"] == doc_type]
        fields_expected = sum(len(d["fields"]) for d in subset)
        fields_correct = sum(sum(s["match"] for s in d["fields"].values()) for d in subset)
        items = _item_stats(subset)
        out[doc_type] = {
            "documents": len(subset),
            "fully_correct": sum(d["fully_correct"] for d in subset),
            "field_accuracy": round(fields_correct / fields_expected, 4) if fields_expected else None,
            "item_f1": items["f1"],
        }
    return out


def _gate_stats(docs: list[dict]) -> dict:
    auto = [d for d in docs if d["disposition"] == "auto_post"]
    review = [d for d in docs if d["disposition"] == "review"]
    auto_correct = [d for d in auto if d["fully_correct"]]
    review_with_errors = [d for d in review if not d["fully_correct"]]
    return {
        "threshold": DEFAULT_THRESHOLD,
        "auto_post": len(auto),
        "auto_post_fully_correct": len(auto_correct),
        "auto_post_precision": round(len(auto_correct) / len(auto), 4) if auto else None,
        "auto_post_misses": sorted(d["id"] for d in auto if not d["fully_correct"]),
        "review": len(review),
        "review_with_extraction_errors": len(review_with_errors),
        "review_error_ids": sorted(d["id"] for d in review_with_errors),
        "review_clean_extractions": len(review) - len(review_with_errors),
    }


def _sweep(docs: list[dict]) -> dict:
    """Sweep the gate threshold over every observed confidence value.

    The gate rule is ``confidence >= threshold AND totals validated``; the
    sweep asks which threshold makes every auto-posted document fully
    correct on this set (and what auto-post volume survives).
    """
    candidates = sorted({round(d["confidence"], 4) for d in docs} | {DEFAULT_THRESHOLD})
    points = []
    best = None
    for threshold in candidates:
        auto = [d for d in docs if d["totals_validated"] and d["confidence"] >= threshold]
        correct = sum(d["fully_correct"] for d in auto)
        precision = round(correct / len(auto), 4) if auto else None
        points.append({"threshold": threshold, "auto_post": len(auto),
                       "fully_correct": correct, "precision": precision})
        if auto and correct == len(auto) and best is None:
            best = points[-1]
    return {
        "points": points,
        "min_threshold_for_100pct": best,  # None if unattainable with volume > 0
    }


# ---------------------------------------------------------------------------
# Confidence calibration
# ---------------------------------------------------------------------------

def _source_calibration(preds: list[dict]) -> dict:
    """Accuracy / mean-confidence / gap for one prediction source."""
    count = len(preds)
    correct = sum(int(p["correct"]) for p in preds)
    accuracy = correct / count
    mean_conf = sum(float(p["confidence"]) for p in preds) / count
    return {
        "predictions": count,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "mean_confidence": round(mean_conf, 4),
        "gap": round(mean_conf - accuracy, 4),
    }


def calibration_from_predictions(preds: list[dict]) -> dict:
    """Calibrate a flat list of ``{source, confidence, correct}`` predictions.

    Treats each *distinct stated confidence value* as a reliability bin (the
    heuristic emits a small, fixed set of confidence constants, so the natural
    support is the honest binning — no arbitrary bin edges). Reports:

    * ``accuracy`` vs ``mean_confidence`` and their signed ``calibration_gap``
      (positive = the extractor is over-confident on average);
    * ``brier_score`` — mean squared error of confidence against the 0/1
      outcome (lower is better);
    * ``ece`` / ``mce`` — expected and maximum calibration error over the
      confidence-level bins;
    * a per-level ``reliability`` table (stated confidence vs. empirical
      accuracy) and a per-``source`` breakdown (header field / line item /
      total), so miscalibration can be located.

    Predictions are only the values that were *extracted* with a confidence;
    silently missed fields carry no confidence and are therefore invisible to
    calibration — the measured expression of "confidence cannot see silent
    omissions".
    """
    n = len(preds)
    if n == 0:
        return {
            "predictions": 0, "correct": 0, "accuracy": None, "mean_confidence": None,
            "calibration_gap": None, "brier_score": None, "ece": None, "mce": None,
            "reliability": [], "by_source": {},
        }

    correct = sum(int(p["correct"]) for p in preds)
    accuracy = correct / n
    mean_conf = sum(float(p["confidence"]) for p in preds) / n
    brier = sum((float(p["confidence"]) - float(p["correct"])) ** 2 for p in preds) / n

    levels: dict[float, dict] = {}
    for p in preds:
        level = round(float(p["confidence"]), 4)
        slot = levels.setdefault(level, {"count": 0, "correct": 0})
        slot["count"] += 1
        slot["correct"] += int(p["correct"])

    reliability: list[dict] = []
    ece = 0.0
    mce = 0.0
    for level in sorted(levels):
        slot = levels[level]
        acc = slot["correct"] / slot["count"]
        error = abs(acc - level)
        reliability.append({
            "confidence": level, "count": slot["count"], "correct": slot["correct"],
            "accuracy": round(acc, 4), "gap": round(level - acc, 4),
        })
        ece += (slot["count"] / n) * error
        mce = max(mce, error)

    by_source = {
        source: _source_calibration([p for p in preds if p["source"] == source])
        for source in sorted({p["source"] for p in preds})
    }

    return {
        "predictions": n,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "mean_confidence": round(mean_conf, 4),
        "calibration_gap": round(mean_conf - accuracy, 4),
        "brier_score": round(brier, 4),
        "ece": round(ece, 4),
        "mce": round(mce, 4),
        "reliability": reliability,
        "by_source": by_source,
    }


def calibration(docs: list[dict]) -> dict:
    """Pool every document's scored predictions and calibrate them."""
    preds = [p for doc in docs for p in doc.get("predictions", [])]
    return calibration_from_predictions(preds)


# ---------------------------------------------------------------------------
# Failure-mode breakdown
# ---------------------------------------------------------------------------

def _field_failure_mode(name: str, expected: object, extracted: object) -> str:
    """Name the symptom for one mismatched *header* field.

    Distinguishes a silently dropped value (``extracted is None`` — the regex
    saw nothing) from a mis-read value, and for party fields separates a name
    that swallowed the trailing address (over-capture) from an outright wrong
    value.
    """
    if name in DATE_FIELDS:
        return "date_not_parsed" if extracted is None else "date_value_wrong"
    if name == "currency":
        return "currency_not_found" if extracted is None else "currency_misinferred"
    if name in PARTY_FIELDS:
        if extracted is None:
            return "party_not_found"
        if expected is not None and str(expected).strip() in str(extracted):
            return "party_over_capture"
        return "party_value_wrong"
    return "field_not_found" if extracted is None else "field_value_wrong"


def _spurious_field_mode(name: str) -> str:
    if name == "currency":
        return "currency_spurious"
    if name in DATE_FIELDS:
        return "date_spurious"
    return "field_spurious"


def failure_modes(docs: list[dict]) -> dict:
    """Attribute every extraction defect to a named failure mode.

    Consumes the per-document score records (nothing is re-extracted) and
    buckets each mismatched/spurious field, spurious/mismatched total and
    line-item error into a symptom. ``documents`` lists the affected ids;
    ``occurrences`` counts individual hits (fields, or — for line items —
    gold rows not correctly recovered). By construction the union of all
    ``documents`` equals the set of not-``fully_correct`` documents.
    """
    modes: dict[str, dict] = {}

    def hit(mode: str, doc_id: str, occ: int = 1) -> None:
        slot = modes.setdefault(mode, {"documents": set(), "occurrences": 0})
        slot["documents"].add(doc_id)
        slot["occurrences"] += occ

    for doc in docs:
        doc_id = doc["id"]
        for name, score in doc["fields"].items():
            if not score["match"]:
                hit(_field_failure_mode(name, score["expected"], score["extracted"]), doc_id)
        for name in doc["spurious_fields"]:
            hit(_spurious_field_mode(name), doc_id)
        for score in doc["totals"].values():
            if not score["match"]:
                hit("total_value_wrong", doc_id)
        for _key in doc["spurious_totals"]:
            hit("total_spurious", doc_id)
        fp, fn = doc["items"]["fp"], doc["items"]["fn"]
        if fp and fn:
            hit("line_item_values_wrong", doc_id, occ=fn)
        elif fn:
            hit("line_item_row_missed", doc_id, occ=fn)
        elif fp:
            hit("line_item_spurious_row", doc_id, occ=fp)

    resolved = {
        name: {"documents": sorted(slot["documents"]), "occurrences": slot["occurrences"]}
        for name, slot in modes.items()
    }
    imperfect = sorted(d["id"] for d in docs if not d["fully_correct"])
    return {
        "fully_correct_documents": len(docs) - len(imperfect),
        "imperfect_documents": len(imperfect),
        "imperfect_ids": imperfect,
        "modes": dict(sorted(resolved.items())),
    }


def _ranked_modes(breakdown: dict) -> list[tuple[str, dict]]:
    """Modes ordered by documents affected, then occurrences, then name."""
    return sorted(
        breakdown["modes"].items(),
        key=lambda kv: (-len(kv[1]["documents"]), -kv[1]["occurrences"], kv[0]),
    )


# ---------------------------------------------------------------------------
# ASCII report
# ---------------------------------------------------------------------------

def _pct(value: float | None) -> str:
    return "  n/a" if value is None else f"{value * 100:5.1f}%"


def format_report(summary: dict) -> str:
    lines: list[str] = []
    add = lines.append
    docs = summary["documents"]
    by_type = summary["by_type"]

    counts = ", ".join(f"{t} {v['documents']}" for t, v in by_type.items())
    add("== doc-extract-agent evaluation (mock pipeline) ==")
    add(f"Documents: {summary['documents_scored']} ({counts})")
    add("")

    dt = summary["field_accuracy"]["doc_type"]
    add("-- Document type detection --")
    add(f"correct {dt['correct']}/{dt['expected']} ({_pct(dt['accuracy']).strip()})")
    add("")

    add("-- Header fields (exact match) --")
    add(f"{'field':<26}{'expected':>9}{'correct':>9}{'accuracy':>10}{'spurious':>10}")
    for name, slot in summary["field_accuracy"]["fields"].items():
        add(f"{name:<26}{slot['expected']:>9}{slot['correct']:>9}{_pct(slot['accuracy']):>10}{slot['spurious']:>10}")
    add("")

    add("-- Stated totals (numeric, +/-0.01) --")
    add(f"{'total':<26}{'expected':>9}{'correct':>9}{'accuracy':>10}{'spurious':>10}")
    for key, slot in summary["totals_accuracy"].items():
        add(f"{key:<26}{slot['expected']:>9}{slot['correct']:>9}{_pct(slot['accuracy']):>10}{slot['spurious']:>10}")
    add("")

    items = summary["line_items"]
    add("-- Line items (matched by description; qty/unit/price/amount must all match) --")
    add(f"tp {items['tp']}  fp {items['fp']}  fn {items['fn']}  "
        f"precision {_pct(items['precision']).strip()}  recall {_pct(items['recall']).strip()}  "
        f"f1 {_pct(items['f1']).strip()}")
    add("")

    add("-- Per document type --")
    add(f"{'type':<16}{'docs':>6}{'fully correct':>15}{'field acc':>11}{'item f1':>9}")
    for doc_type, slot in by_type.items():
        add(f"{doc_type:<16}{slot['documents']:>6}{slot['fully_correct']:>15}"
            f"{_pct(slot['field_accuracy']):>11}{_pct(slot['item_f1']):>9}")
    fully = sum(d["fully_correct"] for d in docs)
    add(f"{'all':<16}{len(docs):>6}{fully:>15}")
    add("")

    gate = summary["gate"]
    add(f"-- Confidence gate @ {gate['threshold']:.2f} (operating stats) --")
    add(f"auto-posted:      {gate['auto_post']:>3} docs, {gate['auto_post_fully_correct']} fully correct "
        f"-> auto-post precision {_pct(gate['auto_post_precision']).strip()}")
    if gate["auto_post_misses"]:
        add(f"  posted WITH errors: {', '.join(gate['auto_post_misses'])}")
    add(f"review-flagged:   {gate['review']:>3} docs, {gate['review_with_extraction_errors']} "
        f"with real extraction errors, {gate['review_clean_extractions']} extraction-clean "
        "(held for unreconciled or absent totals)")
    add("")

    best = summary["sweep"]["min_threshold_for_100pct"]
    add("-- Threshold sweep (gate rule: confidence >= t AND totals validated) --")
    add(f"{'threshold':>10}{'auto-post':>11}{'correct':>9}{'precision':>11}")
    for point in summary["sweep"]["points"]:
        add(f"{point['threshold']:>10.4f}{point['auto_post']:>11}{point['fully_correct']:>9}"
            f"{_pct(point['precision']):>11}")
    if best:
        add(f"lowest threshold with 100% auto-post precision: {best['threshold']:.4f} "
            f"(auto-post volume drops to {best['auto_post']} of {len(docs)} docs)")
    else:
        add("no threshold achieves 100% auto-post precision with non-zero volume on this set")
    add("")

    cal = summary["calibration"]
    add("-- Confidence calibration (per extracted prediction, correct vs. stated confidence) --")
    if cal["predictions"]:
        add(f"predictions {cal['predictions']}  accuracy {_pct(cal['accuracy']).strip()}  "
            f"mean confidence {_pct(cal['mean_confidence']).strip()}  "
            f"gap {cal['calibration_gap']:+.4f}")
        add(f"ECE {cal['ece']:.4f}  MCE {cal['mce']:.4f}  Brier {cal['brier_score']:.4f}  "
            "(gap>0 = over-confident)")
        add(f"{'stated conf':>12}{'preds':>7}{'correct':>9}{'empirical':>11}{'gap':>9}")
        for row in cal["reliability"]:
            add(f"{row['confidence']:>12.2f}{row['count']:>7}{row['correct']:>9}"
                f"{_pct(row['accuracy']):>11}{row['gap']:>+9.3f}")
        add("by source:")
        for source, slot in cal["by_source"].items():
            add(f"  {source:<10} preds {slot['predictions']:>3}  "
                f"accuracy {_pct(slot['accuracy']).strip():>6}  "
                f"mean conf {_pct(slot['mean_confidence']).strip():>6}  gap {slot['gap']:+.4f}")
    else:
        add("no extracted predictions to calibrate")
    add("")

    breakdown = summary["failure_modes"]
    ranked = _ranked_modes(breakdown)
    add("-- Failure modes (imperfect documents grouped by symptom) --")
    add(f"{'mode':<24}{'docs':>5}{'occurrences':>13}   documents")
    for name, slot in ranked:
        add(f"{name:<24}{len(slot['documents']):>5}{slot['occurrences']:>13}   "
            f"{', '.join(slot['documents'])}")
    if ranked:
        top_name, top_slot = ranked[0]
        add(f"{breakdown['fully_correct_documents']} of {len(docs)} documents fully correct; "
            f"{breakdown['imperfect_documents']} imperfect across {len(breakdown['modes'])} "
            f"failure modes; most common: {top_name} ({len(top_slot['documents'])} docs)")
    else:
        add(f"all {len(docs)} documents fully correct — no failure modes")
    return "\n".join(lines)


def main() -> None:
    summary = evaluate()
    report = format_report(summary)
    print(report)
    with open(RESULTS_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"\nresults written to {RESULTS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
