"""A tiny stdlib-only web server for the extraction engine.

Serves the interactive UI from ``web/`` and exposes two JSON endpoints:

``POST /extract``
    Request body:  ``{"text": "<document text>", "gate_threshold": 0.85}``
    (``gate_threshold`` optional). Response body: ``{"doc_type", "fields",
    "line_items", "totals", "confidence", "gate", "trace"}`` — what
    :func:`docextract.extract_document` returns, stamped with the
    auto-post/review disposition from :func:`docextract.evaluate_gate`.
    Extracted values carry their source ``span`` (see :mod:`docextract.spans`)
    inside this same payload, which is what the UI highlights the original
    document with — there is no separate provenance endpoint.

``POST /extract/batch``
    Request body:  ``{"documents": [{"name": "a.txt", "text": "..."}, ...],
    "gate_threshold": 0.85}`` (``name`` and ``gate_threshold`` optional; max
    50 documents, and the whole batch shares the 1 MB request limit).
    Response body: ``{"results": [...], "summary": {"documents",
    "auto_post", "review", "gate_threshold"}}`` where each result is the
    ``/extract`` shape plus the document's ``name``.

Run it with::

    python -m docextract.server            # http://127.0.0.1:8000
    python -m docextract.server --port 9000

No third-party dependencies — just :mod:`http.server` from the standard library.
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from docextract.gate import DEFAULT_THRESHOLD, evaluate_gate
from docextract.pipeline import extract_document

# Directory holding the static UI (web/index.html), resolved relative to repo root.
_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

_MAX_BODY_BYTES = 1_000_000  # 1 MB guard against oversized uploads.
_MAX_DRAIN_BYTES = 16_000_000  # How much of an oversized body we read before replying 413.
_MAX_BATCH_DOCS = 50  # Per-request cap for /extract/batch (the body limit above also applies).


class DocExtractHandler(BaseHTTPRequestHandler):
    """Handles static file serving and the ``/extract`` API."""

    server_version = "docextract/1.0"

    # -- helpers ---------------------------------------------------------

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str) -> None:
        _, ext = os.path.splitext(path)
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json({"error": "not found"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send_file(os.path.join(_WEB_DIR, "index.html"))
            return
        if route == "/health":
            self._send_json({"status": "ok"})
            return
        # Serve any other static asset from web/, guarding against traversal.
        safe = os.path.normpath(route).lstrip("/\\")
        candidate = os.path.join(_WEB_DIR, safe)
        if os.path.commonpath([_WEB_DIR, os.path.abspath(candidate)]) == _WEB_DIR and os.path.isfile(candidate):
            self._send_file(candidate)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route not in ("/extract", "/extract/batch"):
            self._send_json({"error": "not found"}, status=404)
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > _MAX_BODY_BYTES:
            # Drain what the client is still uploading before replying.
            # Responding 413 mid-upload aborts the TCP connection, so browsers
            # surface a generic network error ("Failed to fetch") instead of
            # this JSON message. Reading the body first lets the client see it.
            remaining = min(length, _MAX_DRAIN_BYTES)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            if length > _MAX_DRAIN_BYTES:
                # Truly enormous bodies are not worth reading; close instead.
                self.close_connection = True
            self._send_json({"error": "document too large"}, status=413)
            return

        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return

        if not isinstance(data, dict):
            data = {}
        threshold = data.get("gate_threshold", DEFAULT_THRESHOLD)

        if route == "/extract/batch":
            self._handle_batch(data, threshold)
            return

        text = data.get("text", "")
        if not isinstance(text, str):
            self._send_json({"error": "'text' must be a string"}, status=400)
            return

        # The pipeline never raises on bad input; this is defence in depth.
        try:
            result = extract_document(text)
        except Exception as exc:  # pragma: no cover - safety net
            self._send_json({"error": f"extraction failed: {exc}"}, status=500)
            return
        result["gate"] = evaluate_gate(result, threshold)
        self._send_json(result)

    def _handle_batch(self, data: dict[str, object], threshold: object) -> None:
        """Extract every document in a ``/extract/batch`` request and summarise."""
        documents = data.get("documents")
        if not isinstance(documents, list) or not documents:
            self._send_json({"error": "'documents' must be a non-empty list"}, status=400)
            return
        if len(documents) > _MAX_BATCH_DOCS:
            self._send_json(
                {"error": f"too many documents (max {_MAX_BATCH_DOCS} per batch)"}, status=400
            )
            return

        prepared: list[tuple[str, str]] = []
        for index, entry in enumerate(documents):
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                self._send_json(
                    {"error": f"documents[{index}] must be an object with a string 'text'"},
                    status=400,
                )
                return
            name = entry.get("name")
            name = name.strip() if isinstance(name, str) and name.strip() else f"document {index + 1}"
            prepared.append((name, entry["text"]))

        results: list[dict[str, object]] = []
        auto_post = 0
        gate_threshold = DEFAULT_THRESHOLD
        for name, text in prepared:
            try:
                result = extract_document(text)
            except Exception as exc:  # pragma: no cover - safety net
                self._send_json({"error": f"extraction failed for '{name}': {exc}"}, status=500)
                return
            gate = evaluate_gate(result, threshold)
            gate_threshold = float(gate["threshold"])  # type: ignore[arg-type]
            if gate["disposition"] == "auto_post":
                auto_post += 1
            results.append({"name": name, **result, "gate": gate})

        self._send_json(
            {
                "results": results,
                "summary": {
                    "documents": len(results),
                    "auto_post": auto_post,
                    "review": len(results) - auto_post,
                    "gate_threshold": gate_threshold,
                },
            }
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quieter, single-line logging.
        print(f"[docextract] {self.address_string()} - {format % args}")


def build_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """Construct (but do not start) the HTTP server."""
    return ThreadingHTTPServer((host, port), DocExtractHandler)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: ``python -m docextract.server``."""
    parser = argparse.ArgumentParser(description="Run the doc-extract-agent web server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    args = parser.parse_args(argv)

    httpd = build_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"doc-extract-agent serving on {url}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
