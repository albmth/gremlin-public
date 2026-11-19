"""Ported from backend/tests/test_imprint.py: jobs, leases, sessions, dispatch and isolation."""

import hashlib
import math

import pytest
from gremlin_contract.flows import (
    INPUTS,
    create,
    dispatched_worker,
    intent,
    leased,
    queued,
    two_queued_clients,
    upload_inputs,
)
from gremlin_contract.harness import Worker

FULL_SESSION_SECONDS = 2400
# A stopping worker's time to end its own provider job before the controller cancels it.
STOP_GRACE_SECONDS = 90


def test_request_recovery_ownership_and_closed_paid_admission(api, backend, headers):
    job = create(api, headers)["id"]
    backend.restart()
    api.reapply()
    replay = api.post("/api/imprint/jobs", json=intent(), headers=headers)
    assert replay.json()["id"] == job
    assert api.post("/api/imprint/jobs", json=intent(seed=43), headers=headers).status_code == 409
    wrong = api.upload_input(job, "reference", b"wrong", headers)
    assert not wrong.ok and wrong.status in (400, 422), wrong
    assert api.media(job) == set()
    upload_inputs(api, job, headers)
    other = api.register("b")
    assert api.get(f"/api/imprint/jobs/{job}", headers=other).status_code == 404
    refusal = api.post(f"/api/imprint/jobs/{job}/submit", headers=headers)
    assert refusal.status_code == 503
    assert refusal.json()["detail"]["code"] == "IMPRINT_BUDGET_BLOCKED"
    assert api.records()["sessions"] == []


def test_worker_claim_is_stable_and_cancellation_fences_late_publication(api, headers):
    job_id, worker, attempt = leased(api, headers)
    assert worker.lease().body["id"] == attempt["id"]
    assert api.delete(f"/api/imprint/jobs/{job_id}", headers=headers).status_code == 200
    assert worker.progress(attempt["id"], "finishing").status == 409
    assert api.job_row(job_id)["state"] == "cancelled"
    assert worker.lease().status == 409


def test_expired_worker_does_not_regenerate_and_cannot_publish(api, headers):
    job_id, worker, attempt = leased(api, headers)
    api.advance(121)
    api.reconcile()
    assert api.job_row(job_id)["state"] == "failed"
    assert not worker.complete(attempt["id"], {}).ok
    assert len(api.records()["attempts"]) == 1


def test_live_sessions_hold_their_reservation_and_finished_ones_their_cost(api, headers):
    session_id, _ = api.reserve(620_000)
    second = api.post("/__test/reserve", json={"hourly_microusd": 620_000})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "IMPRINT_SESSION_ACTIVE"
    # The measured cost of a finished session is what the budget keeps counting.
    api.finish_session(session_id, 5_000_000 - 1)
    assert api.release(session_id).status_code == 204
    exhausted = api.post("/__test/reserve", json={"hourly_microusd": 620_000})
    assert exhausted.json()["detail"]["code"] == "IMPRINT_BUDGET_EXHAUSTED"


def test_output_bytes_are_verified_before_terminal_publication(api, headers):
    job_id, worker, attempt = leased(api, headers)
    assert worker.progress(attempt["id"], "finishing").ok
    manifest = {}
    for name in ("result.mp4", "result.webp", "poster.jpg"):
        # Transport fixture, never represented as a generated animation.
        stored = worker.upload(attempt["id"], name, b"transport-test")
        assert stored.ok, stored
        manifest[name] = {"bytes": 14, "sha256": hashlib.sha256(b"transport-test").hexdigest()}
    bad = {name: dict(value) for name, value in manifest.items()}
    bad["result.mp4"]["sha256"] = "0" * 64
    assert worker.complete(attempt["id"], bad).status == 409
    assert worker.complete(attempt["id"], manifest).ok
    # A lost completion response must not trigger another inference or fail recovery.
    assert worker.complete(attempt["id"], manifest).ok
    assert not worker.complete(attempt["id"], bad).ok
    assert api.job_row(job_id)["state"] == "succeeded"
    assert not worker.progress(attempt["id"], "running").ok
    api.advance(86401)
    api.reconcile()
    # Creation age cannot delete the inputs of a terminal result in an unsettled session.
    assert api.media(job_id) >= {"reference", "driving", *manifest}
    assert api.job_row(job_id)["state"] == "succeeded"


def test_dispatch_refuses_before_any_provider_request(api, runpod):
    assert api.dispatch()["code"] == "IMPRINT_BUDGET_BLOCKED"
    # A configured endpoint still needs explicit spending authority.
    api.configure_provider()
    api.settings(imprint_rolling_budget_microusd=0)
    assert api.dispatch()["code"] == "IMPRINT_BUDGET_BLOCKED"
    assert runpod.calls == []


def test_lost_submission_is_identified_by_the_worker_and_settled_once(api, headers, runpod):
    api.configure_provider()
    job_id = queued(api, headers)
    runpod.workers["max"] = 1
    runpod.lose_submission_response = True
    assert api.tick()["raised"] == "ConnectionError"
    assert len(runpod.submissions) == 1
    assert runpod.submissions[0]["origin"] == "http://127.0.0.1:8000"
    # The worker's first lease carries the provider job identity the service never received.
    worker = dispatched_worker(api, runpod)
    assert worker.lease("provider-job-1").body["job_id"] == job_id
    runpod.status = {"status": "COMPLETED", "delayTime": 240_000, "executionTime": 330_000}
    api.tick()
    api.tick()
    assert len(runpod.submissions) == 1
    row = api.session_row(worker.session_id)
    assert row["state"] == "finished" and row["provider_job_id"] == "provider-job-1"
    # Billed: the reported cold start and queue wait, the handler and the idle window.
    assert row["measured_microusd"] == math.ceil(650_000 * (240 + 330 + 30) / 3600)
    assert row["measured_microusd"] < row["reserved_microusd"]


@pytest.mark.parametrize("refusals", [2, 99])
def test_paused_endpoint_refusal_is_retried_and_never_charged(api, headers, runpod, refusals):
    api.configure_provider()
    job_id = queued(api, headers)
    # The restore succeeds, but the job API keeps refusing for a while.
    runpod.paused_submissions = refusals
    raised = api.tick()["raised"]
    (session,) = api.records()["sessions"]
    if refusals == 2:
        assert raised is None and len(runpod.submissions) == 1
        assert dispatched_worker(api, runpod).lease().body["job_id"] == job_id
    else:
        assert raised == "HTTPError" and runpod.submissions == []
        assert session["state"] == "finished" and session["measured_microusd"] == 0
        assert session["dispatch_hold"]
        assert api.job_row(job_id)["state"] == "queued"


@pytest.mark.parametrize(
    ("scenario", "first_state"),
    [
        ("worker_failed", "failed"),
        ("worker_failed_provider_completed", "failed"),
        ("worker_lease_expired", "failed"),
        ("provider_shortage", "queued"),
        ("delayed_empty_completion", "queued"),
        ("cancelled", "cancelled"),
        ("cancelled_worker_ended", "cancelled"),
        ("cancelled_status_unknown", "cancelled"),
        ("cancelled_cancel_unknown", "cancelled"),
        ("provider_unknown", "queued"),
        ("submission_unknown", "queued"),
        ("provider_overrun", "queued"),
        ("unknown_provider_state", "queued"),
    ],
)
def test_two_client_session_failure_isolation_and_durable_accounting(
    api, headers, runpod, scenario, first_state
):
    jobs, auth = two_queued_clients(api, headers)
    runpod.lose_submission_response = scenario == "submission_unknown"
    raised = api.tick()["raised"]
    assert raised == ("ConnectionError" if runpod.lose_submission_response else None)
    assert len(runpod.submissions) == 1
    # A provider week-long scale-down to zero is restored to one, keeping other settings.
    assert runpod.patches == [{"workers": {"min": 0, "idleTimeout": 60, "max": 1}}]
    worker = dispatched_worker(api, runpod)
    runpod.status = {"status": "FAILED", "delayTime": 2000, "executionTime": 1000}
    if scenario.startswith(("worker_", "cancelled")):
        attempt = worker.lease()
        assert attempt.body["job_id"] == jobs[0]
        attempt = attempt.body
        if scenario.startswith("worker_"):
            if scenario == "worker_lease_expired":
                api.advance(121)
                api.reconcile()
            else:
                assert worker.progress(attempt["id"], "failed").ok
            if scenario == "worker_failed_provider_completed":
                runpod.status["status"] = "COMPLETED"
        else:
            cancelled = api.delete(f"/api/imprint/jobs/{jobs[0]}", headers=auth[0])
            assert cancelled.status_code == 200, cancelled.text
            if scenario != "cancelled_worker_ended":
                runpod.status["status"] = "CANCELLED"
        assert worker.lease().status == 409
        assert worker.complete(attempt["id"], {}).status == 409
    elif scenario == "provider_shortage":
        runpod.status = {"status": "TIMED_OUT"}
    elif scenario == "delayed_empty_completion":
        api.advance(FULL_SESSION_SECONDS - 899)
        empty = worker.lease()
        assert empty.ok and empty.body is None
        runpod.status["status"] = "COMPLETED"
    elif scenario in {"submission_unknown", "provider_overrun", "unknown_provider_state"}:
        runpod.status = {
            "status": "NEW_PROVIDER_STATE" if scenario == "unknown_provider_state" else "IN_QUEUE"
        }
        if scenario == "unknown_provider_state":
            api.tick()
            assert api.session_row(worker.session_id)["state"] == "running"
            assert all(api.job_row(job)["state"] == "queued" for job in jobs)
            assert len(runpod.submissions) == 1
            assert runpod.cancels() == []
        api.advance(FULL_SESSION_SECONDS + 601)
    if scenario in {"provider_unknown", "cancelled_status_unknown"}:
        runpod.status_code = 404
    if scenario == "cancelled_cancel_unknown":
        runpod.cancel_code = 404
        runpod.status = {"status": "IN_PROGRESS"}
        api.tick()
        # The cancelled worker gets a grace period to end its own provider job.
        assert runpod.cancels() == []
        api.advance(STOP_GRACE_SECONDS)
        api.tick()
        assert api.session_row(worker.session_id)["state"] == "stopping"
        assert runpod.calls[-1][1].endswith("/cancel/provider-job-1")
        assert len(runpod.submissions) == 1
        runpod.status_code = 404

    reserved = api.session_row(worker.session_id)["reserved_microusd"]
    # A failure the worker reports, or that ends a worker the provider then reports with
    # timings, is the job's own outcome: the cost is known and the queue keeps moving.
    job_level = scenario.startswith("worker_") or scenario == "cancelled_worker_ended"
    api.tick()
    assert api.job_row(jobs[0])["state"] == first_state
    assert len(runpod.submissions) == 1 + job_level
    settled = api.session_row(worker.session_id)
    assert settled["state"] == "finished"
    assert settled["dispatch_hold"] == int(not job_level)
    full_reservation = scenario in {
        "provider_shortage",
        "provider_unknown",
        "cancelled_status_unknown",
        "cancelled_cancel_unknown",
        "submission_unknown",
        "provider_overrun",
        "unknown_provider_state",
    }
    assert settled["measured_microusd"] == (
        reserved if full_reservation else math.ceil(650_000 * 33 / 3600)
    )

    if job_level:
        assert dispatched_worker(api, runpod, 1).lease().body["job_id"] == jobs[1]
        assert api.session_row(worker.session_id) == settled
    else:
        assert api.job_row(jobs[1])["state"] == "queued"
        calls_at_settlement = list(runpod.calls)
        for _ in range(2):
            api.tick()
            assert api.session_row(worker.session_id) == settled
        assert runpod.calls == calls_at_settlement
        records = api.records()
        assert len(records["sessions"]) == 1
        assert not [a for a in records["attempts"] if a["job_id"] == jobs[1]]
    for job_id, client in zip(jobs, auth, strict=True):
        recovered = api.post("/api/imprint/jobs", json=intent(), headers=client).json()
        assert recovered["id"] == job_id
        assert recovered["state"] == api.job_row(job_id)["state"]
        if recovered["state"] == "queued":
            assert api.media(job_id) >= set(INPUTS)
    assert api.get(f"/api/imprint/jobs/{jobs[1]}", headers=auth[0]).status_code == 404


def test_input_rejection_fails_only_its_job_and_the_worker_serves_the_next(api, headers, runpod):
    jobs, auth = two_queued_clients(api, headers)
    api.tick()
    worker = dispatched_worker(api, runpod)
    first = worker.lease().body
    assert first["job_id"] == jobs[0]
    assert worker.progress(first["id"], "failed", failure_code="input_rejected").status == 422
    assert worker.progress(
        first["id"], "failed", failure_code="input_rejected", failure_reason="target_not_tracked"
    ).ok
    rejected = api.get(f"/api/imprint/jobs/{jobs[0]}", headers=auth[0]).json()
    assert rejected["state"] == "failed"
    assert rejected["outcome"] | {"at": 0} == {
        "kind": "failed",
        "at": 0,
        "attempt_started": True,
        "failure_code": "input_rejected",
        "failure_reason": "target_not_tracked",
    }
    # The same session leases the other client's job.
    second = worker.lease().body
    assert second["job_id"] == jobs[1]
    worker.publish(second["id"])
    assert worker.lease().body is None
    runpod.status = {"status": "COMPLETED", "delayTime": 2000, "executionTime": 1000}
    api.tick()
    settled = api.session_row(worker.session_id)
    assert settled["state"] == "finished" and settled["dispatch_hold"] == 0
    assert api.job_row(jobs[1])["state"] == "succeeded"
    assert len(runpod.submissions) == 1


def test_a_session_left_without_work_does_not_pause_the_queue(api, headers, runpod):
    api.configure_provider()
    waiting = queued(api, headers, idempotency_key="cancelled-while-starting")
    api.tick()
    worker = dispatched_worker(api, runpod)
    # The user gives up while the GPU starts; the worker then finds an empty queue.
    cancelled = api.delete(
        f"/api/imprint/jobs/{waiting}", params={"if_not_started": "true"}, headers=headers
    )
    assert cancelled.json()["state"] == "cancelled"
    assert worker.lease().body is None
    runpod.status = {"status": "COMPLETED", "delayTime": 2000, "executionTime": 1000}
    api.tick()
    settled = api.session_row(worker.session_id)
    assert settled["state"] == "finished" and settled["dispatch_hold"] == 0
    queued(api, headers, idempotency_key="next-after-empty-session")
    api.tick()
    assert len(runpod.submissions) == 2


def test_operator_release_recovers_original_queue_without_bypassing_dispatch_authority(
    api, headers, runpod
):
    jobs, _ = two_queued_clients(api, headers)
    api.tick()
    worker = dispatched_worker(api, runpod)
    attempt = worker.lease().body
    assert api.release(worker.session_id).status_code == 409
    assert worker.progress(attempt["id"], "failed").ok
    # The provider had to time the job out, so the session needs review.
    runpod.status = {"status": "TIMED_OUT", "delayTime": 2000, "executionTime": 1000}
    api.tick()
    settled = api.session_row(worker.session_id)
    assert settled["dispatch_hold"] == 1
    requests_after_failure = list(runpod.calls)

    assert api.release(worker.session_id, authorize=False).status_code == 503
    held = api.post("/__test/reserve", json={"hourly_microusd": 650_000})
    assert held.json()["detail"]["code"] == "IMPRINT_DISPATCH_HELD"
    assert api.dispatch()["code"] == "IMPRINT_DISPATCH_HELD"
    assert runpod.calls == requests_after_failure

    assert api.release(worker.session_id).status_code == 204
    released = api.session_row(worker.session_id)
    assert released == settled | {"dispatch_hold": 0}
    assert api.release(worker.session_id).status_code == 204
    assert api.session_row(worker.session_id) == released
    api.finish_session(
        worker.session_id,
        1_000_000,
        error="A stale reconciliation result",
        provider={"status": "TIMED_OUT"},
    )
    assert api.session_row(worker.session_id) == released
    for changes in (
        {"controller_enabled": False},
        {"imprint_rolling_budget_microusd": 0},
        {"imprint_budget_microusd": settled["measured_microusd"]},
        {"imprint_runpod_key": None},
    ):
        refused = api.post("/__test/dispatch", json=changes).json()
        assert refused["raised"] == "HTTPException", (changes, refused)
        assert runpod.calls == requests_after_failure
    assert api.job_row(jobs[1])["state"] == "queued"

    runpod.status = {"status": "IN_QUEUE"}
    api.tick()
    assert len(runpod.submissions) == 2
    recovered = dispatched_worker(api, runpod, 1)
    next_attempt = recovered.lease().body
    assert next_attempt["job_id"] == jobs[1]
    recovered.publish(next_attempt["id"])
    runpod.status = {"status": "COMPLETED", "delayTime": 2000, "executionTime": 1000}
    api.tick()
    assert api.job_row(jobs[0])["state"] == "failed"
    assert api.job_row(jobs[1])["state"] == "succeeded"
    assert api.session_row(worker.session_id) == released
    assert api.session_row(recovered.session_id)["dispatch_hold"] == 0
    records = api.records()
    assert len(records["sessions"]) == 2
    assert sorted(a["job_id"] for a in records["attempts"]) == sorted(jobs)


@pytest.mark.parametrize("result_state", ["succeeded", "cancelled", "expired"])
def test_successful_session_continues_second_client_under_existing_authority(
    api, headers, runpod, result_state
):
    jobs, auth = two_queued_clients(api, headers)
    api.tick()
    first = dispatched_worker(api, runpod)
    attempt = first.lease().body
    assert attempt["job_id"] == jobs[0]
    first.publish(attempt["id"])
    if result_state == "cancelled":
        assert api.delete(f"/api/imprint/jobs/{jobs[0]}", headers=auth[0]).status_code == 200
    elif result_state == "expired":
        api.seed_job(jobs[0], expires_at=api.now() - 1)
        api.reconcile()
    runpod.status = {"status": "COMPLETED", "delayTime": 2000, "executionTime": 1000}
    api.tick()
    assert api.job_row(jobs[0])["state"] == result_state
    assert api.job_row(jobs[1])["state"] == "queued"
    assert api.session_row(first.session_id)["dispatch_hold"] == 0
    assert len(runpod.submissions) == 2
    assert dispatched_worker(api, runpod, 1).lease().body["job_id"] == jobs[1]


@pytest.mark.parametrize("failed_method", ["GET", "PATCH"])
def test_provider_preparation_failure_holds_queue_without_submission_or_charge(
    api, headers, runpod, failed_method
):
    jobs, _ = two_queued_clients(api, headers)
    runpod.management_failure = failed_method
    assert api.tick()["raised"] == "ConnectionError"
    sessions = api.records()["sessions"]
    assert len(sessions) == 1
    settled = sessions[0]
    assert settled["state"] == "finished"
    assert settled["dispatch_hold"] == 1
    assert settled["provider_job_id"] is None
    assert settled["measured_microusd"] == 0
    assert runpod.submissions == []
    assert all(api.job_row(job)["state"] == "queued" for job in jobs)
    calls_after_failure = list(runpod.calls)
    for _ in range(2):
        api.tick()
    assert runpod.calls == calls_after_failure
    assert api.session_row(settled["id"]) == settled


@pytest.mark.parametrize("already_terminal", [False, True])
def test_provider_cancellation_error_does_not_prevent_terminal_reconciliation(
    api, headers, runpod, already_terminal
):
    jobs, _ = two_queued_clients(api, headers)
    api.tick()
    worker = dispatched_worker(api, runpod)
    attempt = worker.lease().body
    assert worker.progress(attempt["id"], "failed").ok
    runpod.cancel_code = 500
    if not already_terminal:
        runpod.status = {"status": "IN_PROGRESS"}
        assert api.tick()["raised"] is None
        assert runpod.cancels() == []
        api.advance(STOP_GRACE_SECONDS)
        assert api.tick()["raised"] == "HTTPError"
        assert api.session_row(worker.session_id)["state"] == "stopping"
        assert len(runpod.submissions) == 1
        assert api.job_row(jobs[1])["state"] == "queued"
        assert worker.lease().status == 409
    runpod.status = {"status": "FAILED", "delayTime": 2000, "executionTime": 1000}
    api.tick()
    settled = api.session_row(worker.session_id)
    assert settled["state"] == "finished"
    # The provider still ended the job by itself with timings: no hold, the queue moves on.
    assert settled["dispatch_hold"] == 0
    assert api.job_row(jobs[0])["state"] == "failed"
    assert len(runpod.cancels()) == int(not already_terminal)
    assert len(runpod.submissions) == 2
    assert dispatched_worker(api, runpod, 1).lease().body["job_id"] == jobs[1]
    assert api.session_row(worker.session_id) == settled


@pytest.mark.parametrize("status_failure", ["http", "transport"])
def test_provider_status_failure_does_not_prevent_cancelling_stopped_worker(
    api, headers, runpod, status_failure
):
    jobs, _ = two_queued_clients(api, headers)
    api.tick()
    worker = dispatched_worker(api, runpod)
    attempt = worker.lease().body
    assert worker.progress(attempt["id"], "failed").ok
    runpod.status_failure = status_failure
    api.advance(STOP_GRACE_SECONDS)
    assert api.tick()["raised"] in {"HTTPError", "ConnectionError"}
    assert runpod.calls[-1][1].endswith("/cancel/provider-job-1")
    unconfirmed = api.session_row(worker.session_id)
    assert unconfirmed["state"] == "stopping"
    assert unconfirmed["finished_at"] is None
    assert len(runpod.submissions) == 1
    assert api.job_row(jobs[1])["state"] == "queued"
    assert worker.lease().status == 409

    runpod.status_failure = None
    runpod.status = {"status": "CANCELLED", "delayTime": 2000, "executionTime": 1000}
    api.tick()
    settled = api.session_row(worker.session_id)
    assert settled["state"] == "finished"
    assert settled["dispatch_hold"] == 1
    assert api.job_row(jobs[1])["state"] == "queued"
    assert len(runpod.submissions) == 1
    calls_after_settlement = list(runpod.calls)
    api.tick()
    assert runpod.calls == calls_after_settlement
    assert api.session_row(worker.session_id) == settled


def test_a_restarted_worker_invocation_cannot_reclaim_an_unfinished_lease(api, backend, headers):
    """Ported from test_retention.py: the executor fence survives a service restart."""
    _, worker, attempt = leased(api, headers)
    backend.restart()
    api.reapply()
    restarted = Worker(api, worker.session_id, worker.token, "00000000-0000-4000-8000-0000000000aa")
    refused = restarted.lease()
    assert refused.status == 409 and refused.code == "IMPRINT_ATTEMPT_OWNED"
    assert worker.lease().body["id"] == attempt["id"]
    assert len(api.records()["attempts"]) == 1
