"""Robustness tests: the pipeline must always emit a trace and never crash."""

from __future__ import annotations

import pytest

from docextract import extract_document
from docextract.llm import MockProvider, get_provider

GARBAGE_INPUTS = [
    "",
    "   \n\t  ",
    "!@#$%^&*()_+{}|:<>?~`",
    "\x00\x01\x02 random bytes as text",
    "1234567890" * 500,
    "Not a document at all, just a sentence.",
    "12  \n  34  \n  Total: not-a-number",
    "🙂📄💥 emoji only content 🚚",
]


@pytest.mark.parametrize("text", GARBAGE_INPUTS)
def test_never_crashes_on_garbage(text: str) -> None:
    result = extract_document(text)
    # Always returns the full contract.
    assert set(result) >= {"doc_type", "fields", "line_items", "totals", "confidence", "trace"}
    assert 0.0 <= result["confidence"] <= 1.0


@pytest.mark.parametrize("text", GARBAGE_INPUTS)
def test_always_emits_trace(text: str) -> None:
    result = extract_document(text)
    trace = result["trace"]
    assert isinstance(trace, list)
    assert len(trace) >= 1
    for event in trace:
        assert {"step", "stage", "message", "status"} <= set(event)
        assert event["status"] in {"ok", "info", "warn"}


def test_trace_steps_are_sequential() -> None:
    result = extract_document("INVOICE\nInvoice Number: INV-1\nTotal: 10.00")
    steps = [e["step"] for e in result["trace"]]
    assert steps == list(range(1, len(steps) + 1))


def test_empty_document_is_unknown() -> None:
    result = extract_document("")
    assert result["doc_type"] == "unknown"
    assert result["line_items"] == []
    assert result["confidence"] == 0.0


def test_default_provider_is_offline_mock() -> None:
    provider = get_provider()
    assert isinstance(provider, MockProvider)
    assert provider.available is False


def test_pipeline_accepts_explicit_provider() -> None:
    result = extract_document("INVOICE\nInvoice Number: INV-9", provider=MockProvider())
    assert result["trace"][0]["detail"]["provider"] == "mock"
