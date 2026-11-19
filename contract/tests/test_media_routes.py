"""Ported from backend/tests/test_retention_routes.py: media routes, diagnostics and cleanup."""

import hashlib
import io
import json
import zipfile

import pytest
import requests
from gremlin_contract.flows import INPUTS, create, leased, queued, upload_inputs
from gremlin_contract.harness import storage_put

# Longer than any presigned upload window, after which no late transfer can land.
PAST_UPLOAD_WINDOW = 301


def archive(record, *, extra=None, comment=b""):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("diagnostics.json", json.dumps(record))
        if extra is not None:
            bundle.writestr("private.txt", extra)
        bundle.comment = comment
    return buffer.getvalue()


def test_results_are_served_until_cancellation_and_then_removed(api, headers):
    job_id, worker, attempt = leased(api, headers)
    payload = b"transport result" * 10_000
    worker.publish(attempt["id"], payload)
    downloaded = api.download(job_id, "result.mp4", headers)
    assert downloaded.status_code == 200 and downloaded.content == payload
    assert downloaded.headers["Cache-Control"] == "no-store"
    assert api.delete(f"/api/imprint/jobs/{job_id}", headers=headers).status_code == 200
    assert api.download(job_id, "result.mp4", headers).status_code == 404
    api.finish_session(worker.session_id, 100)
    api.release(worker.session_id)
    api.advance(PAST_UPLOAD_WINDOW)
    api.reconcile()
    assert api.media(job_id) == set()
    assert api.download(job_id, "result.mp4", headers).status_code == 404


def test_a_late_presigned_upload_after_cancellation_is_removed_after_its_window(api, headers):
    job_id = create(api, headers)["id"]
    ticket = api.post(
        f"/api/imprint/jobs/{job_id}/inputs/reference/upload",
        json={"bytes": len(INPUTS["reference"])},
        headers=headers,
    ).json()
    assert set(ticket) == {"method", "url", "headers", "expires_at"}
    assert api.delete(f"/api/imprint/jobs/{job_id}", headers=headers).status_code == 200
    refused = api.upload_input(job_id, "reference", INPUTS["reference"], headers)
    assert refused.status == 409 and refused.code == "IMPRINT_SEALED"
    # The storage service still honours the earlier link; deletion waits for it to lapse.
    assert storage_put(ticket, INPUTS["reference"]).ok
    api.reconcile()
    assert api.media(job_id) == {"reference"}
    api.advance(PAST_UPLOAD_WINDOW)
    api.reconcile()
    assert api.media(job_id) == set()


def test_upload_links_are_refused_after_the_upload_window_and_size_limits_hold(api, headers):
    job_id = create(api, headers)["id"]
    path = f"/api/imprint/jobs/{job_id}/inputs/reference/upload"
    too_large = api.post(path, json={"bytes": 10 * 1024 * 1024 + 1}, headers=headers)
    assert too_large.status_code == 413
    assert too_large.json()["detail"]["code"] == "IMPRINT_MEDIA_TOO_LARGE"
    assert api.post(path, json={"bytes": 0}, headers=headers).status_code == 422
    ticket = api.post(path, json={"bytes": len(INPUTS["reference"])}, headers=headers).json()
    # Storage refuses bytes other than the declared hash and length, and a missing checksum.
    assert not storage_put(ticket, b"reference-but-longer").ok
    assert not storage_put(ticket, b"ref3r3nce").ok
    unsigned = requests.put(ticket["url"], data=INPUTS["reference"], timeout=30)
    assert unsigned.status_code in (400, 403)
    assert api.media(job_id) == set()
    api.advance(86401)
    sealed = api.post(path, json={"bytes": 9}, headers=headers)
    assert sealed.status_code == 409 and sealed.json()["detail"]["code"] == "IMPRINT_SEALED"


def test_submit_verifies_both_stored_inputs(api, headers):
    api.configure_provider()
    job_id = create(api, headers)["id"]
    assert api.upload_input(job_id, "reference", INPUTS["reference"], headers).ok
    missing = api.post(f"/api/imprint/jobs/{job_id}/submit", headers=headers)
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "IMPRINT_INPUT_REQUIRED"
    assert api.upload_input(job_id, "driving", INPUTS["driving"], headers).ok
    submitted = api.post(f"/api/imprint/jobs/{job_id}/submit", headers=headers).json()
    assert submitted["state"] == "queued"
    assert api.job_row(job_id)["uploads"] == {
        kind: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for kind, data in INPUTS.items()
    }
    assert api.records()["doorbells"] == 1


def test_diagnostics_strip_container_secrets_and_deny_legacy_archives(api, headers):
    job_id, worker, attempt = leased(api, headers)
    record = {"schema_version": 1, "stage": "running", "timings": {"tracking_seconds": 1.25}}
    payload = (
        b"private ZIP prefix"
        + archive(record, comment=b"private ZIP comment")
        + b"private ZIP suffix"
    )
    result = worker.upload(attempt["id"], "diagnostics.zip", payload)
    assert result.ok, result
    downloaded = api.get(f"/api/imprint/jobs/{job_id}/diagnostics", headers=headers)
    assert downloaded.status_code == 200
    stored = downloaded.content
    assert b"private" not in stored and len(stored) < len(payload)
    assert result.body == {"sha256": hashlib.sha256(stored).hexdigest(), "bytes": len(stored)}
    api.put_media(job_id, "diagnostics.zip", archive(record, extra="old private prompt"))
    legacy = api.get(f"/api/imprint/jobs/{job_id}/diagnostics", headers=headers)
    assert legacy.status_code == 404 and "private" not in legacy.text
    assert "diagnostics.zip" in api.media(job_id)


@pytest.mark.parametrize(
    "record",
    [
        {"schema_version": 1, "stage": "failed", "timings": {}, "prompt": "private identity"},
        {"schema_version": 1, "stage": "failed", "timings": {"private identity": 1}},
        {"schema_version": 1, "stage": "failed", "timings": {"tracking_seconds": -1}},
        {"schema_version": 1, "stage": "failed", "timings": {"tracking_seconds": float("nan")}},
        {"schema_version": 1, "stage": "failed", "timings": {"tracking_seconds": True}},
        {"schema_version": 1, "stage": "failed", "timings": {}, "failure_code": "private error"},
        {"schema_version": 1, "stage": ["private identity"], "timings": {}},
    ],
)
def test_unapproved_diagnostics_never_publish(api, headers, record):
    job_id, worker, attempt = leased(api, headers)
    response = worker.upload(attempt["id"], "diagnostics.zip", archive(record))
    assert response.status == 422 and "private" not in json.dumps(response.body)
    assert "diagnostics.zip" not in api.media(job_id)


def test_progress_cannot_persist_arbitrary_personal_diagnostics(api, headers):
    _, worker, attempt = leased(api, headers)
    refused = worker.progress(attempt["id"], "running", {"private prompt": 0})
    assert refused.status == 422
    assert worker.progress(attempt["id"], "running", {"tracking_seconds": 1.25}).ok
    assert api.records()["attempts"][0]["timings"] == {"tracking_seconds": 1.25}


def test_cleanup_failure_retries_but_only_reconciles_existing_provider_work(
    api, backend, headers, runpod
):
    api.configure_provider()
    cancelled = create(api, headers)["id"]
    upload_inputs(api, cancelled, headers)
    assert api.delete(f"/api/imprint/jobs/{cancelled}", headers=headers).status_code == 200
    queued(api, api.register("b"))
    api.advance(PAST_UPLOAD_WINDOW)
    api.settings(controller_enabled=True)
    api.fail_next_delete("reference")
    api.post("/__test/sweep")
    assert "reference" in api.media(cancelled)
    assert runpod.submissions == []
    log = backend.log.read_text()
    assert "gremlin.retention_cleanup_unconfirmed" in log
    assert "private filesystem error" not in log
    api.post("/__test/sweep")
    assert api.media(cancelled) == set()
    assert len(runpod.submissions) == 1


@pytest.mark.parametrize("kind", ["reference", "driving"])
def test_invalid_media_is_rejected_before_any_gpu_submission(api, headers, runpod, kind):
    api.configure_provider()
    bad = b"not decodable media"
    job_id = create(api, headers, **{kind + "_sha256": hashlib.sha256(bad).hexdigest()})["id"]
    for name, data in INPUTS.items():
        assert api.upload_input(job_id, name, bad if name == kind else data, headers).ok
    response = api.post(f"/api/imprint/jobs/{job_id}/submit", headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPRINT_MEDIA_INVALID"
    api.dispatch()
    assert api.job_row(job_id)["state"] == "uploading"
    assert runpod.submissions == []
    assert api.records()["sessions"] == []


def test_invalid_trim_is_rejected_and_valid_submit_replays_without_a_second_queue_entry(
    api, headers
):
    api.configure_provider()
    job_id = create(api, headers, start_ms=4000)["id"]
    upload_inputs(api, job_id, headers)
    response = api.post(f"/api/imprint/jobs/{job_id}/submit", headers=headers)
    assert response.status_code == 422
    assert api.job_row(job_id)["state"] == "uploading"
    assert api.delete(f"/api/imprint/jobs/{job_id}", headers=headers).ok
    job_id = create(api, headers, idempotency_key="valid-replayed-0001")["id"]
    upload_inputs(api, job_id, headers)
    for _ in range(2):
        assert (
            api.post(f"/api/imprint/jobs/{job_id}/submit", headers=headers).json()["state"]
            == "queued"
        )
    assert len(api.records()["jobs"]) == 2
