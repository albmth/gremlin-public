"""The real worker client (`worker/imprint_worker/client.py`) drains a session end to end.

Inference is replaced by transport fixtures; everything else is the shipped worker code
talking HTTP to the service and to storage.
"""

import hashlib
import math

from gremlin_contract.flows import INPUTS, queued
from imprint_worker import client as worker_client


def test_worker_client_downloads_inputs_and_publishes_through_storage(
    api, headers, tmp_path, monkeypatch
):
    api.configure_provider()
    job_id = queued(api, headers)
    session_id, token = api.reserve(650_000)
    received = {}

    def generate(reference, driving, output, *_args, report, **_kwargs):
        received.update(reference=reference.read_bytes(), driving=driving.read_bytes())
        report("running", {"tracking_seconds": 1.0})
        output.mkdir()
        files = {}
        for name in ("result.mp4", "result.webp", "poster.jpg"):
            data = f"transport output: {name}".encode()
            (output / name).write_bytes(data)
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        return files, {"total_seconds": 2.0}

    monkeypatch.setattr(worker_client, "run", generate)
    worker = worker_client.Client(
        "https://placeholder.invalid", session_id, token, jobs_root=tmp_path / "jobs"
    )
    # The worker insists on HTTPS; point the already constructed client at the loopback API.
    worker.base = f"{api.url}/api/imprint/worker/{session_id}"
    assert worker.drain() == 1
    assert received == INPUTS
    job = api.get(f"/api/imprint/jobs/{job_id}", headers=headers).json()
    assert job["state"] == "succeeded"
    for name, expected in job["result"]["files"].items():
        data = api.download(job_id, name, headers).content
        assert hashlib.sha256(data).hexdigest() == expected["sha256"]
    timings = api.records()["attempts"][0]["timings"]
    assert timings["total_seconds"] == 2.0
    assert timings["tracking_seconds"] == 1.0
    for key in ("input_transfer_seconds", "delivery_seconds", "attempt_seconds"):
        assert math.isfinite(timings[key]) and timings[key] >= 0
    assert worker.session_timings["attempt_seconds"] >= timings["attempt_seconds"]
    assert worker.session_timings["completion_seconds"] >= 0
    assert not (tmp_path / "jobs").exists() or not any((tmp_path / "jobs").iterdir())
