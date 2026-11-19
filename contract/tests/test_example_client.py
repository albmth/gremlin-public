"""Run the published example client (examples/gremlin_client.py) against the local service.

Worker outputs are transport fixtures or the labelled simulator, never generated media.
"""

import hashlib
import importlib.util
import io
import json
import time
import types
import zipfile

import pytest
import requests
from gremlin_contract.flows import INPUTS
from gremlin_contract.harness import ROOT, Worker

TOKEN = "local-contract-test"


@pytest.fixture
def backend_settings():
    return {
        "imprint_hourly_microusd": "1580000",
        "imprint_budget_microusd": "5000000",
        "imprint_rolling_budget_microusd": "20000000",
        "imprint_monthly_budget_microusd": "20000000",
        "imprint_daily_jobs": "8",
        "imprint_runpod_key": "fake-provider-key",
        "imprint_endpoint_id": "local-only",
        "client_token_hashes": json.dumps(
            {"example-test": hashlib.sha256(TOKEN.encode()).hexdigest()}
        ),
    }


EXAMPLE = ROOT / "examples/gremlin_client.py"


@pytest.fixture
def handoff():
    spec = importlib.util.spec_from_file_location("gremlin_client", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(handoff, api):
    instance = handoff.Gremlin(api.url, TOKEN)
    instance.http.trust_env = False
    yield instance
    instance.http.close()


@pytest.fixture
def media_intent(tmp_path):
    paths = []
    intent = {
        "idempotency_key": "local-handoff-request",
        "reference_point": {"x": 0.5, "y": 0.5},
        "target_point": {"x": 0.5, "y": 0.5},
        "duration_ms": 3400,
        "prompt": "A person waves at the camera on a quiet street.",
        "tier": "simple-motion",
    }
    for kind in ("reference", "driving"):
        path = tmp_path / kind
        path.write_bytes(INPUTS[kind])
        intent[kind + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        paths.append(path)
    return intent, paths


def finish_attempt(api, *, failure=False):
    """Simulate only the worker protocol, never inference or provider dispatch."""
    session_id, token = api.reserve(1_580_000)
    worker = Worker(api, session_id, token)
    lease = worker.lease().body
    for kind in ("reference", "driving"):
        assert worker.input(lease["id"], kind).content
    if failure:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(
                "diagnostics.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "stage": "failed",
                        "timings": {},
                        "failure_code": "worker_attempt_failed",
                    }
                ),
            )
        assert worker.upload(lease["id"], "diagnostics.zip", archive.getvalue()).ok
        assert worker.progress(lease["id"], "failed").ok
        return
    assert worker.progress(lease["id"], "running").ok
    manifest = {}
    assert worker.progress(lease["id"], "finishing").ok
    for name in ("result.mp4", "result.webp", "poster.jpg"):
        stored = worker.upload(lease["id"], name, f"transport output: {name}".encode())
        manifest[name] = stored.body
    assert worker.complete(lease["id"], manifest).ok


def test_reference_flow_recovers_create_and_stores_before_acknowledgement(
    client, handoff, api, media_intent, monkeypatch
):
    intent, paths = media_intent
    job = client.create(intent)
    # Replaying after a lost create response must reuse its job, including normalized defaults.
    assert client.create(intent)["id"] == job["id"]
    request = client.http.request
    submitted = 0

    def lose_submit_response(method, url, **kwargs):
        nonlocal submitted
        response = request(method, url, **kwargs)
        if url.endswith("/submit"):
            submitted += 1
            if submitted == 1:
                finish_attempt(api)
                raise requests.ConnectionError("Submit response lost while worker finished")
        return response

    monkeypatch.setattr(client.http, "request", lose_submit_response)
    monkeypatch.setattr(
        handoff, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=lambda _: None)
    )
    stored = {}

    def persist(job_id, files):
        assert api.job_row(job_id)["expires_at"] > api.now() + 71 * 3600
        stored.update(files)

    result, files = handoff.run(client, intent, *paths, store_results=persist)
    assert result["state"] == "succeeded"
    assert submitted == 2
    assert files == stored and set(files) == {"result.mp4", "result.webp", "poster.jpg"}
    assert 86300 < api.job_row(job["id"])["expires_at"] - api.now() <= 86400
    download = client.call("GET", f"/api/imprint/jobs/{job['id']}/results/result.mp4")
    assert download.headers["Cache-Control"] == "no-store"
    # The storage link is a different origin; the client's bearer token must not follow it.
    assert download.history and download.history[0].status_code == 307
    assert "Authorization" not in download.request.headers


def test_failed_storage_does_not_shorten_retention(client, handoff, api, media_intent):
    intent, paths = media_intent
    job = client.create(intent)
    for kind, path in zip(("reference", "driving"), paths, strict=True):
        client.upload(job["id"], kind, path)
    client.submit(job["id"])
    finish_attempt(api)

    def storage_failure(job_id, files):
        raise OSError("Client storage unavailable")

    with pytest.raises(OSError, match="storage unavailable"):
        handoff.run(client, intent, *paths, store_results=storage_failure)
    assert api.job_row(job["id"])["expires_at"] > api.now() + 71 * 3600


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "expired"])
def test_reference_flow_terminal_recovery(client, handoff, api, media_intent, terminal):
    intent, paths = media_intent
    job = client.create(intent)
    if terminal == "failed":
        for kind, path in zip(("reference", "driving"), paths, strict=True):
            client.upload(job["id"], kind, path)
        client.submit(job["id"])
        finish_attempt(api, failure=True)
    elif terminal == "cancelled":
        client.cancel(job["id"])
    else:
        api.seed_job(job["id"], expires_at=api.now() - 1)
        api.reconcile()

    def must_not_store(*args):
        pytest.fail("Only succeeded jobs have results to store")

    result, files = handoff.run(client, intent, *paths, store_results=must_not_store)
    assert result["state"] == terminal
    failure = {"failure_code": "worker_attempt_failed", "failure_reason": None}
    assert files == (failure if terminal == "failed" else None)
    assert result["outcome"]["kind"] == terminal
    with pytest.raises(handoff.GremlinError) as exc:
        client.call("GET", f"/api/imprint/jobs/{job['id']}/results/result.mp4")
    assert exc.value.code == "IMPRINT_RESULT_UNAVAILABLE"


def test_auth_validation_limits_and_capability_blind_spots(client, handoff, api, media_intent):
    intent, _ = media_intent
    for header, code in (("", "CLIENT_AUTH_REQUIRED"), ("Bearer wrong", "CLIENT_AUTH_INVALID")):
        with pytest.raises(handoff.GremlinError) as exc:
            client.call("GET", "/api/imprint/capabilities", headers={"Authorization": header})
        assert (exc.value.status, exc.value.code) == (401, code)
    with pytest.raises(handoff.GremlinError) as exc:
        client.create(intent | {"unknown": True})
    assert exc.value.status == 422 and exc.value.code is None
    with pytest.raises(handoff.GremlinError) as exc:
        client.call("GET", "/unknown")
    assert exc.value.status == 404 and "Not Found" in exc.value.message
    api.settings(imprint_daily_jobs=1)
    job = client.create(intent)
    client.cancel(job["id"])
    assert client.capabilities()["admission"]["accepting"] is False
    assert client.capabilities()["admission"]["reason_code"] == "daily_limit_reached"
    with pytest.raises(handoff.GremlinError) as exc:
        client.create(intent | {"idempotency_key": "local-next-request"})
    assert (exc.value.status, exc.value.code) == (429, "IMPRINT_LIMIT")
    assert client.create(intent)["id"] == job["id"]


def test_lost_create_and_upload_responses_reuse_intent_and_rewind_bytes(
    client, handoff, media_intent, monkeypatch
):
    intent, paths = media_intent
    request = client.http.request
    lost = set()
    sleeps = []

    def lose_once(method, url, **kwargs):
        response = request(method, url, **kwargs)
        if method in {"POST", "PUT"} and url not in lost:
            lost.add(url)
            raise requests.ConnectionError("Response lost after the API committed it")
        return response

    monkeypatch.setattr(client.http, "request", lose_once)
    monkeypatch.setattr(handoff, "time", types.SimpleNamespace(sleep=sleeps.append))
    job = client.create(intent)
    assert client.create(intent)["id"] == job["id"]
    for kind, path in zip(("reference", "driving"), paths, strict=True):
        stored = client.upload(job["id"], kind, path)
        assert stored == {"sha256": intent[kind + "_sha256"], "bytes": path.stat().st_size}
    assert sleeps == [1, 1, 1]


def test_a_lost_storage_response_is_retried_with_the_same_bytes(
    client, handoff, media_intent, monkeypatch
):
    intent, paths = media_intent
    job = client.create(intent)
    sent = []
    original = requests.request

    def lose_first_transfer(method, url, **kwargs):
        response = original(method, url, **kwargs)
        sent.append(url)
        if len(sent) == 1:
            raise requests.ConnectionError("Storage committed the bytes but the reply was lost")
        return response

    monkeypatch.setattr(handoff.requests, "request", lose_first_transfer)
    monkeypatch.setattr(handoff, "time", types.SimpleNamespace(sleep=lambda _: None))
    stored = client.upload(job["id"], "reference", paths[0])
    assert stored == {"sha256": intent["reference_sha256"], "bytes": paths[0].stat().st_size}
    assert len(sent) == 2


def test_closed_submit_remains_recoverable_without_automatic_retry(
    client, handoff, api, media_intent, monkeypatch
):
    intent, paths = media_intent
    job = client.create(intent)
    for kind, path in zip(("reference", "driving"), paths, strict=True):
        client.upload(job["id"], kind, path)
    api.settings(imprint_rolling_budget_microusd=0)
    sleeps = []
    monkeypatch.setattr(handoff, "time", types.SimpleNamespace(sleep=sleeps.append))
    with pytest.raises(handoff.GremlinError) as exc:
        client.submit(job["id"])
    assert exc.value.code == "IMPRINT_BUDGET_BLOCKED" and sleeps == []
    assert client.job(job["id"])["state"] == "uploading"
    assert client.create(intent)["id"] == job["id"]
    with pytest.raises(handoff.GremlinError) as exc:
        client.create(intent | {"idempotency_key": "another-local-intent"})
    assert exc.value.code == "IMPRINT_ACTIVE"
    assert client.cancel(job["id"])["state"] == "cancelled"


@pytest.mark.parametrize("problem", ["corrupt_download", "missing_file", "expired_manifest"])
def test_incomplete_delivery_never_reaches_storage_or_saved(
    client, handoff, api, media_intent, monkeypatch, problem
):
    intent, paths = media_intent
    job = client.create(intent)
    for kind, path in zip(("reference", "driving"), paths, strict=True):
        client.upload(job["id"], kind, path)
    client.submit(job["id"])
    finish_attempt(api)
    request = client.http.request

    def incomplete_delivery(method, url, **kwargs):
        response = request(method, url, **kwargs)
        if problem == "corrupt_download" and url.endswith("/results/result.mp4"):
            response._content = b"corrupt response"
        elif method == "GET" and url.endswith("/jobs/" + job["id"]):
            snapshot = response.json()
            if problem == "missing_file":
                snapshot["result"]["files"].pop("poster.jpg")
            elif problem == "expired_manifest":
                snapshot["result"] = None
            response._content = json.dumps(snapshot).encode()
        return response

    def must_not_store(*args):
        pytest.fail("Incomplete or corrupt results must not reach storage")

    monkeypatch.setattr(client.http, "request", incomplete_delivery)
    with pytest.raises(ValueError, match="failed verification"):
        handoff.run(client, intent, *paths, store_results=must_not_store)
    assert api.job_row(job["id"])["expires_at"] > api.now() + 71 * 3600


@pytest.mark.parametrize(
    "body", [b'["gateway failure"]', b'"gateway failure"', b"null", b"bad gateway"]
)
def test_reference_client_retries_non_object_gateway_errors(client, handoff, monkeypatch, body):
    request = client.http.request
    sleeps = []
    calls = 0

    def transient(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            response = requests.Response()
            response.status_code = 502
            response._content = body
            return response
        return request(*args, **kwargs)

    monkeypatch.setattr(client.http, "request", transient)
    # Replace only the extracted module's clock, without changing the API server's clock.
    monkeypatch.setattr(handoff, "time", types.SimpleNamespace(sleep=sleeps.append))
    assert client.health()["status"] == "ok"
    assert calls == 2 and sleeps == [1]
