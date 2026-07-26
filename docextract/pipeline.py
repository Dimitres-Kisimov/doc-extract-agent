"""The agentic extraction pipeline.

Runs the document through ordered stages, each emitting a trace event:

1. **detect** — classify the document type.
2. **header** — extract labelled header fields (parties, dates, currency).
3. **line_items** — parse the tabular line items.
4. **totals** — extract and cross-validate totals against the line items.
5. **confidence** — aggregate a single overall confidence score.

The pipeline consults an :mod:`~docextract.llm` provider so it mirrors a real
agentic design; with the default offline :class:`~docextract.llm.MockProvider`
it uses the deterministic heuristics in :mod:`~docextract.extractors`.

Example::

    from docextract.pipeline import extract_document

    result = extract_document(open("samples/invoice.txt").read())
    assert result["doc_type"] == "invoice"
    assert 0.0 <= result["confidence"] <= 1.0
"""

from __future__ import annotations

import time

from docextract import extractors
from docextract.llm import LLMProvider, get_provider


def _clamp(value: float) -> float:
    """Constrain a confidence score to the [0, 1] range."""
    return max(0.0, min(1.0, value))


class _Tracer:
    """Collects ordered, timed trace events emitted by each stage."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self._step = 0

    def emit(self, stage: str, message: str, status: str = "ok", **detail: object) -> None:
        self._step += 1
        self.events.append(
            {
                "step": self._step,
                "stage": stage,
                "message": message,
                "status": status,  # "ok" | "info" | "warn"
                "detail": detail,
                "ts_ms": round(time.time() * 1000),
            }
        )


def extract_document(
    text: str, *, provider: LLMProvider | None = None
) -> dict[str, object]:
    """Run the full extraction pipeline on ``text``.

    Args:
        text: The raw document text (pasted email/invoice/delivery note).
        provider: Optional LLM provider; defaults to the configured one.

    Returns:
        A dict with keys ``doc_type``, ``fields``, ``line_items``, ``totals``,
        ``confidence`` (overall, in [0, 1]) and ``trace`` (list of events).

    The pipeline never raises on malformed input — it degrades to
    ``doc_type = "unknown"`` with an empty result and a trace explaining why.
    """
    provider = provider or get_provider()
    tracer = _Tracer()
    text = text or ""

    tracer.emit(
        "start",
        f"Pipeline started using '{provider.name}' provider"
        + ("" if provider.available else " (heuristic mode, no API key)"),
        status="info",
        provider=provider.name,
        provider_available=provider.available,
        chars=len(text),
    )

    if not text.strip():
        tracer.emit("detect", "Empty document — nothing to extract", status="warn")
        return {
            "doc_type": "unknown",
            "fields": {},
            "line_items": [],
            "totals": {"validated": False, "computed_line_total": 0.0},
            "confidence": 0.0,
            "trace": tracer.events,
        }

    # Stage 1 — detect document type.
    doc_type, type_conf = extractors.detect_doc_type(text)
    tracer.emit(
        "detect",
        f"Classified document as '{doc_type}'",
        status="ok" if doc_type != "unknown" else "warn",
        doc_type=doc_type,
        confidence=round(type_conf, 2),
    )

    # Stage 2 — header fields.
    fields = extractors.extract_header_fields(text)
    tracer.emit(
        "header",
        f"Extracted {len(fields)} header field(s): {', '.join(sorted(fields)) or 'none'}",
        status="ok" if fields else "warn",
        count=len(fields),
    )

    # Stage 3 — line items.
    line_items = extractors.parse_line_items(text)
    tracer.emit(
        "line_items",
        f"Parsed {len(line_items)} line item(s)",
        status="ok" if line_items else "warn",
        count=len(line_items),
    )

    # Stage 4 — totals + validation.
    totals = extractors.extract_totals(text, line_items)
    validated = bool(totals.get("validated"))
    has_money = any(
        isinstance(totals.get(key), dict) for key in ("subtotal", "tax", "total")
    ) or bool(totals.get("computed_line_total"))
    if validated:
        totals_message = "Totals validated against line items"
    elif has_money:
        totals_message = "Totals not cross-validated (missing or mismatched figures)"
    else:
        totals_message = "No monetary totals in this document — nothing to cross-validate"
    tracer.emit(
        "totals",
        totals_message,
        status="ok" if validated else "info",
        computed_line_total=totals.get("computed_line_total"),
        validated=validated,
    )

    # Stage 5 — aggregate confidence.
    overall = _aggregate_confidence(type_conf, fields, line_items, totals)
    tracer.emit(
        "confidence",
        f"Overall extraction confidence: {overall:.0%}",
        status="ok",
        confidence=overall,
    )

    return {
        "doc_type": doc_type,
        "fields": fields,
        "line_items": line_items,
        "totals": totals,
        "confidence": overall,
        "trace": tracer.events,
    }


def _aggregate_confidence(
    type_conf: float,
    fields: dict[str, dict[str, object]],
    line_items: list[dict[str, object]],
    totals: dict[str, object],
) -> float:
    """Combine per-signal confidences into a single overall score in [0, 1]."""
    scores: list[float] = [type_conf]
    scores.extend(float(f["confidence"]) for f in fields.values())
    scores.extend(
        float(item["confidence"])
        for item in line_items
        if isinstance(item.get("confidence"), (int, float))
    )
    for key in ("subtotal", "total", "tax"):
        entry = totals.get(key)
        if isinstance(entry, dict):
            scores.append(float(entry["confidence"]))

    if not scores:
        return 0.0
    average = sum(scores) / len(scores)
    # Reward cross-validated totals with a small bonus.
    if totals.get("validated"):
        average = average + 0.02
    return round(_clamp(average), 4)
