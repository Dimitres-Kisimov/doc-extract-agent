"""Tests for the confidence gate — the auto-post vs. review disposition."""

from __future__ import annotations

import pathlib

import pytest

from docextract import evaluate_gate, extract_document
from docextract.gate import DEFAULT_THRESHOLD

SAMPLES_DIR = pathlib.Path(__file__).resolve().parent.parent / "samples"


def _load(name: str) -> str:
    return (SAMPLES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def invoice() -> dict:
    return extract_document(_load("invoice.txt"))


@pytest.fixture
def rfq() -> dict:
    return extract_document(_load("rfq_email.txt"))


def test_default_threshold_matches_business_case() -> None:
    # docs/BUSINESS_CASE.md: "overall confidence >= 0.85 and totals cross-validated".
    assert DEFAULT_THRESHOLD == 0.85


def test_invoice_clears_the_gate(invoice: dict) -> None:
    gate = evaluate_gate(invoice)
    assert gate["disposition"] == "auto_post"
    assert gate["threshold"] == 0.85
    # Auto-post carries the single passing rule as its explanation.
    assert len(gate["reasons"]) == 1
    assert "cross-validated" in gate["reasons"][0]


def test_invoice_flags_below_gate_fields_without_blocking(invoice: dict) -> None:
    # The heuristic seller (0.75) sits below the gate: flagged for the clerk,
    # but per the business-case rule flags never change the disposition.
    gate = evaluate_gate(invoice)
    assert "seller" in gate["fields_below"]
    assert gate["disposition"] == "auto_post"


def test_rfq_routes_to_review(rfq: dict) -> None:
    # No monetary totals means nothing reconciled — conservative default is review.
    gate = evaluate_gate(rfq)
    assert gate["disposition"] == "review"
    assert any("no monetary totals" in reason for reason in gate["reasons"])


def test_delivery_note_routes_to_review() -> None:
    gate = evaluate_gate(extract_document(_load("delivery_note.txt")))
    assert gate["disposition"] == "review"


def test_high_threshold_forces_review(invoice: dict) -> None:
    gate = evaluate_gate(invoice, threshold=0.99)
    assert gate["disposition"] == "review"
    assert any("below the 99% gate" in reason for reason in gate["reasons"])


def test_threshold_is_clamped_and_defaults_on_garbage(invoice: dict) -> None:
    assert evaluate_gate(invoice, threshold=5)["threshold"] == 1.0
    assert evaluate_gate(invoice, threshold=-1)["threshold"] == 0.0
    assert evaluate_gate(invoice, threshold="nope")["threshold"] == DEFAULT_THRESHOLD
    assert evaluate_gate(invoice, threshold=None)["threshold"] == DEFAULT_THRESHOLD


def test_gate_on_empty_result_is_review() -> None:
    gate = evaluate_gate(extract_document(""))
    assert gate["disposition"] == "review"
    assert gate["fields_below"] == []
    assert gate["items_below"] == []


def test_gate_is_deterministic(invoice: dict) -> None:
    assert evaluate_gate(invoice) == evaluate_gate(invoice)
