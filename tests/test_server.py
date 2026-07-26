"""HTTP-level tests for the stdlib web server (routing + oversized-body handling)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from docextract.server import build_server


@pytest.fixture(scope="module")
def base_url():
    httpd = build_server(port=0)  # port 0 -> OS assigns a free port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _post(url: str, body: bytes):
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    return urllib.request.urlopen(request, timeout=10)


def test_health(base_url: str) -> None:
    with urllib.request.urlopen(base_url + "/health", timeout=10) as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}


def test_extract_roundtrip(base_url: str) -> None:
    body = json.dumps({"text": "INVOICE\nInvoice Number: INV-1\nTotal: 10.00"}).encode()
    with _post(base_url + "/extract", body) as resp:
        data = json.loads(resp.read())
    assert data["doc_type"] == "invoice"
    assert data["fields"]["document_number"]["value"] == "INV-1"


def test_unknown_route_is_json_404(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(base_url + "/nope", timeout=10)
    assert excinfo.value.code == 404
    assert json.loads(excinfo.value.read()) == {"error": "not found"}


def test_oversized_body_returns_readable_413(base_url: str) -> None:
    # Regression: the server used to reply 413 without draining the upload,
    # aborting the TCP connection mid-send — clients saw a network error
    # (ConnectionAbortedError / "Failed to fetch") instead of the JSON message.
    body = json.dumps({"text": "x" * 1_100_000}).encode()
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base_url + "/extract", body)
    error = excinfo.value
    assert error.code == 413
    assert json.loads(error.read()) == {"error": "document too large"}
