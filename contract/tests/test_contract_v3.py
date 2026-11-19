"""Contract v3: owner scope, honest admission, immutable outcomes, conditional cancel, listing."""

import pytest
import requests
from gremlin_contract.flows import create, intent, leased, queued, upload_inputs
from gremlin_contract.harness import Worker


def admission(api, headers):
    return api.get("/api/imprint/capabilities", headers=headers).json()["admission"]


def test_capabilities_report_the_first_blocker_and_daily_reset(api, headers):
    assert admission(api, headers)["reason_code"] == "endpoint_unconfigured"
    api.configure_provider()
    status = admission(api, headers)
    # Admission is open but nothing will start: the controller is off.
    assert (status["accepting"], status["dispatching"]) == (True, False)
    assert status["reason_code"] == "dispatch_disabled"
    api.settings(controller_enabled=True)
    assert admission(api, headers) | {"observed_at": 0} == {
        "accepting": True,
        "dispatching": True,
        "reason_code": None,
        "retry_after_seconds": None,
        "daily_jobs_limit": 8,
        "daily_jobs_remaining": 8,
        "observed_at": 0,
    }
    api.settings(imprint_daily_jobs=1)
    # A cancelled job still used its daily slot.
    api.delete(f"/api/imprint/jobs/{create(api, headers)['id']}", headers=headers)
    status = admission(api, headers)
    assert (status["accepting"], status["reason_code"]) == (False, "daily_limit_reached")
    assert 86000 < status["retry_after_seconds"] <= 86400
    refused = api.post(
        "/api/imprint/jobs", json=intent(idempotency_key="another-key-00001"), headers=headers
    )
    assert (
        refused.status_code == 429 and refused.json()["detail"]["reason"] == "daily_limit_reached"
    )
    assert 86000 < int(refused.headers["Retry-After"]) <= 86400


def test_delegated_owners_are_isolated_under_one_service_credential(api, headers):
    service = api.register("s")
    api.settings(delegated_clients=["s"])
    alice = service | {"X-Gremlin-Owner": "alice"}
    bob = service | {"X-Gremlin-Owner": "bob"}
    missing = api.get("/api/imprint/jobs", headers=service)
    assert (
        missing.status_code == 400 and missing.json()["detail"]["code"] == "CLIENT_OWNER_REQUIRED"
    )
    # Owner IDs are ASCII letters, digits, _ and - only.
    accented = api.get("/api/imprint/jobs", headers=service | {"X-Gremlin-Owner": "al\u00ece"})
    assert accented.json()["detail"]["code"] == "CLIENT_OWNER_REQUIRED"
    direct = api.get("/api/imprint/jobs", headers=headers | {"X-Gremlin-Owner": "alice"})
    assert direct.json()["detail"]["code"] == "CLIENT_OWNER_UNSUPPORTED"
    first = create(api, alice)["id"]
    # The same key is a different intent in another owner's scope, and each owner has their
    # own active-job slot.
    second = create(api, bob)["id"]
    assert first != second
    assert api.get(f"/api/imprint/jobs/{first}", headers=bob).status_code == 404
    assert api.delete(f"/api/imprint/jobs/{first}", headers=bob).status_code == 404
    assert api.get(f"/api/imprint/jobs/{first}", headers=headers).status_code == 404
    assert [job["id"] for job in api.get("/api/imprint/jobs", headers=alice).json()["jobs"]] == [
        first
    ]
    active = api.post(
        "/api/imprint/jobs", json=intent(idempotency_key="alice-second-0001"), headers=alice
    )
    assert active.status_code == 409 and active.json()["detail"]["code"] == "IMPRINT_ACTIVE"


def test_conditional_cancel_loses_to_a_started_attempt(api, headers):
    api.configure_provider()
    waiting = queued(api, headers, idempotency_key="cancel-before-start")
    cancelled = api.delete(
        f"/api/imprint/jobs/{waiting}", params={"if_not_started": "true"}, headers=headers
    ).json()
    assert cancelled["state"] == "cancelled"
    assert cancelled["outcome"] | {"at": 0} == {
        "kind": "cancelled",
        "at": 0,
        "attempt_started": False,
        "failure_code": None,
        "failure_reason": None,
    }
    job_id, _, _ = leased(api, headers, key="cancel-after-start")
    refused = api.delete(
        f"/api/imprint/jobs/{job_id}", params={"if_not_started": "true"}, headers=headers
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "IMPRINT_STARTED"
    assert api.get(f"/api/imprint/jobs/{job_id}", headers=headers).json()["state"] == "preparing"


def test_outcome_and_failure_code_survive_later_expiry(api, headers):
    job_id, _, _ = leased(api, headers)
    api.advance(121)
    api.reconcile()
    failed = api.get(f"/api/imprint/jobs/{job_id}", headers=headers).json()
    assert failed["state"] == "failed" and failed["progress"] is None
    assert failed["outcome"]["kind"] == "failed"
    assert failed["outcome"]["failure_code"] == "lease_expired"
    assert failed["outcome"]["attempt_started"] is True
    ended = failed["outcome"]["at"]
    api.advance(80 * 3600)
    api.reconcile()
    later = api.get(f"/api/imprint/jobs/{job_id}", headers=headers).json()
    assert later["outcome"]["at"] == ended and later["outcome"]["kind"] == "failed"


def test_progress_reports_stage_and_queue_position(api, headers):
    api.configure_provider()
    other = api.register("b")
    first = queued(api, headers, idempotency_key="queue-first-00001")
    second = queued(api, other, idempotency_key="queue-second-0001")
    one = api.get(f"/api/imprint/jobs/{first}", headers=headers).json()["progress"]
    two = api.get(f"/api/imprint/jobs/{second}", headers=other).json()["progress"]
    assert (one["stage"], one["queue_position"]) == ("queued", 0)
    assert (two["stage"], two["queue_position"]) == ("queued", 1)


def test_execution_follows_the_gpu_session_serving_the_job(api, headers):
    api.configure_provider()
    other = api.register("b")
    first = queued(api, headers, idempotency_key="execution-first-001")
    second = queued(api, other, idempotency_key="execution-second-01")

    def execution(job_id, auth):
        return api.get(f"/api/imprint/jobs/{job_id}", headers=auth).json()["execution"]

    assert execution(first, headers) is None
    session_id, token = api.reserve(620_000)
    requested = execution(first, headers)
    assert requested["gpu_requested_at"] > 0
    assert (requested["started_at"], requested["timings"]) == (None, {})
    # The session starts for the first job in the queue only.
    assert execution(second, other) is None
    worker = Worker(api, session_id, token)
    attempt = worker.lease().body
    timings = {"engine_first_use": 1, "tracking_seconds": 12.5}
    assert worker.progress(attempt["id"], "running", timings).ok
    running = execution(first, headers)
    assert running["gpu_requested_at"] == requested["gpu_requested_at"]
    assert running["started_at"] >= running["gpu_requested_at"]
    assert running["timings"] == timings
    # The next job waits behind a session that is busy with another job.
    assert execution(second, other) is None
    worker.publish(attempt["id"])
    done = api.get(f"/api/imprint/jobs/{first}", headers=headers).json()
    assert done["state"] == "succeeded"
    assert done["execution"]["started_at"] == running["started_at"]


def test_admission_deadline_is_bounded_preserved_and_enforced(api, headers):
    now = api.get("/api/imprint/capabilities", headers=headers).json()["admission"]["observed_at"]
    too_late = api.post(
        "/api/imprint/jobs", json=intent(admission_expires_at=now + 2 * 86400), headers=headers
    )
    assert too_late.json()["detail"]["code"] == "IMPRINT_ADMISSION_TOO_LATE"
    past = api.post("/api/imprint/jobs", json=intent(admission_expires_at=now - 1), headers=headers)
    assert past.json()["detail"]["code"] == "IMPRINT_ADMISSION_EXPIRED"
    body = intent(admission_expires_at=now + 600)
    job = api.post("/api/imprint/jobs", json=body, headers=headers).json()
    assert job["admission_expires_at"] == now + 600
    api.advance(300)
    assert api.post("/api/imprint/jobs", json=body, headers=headers).json()["id"] == job["id"]
    upload_inputs(api, job["id"], headers)
    api.advance(301)
    api.reconcile()
    expired = api.get(f"/api/imprint/jobs/{job['id']}", headers=headers).json()
    assert expired["state"] == "expired" and expired["outcome"]["kind"] == "expired"
    assert expired["outcome"]["attempt_started"] is False


def test_list_filters_by_state_and_preflight_reasons_are_typed(api, headers, runpod):
    api.configure_provider()
    invalid = create(
        api, headers, idempotency_key="list-invalid-0001", duration_ms=5000, start_ms=3000
    )
    upload_inputs(api, invalid["id"], headers)
    refused = api.post(f"/api/imprint/jobs/{invalid['id']}/submit", headers=headers)
    assert refused.status_code == 422
    assert refused.json()["detail"]["reason"] == "driving_too_short"
    listed = api.get("/api/imprint/jobs", params={"state": "active"}, headers=headers).json()
    assert [job["id"] for job in listed["jobs"]] == [invalid["id"]]
    api.delete(f"/api/imprint/jobs/{invalid['id']}", headers=headers)
    assert api.get("/api/imprint/jobs", params={"state": "active"}, headers=headers).json() == {
        "jobs": []
    }
    assert runpod.calls == []


@pytest.mark.parametrize("path", ["/openapi.json"])
def test_openapi_hides_worker_and_provider_routes(api, path):
    document = requests.get(api.url + path, timeout=10).json()
    assert document["info"]["version"].startswith("3.")
    assert all("/worker/" not in route and "/provider/" not in route for route in document["paths"])
    assert "/api/imprint/jobs" in document["paths"]


def test_owner_erasure_retries_after_upload_expiry_and_preserves_other_owner(api, headers, backend):
    from gremlin_contract.flows import upload_inputs

    api.freeze(1_800_000_000.25)
    service = api.register("s")
    api.settings(delegated_clients=["s"])
    alice = service | {"X-Gremlin-Owner": "alice"}
    bob = service | {"X-Gremlin-Owner": "bob"}
    assert api.delete("/api/imprint/owner", headers=headers).status_code == 400
    first = create(api, alice)["id"]
    second = create(api, bob)["id"]
    upload_inputs(api, first, alice)
    receipt = api.delete("/api/imprint/owner", headers=alice).json()
    assert receipt["status"] == "pending"
    assert "owner_erasure" in api.get("/api/imprint/capabilities", headers=alice).json()["features"]
    rejected = api.post(
        "/api/imprint/jobs", headers=alice, json=intent(idempotency_key="new-owner-job-0001")
    )
    assert rejected.status_code == 409
    api.reconcile()
    assert api.delete("/api/imprint/owner", headers=alice).json()["status"] == "pending"
    backend.restart()
    api.reapply()
    api.advance(301)
    api.reconcile()
    completed = api.delete("/api/imprint/owner", headers=alice).json()
    assert completed["status"] == "completed"
    assert completed["requested_at"] == receipt["requested_at"]
    assert not api.media(first)
    assert api.get(f"/api/imprint/jobs/{first}", headers=alice).status_code == 404
    assert api.get(f"/api/imprint/jobs/{second}", headers=bob).status_code == 200
    assert api.delete("/api/imprint/owner", headers=alice).json() == completed


def test_period_limits_settle_once_and_recover_after_rolling_window(api, headers):
    api.freeze(1_800_000_000)
    api.configure_provider()
    api.settings(
        controller_enabled=True,
        imprint_budget_microusd=0,
        imprint_rolling_budget_microusd=600_000,
        imprint_monthly_budget_microusd=1_000_000,
    )
    session, _ = api.reserve(650_000)
    api.finish_session(
        session, 100_000, provider={"status": "COMPLETED", "delayTime": 1, "executionTime": 1}
    )
    api.finish_session(session, 900_000)
    assert api.release(session).status_code == 204
    status = admission(api, headers)
    assert status["reason_code"] == "spending_limit_reached"
    assert status["accepting"] is False and status["retry_after_seconds"] == 86400
    refused = api.post("/__test/reserve", json={"hourly_microusd": 650_000})
    assert refused.status_code == 503
    assert refused.json()["detail"]["reason"] == "spending_limit_reached"
    api.advance(86400)
    assert admission(api, headers)["reason_code"] is None
    api.settings(imprint_monthly_budget_microusd=600_000)
    assert admission(api, headers)["reason_code"] == "spending_limit_reached"
    assert api.post("/api/imprint/jobs", headers=headers, json=intent()).status_code == 429


def test_erasure_removes_private_data_after_fencing_without_releasing_cost_hold(api, headers):
    api.freeze(1_800_000_000)
    service = api.register("s")
    api.settings(delegated_clients=["s"])
    alice = service | {"X-Gremlin-Owner": "alice"}
    job_id, worker, _ = leased(api, alice)
    assert api.delete("/api/imprint/owner", headers=alice).json()["status"] == "pending"
    api.reconcile()
    api.advance(301)
    api.reconcile()
    assert api.delete("/api/imprint/owner", headers=alice).json()["status"] == "pending"
    api.finish_session(worker.session_id, 123_456, error="Provider job unknown")
    api.advance(61)
    api.reconcile()
    assert api.delete("/api/imprint/owner", headers=alice).json()["status"] == "completed"
    assert api.get(f"/api/imprint/jobs/{job_id}", headers=alice).status_code == 404
    assert not api.media(job_id)
    assert api.session_row(worker.session_id)["dispatch_hold"]
    assert api.session_row(worker.session_id)["measured_microusd"] == 123_456
