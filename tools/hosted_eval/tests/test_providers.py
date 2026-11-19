"""Exercise the plain HTTP protocol against a local fake provider, never paid APIs."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from tools.hosted_eval.ledger import Hold
from tools.hosted_eval.providers import Client, HTTPFailure, download


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        calls = []
        statuses = []
        payloads = []

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.calls.append(("POST", self.path))
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.respond()

        def do_GET(self):
            self.calls.append(("GET", self.path))
            self.respond()

        def do_PUT(self):
            self.calls.append(("PUT", self.path))
            self.respond()

        def respond(self):
            self.send_response(self.statuses.pop(0) if self.statuses else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.payloads.pop(0) if self.payloads else {}).encode())

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", Handler
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def route(client, address, monkeypatch):
    def request(method, url, **kwargs):
        path = "/" + url.split("/", 3)[-1]
        return requests.request(method, address + path, **kwargs)

    monkeypatch.setattr(client.session, "request", request)


@pytest.mark.parametrize("provider", ["replicate", "fal"])
def test_submit_and_terminal_poll(server, monkeypatch, provider):
    address, handler = server
    client = Client(provider, "fake-key", "test/model")
    route(client, address, monkeypatch)
    monkeypatch.setattr("tools.hosted_eval.providers.time.sleep", lambda _: None)
    if provider == "replicate":
        handler.payloads = [
            {"id": "abc", "status": "starting"},
            {"id": "abc", "status": "succeeded", "output": "https://replicate.delivery/test.mp4"},
        ]
    else:
        handler.payloads = [
            {
                "request_id": "abc",
                "status_url": "https://queue.fal.run/status",
                "response_url": "https://queue.fal.run/result",
            },
            {"status": "COMPLETED"},
            {"images": [{"url": "https://v3.fal.media/test.png"}]},
        ]
    record = client.submit({"prompt": "fixture"})
    observed = []
    record = client.poll(record, lambda record: observed.append(record.get("status")))
    assert record["status"] in {"succeeded", "COMPLETED"}
    assert handler.calls[0][0] == "POST"
    assert sum(method == "POST" for method, _ in handler.calls) == 1


@pytest.mark.parametrize("status", [429, 500])
def test_http_rejection_is_explicit_not_hidden_retry(server, monkeypatch, status):
    address, handler = server
    handler.statuses = [status]
    client = Client("replicate", "fake", "test/model")
    route(client, address, monkeypatch)
    with pytest.raises(HTTPFailure) as error:
        client.submit({})
    assert error.value.status == status
    assert len(handler.calls) == 1


@pytest.mark.parametrize("provider", ["replicate", "fal"])
def test_timeout_requests_cancel_without_inference_resubmission(server, monkeypatch, provider):
    address, handler = server
    client = Client(provider, "fake", "test/model")
    route(client, address, monkeypatch)
    record = (
        {"id": "abc", "status": "starting"}
        if provider == "replicate"
        else {
            "request_id": "abc",
            "status": "IN_QUEUE",
            "status_url": "https://queue.fal.run/status",
            "cancel_url": "https://queue.fal.run/cancel",
        }
    )
    with pytest.raises(Hold, match="Timed out"):
        client.poll(record, lambda _: None, timeout=0)
    assert len(handler.calls) == 1
    assert "cancel" in handler.calls[0][1]


def test_download_declared_size_cap_without_reading_body(tmp_path, monkeypatch):
    class Response:
        is_redirect = False
        status_code = 200
        headers = {"Content-Length": str(101 * 1024 * 1024)}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **kw: Response())
    with pytest.raises(Hold, match="100 MB"):
        download("https://replicate.delivery/output", tmp_path / "raw.mp4", ["replicate.delivery"])
    assert not (tmp_path / "raw.mp4").exists()
