"""Dispatch queued jobs to one serverless endpoint and settle each session from its job status.

A port of `backend/app/imprint/runpod.py`. Nothing here polls: the sweep, the submit doorbell
and the provider's completion webhook each run one `tick`, and every step is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

import requests

from gremlin_control import clock
from gremlin_control.errors import api_error
from gremlin_control.gpu_selection import select_gpus
from gremlin_control.model_release import configured_release
from gremlin_control.store import (
    ADMISSION_MESSAGES,
    IDLE_ALLOWANCE,
    MODEL_REJECTION_REASONS,
    SESSION_SECONDS,
    STOP_GRACE_SECONDS,
    ImprintStore,
    provider_evidence,
    worker_release_changed,
)

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
logger = logging.getLogger(__name__)
DISPATCH_SECONDS = 75


def serialized(method):
    """Serialize external endpoint effects, including a previous session's final stop."""

    @wraps(method)
    def call(self, *args, **kwargs):
        if self.lock_owner:
            return method(self, *args, **kwargs)
        owner = self.store.acquire_provider_lock()
        if owner is None:
            return None
        self.lock_owner = owner
        self.request_deadline = time.monotonic() + DISPATCH_SECONDS
        try:
            return method(self, *args, **kwargs)
        finally:
            self.store.release_provider_lock(owner)
            self.lock_owner = None
            self.request_deadline = None

    return call


def configuration_digest(endpoint):
    configuration = {
        key: endpoint.get(key)
        for key in (
            "image",
            "env",
            "disk",
            "mounts",
            "networkVolumes",
            "args",
            "templateId",
            "modelReferences",
        )
    }
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


def configuration_changes(endpoint, patch):
    """The endpoint fields the patch would change; empty when the endpoint already matches."""
    changes = []
    for key, expected in patch["gpu"].items():
        actual = endpoint.get("gpu", {}).get(key, [] if isinstance(expected, list) else None)
        if isinstance(expected, list):
            actual, expected = sorted(actual), sorted(expected)
        if actual != expected:
            changes.append("gpu." + key)
    if sorted(endpoint.get("dataCenterIds", [])) != sorted(patch["dataCenterIds"]):
        changes.append("dataCenterIds")
    return changes


def worker_release_matches(worker, endpoint, receipt):
    source, release = configured_release(endpoint)
    if source is None:
        return True
    # Provider worker metadata does not promise an HF commit. Credit a warm explicit
    # release only after this same worker returned a verified, successful boot receipt.
    return bool(
        receipt
        and receipt.get("worker_id") == worker.get("id")
        and receipt.get("image") == endpoint.get("image")
        and receipt.get("weight_source") == source
        and receipt.get("model_release") == release
    )


# RunPod refuses a job while its job API still sees zero maximum workers, which lags a
# capacity change by seconds. A refusal queues nothing, so resubmitting it is safe.
PAUSED_RETRY_SECONDS = (1, 2, 4, 8)


def paused(response):
    if response.status_code != 409:
        return False
    try:
        return response.json().get("code") == "ENDPOINT_PAUSED"
    except ValueError:
        return False


class RunPodEndpoint:
    """The worker holds no provider credential: the platform starts it and stops it."""

    def __init__(self, settings, transport=None):
        self.settings = settings
        self.store = ImprintStore(settings)
        self.transport = transport
        self.sessions = threading.local()
        key = settings.imprint_runpod_key
        self.headers = {"Authorization": "Bearer " + (key.get_secret_value() if key else "")}
        endpoint = settings.imprint_endpoint_id
        self.jobs = f"{settings.runpod_jobs_url.rstrip('/')}/{endpoint}"
        self.endpoint = f"{settings.runpod_management_url.rstrip('/')}/serverless/{endpoint}"
        self.management = settings.runpod_management_url.rstrip("/")
        self.request_deadline = None
        self.lock_owner = None

    @property
    def http(self):
        # Discovery reads run side by side; a requests session is not shared across threads.
        if self.transport is not None:
            return self.transport
        session = getattr(self.sessions, "session", None)
        if session is None:
            session = self.sessions.session = requests.Session()
        return session

    def request(self, method, url, **kwargs):
        timeout = 8
        if self.request_deadline is not None:
            timeout = min(timeout, self.request_deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("Provider operation exceeded its request allowance")
        return self.http.request(method, url, headers=self.headers, timeout=timeout, **kwargs)

    def read(self, path, **kwargs):
        response = self.request("GET", self.management + path, **kwargs)
        response.raise_for_status()
        return response.json()

    def read_endpoint(self):
        endpoint = self.read(f"/serverless/{self.settings.imprint_endpoint_id}")
        environment = endpoint.get("env", {})
        if "IMPRINT_WEIGHT_SOURCE" in environment or "IMPRINT_MODEL_RELEASE" in environment:
            # REST omits modelReferences. All observations used for drift detection must
            # hydrate it from the same supported read-only GraphQL surface.
            response = self.request(
                "POST",
                self.settings.runpod_graphql_url,
                json={
                    "query": (
                        "query EndpointModelReferences($id: String!) { "
                        "myself { endpoint(id: $id) { modelReferences } } }"
                    ),
                    "variables": {"id": self.settings.imprint_endpoint_id},
                },
            )
            response.raise_for_status()
            try:
                payload = response.json()
                if payload.get("errors"):
                    raise ValueError
                references = payload["data"]["myself"]["endpoint"]["modelReferences"]
                if not isinstance(references, list) or any(
                    not isinstance(value, str) or len(value) > 300 for value in references
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ValueError("Provider model reference readback is unavailable") from None
            endpoint["modelReferences"] = references
        return endpoint

    def discover(self, *, allow_trial=False, record=True):
        if allow_trial and self.settings.environment not in {"staging", "test"}:
            raise ValueError("Trial planning is restricted to staging")
        if self.request_deadline is None:
            self.request_deadline = time.monotonic() + DISPATCH_SECONDS
        # The four provider reads are independent; serially they cost 6 to 10 s of every
        # dispatch, including the warm path, so they run side by side.
        with ThreadPoolExecutor(4) as pool:
            reads = (
                pool.submit(self.read_endpoint),
                pool.submit(
                    self.read,
                    "/catalog/gpus",
                    params={
                        "include": "AVAILABILITY",
                        "product": "SERVERLESS",
                        "cloud": "SECURE",
                        "count": 1,
                        "minCudaVersion": "13.0",
                    },
                ),
                pool.submit(self.read, "/catalog/datacenters"),
                pool.submit(self.read, f"/serverless/{self.settings.imprint_endpoint_id}/workers"),
            )
            endpoint, catalog, data_centers, active = (read.result() for read in reads)
        volume_ids = list(
            dict.fromkeys(
                endpoint.get("networkVolumes", [])
                + [v["volumeId"] for v in endpoint.get("mounts", {}).get("network", [])]
            )
        )
        if len(volume_ids) > 3:
            raise ValueError("At most three model-volume locations can be discovered per dispatch")
        volumes = [self.read(f"/network-volumes/{volume_id}") for volume_id in volume_ids]
        ledger = self.store.ledger()
        warm = {
            worker.get("gpuTypeId")
            for worker in active["workers"]
            if worker.get("isStale") is False
            and worker.get("image") == endpoint.get("image")
            and worker.get("status") in {"IDLE", "RUNNING"}
            and worker_release_matches(worker, endpoint, ledger.get("verified_worker"))
        }
        options = dict(
            hourly_cap_microusd=self.settings.imprint_hourly_microusd,
            budget_microusd=self.store.available_budget(ledger),
            data_centers=data_centers,
            workload=self.store.queued_workload() or None,
            warm_gpu_ids=warm,
            allow_trial=allow_trial,
            qualification_target_gpu_id=self.settings.imprint_gpu_qualification_target,
        )
        report = select_gpus(
            catalog,
            endpoint,
            volumes,
            **options,
            avoid_placements=(
                ledger.get("gpu_exclusions", [])
                if ledger.get("capacity_expires_at", clock.now() + 1) > clock.now()
                else []
            ),
        )
        if (
            not ledger["live_session"]
            and not ledger["held"]
            and worker_release_changed(self.store.gpu_decision(), report)
        ):
            report = select_gpus(catalog, endpoint, volumes, **options)
        report["observed_at"] = clock.now()
        report["configuration_sha256"] = configuration_digest(endpoint)
        if record:
            self.store.record_gpu_decision(report)
        logger.info(
            "gremlin.gpu_selection candidates=%d selected=%s reservation_microusd=%s",
            len(report["candidates"]),
            report.get("selected_gpu_ids", []),
            report.get("reservation_microusd"),
        )
        return report

    @serialized
    def tick(self):
        self.reconcile()
        if not self.settings.controller_enabled:
            return
        if self.store.admission_blocker() or self.store.dispatch_held():
            return
        if self.store.queued_exists() and not self.store.ledger()["live_session"]:
            self.dispatch()

    @serialized
    def dispatch(self):
        blocker = self.store.admission_blocker()
        if blocker:
            raise api_error(
                503, "IMPRINT_BUDGET_BLOCKED", ADMISSION_MESSAGES[blocker], reason=blocker
            )
        if not self.settings.controller_enabled:
            raise api_error(503, "IMPRINT_DISPATCH_DISABLED", "GPU dispatch is disabled.")
        if not self.settings.imprint_runpod_key:
            raise api_error(
                503,
                "IMPRINT_BUDGET_BLOCKED",
                ADMISSION_MESSAGES["endpoint_unconfigured"],
                reason="endpoint_unconfigured",
            )
        if self.store.dispatch_held():
            raise api_error(503, "IMPRINT_DISPATCH_HELD", "Dispatch requires operator review.")
        ledger = self.store.ledger()
        if ledger["live_session"]:
            raise api_error(409, "IMPRINT_SESSION_ACTIVE", "Reconcile the current session first.")
        if (
            self.settings.imprint_budget_microusd > 0
            and ledger["settled_microusd"] >= self.settings.imprint_budget_microusd
        ):
            raise api_error(
                503, "IMPRINT_BUDGET_EXHAUSTED", "The authorized GPU budget is exhausted."
            )
        if not self.store.queued_exists():
            return None
        phases = {}
        started = time.monotonic()

        def phase(name):
            nonlocal started
            phases[name] = round(time.monotonic() - started, 2)
            started = time.monotonic()

        self.store.refresh_capacity_retries()
        selection = (
            self.discover(allow_trial=self.settings.imprint_gpu_qualification_enabled)
            if self.settings.imprint_gpu_selection_enabled
            else None
        )
        phase("discover")
        if selection is not None and not selection.get("selected_gpu_ids"):
            # Capacity discovery is read-only. A later sweep retries fresh availability
            # against the original job expiry without creating a paid retry loop.
            return None
        # Reserve transactionally before any endpoint mutation. This also checks durable
        # recovery holds, budget headroom and the one-live-session constraint.
        session_id, token, webhook = self.store.reserve_session(
            selection["hourly_microusd"] if selection else self.settings.imprint_hourly_microusd,
            financial_authority=True,
            selection=selection,
        )
        phase("reserve")
        try:
            ready = self.restore_capacity(selection)
        except Exception as failure:
            # No /run request was made. Stop here without charging an unsubmitted session
            # or retrying the endpoint mutation on every tick.
            self.store.finish_session(
                session_id, 0, error=f"Endpoint preparation failed: {type(failure).__name__}"
            )
            raise
        if ready is False:
            self.store.finish_session(
                session_id,
                0,
                error="Retired stale workers before provider submission; discovery will retry",
                retired_before_submission=True,
            )
            logger.info("gremlin.gpu_retired_before_submission session=%s", session_id)
            return None
        phase("capacity")
        origin = self.settings.public_base_url
        body = {
            "input": {"origin": origin, "session_id": session_id, "token": token},
            # A doorbell only: settlement always reads the authenticated job status.
            "webhook": f"{origin}/api/imprint/provider/{session_id}/{webhook}",
            # The job expires unstarted, and its handler is cut off, at the session deadline.
            "policy": {"executionTimeout": SESSION_SECONDS * 1000, "ttl": SESSION_SECONDS * 1000},
        }
        if selection:
            body["input"]["gpu_constraints"] = selection["gpu_constraints"]
            if selection.get("worker_supports_model_release"):
                body["input"]["weight_source"] = selection["weight_source"]
                body["input"]["model_release"] = selection["model_release"]
        self.store.mark_submission_started(session_id)
        try:
            response = self.submit(body)
            if not paused(response):
                response.raise_for_status()
                provider_job_id = response.json()["id"]
        except Exception as failure:
            # The provider may have queued the job although the response was lost. The worker
            # then reports the job through its first lease; otherwise the session ends at its
            # deadline holding the whole reservation. Nothing is ever resubmitted blindly.
            detail = type(failure).__name__
            if isinstance(failure, requests.HTTPError) and failure.response is not None:
                detail += f" HTTP {failure.response.status_code}"
            self.store.set_session_error(session_id, detail)
            raise
        if paused(response):
            # Refused, so no provider job exists: settle without charge and hold for review.
            self.store.finish_session(
                session_id, 0, error="Endpoint paused: the provider refused the job"
            )
            response.raise_for_status()
        self.store.record_provider_job(session_id, provider_job_id)
        phase("submit")
        logger.info(
            "gremlin.dispatch_phases session=%s %s",
            session_id,
            " ".join(f"{name}={seconds}" for name, seconds in phases.items()),
        )
        return session_id

    def submit(self, body):
        for delay in PAUSED_RETRY_SECONDS:
            response = self.request("POST", self.jobs + "/run", json=body)
            if not paused(response):
                return response
            if self.request_deadline and time.monotonic() + delay >= self.request_deadline:
                return response
            time.sleep(delay)
        return self.request("POST", self.jobs + "/run", json=body)

    def restore_capacity(self, selection=None):
        # After a week without requests the provider sets the endpoint's maximum workers to
        # zero and leaves it there, which would queue jobs forever.
        with ThreadPoolExecutor(2) as pool:
            reading = pool.submit(self.read_endpoint)
            listing = (
                pool.submit(self.read, f"/serverless/{self.settings.imprint_endpoint_id}/workers")
                if selection
                else None
            )
            endpoint = reading.result()
            active = listing.result() if listing else None
        workers = endpoint["workers"]
        if selection is None and configured_release(endpoint)[0] is not None:
            raise RuntimeError("Explicit model releases require qualified GPU selection")
        if selection:
            if configuration_digest(endpoint) != selection["configuration_sha256"]:
                raise RuntimeError("Worker configuration changed after GPU discovery")
            if any(
                worker.get("isStale") is not False
                or worker.get("status") in {"UNHEALTHY", "THROTTLED"}
                or worker.get("gpuTypeId") not in selection["selected_gpu_ids"]
                or worker.get("image") != endpoint["image"]
                for worker in active["workers"]
            ):
                # A GPU/configuration change does not replace existing FlashBoot workers, and
                # a throttled worker holds the only slot without serving. Stop first under
                # this reservation, submit nothing and retry fresh discovery on the next
                # sweep. The explicit pre-submission settlement is never used after /run,
                # including when its response is lost.
                self.stop_capacity()
                return False
            # A worker of the current image on a selected GPU stays even without the boot
            # receipt: the lease and the handler fence the release, and RunPod leaves such
            # preparation candidates after most placements. Retiring them discarded the
            # retained worker and its host's cached image and weights on 28 and 29 October
            # 2026 (324 and 352 s to the first lease against 79 to 116 s when it survived).
            # Every endpoint change becomes a provider release that replaces the retained
            # worker's container after its job, so nothing is patched unless the selected GPU
            # set or the fixed placement list differs from the endpoint.
            changes = configuration_changes(endpoint, selection["gpu_patch"])
            if not changes and workers == {"min": 0, "max": 1, "idleTimeout": 5}:
                logger.info("gremlin.gpu_endpoint_unchanged")
                return True
            logger.info(
                "gremlin.gpu_endpoint_patched changes=%s workers=%s",
                ",".join(changes) or "none",
                json.dumps(workers, sort_keys=True),
            )
            patch = self.request(
                "PATCH",
                self.endpoint,
                json=selection["gpu_patch"] | {"workers": {"min": 0, "max": 1, "idleTimeout": 5}},
            )
            patch.raise_for_status()
            confirmed = self.read_endpoint()
            if (
                configuration_digest(confirmed) != selection["configuration_sha256"]
                or configuration_changes(confirmed, selection["gpu_patch"])
                or confirmed["workers"] != {"min": 0, "max": 1, "idleTimeout": 5}
            ):
                raise RuntimeError("GPU eligibility update was not confirmed before submission")
            return True
        if workers["max"] < 1:
            kept = {key: workers[key] for key in ("min", "idleTimeout") if key in workers}
            patch = self.request("PATCH", self.endpoint, json={"workers": kept | {"max": 1}})
            patch.raise_for_status()

    @serialized
    def reconcile(self):
        live = self.store.ledger()["live_session"]
        if live:
            self.settle(self.store.get_session(live))

    def cancel_provider_job(self, job_id):
        response = self.request("POST", f"{self.jobs}/cancel/{job_id}")
        # A missing cancellation target still needs status reconciliation and conservative
        # settlement. Other errors leave the session live for a later read.
        if response.status_code != 404:
            response.raise_for_status()

    def stop_capacity(self):
        response = self.request(
            "PATCH", self.endpoint, json={"workers": {"min": 0, "max": 0, "idleTimeout": 5}}
        )
        response.raise_for_status()

    @serialized
    def close(self):
        """Close drained capacity without changing the replay or financial ledgers."""
        if (
            self.settings.controller_enabled
            or self.settings.imprint_daily_jobs
            or self.settings.imprint_hourly_microusd
            or self.settings.imprint_budget_microusd
        ):
            raise ValueError(
                "Deploy closed admissions, dispatch and spending before closing capacity"
            )
        if self.store.ledger()["live_session"]:
            raise ValueError(
                "A session is still live; keep reconciliation running until it settles"
            )
        self.stop_capacity()
        endpoint = self.read_endpoint()
        active = self.read(f"/serverless/{self.settings.imprint_endpoint_id}/workers")
        workers = endpoint["workers"]
        return {
            "closed": workers == {"min": 0, "max": 0, "idleTimeout": 5} and not active["workers"],
            "workers": workers,
            "worker_count": len(active["workers"]),
        }

    def observe_workers(self, session):
        active = self.read(f"/serverless/{self.settings.imprint_endpoint_id}/workers")
        selection = json.loads(session["selection_json"])
        known_gpus = {row["gpu_id"] for row in selection["candidates"] + selection["exclusions"]}
        # Each observation replaces the last; a worker keeps the time it was first seen.
        first_seen = {
            worker["id"]: worker.get("first_seen_at", worker["observed_at"])
            for worker in json.loads(session.get("endpoint_workers_json") or "[]")
            if worker.get("id")
        }
        records = []
        for worker in active["workers"][:8]:
            now = clock.now()
            record = {"observed_at": now}
            for source, target, allowed in (
                ("id", "id", r"[A-Za-z0-9_-]{1,100}"),
                ("dataCenterId", "data_center_id", r"[A-Z]{2,5}-[A-Z0-9]{1,10}-\d{1,2}"),
            ):
                value = worker.get(source)
                if isinstance(value, str) and re.fullmatch(allowed, value):
                    record[target] = value
            record["first_seen_at"] = first_seen.get(record.get("id"), now)
            if worker.get("gpuTypeId") in known_gpus:
                record["gpu_id"] = worker["gpuTypeId"]
            if worker.get("image") == selection.get("worker_image"):
                record["image"] = worker["image"]
            for key in ("version", "uptimeSeconds"):
                value = worker.get(key)
                if type(value) in (int, float) and 0 <= value < 1e9:
                    record[key] = value
            records.append(record)
        if records:
            self.store.record_endpoint_workers(session["id"], records)
            session["endpoint_workers_json"] = json.dumps(records)
        return active

    @serialized
    def settle(self, session):
        # A caller may have loaded this session before another invocation completed it.
        session = self.store.get_session(session["id"])
        if session["state"] == "finished":
            return
        now = clock.now()
        job_id = session["provider_job_id"]
        reserved = session["reserved_microusd"]
        if job_id is None:
            if (
                session.get("queue_timeout_at")
                and session["queue_timeout_at"] <= now
                and (
                    session.get("capacity_stop") or self.store.stop_unleased_session(session["id"])
                )
            ):
                self.stop_capacity()
            if session["deadline"] <= now:
                self.store.finish_session(
                    session["id"], reserved, error="Provider job never identified"
                )
            return
        # Read first: a failed cancellation request must not hide an already-terminal job.
        stop_requested = session.get("stop_requested_at")
        should_cancel = (
            session["state"] == "stopping"
            and (stop_requested is None or now - stop_requested >= STOP_GRACE_SECONDS)
        ) or session["deadline"] + 600 <= now
        try:
            response = self.request("GET", f"{self.jobs}/status/{job_id}")
            if response.status_code != 404:
                response.raise_for_status()
                status = response.json()
                state = status.get("status")
        except Exception:
            # A status outage must not prevent a requested stop either. Keep the session
            # unresolved and its reservation held until a later read can settle it.
            if should_cancel:
                try:
                    self.cancel_provider_job(job_id)
                finally:
                    if session.get("capacity_stop"):
                        self.stop_capacity()
            raise
        if response.status_code == 404:
            if session.get("selection_json"):
                self.stop_capacity()
            self.store.finish_session(session["id"], reserved, error="Provider job unknown")
            return
        if state not in TERMINAL:
            unhealthy = False
            if session.get("selection_json"):
                try:
                    active = self.observe_workers(session)
                    unhealthy = any(
                        worker.get("status") == "UNHEALTHY" for worker in active["workers"]
                    )
                except (requests.RequestException, KeyError, TypeError, ValueError):
                    # Diagnostic availability cannot delay a requested stop. Terminal worker
                    # emptiness below is a separate mandatory condition for an automatic retry.
                    logger.warning(
                        "gremlin.gpu_worker_observation_unconfirmed session=%s", session["id"]
                    )
            queue_expired = (
                state == "IN_QUEUE"
                and session.get("queue_timeout_at")
                and session["queue_timeout_at"] <= now
            )
            reason = "unhealthy_worker" if unhealthy else "queue_timeout"
            if (
                queue_expired or (state == "IN_QUEUE" and unhealthy)
            ) and self.store.stop_unleased_session(session["id"], reason=reason):
                should_cancel = True
                session["capacity_stop"] = True
                logger.warning(
                    "gremlin.gpu_capacity_stop session=%s reason=%s", session["id"], reason
                )
            if should_cancel:
                try:
                    self.cancel_provider_job(job_id)
                finally:
                    if session.get("capacity_stop"):
                        self.stop_capacity()
            if session["deadline"] + 600 <= now:
                self.store.finish_session(
                    session["id"],
                    reserved,
                    error="Provider job overran the session deadline",
                    provider=status,
                )
            return
        if session.get("selection_json") and not session.get("capacity_stop"):
            try:
                self.observe_workers(session)
            except (requests.RequestException, KeyError, TypeError, ValueError):
                logger.warning(
                    "gremlin.gpu_worker_observation_unconfirmed session=%s", session["id"]
                )
            evidence = provider_evidence(status, json.loads(session["selection_json"]))
            rejection = evidence.get("worker_rejection")
            if (
                state in {"COMPLETED", "FAILED"}
                and rejection in {"worker_gpu_incompatible", *MODEL_REJECTION_REASONS}
                and self.store.stop_unleased_session(session["id"], reason=rejection)
            ):
                # The pinned worker emits this result before constructing the lease client.
                # The atomic fence still proves no attempt raced with its rejection.
                session["capacity_stop"] = True
                logger.warning(
                    "gremlin.gpu_capacity_stop session=%s reason=%s", session["id"], rejection
                )
        if session.get("capacity_stop"):
            # A previous stop request may have failed. Do not release the reservation until
            # maximum zero has also been acknowledged, even after terminal job confirmation.
            self.stop_capacity()
            active = self.read(f"/serverless/{self.settings.imprint_endpoint_id}/workers")
            if active["workers"]:
                return
        # The provider bills a worker from its start to the end of its idle timeout. delayTime
        # runs from submission to pickup, so it covers the cold start (and any unbilled wait for
        # capacity). Incomplete timings cannot establish a cheaper charge: retain the full
        # reservation rather than treating missing execution time as zero.
        timings = provider_evidence(status)
        complete_timings = all(key in timings for key in ("delayTime", "executionTime"))
        billed_seconds = (
            (timings["delayTime"] + timings["executionTime"]) / 1000 + IDLE_ALLOWANCE
            if complete_timings
            else 0
        )
        if session.get("capacity_stop"):
            # Cancellation often omits timings. Include sweep/cancellation/worker-stop delay
            # before another reservation, even when some elapsed queue time was not billed.
            billed_seconds = max(billed_seconds, now - session["created_at"] + IDLE_ALLOWANCE)
        measured = math.ceil(session["hourly_microusd"] * billed_seconds / 3600)
        if not complete_timings:
            measured = max(reserved, measured)
        self.store.finish_session(
            session["id"],
            measured,
            error=None if state == "COMPLETED" else f"Provider job {state}",
            provider=status,
        )
        if session.get("capacity_stop"):
            selection = json.loads(session["selection_json"])
            placements = [
                {"gpu_id": worker["gpu_id"], "data_center_id": worker["data_center_id"]}
                for worker in json.loads(session.get("endpoint_workers_json") or "[]")
                if worker.get("gpu_id") and worker.get("data_center_id")
            ] or [
                {"gpu_id": candidate["gpu_id"], "data_center_id": dc["id"]}
                for candidate in selection["candidates"]
                if candidate["gpu_id"] in selection["selected_gpu_ids"]
                for dc in candidate["data_centers"]
            ]
            if self.store.recover_capacity(session["id"], placements):
                logger.warning("gremlin.gpu_capacity_fallback session=%s", session["id"])
        logger.info(
            "gremlin.gpu_session_settled session=%s reserved_microusd=%d "
            "provider_timing_estimate_microusd=%d status=%s",
            session["id"],
            reserved,
            measured,
            state,
        )
