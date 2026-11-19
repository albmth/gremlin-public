"""Ported from backend/tests/test_retention.py: deterministic retention on a frozen clock."""

import hashlib
import io
import json
import zipfile

import pytest
from gremlin_contract.flows import create, intent, queued, upload_inputs
from gremlin_contract.harness import Worker

T0 = 1_800_000_000.0
MEDIA_SECONDS = 72 * 3600
DIAGNOSTIC_SECONDS = 7 * 86400
RECORD_SECONDS = 30 * 86400
RESULTS = ("result.mp4", "result.webp", "poster.jpg")
ALL_MEDIA = {"reference", "driving", *RESULTS, "diagnostics.zip"}


def diagnostics(stage="running", **extra):
    record = {"schema_version": 1, "stage": stage, "timings": {"total_seconds": 1}} | extra
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("diagnostics.json", json.dumps(record))
    return buffer.getvalue()


@pytest.fixture
def frozen(api, headers):
    api.freeze(T0)
    api.configure_provider()
    return api, headers


def attempt(api, headers, key="private-intent-key-0001"):
    job_id = queued(api, headers, idempotency_key=key)
    session_id, token = api.reserve(650_000)
    worker = Worker(api, session_id, token)
    leased = worker.lease()
    assert leased.ok and leased.body["job_id"] == job_id, leased
    return job_id, worker, leased.body["id"]


def files(worker, attempt_id):
    """Every worker-written kind, as transport fixtures; inputs were uploaded by the client."""
    for name in RESULTS:
        assert worker.upload(attempt_id, name, b"private pixels").ok
    assert worker.upload(attempt_id, "diagnostics.zip", diagnostics()).ok


def succeed(api, headers, *, settle=True):
    job_id, worker, attempt_id = attempt(api, headers)
    files(worker, attempt_id)
    assert worker.progress(attempt_id, "finishing", {"total_seconds": 1}).ok
    manifest = {
        name: {"bytes": 14, "sha256": hashlib.sha256(b"private pixels").hexdigest()}
        for name in RESULTS
    }
    assert worker.complete(attempt_id, manifest).ok
    if settle:
        api.finish_session(
            worker.session_id, 12345, provider={"status": "COMPLETED", "executionTime": 1}
        )
    return job_id, worker


def test_terminal_media_diagnostics_and_personal_rows_have_independent_boundaries(frozen):
    api, headers = frozen
    job_id, worker = succeed(api, headers)
    ended = api.job_row(job_id)["terminal_at"]
    assert ended == T0
    for delta in (MEDIA_SECONDS - 0.001, MEDIA_SECONDS):
        api.freeze(ended + delta)
        api.reconcile()
        media = api.media(job_id)
        assert ("reference" in media) == (delta < MEDIA_SECONDS)
        assert ("result.mp4" in media) == (delta < MEDIA_SECONDS)
        assert "diagnostics.zip" in media
    for delta in (DIAGNOSTIC_SECONDS - 0.001, DIAGNOSTIC_SECONDS):
        api.freeze(ended + delta)
        api.reconcile()
        assert ("diagnostics.zip" in api.media(job_id)) == (delta < DIAGNOSTIC_SECONDS)
        assert api.job_row(job_id)["terminal_at"] == ended
    api.freeze(ended + RECORD_SECONDS - 0.001)
    api.reconcile()
    assert api.get(f"/api/imprint/jobs/{job_id}", headers=headers).status_code == 200
    api.freeze(ended + RECORD_SECONDS)
    api.reconcile()
    assert api.get(f"/api/imprint/jobs/{job_id}", headers=headers).status_code == 404
    records = api.records()
    assert records["jobs"] == [] and records["attempts"] == []
    ledger = api.session_row(worker.session_id)
    assert ledger["measured_microusd"] == 12345
    assert ledger["provider"] is None and ledger["token_cleared"]
    # The permanent replay ledger keeps a digest and the admission time, nothing personal.
    (tombstone,) = records["replays"]
    assert set(tombstone) == {"digest", "created_at"}
    assert "private" not in json.dumps(tombstone)
    assert records["media"] == {}


def test_saved_only_shortens_and_late_replays_cannot_resurrect_media(frozen):
    api, headers = frozen
    job_id, _ = succeed(api, headers)
    terminal = api.job_row(job_id)["terminal_at"]
    saved = f"/api/imprint/jobs/{job_id}/saved"
    assert api.post(saved, headers=headers).json() == {"acknowledged": True}
    deadline = terminal + 86400
    assert api.job_row(job_id)["expires_at"] == deadline
    api.advance(3600)
    api.post(saved, headers=headers)
    assert api.job_row(job_id)["expires_at"] == deadline
    api.freeze(deadline)
    result = f"/api/imprint/jobs/{job_id}/results/result.mp4"
    assert api.get(result, headers=headers).status_code == 404
    assert api.post(saved, headers=headers).json() == {"acknowledged": True}
    assert api.get(result, headers=headers).status_code == 404
    api.reconcile()
    assert api.job_row(job_id)["terminal_at"] == terminal
    assert "diagnostics.zip" in api.media(job_id)


@pytest.mark.parametrize("resolution", ["live", "held"])
def test_uncertain_or_held_work_retains_media_records_and_full_reservation(
    frozen, backend, resolution
):
    api, headers = frozen
    job_id, worker, attempt_id = attempt(api, headers)
    files(worker, attempt_id)
    assert api.delete(f"/api/imprint/jobs/{job_id}", headers=headers).status_code == 200
    reservation = api.session_row(worker.session_id)["reserved_microusd"]
    if resolution == "held":
        api.finish_session(worker.session_id, reservation, error="Provider job unknown")
    api.advance(RECORD_SECONDS + 1)
    backend.restart()
    api.reapply()
    api.reconcile()
    assert api.job_row(job_id)["state"] == "cancelled"
    assert api.media(job_id) == ALL_MEDIA
    assert (
        api.get(f"/api/imprint/jobs/{job_id}/results/result.mp4", headers=headers).status_code
        == 404
    )
    assert api.get(f"/api/imprint/jobs/{job_id}/diagnostics", headers=headers).status_code == 404
    if resolution == "live":
        api.finish_session(worker.session_id, reservation, error="Provider job unknown")
    assert api.release(worker.session_id).status_code == 204
    api.reconcile()
    assert api.get(f"/api/imprint/jobs/{job_id}", headers=headers).status_code == 404
    api.finish_session(worker.session_id, 0)
    assert api.session_row(worker.session_id)["measured_microusd"] == reservation
    assert api.records()["attempts"] == []
    api.settings(imprint_budget_microusd=reservation)
    exhausted = api.post("/__test/reserve", json={"hourly_microusd": 650_000})
    assert exhausted.json()["detail"]["code"] == "IMPRINT_BUDGET_EXHAUSTED"


def test_queued_expiry_during_hold_keeps_original_deadline_and_retains_evidence(frozen):
    api, headers = frozen
    session_id, _ = api.reserve(650_000)
    api.finish_session(session_id, 0)
    job_id = queued(api, headers, idempotency_key="private-intent-key-0001")
    expires = api.job_row(job_id)["expires_at"]
    api.freeze(expires)
    api.reconcile()
    expired = api.job_row(job_id)
    assert expired["state"] == "expired" and expired["terminal_at"] == expires
    assert expired["retention_hold"] == 1
    api.advance(RECORD_SECONDS + 1)
    api.reconcile()
    assert "reference" in api.media(job_id)
    assert api.release(session_id).status_code == 204
    api.reconcile()
    assert api.media(job_id) == set()
    retired = api.post(
        "/api/imprint/jobs", json=intent(idempotency_key="private-intent-key-0001"), headers=headers
    )
    assert retired.json()["detail"]["code"] == "IMPRINT_INTENT_EXPIRED"


def test_old_inputs_survive_a_late_lease_and_failures_begin_terminal_retention(frozen):
    api, headers = frozen
    job_id = create(api, headers, idempotency_key="private-intent-key-0001")["id"]
    upload_inputs(api, job_id, headers)
    api.advance(86400 - 1)
    assert api.post(f"/api/imprint/jobs/{job_id}/submit", headers=headers).json()["state"] == (
        "queued"
    )
    session_id, token = api.reserve(650_000)
    worker = Worker(api, session_id, token)
    attempt_id = worker.lease().body["id"]
    assert worker.upload(attempt_id, "diagnostics.zip", diagnostics()).ok
    api.advance(2)
    api.reconcile()
    assert api.job_row(job_id)["state"] == "preparing"
    assert "reference" in api.media(job_id)
    assert worker.progress(attempt_id, "failed").ok
    terminal = api.now()
    assert api.job_row(job_id)["expires_at"] == terminal + MEDIA_SECONDS
    api.finish_session(session_id, 100)
    assert api.release(session_id).status_code == 204
    api.advance(MEDIA_SECONDS)
    api.reconcile()
    media = api.media(job_id)
    assert "reference" not in media and "diagnostics.zip" in media
    assert api.job_row(job_id)["state"] == "failed"


def test_unsettled_success_keeps_integrity_manifest_after_access_expires(frozen):
    api, headers = frozen
    job_id, worker = succeed(api, headers, settle=False)
    original = api.job_row(job_id)["result"]
    api.advance(MEDIA_SECONDS)
    api.reconcile()
    expired = api.job_row(job_id)
    assert expired["state"] == "expired"
    assert expired["result"] == original
    assert api.get(f"/api/imprint/jobs/{job_id}", headers=headers).json()["result"] is None
    assert "result.mp4" in api.media(job_id)
    api.finish_session(worker.session_id, 12345)
    api.reconcile()
    assert api.job_row(job_id)["result"] is None
    assert "result.mp4" not in api.media(job_id)


def test_interrupted_deletion_retries_before_pruning(frozen):
    api, headers = frozen
    job_id, _ = succeed(api, headers)
    api.advance(RECORD_SECONDS)
    api.fail_next_delete("result.mp4")
    assert api.reconcile()["raised"] == "OSError"
    # The record that names the remaining media survives the failed pass.
    assert api.job_row(job_id) is not None
    assert "result.mp4" in api.media(job_id)
    api.reconcile()
    assert api.media(job_id) == set()
    assert api.job_row(job_id) is None
    retired = api.post(
        "/api/imprint/jobs", json=intent(idempotency_key="private-intent-key-0001"), headers=headers
    )
    assert retired.status_code == 410


def test_record_pruning_never_reopens_old_key_or_resets_daily_admission(frozen):
    api, headers = frozen
    api.settings(imprint_daily_jobs=1)
    job_id = create(api, headers, idempotency_key="private-intent-key-0001")["id"]
    assert api.delete(f"/api/imprint/jobs/{job_id}", headers=headers).status_code == 200
    # Even an inconsistent legacy terminal timestamp cannot reset a recent admission.
    api.seed_job(job_id, terminal_at=api.now() - RECORD_SECONDS)
    api.reconcile()
    assert api.job_row(job_id) is None
    old = intent(idempotency_key="private-intent-key-0001")
    refused = api.post("/api/imprint/jobs", json=old, headers=headers)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (
        410,
        "IMPRINT_INTENT_EXPIRED",
    )
    new = intent(idempotency_key="new-distinct-intent")
    limited = api.post("/api/imprint/jobs", json=new, headers=headers)
    assert limited.json()["detail"]["code"] == "IMPRINT_LIMIT"
    api.advance(86400)
    admitted = api.post("/api/imprint/jobs", json=new, headers=headers).json()
    assert admitted["state"] == "uploading"
    api.delete(f"/api/imprint/jobs/{admitted['id']}", headers=headers)
    assert api.post("/api/imprint/jobs", json=old, headers=headers).status_code == 410


def test_provider_and_timing_evidence_omit_sensitive_strings(frozen):
    api, headers = frozen
    _, worker, attempt_id = attempt(api, headers)
    assert worker.progress(attempt_id, "failed", {"total_seconds": 2}).ok
    # A provider timeout holds the session for review, so its evidence outlives the window.
    api.finish_session(
        worker.session_id,
        100,
        provider={
            "status": "TIMED_OUT",
            "executionTime": 12,
            "delayTime": 3,
            "output": {"token": "private token"},
            "error": "private likeness",
            "input": {"prompt": "private likeness"},
        },
    )
    row = api.session_row(worker.session_id)
    assert row["provider"] == {"status": "TIMED_OUT", "executionTime": 12, "delayTime": 3}
    assert row["token_cleared"]
    assert api.records()["attempts"][0]["timings"] == {"total_seconds": 2}
    api.advance(DIAGNOSTIC_SECONDS)
    api.reconcile()
    assert api.session_row(worker.session_id)["provider"] is not None
    assert api.release(worker.session_id).status_code == 204
    api.reconcile()
    assert api.session_row(worker.session_id)["provider"] is None
