"""doc-extract-agent — an agentic document-intelligence engine.

Paste a business document (RFQ email, invoice, or delivery note) and an
agentic pipeline extracts structured fields, line items, and totals with a
per-field confidence score and a step-by-step trace.

The engine is deterministic (regex/heuristic based) so it runs with **no API
key**, but it is structured around a pluggable LLM provider interface
(:mod:`docextract.llm`) so the same pipeline can be backed by a real model in
production.

Basic usage::

    from docextract import extract_document

    result = extract_document("INVOICE\\nInvoice Number: INV-1\\n...")
    print(result["doc_type"])          # "invoice"
    print(result["totals"]["total"])   # {"value": 124.95, "confidence": 0.99}
    for event in result["trace"]:
        print(event["stage"], event["message"])
"""

from docextract.llm import MockProvider, get_provider
from docextract.pipeline import extract_document

__all__ = ["extract_document", "MockProvider", "get_provider"]
__version__ = "1.0.0"
