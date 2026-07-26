"""HTTP-level tests for the stdlib web server (routing, gate, batch, oversized bodies)."""

from __future__ import annotations

import json
import pathlib
import threading
import urllib.error
import urllib.request

import pytest

from docextract.server import build_server

SAMPLES_DIR = pathlib.Path(__file__).resolve().parent.parent / "samples"


def _sample(name: str) -> str:
    return (SAMPLES_DIR / name).read_text(encoding="utf-8")


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


def test_extract_response_includes_gate(base_url: str) -> None:
    body = json.dumps({"text": _sample("invoice.txt")}).encode()
    with _post(base_url + "/extract", body) as resp:
        data = json.loads(resp.read())
    assert data["gate"]["disposition"] == "auto_post"
    assert data["gate"]["threshold"] == 0.85
    assert "seller" in data["gate"]["fields_below"]


def test_extract_gate_threshold_override(base_url: str) -> None:
    body = json.dumps({"text": _sample("invoice.txt"), "gate_threshold": 0.99}).encode()
    with _post(base_url + "/extract", body) as resp:
        data = json.loads(resp.read())
    assert data["gate"]["threshold"] == 0.99
    assert data["gate"]["disposition"] == "review"


def test_batch_roundtrip_with_summary(base_url: str) -> None:
    documents = [
        {"name": "invoice.txt", "text": _sample("invoice.txt")},
        {"name": "rfq_email.txt", "text": _sample("rfq_email.txt")},
    ]
    body = json.dumps({"documents": documents}).encode()
    with _post(base_url + "/extract/batch", body) as resp:
        data = json.loads(resp.read())
    assert [r["name"] for r in data["results"]] == ["invoice.txt", "rfq_email.txt"]
    assert [r["doc_type"] for r in data["results"]] == ["invoice", "rfq"]
    assert data["results"][0]["gate"]["disposition"] == "auto_post"
    assert data["results"][1]["gate"]["disposition"] == "review"
    assert data["summary"] == {
        "documents": 2,
        "auto_post": 1,
        "review": 1,
        "gate_threshold": 0.85,
    }


def test_batch_names_default_when_missing(base_url: str) -> None:
    body = json.dumps({"documents": [{"text": "INVOICE\nInvoice Number: INV-7"}]}).encode()
    with _post(base_url + "/extract/batch", body) as resp:
        data = json.loads(resp.read())
    assert data["results"][0]["name"] == "document 1"
    assert data["results"][0]["fields"]["document_number"]["value"] == "INV-7"


@pytest.mark.parametrize(
    ("payload", "message_part"),
    [
        ({}, "non-empty list"),
        ({"documents": []}, "non-empty list"),
        ({"documents": "nope"}, "non-empty list"),
        ({"documents": [{"text": 42}]}, "documents[0]"),
        ({"documents": ["just a string"]}, "documents[0]"),
        ({"documents": [{"name": "ok.txt", "text": "fine"}, {"name": "bad"}]}, "documents[1]"),
    ],
)
def test_batch_validation_errors(base_url: str, payload: dict, message_part: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base_url + "/extract/batch", json.dumps(payload).encode())
    assert excinfo.value.code == 400
    assert message_part in json.loads(excinfo.value.read())["error"]


def test_batch_too_many_documents(base_url: str) -> None:
    docs = [{"text": "INVOICE"} for _ in range(51)]
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base_url + "/extract/batch", json.dumps({"documents": docs}).encode())
    assert excinfo.value.code == 400
    assert "max 50" in json.loads(excinfo.value.read())["error"]


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
