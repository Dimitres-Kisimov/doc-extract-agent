"""The confidence gate — auto-post vs. review disposition for a result.

Implements the straight-through rule documented in ``docs/BUSINESS_CASE.md``:
a document may **auto-post** only when its overall confidence clears the
threshold *and* its totals cross-validated against the line items; everything
else routes to a clerk for **review**. This is a deliberately conservative
heuristic — a document whose figures did not reconcile never posts, and a
document with no monetary totals (an RFQ, a delivery note) always gets a
human look.

Below-gate header fields and line items are reported as flags but never change
the disposition — they are the "check these first" markers a reviewing clerk
starts with, exactly as the business case describes.

The evaluation is pure and deterministic. The web UI mirrors the same rule in
JavaScript (``evaluateGate`` in ``web/index.html``) so the threshold control
re-evaluates instantly without a server round trip — keep the two in sync.

Example::

    from docextract import evaluate_gate, extract_document

    result = extract_document(open("samples/invoice.txt").read())
    gate = evaluate_gate(result)
    assert gate["disposition"] == "auto_post"
"""

from __future__ import annotations

# Matches docs/BUSINESS_CASE.md: "overall confidence >= 0.85 and totals cross-validated".
DEFAULT_THRESHOLD = 0.85


def _clamp_threshold(threshold: object) -> float:
    """Coerce ``threshold`` to a float in [0, 1]; non-numeric falls back to the default."""
    try:
        value = float(threshold)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return max(0.0, min(1.0, value))


def evaluate_gate(
    result: dict[str, object], threshold: object = DEFAULT_THRESHOLD
) -> dict[str, object]:
    """Evaluate the straight-through gate for an extraction ``result``.

    Args:
        result: A result dict as returned by :func:`docextract.extract_document`.
        threshold: Overall-confidence gate in [0, 1]. Out-of-range values are
            clamped; non-numeric values fall back to :data:`DEFAULT_THRESHOLD`
            (the request body is client-supplied JSON, so anything can arrive).

    Returns:
        A dict with ``threshold`` (the clamped value actually used),
        ``disposition`` (``"auto_post"`` or ``"review"``), ``reasons``
        (human-readable — one per review rule that fired, or the single
        passing rule for auto-post), ``fields_below`` (header field names
        under the gate, sorted) and ``items_below`` (line-item positions
        under the gate).
    """
    gate_threshold = _clamp_threshold(threshold)
    overall = float(result.get("confidence") or 0.0)  # type: ignore[arg-type]

    totals_raw = result.get("totals")
    totals: dict[str, object] = totals_raw if isinstance(totals_raw, dict) else {}
    validated = bool(totals.get("validated"))
    has_money = any(
        isinstance(totals.get(key), dict) for key in ("subtotal", "tax", "total")
    ) or bool(totals.get("computed_line_total"))

    fields_raw = result.get("fields")
    fields: dict[str, object] = fields_raw if isinstance(fields_raw, dict) else {}
    fields_below = sorted(
        name
        for name, field in fields.items()
        if isinstance(field, dict) and float(field.get("confidence") or 0.0) < gate_threshold  # type: ignore[arg-type]
    )

    items_raw = result.get("line_items")
    line_items: list[object] = items_raw if isinstance(items_raw, list) else []
    items_below = [
        item.get("position")
        for item in line_items
        if isinstance(item, dict) and float(item.get("confidence") or 0.0) < gate_threshold  # type: ignore[arg-type]
    ]

    reasons: list[str] = []
    if overall < gate_threshold:
        reasons.append(
            f"overall confidence {overall:.0%} is below the {gate_threshold:.0%} gate"
        )
    if not validated:
        reasons.append(
            "totals did not cross-validate against the line items"
            if has_money
            else "no monetary totals to cross-validate"
        )
    disposition = "review" if reasons else "auto_post"
    if not reasons:
        reasons.append(
            f"overall {overall:.0%} >= gate {gate_threshold:.0%} and totals cross-validated"
        )

    return {
        "threshold": gate_threshold,
        "disposition": disposition,
        "reasons": reasons,
        "fields_below": fields_below,
        "items_below": items_below,
    }
