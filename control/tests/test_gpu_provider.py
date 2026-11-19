"""Exercise paid-attempt ownership against real local storage and an HTTP provider double."""

import hashlib
import json
import sys
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from fastapi import HTTPException
from gremlin_control import clock, controller, gpu_selection, local
from gremlin_control.provider import RunPodEndpoint
from gremlin_control.settings import Settings
from gremlin_control.store import STOP_GRACE_SECONDS, ImprintStore
from test_gpu_selection import explicit_release
from test_gpu_selection import inputs as selection_inputs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "contract"))
from gremlin_contract.fake_runpod import FakeRunPod  # noqa: E402
from gremlin_contract.flows import INPUTS, intent  # noqa: E402

inputs = selection_inputs


@pytest.fixture
def provider_case(inputs, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "gremlinlocal")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "gremlinlocalsecret")
    monkeypatch.setattr(gpu_selection, "load_qualifications", lambda: inputs["qualifications"])
    fake = FakeRunPod().start()
    fake.catalog = inputs["catalog"]
    fake.endpoint = inputs["endpoint"]
    fake.workers = dict(inputs["endpoint"]["workers"], min=0, max=0)
    fake.data_centers = inputs["data_centers"]
    name = "gremlin-gpu-" + uuid.uuid4().hex[:12]
    settings = Settings(
        environment="test",
        dynamodb_endpoint="http://127.0.0.1:8001",
        s3_endpoint="http://127.0.0.1:9100",
        table_name=name,
        media_bucket=name,
        public_base_url="https://example.test",
        controller_enabled=True,
        imprint_runpod_key="fake-provider-key",
        imprint_endpoint_id="qualified-endpoint",
        imprint_hourly_microusd=2_000_000,
        imprint_budget_microusd=2_000_000,
        imprint_rolling_budget_microusd=20_000_000,
        imprint_monthly_budget_microusd=20_000_000,
        runpod_management_url=fake.url + "/mgmt/v2",
        runpod_jobs_url=fake.url + "/jobs/v2",
        runpod_graphql_url=fake.url + "/graphql",
    )
    local.provision(settings)
    clock.freeze(1_800_000_000)
    store = ImprintStore(settings)
    data = INPUTS
    job = store.create("client", "gpu-provider-request", intent(tier="complex-motion"))
    for kind, value in data.items():
        ticket = store.input_ticket(job["id"], kind, len(value))
        response = requests.put(ticket["url"], headers=ticket["headers"], data=value, timeout=5)
        response.raise_for_status()
    store.submit(job["id"])
    yield SimpleNamespace(
        fake=fake,
        store=store,
        endpoint=RunPodEndpoint(settings),
        settings=settings,
        job_id=job["id"],
    )
    clock.set_offset(0)
    fake.stop()


def live(case):
    return case.store.get_session(case.store.ledger()["live_session"])


def unavailable(gpu):
    gpu.update(availability="NONE", dataCenters=[])


def available(gpu, dc="EU-RO-1"):
    gpu.update(availability="HIGH", dataCenters=[{"id": dc, "availability": "HIGH"}])


def test_no_capacity_reserves_nothing_and_next_discovery_dispatches_available_alternative(
    provider_case,
):
    case = provider_case
    for gpu in case.fake.catalog["gpus"]:
        unavailable(gpu)
    case.endpoint.tick()
    assert case.fake.submissions == case.fake.patches == []
    assert case.store.ledger()["live_session"] is None
    assert case.store.gpu_decision()["selected_gpu_ids"] == []
    available(case.fake.catalog["gpus"][1])
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1
    assert case.fake.submissions[0]["gpu_constraints"][0]["gpu_id"] == (
        "NVIDIA RTX PRO 4500 Blackwell"
    )
    session = live(case)
    assert session["reserved_microusd"] == 977_500
    assert json.loads(session["selection_json"])["selected_gpu_ids"] == [
        "NVIDIA RTX PRO 4500 Blackwell"
    ]
    assert case.store.job(case.job_id)["state"] == "queued"


def test_lost_submission_response_never_creates_another_billable_job(provider_case):
    case = provider_case
    case.fake.lose_submission_response = True
    with pytest.raises(requests.ConnectionError):
        case.endpoint.tick()
    session = live(case)
    assert session["provider_job_id"] is None
    case.endpoint.tick()
    clock.freeze(session["queue_timeout_at"] + 1)
    case.endpoint.tick()
    assert case.fake.workers["max"] == 0
    assert len(case.fake.submissions) == 1
    clock.freeze(session["deadline"] + 1)
    case.endpoint.tick()
    case.endpoint.tick()
    finished = case.store.get_session(session["id"])
    assert finished["dispatch_hold"]
    assert finished["measured_microusd"] == finished["reserved_microusd"]
    assert len(case.fake.submissions) == 1
    assert case.store.job(case.job_id)["state"] == "queued"


def test_capacity_retry_requires_terminal_cancellation_and_empty_workers_and_stops_at_three(
    provider_case,
):
    case = provider_case
    # Missing terminal timings keep the full reservation for each cancelled boot.
    case.settings.imprint_budget_microusd = 6_000_000
    first, second = case.fake.catalog["gpus"]
    unavailable(second)
    case.endpoint.tick()
    for attempt in range(3):
        session = live(case)
        assert session and len(case.fake.submissions) == attempt + 1
        selected = json.loads(session["selection_json"])
        gpu_id = selected["selected_gpu_ids"][0]
        dc = selected["candidates"][0]["data_centers"][0]["id"]
        case.fake.worker_records = [
            {
                "gpuTypeId": gpu_id,
                "dataCenterId": dc,
                "image": case.fake.endpoint["image"],
                "status": "INITIALIZING",
                "isStale": False,
            }
        ]
        clock.freeze(session["queue_timeout_at"] + 1)
        case.fake.status = {"status": "IN_QUEUE"}
        case.endpoint.tick()
        assert case.fake.workers["max"] == 0
        assert len(case.fake.cancels()) >= attempt + 1
        assert len(case.fake.submissions) == attempt + 1

        # Terminal /status alone is insufficient: a worker can still be stopping and billing.
        case.fake.status = {"status": "CANCELLED"}
        case.endpoint.tick()
        assert len(case.fake.submissions) == attempt + 1
        case.fake.worker_records = []
        if attempt == 0:
            available(second)
        elif attempt == 1:
            available(first, "EUR-NO-1")
        case.endpoint.tick()
        case.fake.status = {"status": "IN_QUEUE"}
        if attempt < 2:
            # A delayed callback/reader for the old session cannot stop its replacement.
            calls = list(case.fake.calls)
            case.endpoint.settle(session)
            assert case.fake.calls == calls
            assert case.fake.workers["max"] == 1

    case.endpoint.tick()
    assert len(case.fake.submissions) == 3
    assert case.store.ledger()["live_session"] is None
    assert case.store.dispatch_held()
    assert case.store.ledger()["capacity_attempts"] == 3
    assert case.store.ledger()["settled_microusd"] < case.settings.imprint_budget_microusd
    assert case.store.job(case.job_id)["state"] == "queued"


@pytest.mark.parametrize("error", ["worker_session_failed", "worker_gpu_incompatible"])
def test_a_real_worker_lease_prevents_capacity_retry_even_if_provider_reports_queue(
    provider_case, error
):
    case = provider_case
    case.endpoint.tick()
    submission = case.fake.submissions[0]
    attempt = case.store.lease(
        submission["session_id"],
        submission["token"],
        "provider-job-1",
        executor_id=str(uuid.uuid4()),
    )
    session = live(case)
    clock.freeze(session["queue_timeout_at"] + 1)
    case.endpoint.reconcile()
    assert case.fake.cancels() == []
    assert case.store.job(case.job_id)["attempt_id"] == attempt["id"]
    case.fake.status = {"status": "FAILED", "executionTime": 20_000, "error": error}
    case.endpoint.tick()
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1
    assert case.store.dispatch_held()
    assert case.store.job(case.job_id)["state"] == "failed"


def test_job_execution_names_the_gpu_from_its_first_sighting_without_identifiers(provider_case):
    case = provider_case
    case.endpoint.tick()
    session = live(case)
    gpu_id = json.loads(session["selection_json"])["selected_gpu_ids"][0]
    job = case.store.job(case.job_id)
    assert case.store.execution(job, 0) == {
        "gpu_requested_at": session["created_at"],
        "gpu": None,
        "gpu_seen_at": None,
        "started_at": None,
        "timings": {},
    }
    # A job further back in the queue is not the one this session is starting for.
    assert case.store.execution(job, 1) is None
    case.fake.worker_records = [
        {"id": "worker-123", "gpuTypeId": gpu_id, "dataCenterId": "EU-RO-1", "status": "RUNNING"}
    ]
    first = clock.now()
    case.endpoint.reconcile()
    clock.freeze(first + 60)
    case.endpoint.reconcile()
    seen = case.store.execution(job, 0)
    assert (seen["gpu"], seen["gpu_seen_at"]) == (gpu_id, first)
    clock.freeze(first + 120)
    submission = case.fake.submissions[0]
    attempt = case.store.lease(
        submission["session_id"], submission["token"], "provider-job-1", executor_id="worker"
    )
    case.store.progress(
        session["id"], attempt["id"], "running", {"tracking_seconds": 12.5, "secret": 7}
    )
    running = case.store.execution(case.store.job(case.job_id))
    assert running == {
        "gpu_requested_at": session["created_at"],
        "gpu": gpu_id,
        "gpu_seen_at": first,
        "started_at": first + 120,
        "timings": {"tracking_seconds": 12.5},
    }
    assert not any(private in json.dumps(running) for private in ("worker-123", "EU-RO", "secret"))


@pytest.mark.parametrize("allowed", [1, 2])
def test_a_lease_names_the_gpu_before_any_sighting_only_when_one_is_allowed(provider_case, allowed):
    case = provider_case
    if allowed == 1:
        unavailable(case.fake.catalog["gpus"][1])
    case.endpoint.tick()
    selected = json.loads(live(case)["selection_json"])["selected_gpu_ids"]
    assert len(selected) == allowed
    submission = case.fake.submissions[0]
    case.store.lease(submission["session_id"], submission["token"], executor_id="worker")
    execution = case.store.execution(case.store.job(case.job_id))
    if allowed == 1:
        # The worker refuses a lease unless its GPU matches the session's only constraint.
        assert execution["gpu"] == selected[0]
        assert execution["gpu_seen_at"] == execution["started_at"] == clock.now()
    else:
        assert (execution["gpu"], execution["gpu_seen_at"]) == (None, None)


def test_unhealthy_provisioning_worker_stops_early_and_retries_only_after_empty_confirmation(
    provider_case,
):
    case = provider_case
    case.settings.imprint_budget_microusd = 3_000_000
    first, second = case.fake.catalog["gpus"]
    unavailable(second)
    case.endpoint.tick()
    session = live(case)
    clock.freeze(session["created_at"] + 30)
    assert clock.now() < session["queue_timeout_at"]
    case.fake.worker_records = [
        {
            "gpuTypeId": first["id"],
            "dataCenterId": "EU-RO-1",
            "status": "UNHEALTHY",
        }
    ]
    case.endpoint.tick()
    stopping = live(case)
    assert stopping["capacity_failure_reason"] == "unhealthy_worker"
    assert case.fake.workers["max"] == 0
    assert len(case.fake.cancels()) == 1
    case.fake.status = {"status": "CANCELLED"}
    available(second)
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1
    case.fake.worker_records = []
    case.endpoint.tick()
    assert len(case.fake.submissions) == 2
    assert json.loads(live(case)["selection_json"])["selected_gpu_ids"] == [second["id"]]
    assert case.store.job(case.job_id)["state"] == "queued"


@pytest.mark.parametrize("state", ["COMPLETED", "FAILED"])
@pytest.mark.parametrize("extra_status", [{}, {"stopPod": True}])
def test_exact_prelease_guard_rejection_retries_only_after_workers_stop(
    provider_case, state, extra_status
):
    case = provider_case
    first, second = case.fake.catalog["gpus"]
    unavailable(second)
    case.endpoint.tick()
    session = live(case)
    submission = case.fake.submissions[0]
    clock.freeze(session["created_at"] + 120)
    case.fake.worker_records = [
        {
            "id": "rejected-worker",
            "gpuTypeId": first["id"],
            "dataCenterId": "EU-RO-1",
            "status": "IDLE",
            "image": case.fake.endpoint["image"],
        }
    ]
    case.fake.status = {
        "status": state,
        "delayTime": 20_000,
        "executionTime": 1_000,
        "error": "worker_gpu_incompatible",
    } | extra_status
    available(second)
    case.endpoint.reconcile()
    assert live(case)["capacity_failure_reason"] == "worker_gpu_incompatible"
    assert case.fake.workers["max"] == 0
    assert len(case.fake.submissions) == 1
    with pytest.raises(HTTPException):
        case.store.lease(
            submission["session_id"],
            submission["token"],
            "provider-job-1",
            executor_id=str(uuid.uuid4()),
        )
    case.fake.worker_records = []
    case.endpoint.reconcile()
    settled = case.store.get_session(session["id"])
    assert settled["capacity_recovered"]
    evidence = json.loads(settled["provider_json"])
    assert evidence["worker_rejection"] == "worker_gpu_incompatible"
    assert "error" not in evidence
    assert settled["measured_microusd"] >= session["hourly_microusd"] * 150 / 3600
    assert case.store.ledger()["gpu_exclusions"] == [
        {"gpu_id": first["id"], "data_center_id": "EU-RO-1"}
    ]
    case.fake.status = {"status": "IN_QUEUE"}
    case.endpoint.tick()
    assert len(case.fake.submissions) == 2
    assert json.loads(live(case)["selection_json"])["selected_gpu_ids"] == [second["id"]]
    assert case.store.job(case.job_id)["state"] == "queued"


def test_failed_job_without_a_lease_does_not_guess_that_generic_worker_error_is_capacity(
    provider_case,
):
    case = provider_case
    case.endpoint.tick()
    session = live(case)
    case.fake.status = {
        "status": "FAILED",
        "delayTime": 20_000,
        "executionTime": 1_000,
        "error": "Allocated GPU does not match a qualified configuration",
        "output": {"gpu_rejection": {"reason": "configuration_mismatch"}},
    }
    case.endpoint.tick()
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1
    assert case.store.dispatch_held()
    settled = case.store.get_session(session["id"])
    assert not settled.get("capacity_recovered")
    assert "error" not in json.loads(settled["provider_json"])
    assert "gpu_rejection" not in json.loads(settled["provider_json"])
    assert case.store.job(case.job_id)["state"] == "queued"


def test_guard_error_from_an_image_without_a_known_guard_does_not_allow_retry(
    provider_case, inputs
):
    case = provider_case
    inputs["qualifications"]["images"][case.fake.endpoint["image"]]["supports_gpu_constraints"] = (
        False
    )
    case.settings.imprint_gpu_qualification_enabled = True
    case.endpoint.tick()
    session = live(case)
    assert not json.loads(session["selection_json"])["worker_supports_gpu_constraints"]
    case.fake.status = {
        "status": "FAILED",
        "error": "worker_gpu_incompatible",
        "output": {"gpu_rejection": {"reason": "configuration_mismatch"}},
    }
    case.endpoint.tick()
    case.endpoint.tick()
    settled = case.store.get_session(session["id"])
    assert case.store.dispatch_held()
    assert len(case.fake.submissions) == 1
    assert "worker_rejection" not in json.loads(settled["provider_json"])
    assert "gpu_rejection" not in json.loads(settled["provider_json"])


@pytest.mark.parametrize("invalid_hardware", [False, True])
def test_guard_failure_diagnostics_survive_settlement_and_readonly_state_without_secrets(
    provider_case, invalid_hardware
):
    case = provider_case
    case.endpoint.tick()
    session = live(case)
    selected = json.loads(session["selection_json"])
    hardware = {
        "gpu_name": selected["gpu_constraints"][0]["runtime_gpu_names"][0],
        "compute_capability": [12, 0],
        "vram_bytes": 32 * 1024**3,
        "host_memory_bytes": 96 * 1024**3,
        "container_memory_bytes": 0,
        "container_cpus": 12,
        "cuda_version": "13.0",
        "attention_available": False,
        "cgroup_version": "v1",
    }
    expected = {
        "reason": "probe_memory_failed",
        "probe_exception_type": "FileNotFoundError",
        "hardware": hardware,
    }
    diagnostics = deepcopy(expected)
    diagnostics["exception_message"] = "must-not-persist"
    diagnostics["hardware"]["token"] = "must-not-persist"
    if invalid_hardware:
        diagnostics["probe_exception_type"] = "must-not-persist"
        diagnostics["hardware"] = {
            "gpu_name": "must-not-persist",
            "compute_capability": [12, False],
            "vram_bytes": 1e200,
            "host_memory_bytes": -1,
            "container_memory_bytes": "must-not-persist",
            "container_cpus": True,
            "cuda_version": "must-not-persist",
            "attention_available": 1,
            "cgroup_version": "must-not-persist",
        }
        expected = {"reason": "probe_memory_failed"}
    case.fake.status = {
        "status": "FAILED",
        "error": "worker_gpu_incompatible",
        "output": {"gpu_rejection": diagnostics, "stdout": "must-not-persist"},
    }
    case.endpoint.reconcile()
    saved = case.store.get_session(session["id"])
    assert saved["capacity_recovered"]
    assert json.loads(saved["provider_json"])["gpu_rejection"] == expected
    reported = next(
        s for s in controller.state(case.settings)["sessions"] if s["id"] == session["id"]
    )
    assert reported["provider"]["gpu_rejection"] == expected
    assert "must-not-persist" not in json.dumps(saved)
    assert "must-not-persist" not in json.dumps(reported)


@pytest.mark.parametrize(
    "options",
    [
        {"environment": "staging"},
        {"environment": "prod", "imprint_gpu_qualification_enabled": True},
        {
            "environment": "staging",
            "imprint_gpu_qualification_enabled": True,
            "imprint_gpu_selection_enabled": False,
        },
    ],
)
def test_qualification_target_requires_staging_qualification_and_selector(options):
    with pytest.raises(ValueError):
        Settings(imprint_gpu_qualification_target="NVIDIA GeForce RTX 5090", **options)


def test_staging_target_qualifies_selected_gpu_through_normal_guarded_dispatch(provider_case):
    case = provider_case
    target = case.fake.catalog["gpus"][0]["id"]
    case.settings.imprint_gpu_qualification_enabled = True
    case.settings.imprint_gpu_qualification_target = target
    plan = case.endpoint.discover(allow_trial=True, record=False)
    assert plan["selected_gpu_ids"] == [target]
    assert plan["qualification_target_gpu_id"] == target
    assert case.fake.submissions == case.fake.patches == []
    case.endpoint.tick()
    assert [gpu["gpu_id"] for gpu in case.fake.submissions[0]["gpu_constraints"]] == [target]
    assert live(case)["reserved_microusd"] <= case.settings.imprint_budget_microusd


def test_readonly_gpu_plan_subtracts_live_reservation_and_preserves_decision(provider_case):
    case = provider_case
    case.endpoint.tick()
    decision = case.store.gpu_decision()
    ledger = case.store.ledger()
    patches = list(case.fake.patches)
    submissions = list(case.fake.submissions)
    report = case.endpoint.discover(record=False)
    assert report["selected_gpu_ids"] == []
    assert all("reservation_exceeds_budget" in item["reasons"] for item in report["exclusions"])
    assert case.store.gpu_decision() == decision
    assert case.store.ledger() == ledger
    assert case.fake.patches == patches
    assert case.fake.submissions == submissions


def test_no_placement_observation_excludes_every_requested_native_candidate_before_retry(
    provider_case,
):
    case = provider_case
    case.settings.imprint_budget_microusd = 3_000_000
    case.endpoint.tick()
    session = live(case)
    assert len(json.loads(session["selection_json"])["selected_gpu_ids"]) == 2
    clock.freeze(session["queue_timeout_at"] + 1)
    case.endpoint.tick()
    case.fake.status = {"status": "CANCELLED"}
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1
    assert case.store.ledger()["live_session"] is None
    assert len(case.store.ledger()["gpu_exclusions"]) == 2
    available(case.fake.catalog["gpus"][0], "EUR-NO-1")
    case.endpoint.tick()
    assert len(case.fake.submissions) == 2
    assert case.fake.endpoint["dataCenterIds"] == ["EUR-NO-1"]


def test_cancel_without_timings_accounts_for_sweep_and_worker_shutdown_delay(provider_case):
    case = provider_case
    case.endpoint.tick()
    session = live(case)
    clock.freeze(session["queue_timeout_at"] + 60)
    case.endpoint.tick()
    case.fake.status = {"status": "CANCELLED"}
    case.fake.worker_records = [{"status": "INITIALIZING"}]
    case.endpoint.tick()
    assert case.store.ledger()["live_session"] == session["id"]
    clock.freeze(session["created_at"] + 1_000)
    case.fake.worker_records = []
    case.endpoint.tick()
    settled = case.store.get_session(session["id"])
    assert settled["measured_microusd"] >= (session["hourly_microusd"] * 1_000 + 3599) // 3600
    assert case.store.ledger()["settled_microusd"] == settled["measured_microusd"]


def test_stale_worker_is_stopped_before_submission_and_fresh_discovery_can_resume(provider_case):
    case = provider_case
    case.fake.worker_records = [{"isStale": True, "gpuTypeId": "unqualified", "image": "old"}]
    case.endpoint.tick()
    assert case.fake.submissions == []
    assert case.fake.workers["max"] == 0
    assert case.store.ledger()["live_session"] is None
    assert case.store.ledger()["settled_microusd"] == 0
    assert not case.store.dispatch_held()
    case.fake.worker_records = []
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1


def test_budget_changed_after_discovery_prevents_endpoint_mutation(provider_case, monkeypatch):
    case = provider_case
    discover = case.endpoint.discover

    def consume_budget(**kwargs):
        report = discover(**kwargs)
        ledger = case.store.ledger()
        ledger["settled_microusd"] = case.settings.imprint_budget_microusd - 1
        case.store.table.put(ledger)
        return report

    monkeypatch.setattr(case.endpoint, "discover", consume_budget)
    with pytest.raises(HTTPException) as exhausted:
        case.endpoint.tick()
    assert exhausted.value.detail["code"] == "IMPRINT_BUDGET_EXHAUSTED"
    assert case.fake.patches == case.fake.submissions == []
    assert case.store.ledger()["live_session"] is None


def test_another_controller_cannot_mutate_endpoint_while_provider_owner_is_active(provider_case):
    case = provider_case
    owner = case.store.acquire_provider_lock()
    assert owner
    other = RunPodEndpoint(case.settings)
    other.tick()
    assert case.fake.calls == []
    case.store.release_provider_lock(owner)
    other.tick()
    assert len(case.fake.submissions) == 1


def test_expired_controller_owner_cannot_release_its_replacements_lock(provider_case):
    case = provider_case
    old = case.store.acquire_provider_lock()
    clock.freeze(clock.now() + 181)
    new = case.store.acquire_provider_lock()
    assert new and new != old
    case.store.release_provider_lock(old)
    case.endpoint.tick()
    assert case.fake.calls == []
    case.store.release_provider_lock(new)
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1


def test_expired_intents_do_not_permanently_exclude_capacity_for_a_later_workload(provider_case):
    case = provider_case
    case.settings.imprint_budget_microusd = 3_000_000
    original_expiry = case.store.job(case.job_id)["expires_at"]
    case.endpoint.tick()
    session = live(case)
    clock.freeze(session["queue_timeout_at"] + 1)
    case.endpoint.tick()
    case.fake.status = {"status": "CANCELLED"}
    case.endpoint.tick()
    assert case.store.ledger()["gpu_exclusions"] and not case.store.dispatch_held()
    assert case.store.ledger()["live_session"] is None

    clock.freeze(original_expiry + 1)
    case.store.reconcile()
    job = case.store.create(
        "new-client",
        "new-gpu-provider-request",
        intent(tier="complex-motion"),
    )
    for kind in ("reference", "driving"):
        ticket = case.store.input_ticket(job["id"], kind, len(INPUTS[kind]))
        requests.put(
            ticket["url"], headers=ticket["headers"], data=INPUTS[kind], timeout=5
        ).raise_for_status()
    case.store.submit(job["id"])
    case.endpoint.tick()
    assert len(case.fake.submissions) == 2
    assert case.store.ledger()["capacity_attempts"] == 1
    assert case.store.ledger()["gpu_exclusions"] == []
    assert case.store.job(case.job_id)["state"] == "expired"


def test_provider_hardware_is_allowlisted_and_unrelated_output_never_enters_evidence(provider_case):
    case = provider_case
    case.endpoint.tick()
    session = live(case)
    selected = json.loads(session["selection_json"])
    gpu_id = selected["selected_gpu_ids"][0]
    hardware = {
        "gpu_id": gpu_id,
        "gpu_name": selected["gpu_constraints"][0]["runtime_gpu_names"][0],
        "compute_capability": [12, 0],
        "vram_bytes": 32 * 1024**3,
        "host_memory_bytes": 96 * 1024**3,
        "cuda_version": "13.0",
        "attention_available": True,
        "secret": "must-not-persist",
    }
    case.fake.worker_records = [{"id": "worker-123", "gpuTypeId": None, "dataCenterId": None}]
    case.endpoint.reconcile()
    case.fake.worker_records = [
        {
            "id": "worker-123",
            "gpuTypeId": gpu_id,
            "dataCenterId": "EU-RO-1",
            "image": case.fake.endpoint["image"],
            "version": 2,
            "uptimeSeconds": 40,
            "env": {"TOKEN": "must-not-persist"},
        }
    ]
    case.endpoint.reconcile()
    case.fake.status = {
        "status": "COMPLETED",
        "delayTime": 20_000,
        "executionTime": 30_000,
        "output": {
            "gpu": hardware,
            "boot": {"engine_ready_seconds": 20, "private": "must-not-persist"},
            "token": "must-not-persist",
        },
        "input": {"token": "must-not-persist"},
    }
    case.endpoint.reconcile()
    settled = case.store.get_session(session["id"])
    evidence = json.loads(settled["provider_json"])
    assert evidence["gpu"] == {key: value for key, value in hardware.items() if key != "secret"}
    assert evidence["boot"] == {"engine_ready_seconds": 20}
    assert json.loads(settled["endpoint_workers_json"])[0]["data_center_id"] == "EU-RO-1"
    assert "must-not-persist" not in json.dumps(settled)


def test_configuration_change_between_discovery_and_preparation_stops_before_run(
    provider_case, monkeypatch
):
    case = provider_case
    discover = case.endpoint.discover

    def change_image(**kwargs):
        report = discover(**kwargs)
        case.fake.endpoint = deepcopy(case.fake.endpoint)
        case.fake.endpoint["image"] = "unvalidated-image"
        return report

    monkeypatch.setattr(case.endpoint, "discover", change_image)
    with pytest.raises(RuntimeError, match="configuration changed"):
        case.endpoint.tick()
    assert case.fake.submissions == case.fake.patches == []
    assert case.store.ledger()["settled_microusd"] == 0
    assert case.store.dispatch_held()


@pytest.mark.parametrize("execution", [None, -1, float("nan"), True, "1000"])
def test_unknown_terminal_execution_time_keeps_full_reservation(provider_case, execution):
    case = provider_case
    case.endpoint.tick()
    session = live(case)
    case.fake.status = {"status": "COMPLETED", "delayTime": 1000, "executionTime": execution}
    case.endpoint.tick()
    finished = case.store.get_session(session["id"])
    assert finished["measured_microusd"] >= session["reserved_microusd"]
    assert finished["dispatch_hold"]
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1


def test_dispatch_disabled_still_stops_and_settles_existing_paid_work(provider_case):
    case = provider_case
    case.endpoint.tick()
    session = live(case)
    token = case.fake.submissions[0]["token"]
    attempt = case.store.lease(session["id"], token, executor_id="worker")
    assert attempt["job_id"] == case.job_id
    case.store.cancel(case.job_id)
    case.settings.controller_enabled = False
    case.fake.status = {"status": "IN_PROGRESS"}
    controller.run(case.settings)
    # A cancelled worker normally ends its own provider job; it gets a grace period first.
    assert case.fake.cancels() == []
    clock.freeze(clock.now() + STOP_GRACE_SECONDS)
    controller.run(case.settings)
    assert case.fake.cancels()
    case.fake.status = {"status": "CANCELLED", "delayTime": 1000, "executionTime": 2000}
    controller.run(case.settings)
    assert case.store.ledger()["live_session"] is None
    assert len(case.fake.submissions) == 1


def test_closed_recovery_preserves_replay_and_accounting_across_restart(provider_case):
    case = provider_case
    case.endpoint.tick()
    session = live(case)
    with pytest.raises(ValueError, match="Deploy closed"):
        case.endpoint.close()
    case.settings.controller_enabled = False
    case.settings.imprint_hourly_microusd = 0
    case.settings.imprint_budget_microusd = 0
    case.settings.imprint_daily_jobs = 0
    with pytest.raises(ValueError, match="still live"):
        case.endpoint.close()
    case.fake.status = {"status": "CANCELLED", "delayTime": 1000, "executionTime": 2000}
    controller.run(case.settings)
    ledger = case.store.ledger()
    assert ledger["live_session"] is None and ledger["settled_microusd"] > 0
    assert session["id"] in ledger["held"]
    assert case.endpoint.close()["closed"] is True
    restarted = ImprintStore(case.settings)
    assert restarted.ledger() == ledger
    assert (
        restarted.create("client", "gpu-provider-request", intent(tier="complex-motion"))["id"]
        == case.job_id
    )
    with pytest.raises(HTTPException) as refused:
        restarted.create("other", "recovery-new-request", intent())
    assert refused.value.status_code == 429
    assert len(case.fake.submissions) == 1


def test_monitoring_reports_failures_holds_queue_age_and_spending_without_private_data(
    provider_case, monkeypatch, capsys, caplog
):
    case = provider_case
    case.settings.controller_enabled = False
    case.settings.monitoring_enabled = True
    case.settings.imprint_budget_microusd = 0
    ledger = case.store.ledger() | {"settled_microusd": 1_761_641}
    case.store.table.put(ledger)
    controller.run(case.settings)
    assert json.loads(capsys.readouterr().out)["Healthy"] == 1

    def failed_cleanup(*args, **kwargs):
        raise RuntimeError("private-runtime-value")

    monkeypatch.setattr(ImprintStore, "reconcile", failed_cleanup)
    controller.run(case.settings)
    report = json.loads(capsys.readouterr().out)
    assert report["Healthy"] == 0 and report["reasons"] == ["reconciliation_failed"]
    assert "private-runtime-value" not in caplog.text

    case.store.table.put(ledger | {"held": ["review-required"]})
    case.settings.imprint_budget_microusd = ledger["settled_microusd"]
    clock.freeze(clock.now() + 901)
    controller.report_health(case.settings)
    report = json.loads(capsys.readouterr().out)
    assert report["Healthy"] == 0
    assert set(report["reasons"]) == {"operator_hold", "queue_stalled", "spending_limit_reached"}
    assert "client" not in json.dumps(report) and case.job_id not in json.dumps(report)

    # A service-side job failure no longer holds the queue; it alerts for 15 minutes instead.
    case.store.table.put(case.store.ledger() | {"held": [], "last_failure_at": clock.now()})
    controller.report_health(case.settings)
    assert "recent_failure" in json.loads(capsys.readouterr().out)["reasons"]
    clock.freeze(clock.now() + 900)
    controller.report_health(case.settings)
    assert "recent_failure" not in json.loads(capsys.readouterr().out)["reasons"]


def test_discovery_keeps_compatible_ready_worker_and_records_actual_queue(provider_case):
    case = provider_case
    gpu_id = "NVIDIA GeForce RTX 5090"
    case.fake.worker_records = [
        {
            "gpuTypeId": gpu_id,
            "image": case.fake.endpoint["image"],
            "status": "IDLE",
            "isStale": False,
        }
    ]
    case.endpoint.tick()
    selection = json.loads(live(case)["selection_json"])
    assert selection["workload"] == [
        {
            "tier": "complex-motion",
            "duration_ms": 3400,
            "model_pixels": 512 * 512,
        }
    ]
    assert selection["selected_gpu_ids"] == [gpu_id]
    assert not any(patch.get("workers", {}).get("max") == 0 for patch in case.fake.patches)
    assert len(case.fake.submissions) == 1


def test_repeat_dispatch_with_changed_stock_leaves_the_endpoint_untouched(provider_case, inputs):
    case = provider_case
    configure_release(case, inputs)
    case.settings.imprint_budget_microusd = 10_000_000
    case.fake.worker_records = []
    case.endpoint.tick()
    (first,) = case.fake.patches
    assert first["dataCenterIds"] == ["EU-RO-1", "EUR-NO-1"]
    complete_synthetic_attempt(case)
    case.fake.status = {"status": "COMPLETED", "delayTime": 1000, "executionTime": 1000}
    case.endpoint.reconcile()
    assert case.store.ledger()["live_session"] is None
    assert not case.store.dispatch_held()
    # The previous worker is retained; stock now shows only another data centre.
    case.fake.worker_records = [
        {
            "gpuTypeId": "NVIDIA GeForce RTX 5090",
            "image": case.fake.endpoint["image"],
            "status": "IDLE",
            "isStale": False,
        }
    ]
    for gpu in case.fake.catalog["gpus"]:
        gpu["dataCenters"] = [{"id": "EUR-NO-1", "availability": "LOW"}]
    job = case.store.create("client-two", "second-request", intent(tier="complex-motion"))
    for kind, value in INPUTS.items():
        ticket = case.store.input_ticket(job["id"], kind, len(value))
        response = requests.put(ticket["url"], headers=ticket["headers"], data=value, timeout=5)
        response.raise_for_status()
    case.store.submit(job["id"])
    case.endpoint.tick()
    assert len(case.fake.submissions) == 2
    assert case.fake.patches == [first]


def configure_release(case, inputs, source="native-cache"):
    release = explicit_release(inputs, source)
    case.fake.model_references = list(inputs["endpoint"]["modelReferences"])
    case.settings.imprint_gpu_qualification_enabled = True
    return release


def rejected_release(case, inputs, *, held=False, legacy=False):
    if not legacy:
        configure_release(case, inputs)
        registration = inputs["qualifications"]["images"][case.fake.endpoint["image"]]
        registration["status"] = "accepted"
        registration["releases"][0]["accepted_gpu_ids"] = registration["releases"][0][
            "trial_gpu_ids"
        ]
    case.settings.imprint_gpu_qualification_enabled = held
    case.settings.imprint_budget_microusd = 6_000_000
    unavailable(case.fake.catalog["gpus"][0])
    case.endpoint.tick()
    session = live(case)
    case.fake.status = {"status": "FAILED", "error": "worker_gpu_incompatible"}
    case.endpoint.reconcile()
    return case.store.get_session(session["id"])


@pytest.mark.parametrize("change", ["legacy_to_native", "image", "source", "revision"])
def test_new_immutable_release_does_not_inherit_failed_placement_or_retry_count(
    provider_case, inputs, change
):
    case = provider_case
    failed = rejected_release(case, inputs, legacy=change == "legacy_to_native")
    if change == "legacy_to_native":
        # The deployed historical report predates these fields entirely.
        previous = case.store.table.get("CONTROL", "GPU_SELECTION")
        report = json.loads(previous["report_json"])
        for key in ("weight_source", "model_release", "worker_supports_model_release"):
            report.pop(key, None)
        previous["report_json"] = json.dumps(report)
        case.store.table.put(previous)
    ledger = case.store.ledger()
    decision = case.store.gpu_decision()
    replays = [item for item in case.store.table.scan() if item["pk"].startswith("REPLAY#")]
    assert ledger["gpu_exclusions"] and ledger["capacity_attempts"] == 1
    assert not ledger["held"] and ledger["capacity_expires_at"] > clock.now()

    image = case.fake.endpoint["image"]
    registration = inputs["qualifications"]["images"][image]
    if change == "legacy_to_native":
        configure_release(case, inputs)
    elif change == "image":
        image = "ghcr.io/example/worker@sha256:" + "b" * 64
        inputs["qualifications"]["images"][image] = deepcopy(registration)
        case.fake.endpoint["image"] = image
    elif change == "source":
        configure_release(case, inputs, "runtime-hf")
    else:
        release = dict(registration["releases"][0]["model_release"], revision="3" * 40)
        registration["releases"][0]["model_release"] = release
        case.fake.endpoint["env"]["IMPRINT_MODEL_RELEASE"] = json.dumps(release)
        case.fake.model_references = [
            f"https://huggingface.co/{release['repo']}:{release['revision']}"
        ]

    for _ in range(2):
        plan = case.endpoint.discover(allow_trial=True, record=False)
        assert plan["selected_gpu_ids"] == ["NVIDIA RTX PRO 4500 Blackwell"]
        assert plan["avoid_placements"] == []
        assert case.store.ledger() == ledger
        assert case.store.gpu_decision() == decision
    case.fake.status = {"status": "IN_QUEUE"}
    case.endpoint.tick()
    assert len(case.fake.submissions) == 2
    assert case.store.ledger()["capacity_attempts"] == 1
    assert case.store.ledger()["gpu_exclusions"] == []
    assert case.store.ledger()["settled_microusd"] == ledger["settled_microusd"]
    assert case.store.get_session(failed["id"]) == failed
    assert [item for item in case.store.table.scan() if item["pk"].startswith("REPLAY#")] == replays
    assert case.store.job(case.job_id)["state"] == "queued"


def test_other_endpoint_changes_keep_the_existing_capacity_retry_window(provider_case, inputs):
    case = provider_case
    failed = rejected_release(case, inputs)
    ledger = case.store.ledger()
    case.fake.endpoint.update(disk=51, templateId="new-template")
    case.fake.endpoint["env"]["HF_XET_HIGH_PERFORMANCE"] = "1"
    case.settings.imprint_gpu_qualification_enabled = True
    case.settings.imprint_gpu_qualification_target = "NVIDIA RTX PRO 4500 Blackwell"
    plan = case.endpoint.discover(allow_trial=True, record=False)
    assert plan["configuration_sha256"] != case.store.gpu_decision()["configuration_sha256"]
    assert plan["avoid_placements"] == ledger["gpu_exclusions"]
    assert plan["selected_gpu_ids"] == []
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1
    assert case.store.ledger()["capacity_attempts"] == ledger["capacity_attempts"]
    assert case.store.ledger()["gpu_exclusions"] == ledger["gpu_exclusions"]
    assert case.store.ledger()["capacity_expires_at"] == ledger["capacity_expires_at"]
    assert case.store.get_session(failed["id"]) == failed


@pytest.mark.parametrize("identity", ["unknown_previous", "mutable_image"])
def test_unproven_release_change_keeps_capacity_failures(provider_case, inputs, identity):
    case = provider_case
    failed = rejected_release(case, inputs)
    ledger = case.store.ledger()
    if identity == "unknown_previous":
        previous = case.store.table.get("CONTROL", "GPU_SELECTION")
        previous["report_json"] = "{}"
        case.store.table.put(previous)
    else:
        case.fake.endpoint["image"] = "ghcr.io/example/worker:latest"
    plan = case.endpoint.discover(allow_trial=True, record=False)
    assert plan["avoid_placements"] == ledger["gpu_exclusions"]
    assert plan["selected_gpu_ids"] == []
    case.endpoint.tick()
    assert case.store.ledger()["capacity_attempts"] == ledger["capacity_attempts"]
    assert case.store.ledger()["gpu_exclusions"] == ledger["gpu_exclusions"]
    assert case.store.ledger()["capacity_expires_at"] == ledger["capacity_expires_at"]
    assert case.store.get_session(failed["id"]) == failed
    assert len(case.fake.submissions) == 1


@pytest.mark.parametrize("boundary", ["hold", "live"])
def test_changed_release_cannot_reset_a_held_or_live_capacity_chain(
    provider_case, inputs, boundary
):
    case = provider_case
    if boundary == "hold":
        rejected_release(case, inputs, held=True)
    else:
        configure_release(case, inputs)
        case.endpoint.tick()
    ledger = case.store.ledger()
    registration = inputs["qualifications"]["images"][case.fake.endpoint["image"]]
    image = "ghcr.io/example/worker@sha256:" + "b" * 64
    inputs["qualifications"]["images"][image] = deepcopy(registration)
    case.fake.endpoint["image"] = image
    case.endpoint.discover(allow_trial=True)
    assert case.store.ledger() == ledger
    assert len(case.fake.submissions) == 1


@pytest.mark.parametrize("source", ["native-cache", "runtime-hf"])
@pytest.mark.parametrize("failure", ["worker_gpu_incompatible", "queue_timeout"])
def test_qualification_failure_keeps_hold_instead_of_buying_a_replacement(
    provider_case, inputs, source, failure
):
    case = provider_case
    configure_release(case, inputs, source)
    case.settings.imprint_budget_microusd = 6_000_000
    first, second = case.fake.catalog["gpus"]
    unavailable(second)
    case.endpoint.tick()
    session = live(case)
    case.fake.worker_records = [
        {
            "id": "failed-qualification-worker",
            "gpuTypeId": first["id"],
            "dataCenterId": "EU-RO-1",
            "status": "INITIALIZING",
            "image": case.fake.endpoint["image"],
        }
    ]
    if failure == "queue_timeout":
        clock.freeze(session["queue_timeout_at"] + 1)
        case.fake.status = {"status": "IN_QUEUE"}
    else:
        clock.freeze(session["created_at"] + 120)
        case.fake.status = {"status": "FAILED", "error": failure}
    case.endpoint.tick()
    assert case.fake.workers["max"] == 0
    assert len(case.fake.submissions) == 1
    assert live(case)["capacity_failure_reason"] == failure
    if failure == "queue_timeout":
        assert case.fake.cancels()
        case.fake.status = {"status": "CANCELLED"}
    case.fake.worker_records = []
    available(second)
    case.endpoint.tick()
    case.endpoint.tick()
    finished = case.store.get_session(session["id"])
    assert finished["state"] == "finished"
    assert finished["dispatch_hold"]
    assert not finished.get("capacity_recovered")
    assert finished["measured_microusd"] >= finished["reserved_microusd"]
    assert case.store.ledger()["live_session"] is None
    assert case.store.ledger()["held"] == [session["id"]]
    assert case.store.job(case.job_id)["state"] == "queued"
    assert case.fake.workers["max"] == 0
    assert len(case.fake.submissions) == 1


@pytest.mark.parametrize("source", ["native-cache", "runtime-hf"])
def test_disabling_selector_cannot_start_an_explicit_release_without_qualification(
    provider_case, inputs, source
):
    case = provider_case
    configure_release(case, inputs, source)
    case.settings.imprint_gpu_selection_enabled = False
    with pytest.raises(RuntimeError, match="require qualified GPU selection"):
        case.endpoint.tick()
    assert case.fake.patches == case.fake.submissions == []
    assert case.store.dispatch_held()
    assert case.store.ledger()["settled_microusd"] == 0


def test_old_or_wrong_release_worker_cannot_lease_or_resume_a_new_release_session(
    provider_case, inputs
):
    case = provider_case
    release = configure_release(case, inputs)
    case.endpoint.tick()
    session = live(case)
    token = case.fake.submissions[0]["token"]
    executor = str(uuid.uuid4())
    for receipt in (
        {},
        {"weight_source": "native-cache", "model_release": dict(release, revision="3" * 40)},
    ):
        with pytest.raises(HTTPException) as failure:
            case.store.lease(session["id"], token, executor_id=executor, **receipt)
        assert failure.value.detail["code"] == "IMPRINT_MODEL_RELEASE_MISMATCH"
        assert live(case)["attempt_count"] == 0
        assert case.store.job(case.job_id)["state"] == "queued"
    attempt = case.store.lease(
        session["id"],
        token,
        executor_id=executor,
        weight_source="native-cache",
        model_release=release,
    )
    assert attempt["job_id"] == case.job_id
    with pytest.raises(HTTPException) as failure:
        case.store.lease(session["id"], token, executor_id=executor)
    assert failure.value.detail["code"] == "IMPRINT_MODEL_RELEASE_MISMATCH"
    assert live(case)["attempt_count"] == 1
    assert live(case)["open_attempt"] == attempt["id"]


@pytest.mark.parametrize("source", ["native-cache", "runtime-hf"])
def test_release_readback_is_used_at_every_observation_and_sent_before_lease(
    provider_case, inputs, source
):
    case = provider_case
    release = configure_release(case, inputs, source)
    case.endpoint.tick()
    submission = case.fake.submissions[0]
    assert submission["weight_source"] == source
    assert submission["model_release"] == release
    assert len([call for call in case.fake.calls if call == ("POST", "/graphql")]) == 3
    assert all("modelReferences" not in patch for patch in case.fake.patches)
    assert case.store.job(case.job_id)["state"] == "queued"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"errors": [{"message": "must-not-persist"}]},
        {"data": {"myself": {"endpoint": {}}}},
        {"data": {"myself": {"endpoint": {"modelReferences": None}}}},
        {"data": {"myself": {"endpoint": {"modelReferences": "must-not-persist"}}}},
    ],
)
def test_missing_or_failed_model_reference_query_never_enables_capacity(
    provider_case, inputs, payload
):
    case = provider_case
    configure_release(case, inputs)
    case.fake.graphql_payload = payload
    with pytest.raises(ValueError, match="readback is unavailable"):
        case.endpoint.tick()
    assert case.fake.submissions == case.fake.patches == []
    assert case.store.ledger()["live_session"] is None
    assert "must-not-persist" not in json.dumps(controller.state(case.settings))


@pytest.mark.parametrize("stage", ["after_discovery", "during_patch"])
def test_model_reference_drift_prevents_submission_and_holds_reserved_session(
    provider_case, inputs, monkeypatch, stage
):
    case = provider_case
    configure_release(case, inputs)
    if stage == "after_discovery":
        discover = case.endpoint.discover

        def change_reference(**kwargs):
            report = discover(**kwargs)
            case.fake.model_references = ["https://huggingface.co/example/different:main"]
            return report

        monkeypatch.setattr(case.endpoint, "discover", change_reference)
    else:
        request = case.endpoint.request

        def change_reference(method, url, **kwargs):
            response = request(method, url, **kwargs)
            if method == "PATCH":
                case.fake.model_references = []
            return response

        monkeypatch.setattr(case.endpoint, "request", change_reference)
    with pytest.raises(RuntimeError):
        case.endpoint.tick()
    assert case.fake.submissions == []
    assert case.store.dispatch_held()
    assert case.store.ledger()["settled_microusd"] == 0


@pytest.mark.parametrize(
    "error", ["worker_model_cache_invalid", "worker_model_configuration_invalid"]
)
def test_cache_failure_is_held_after_stop_without_capacity_retry_or_lost_reservation(
    provider_case, inputs, error
):
    case = provider_case
    configure_release(case, inputs)
    case.settings.imprint_budget_microusd = 10_000_000
    case.endpoint.tick()
    session = live(case)
    case.fake.status = {
        "status": "FAILED",
        "error": error,
        "output": {"private": "must-not-persist"},
    }
    case.endpoint.tick()
    case.endpoint.tick()
    settled = case.store.get_session(session["id"])
    assert case.fake.workers["max"] == 0
    assert len(case.fake.submissions) == 1
    assert settled["measured_microusd"] >= session["reserved_microusd"]
    assert settled["dispatch_hold"]
    assert not settled.get("capacity_recovered")
    assert json.loads(settled["provider_json"])["worker_rejection"] == error
    assert "must-not-persist" not in json.dumps(settled)
    assert case.store.job(case.job_id)["state"] == "queued"


@pytest.mark.parametrize("unhealthy", [False, True])
def test_unexplained_native_preparation_failure_cannot_become_another_paid_cache_start(
    provider_case, inputs, unhealthy
):
    case = provider_case
    configure_release(case, inputs)
    case.settings.imprint_budget_microusd = 10_000_000
    case.endpoint.tick()
    session = live(case)
    if unhealthy:
        case.fake.worker_records = [{"status": "UNHEALTHY"}]
    else:
        clock.freeze(session["queue_timeout_at"] + 1)
    case.endpoint.tick()
    case.fake.worker_records = []
    case.fake.status = {"status": "CANCELLED"}
    case.endpoint.tick()
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1
    assert case.store.dispatch_held()
    assert case.fake.workers["max"] == 0


def complete_synthetic_attempt(case):
    session = live(case)
    attempt = case.store.lease(
        session["id"],
        case.fake.submissions[-1]["token"],
        executor_id=str(uuid.uuid4()),
        weight_source=case.fake.submissions[-1]["weight_source"],
        model_release=case.fake.submissions[-1]["model_release"],
    )
    case.store.progress(session["id"], attempt["id"], "finishing")
    manifest = {}
    for name in ("result.mp4", "result.webp", "poster.jpg"):
        data = b"transport test only"
        digest = hashlib.sha256(data).hexdigest()
        case.store.media.put(f"jobs/{case.job_id}/{name}", data, digest)
        manifest[name] = {"bytes": len(data), "sha256": digest}
    case.store.complete(session["id"], attempt["id"], manifest)


@pytest.mark.parametrize("corrupt_receipt", [False, True])
def test_verified_release_receipt_credits_only_the_matching_flashboot_worker(
    provider_case, inputs, corrupt_receipt
):
    case = provider_case
    release = configure_release(case, inputs)
    case.settings.imprint_budget_microusd = 10_000_000
    case.endpoint.tick()
    session = live(case)
    complete_synthetic_attempt(case)
    boot = {
        "weight_source": "native-cache",
        "model_release": release,
        "hash_seconds": 12.5,
        "cache_resolve_seconds": 0.1,
        "download_seconds": 0,
        "engine_ready_seconds": 20,
        "private_path": "must-not-persist",
        "invalid_seconds": float("nan"),
    }
    if corrupt_receipt:
        boot["model_release"] = dict(release, revision="3" * 40)
    case.fake.status = {
        "status": "COMPLETED",
        "delayTime": 1000,
        "executionTime": 1000,
        "workerId": "verified-worker",
        "output": {
            "boot": boot,
            "session_timings": {
                "session_seconds": 45,
                "attempt_seconds": 40,
                "completion_seconds": 2,
                "private": "must-not-persist",
            },
        },
    }
    case.endpoint.reconcile()
    saved = case.store.get_session(session["id"])
    assert "must-not-persist" not in json.dumps(saved)
    assert "must-not-persist" not in json.dumps(controller.state(case.settings))
    if corrupt_receipt:
        assert "verified_worker" not in case.store.ledger()
    else:
        assert case.store.ledger()["verified_worker"]["model_release"] == release
    case.fake.worker_records = [
        {
            "id": "verified-worker",
            "gpuTypeId": "NVIDIA GeForce RTX 5090",
            "image": case.fake.endpoint["image"],
            "isStale": False,
            "status": "IDLE",
        }
    ]
    report = case.endpoint.discover(allow_trial=True, record=False)
    assert (
        any(candidate["warm_worker_observed"] for candidate in report["candidates"])
        is not corrupt_receipt
    )
    # The receipt decides warm credit only; a worker of the current image on a selected GPU
    # is submitted to either way, because the lease and the handler fence the release.
    assert case.endpoint.restore_capacity(report) is True
    assert case.fake.workers["max"] == 1
    if not corrupt_receipt:
        # An unverified worker of the current image earns no warm credit but is kept: the
        # handler verifies its own release, and retiring it would discard the cached host.
        case.fake.worker_records[0]["id"] = "another-worker"
        report = case.endpoint.discover(allow_trial=True, record=False)
        assert not any(candidate["warm_worker_observed"] for candidate in report["candidates"])
        assert case.endpoint.restore_capacity(report) is True
        assert case.fake.workers["max"] == 1
        # A throttled worker cannot serve and holds the only slot, so it is retired first.
        case.fake.worker_records[0]["status"] = "THROTTLED"
        report = case.endpoint.discover(allow_trial=True, record=False)
        assert not any(candidate["warm_worker_observed"] for candidate in report["candidates"])
        assert case.endpoint.restore_capacity(report) is False
        assert case.fake.workers["max"] == 0


def test_existing_settlement_migration_preserves_lifetime_and_is_idempotent(provider_case):
    from gremlin_control.aws import optimistic

    case = provider_case
    session, _, _ = case.store.reserve_session(1_580_000, financial_authority=True)
    case.store.finish_session(session, 123_456)
    ledger = case.store.ledger()
    before = ledger["settled_microusd"]
    # Reproduce the deployed ledger schema before period accounting existed.
    for key in ("spending_initialized", "spend_month", "spend_month_microusd"):
        ledger.pop(key, None)
    case.store.table.put(ledger)
    for receipt in case.store.table.query("SPEND"):
        optimistic(
            case.store.table,
            lambda txn, receipt=receipt: txn.delete(receipt["pk"], receipt["sk"], receipt["v"]),
        )
    assert case.store.spending_limit(case.store.ledger(), 1, clock.now())[0]
    case.store.initialize_spending()
    case.store.initialize_spending()
    current = case.store.ledger()
    assert current["settled_microusd"] == before
    assert current["spend_month_microusd"] == 123_456
    assert sum(row["microusd"] for row in case.store.table.query("SPEND")) == 123_456


def test_period_authority_dispatches_without_a_lifetime_ceiling(provider_case):
    case = provider_case
    case.settings.imprint_budget_microusd = 0
    case.settings.imprint_rolling_budget_microusd = 3_000_000
    case.settings.imprint_monthly_budget_microusd = 10_000_000
    case.endpoint.tick()
    assert len(case.fake.submissions) == 1
    assert live(case)["reserved_microusd"] <= 3_000_000
