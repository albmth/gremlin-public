"""Shared request fixtures and multi-step flows used across the contract tests."""

import hashlib
import io

from PIL import Image

from gremlin_contract.harness import Worker


def media_inputs():
    reference, driving = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(reference, format="PNG")
    Image.new("RGB", (32, 32), "blue").save(
        driving,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (32, 32), "red")],
        duration=2500,
        loop=0,
    )
    return {"reference": reference.getvalue(), "driving": driving.getvalue()}


INPUTS = media_inputs()


def intent(**changes):
    return {
        "idempotency_key": "fixture-request-0001",
        "reference_sha256": hashlib.sha256(INPUTS["reference"]).hexdigest(),
        "driving_sha256": hashlib.sha256(INPUTS["driving"]).hexdigest(),
        "reference_point": {"x": 0.5, "y": 0.5},
        "target_point": {"x": 0.5, "y": 0.5},
        "duration_ms": 3400,
        "start_ms": 0,
        "seed": 42,
        "prompt": "A person waves at the camera on a quiet street.",
        "tier": "simple-motion",
    } | changes


def create(api, headers, **changes):
    response = api.post("/api/imprint/jobs", json=intent(**changes), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def upload_inputs(api, job_id, headers):
    for kind, data in INPUTS.items():
        stored = api.upload_input(job_id, kind, data, headers)
        assert stored.ok, stored


def queued(api, headers, **changes):
    """A job queued under configured, closed-by-default dispatch."""
    job = create(api, headers, **changes)
    upload_inputs(api, job["id"], headers)
    submitted = api.post(f"/api/imprint/jobs/{job['id']}/submit", headers=headers)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == "queued"
    return job["id"]


def two_queued_clients(api, first_headers):
    api.configure_provider()
    headers = [first_headers, api.register("b")]
    return [queued(api, auth) for auth in headers], headers


def dispatched_worker(api, runpod, index=0):
    submission = runpod.submissions[index]
    return Worker(api, submission["session_id"], submission["token"])


def leased(api, headers, key="lease-fixture-0001"):
    """An attempt on a directly reserved session; no provider is involved."""
    api.configure_provider()
    job_id = queued(api, headers, idempotency_key=key)
    session_id, token = api.reserve(620_000)
    worker = Worker(api, session_id, token, "00000000-0000-4000-8000-00000000000f")
    attempt = worker.lease()
    assert attempt.ok and attempt.body["job_id"] == job_id, attempt
    return job_id, worker, attempt.body
