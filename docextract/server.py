"""A tiny stdlib-only web server for the extraction engine.

Serves the interactive UI from ``web/`` and exposes one JSON endpoint:

``POST /extract``
    Request body:  ``{"text": "<document text>"}``
    Response body: ``{"doc_type", "fields", "line_items", "totals",
    "confidence", "trace"}`` — exactly what :func:`docextract.extract_document`
    returns.

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
        if urlparse(self.path).path != "/extract":
            self._send_json({"error": "not found"}, status=404)
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > _MAX_BODY_BYTES:
            self._send_json({"error": "document too large"}, status=413)
            return

        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return

        text = data.get("text", "") if isinstance(data, dict) else ""
        if not isinstance(text, str):
            self._send_json({"error": "'text' must be a string"}, status=400)
            return

        # The pipeline never raises on bad input; this is defence in depth.
        try:
            result = extract_document(text)
        except Exception as exc:  # pragma: no cover - safety net
            self._send_json({"error": f"extraction failed: {exc}"}, status=500)
            return
        self._send_json(result)

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
