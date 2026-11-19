"""Client-owned Imprint jobs with fenced publication and conservative session accounting.

This ports the SQLite store (`backend/app/imprint/store.py`) to one DynamoDB table. Every
decision reads current items with strongly consistent reads and commits one transaction
conditioned on the versions it read, so concurrent writers re-read and decide again.

Items, by key, and the invariant each carries:

- `JOB#<id>`: the state machine, retention clocks, the immutable outcome and the fenced
  attempt pointer.
- `REPLAY#<sha256(client, key)>`: idempotency (a job pointer until pruning) and the permanent
  410 ledger.
- `CLIENT#<client>` / `ACTIVE`: one active job per client; `JOB#<stamp>#<id>` entries list
  the client's unpruned jobs. The client is the owner scope: a direct client's ID, or
  `<client>:<owner>` for a delegated client serving many end users.
- `CONTROL` / `ADMISSION` with `ADMIT` entries: the rolling daily limit, serialized by version.
- `CONTROL` / `LEDGER`: one live session, cumulative settled cost and the dispatch holds.
- `CONTROL` / `PROVIDER_LOCK`: a bounded owner lease serializes external endpoint changes.
- `CONTROL` / `GPU_SELECTION`: the latest sanitized discovery and exclusion evidence.
- `SESSION#<id>`: token, deadline, reservation, settlement and the one open attempt.
- `PROVIDER#<job id>`: a provider job belongs to one session.
- `ATTEMPT#<id>`: lease, executor and the publication fence.
- `QUEUE` and `DUE` entries: lease order, and when the sweep must look at a job or session.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import uuid
from datetime import UTC, datetime
from functools import cache

from fastapi import HTTPException

from gremlin_control import clock, moderation, preflight
from gremlin_control.aws import Conflict, Media, Table, optimistic
from gremlin_control.errors import api_error
from gremlin_control.limits import (
    ACTIVE,
    ADMISSION_SECONDS,
    CONTENT_TYPES,
    DIAGNOSTIC_SECONDS,
    DOWNLOAD_SECONDS,
    INPUT_LIMITS,
    MEDIA_SECONDS,
    RECORD_SECONDS,
    RESULT_LIMITS,
    RESULT_NAMES,
    RUNNING,
    SAVED_SECONDS,
    UPLOAD_SECONDS,
)
from gremlin_control.model_release import valid_release

# One serverless session may run for 40 minutes after its handler starts, and the provider
# also bills the cold start before the handler and the idle timeout after it. The allowance
# covers a cold start that downloads the weights (216 s measured on 20 October 2026) with
# room to spare; settlement replaces it with the provider's delayTime plus the idle window.
SESSION_SECONDS = 2400
SESSION_ALLOWANCE = 660
# The endpoint's 5 s idle timeout plus the worker's shutdown, with margin.
IDLE_ALLOWANCE = 30
# Deletion waits for every presigned upload link to lapse, so a late transfer can never
# leave media behind a completed sweep.
MEDIA_KINDS = ("reference", "driving", *RESULT_NAMES, "diagnostics.zip")
ADMISSION_MESSAGES = {
    "spending_unauthorized": "No GPU spending is authorized.",
    "endpoint_unconfigured": "The GPU endpoint is not configured.",
    "daily_limit_reached": "The daily job limit is reached.",
    "dispatch_disabled": "GPU dispatch is disabled.",
    "dispatch_held": "Dispatch requires operator review of a finished session.",
    "spending_limit_reached": "The service spending limit is reached. Please try later.",
    "budget_exhausted": "The authorized GPU budget cannot cover another session.",
}
TIMING_KEYS = {
    "preparation_seconds",
    "tracking_seconds",
    "generation_seconds",
    "restoration_seconds",
    "encoding_seconds",
    "total_seconds",
    "input_transfer_seconds",
    "delivery_seconds",
    "attempt_seconds",
    "model_file_load_seconds",
    "model_device_load_seconds",
    "engine_first_use",
}
SESSION_FAILED = "The GPU session ended before this attempt completed."
INPUT_REJECTION_MESSAGES = {
    "reference_subject_not_found": "No subject was found at reference_point in the reference.",
    "target_not_tracked": "The character at target_point could not be tracked through the clip.",
    "driving_scene_cut": "The interval contains a scene cut; select one continuous shot.",
}
# A worker that reported a failure, or lost its attempt to a cancel or a lease expiry, ends its
# provider job by itself within seconds, keeping the provider's billed timings. The controller
# cancels the provider job only after this grace.
STOP_GRACE_SECONDS = 90
GPU_REJECTION_REASONS = {
    "constraints_invalid",
    "gpu_count_mismatch",
    "memory_limit_unknown",
    "library_versions_mismatch",
    "attention_unavailable",
    "torch_arch_unsupported",
    "configuration_mismatch",
    "probe_import_failed",
    "probe_device_failed",
    "probe_memory_failed",
    "probe_libraries_failed",
    "probe_attention_failed",
    "probe_timeout",
    "probe_process_failed",
    "probe_output_invalid",
}
GPU_PROBE_EXCEPTIONS = {
    "FileNotFoundError",
    "PermissionError",
    "ValueError",
    "RuntimeError",
    "ImportError",
    "ModuleNotFoundError",
    "PackageNotFoundError",
    "TimeoutExpired",
    "CalledProcessError",
    "JSONDecodeError",
    "OSError",
    "UnknownError",
}
MODEL_REJECTION_REASONS = {
    "worker_model_configuration_invalid",
    "worker_model_cache_invalid",
}


def worker_release_changed(previous, current):
    """Only a known immutable release change starts a different capacity retry chain."""

    def identity(report):
        if not report or not re.fullmatch(
            r".+@sha256:[0-9a-f]{64}", report.get("worker_image") or ""
        ):
            return None
        source, release = report.get("weight_source"), report.get("model_release")
        legacy = (
            source is None and release is None and not report.get("worker_supports_model_release")
        )
        if not legacy and not valid_release(release, source):
            return None
        return report["worker_image"], source, release

    before, after = identity(previous), identity(current)
    return before is not None and after is not None and before != after


def replay_digest(client_id, key):
    # Length-delimited by JSON, so distinct (client, key) pairs cannot alias.
    return hashlib.sha256(json.dumps([client_id, key], separators=(",", ":")).encode()).hexdigest()


def gpu_rejection_evidence(value, selection):
    """Persist useful allocation diagnostics without exception text or arbitrary device strings."""
    if not isinstance(value, dict):
        return {}
    reason = value.get("reason")
    if not isinstance(reason, str) or reason not in GPU_REJECTION_REASONS:
        return {}
    evidence = {"reason": reason}
    exception = value.get("probe_exception_type")
    if isinstance(exception, str) and exception in GPU_PROBE_EXCEPTIONS:
        evidence["probe_exception_type"] = exception
    hardware = value.get("hardware")
    if not isinstance(hardware, dict):
        return evidence
    measured = {}
    names = {
        name
        for constraint in selection.get("gpu_constraints", [])
        for name in constraint["runtime_gpu_names"]
    } | {row["gpu_id"] for row in selection.get("candidates", []) + selection.get("exclusions", [])}
    name = hardware.get("gpu_name")
    if isinstance(name, str) and name in names:
        measured["gpu_name"] = name
    for key in ("vram_bytes", "host_memory_bytes", "container_memory_bytes", "container_cpus"):
        number = hardware.get(key)
        bound = 100_000 if key == "container_cpus" else 10**15
        if type(number) is int and 0 <= number < bound:
            measured[key] = number
    capability = hardware.get("compute_capability")
    if (
        isinstance(capability, list)
        and len(capability) == 2
        and all(type(part) is int and 0 <= part < 100 for part in capability)
    ):
        measured["compute_capability"] = capability
    version = hardware.get("cuda_version")
    if isinstance(version, str) and re.fullmatch(r"\d{1,2}\.\d{1,2}", version):
        measured["cuda_version"] = version
    if type(hardware.get("attention_available")) is bool:
        measured["attention_available"] = hardware["attention_available"]
    if hardware.get("cgroup_version") in ("v1", "v2"):
        measured["cgroup_version"] = hardware["cgroup_version"]
    if measured:
        evidence["hardware"] = measured
    return evidence


def provider_evidence(value, selection=None):
    """Keep timings and qualified hardware measurements, never arbitrary provider output."""
    if not isinstance(value, dict):
        return {}
    evidence = {}
    if isinstance(value.get("status"), str) and value["status"] in {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "IN_QUEUE",
        "IN_PROGRESS",
    }:
        evidence["status"] = value["status"]
    for key in ("delayTime", "executionTime"):
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 1e12:
            evidence[key] = number
    if not selection:
        return evidence
    constraints = selection.get("gpu_constraints", [])
    if (
        constraints
        and selection.get("worker_supports_gpu_constraints") is True
        and value.get("error") == "worker_gpu_incompatible"
    ):
        # RunPod SDK 1.12 promotes handler errors to the top level and removes empty output.
        evidence["worker_rejection"] = "worker_gpu_incompatible"
    if (
        selection.get("worker_supports_model_release") is True
        and isinstance(value.get("error"), str)
        and value["error"] in MODEL_REJECTION_REASONS
    ):
        evidence["worker_rejection"] = value["error"]
    output = value.get("output")
    if not isinstance(output, dict):
        return evidence
    session_timings = output.get("session_timings")
    if isinstance(session_timings, dict):
        evidence["session_timings"] = {
            key: number
            for key, number in session_timings.items()
            if key in {"session_seconds", "completion_seconds", "attempt_seconds"}
            and type(number) in (int, float)
            and 0 <= number <= 1e6
        }
    if evidence.get("worker_rejection") == "worker_gpu_incompatible":
        rejection = gpu_rejection_evidence(output.get("gpu_rejection"), selection)
        if rejection:
            evidence["gpu_rejection"] = rejection
    hardware = output.get("gpu")
    if isinstance(hardware, dict) and any(
        hardware.get("gpu_id") == allowed["gpu_id"]
        and hardware.get("gpu_name") in allowed["runtime_gpu_names"]
        for allowed in constraints
    ):
        gpu = {key: hardware[key] for key in ("gpu_id", "gpu_name")}
        for key in ("vram_bytes", "host_memory_bytes", "container_memory_bytes", "container_cpus"):
            number = hardware.get(key)
            if type(number) is int and 0 < number < 10**15:
                gpu[key] = number
        capability = hardware.get("compute_capability")
        if (
            isinstance(capability, list)
            and len(capability) == 2
            and all(type(part) is int and 0 <= part < 100 for part in capability)
        ):
            gpu["compute_capability"] = capability
        version = hardware.get("cuda_version")
        if isinstance(version, str) and re.fullmatch(r"\d{1,2}\.\d{1,2}", version):
            gpu["cuda_version"] = version
        if type(hardware.get("attention_available")) is bool:
            gpu["attention_available"] = hardware["attention_available"]
        evidence["gpu"] = gpu
    boot = output.get("boot")
    if isinstance(boot, dict):
        evidence["boot"] = {
            key: number
            for key, number in boot.items()
            if key
            in {
                "weights_seconds",
                "stage_seconds",
                "engine_ready_seconds",
                "cache_resolve_seconds",
                "hash_seconds",
                "download_seconds",
            }
            and type(number) in (int, float)
            and 0 <= number <= 1e6
        }
        source, release = boot.get("weight_source"), boot.get("model_release")
        if (
            selection.get("worker_supports_model_release") is True
            and valid_release(release, source)
            and source == selection.get("weight_source")
            and release == selection.get("model_release")
        ):
            evidence["boot"].update(weight_source=source, model_release=release)
            worker_id = value.get("workerId")
            if isinstance(worker_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", worker_id):
                evidence["worker_id"] = worker_id
    return evidence


def timing_evidence(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: number
        for key, number in value.items()
        if key in TIMING_KEYS and type(number) in (int, float) and 0 <= number <= 1e12
    }


def stamp(seconds):
    # Fixed width sorts lexically in time order until the year 2286.
    return f"{seconds:017.6f}"


def media_key(job_id, kind):
    if kind not in MEDIA_KINDS:
        raise api_error(404, "IMPRINT_MEDIA_UNKNOWN", "Unknown media.")
    return f"jobs/{uuid.UUID(job_id)}/{kind}"


@cache
def _resources(region, dynamodb_endpoint, s3_endpoint, table_name, bucket):
    from gremlin_control.settings import Settings

    settings = Settings(
        region=region,
        dynamodb_endpoint=dynamodb_endpoint,
        s3_endpoint=s3_endpoint,
        table_name=table_name,
        media_bucket=bucket,
    )
    return Table(settings), Media(settings)


class ImprintStore:
    def __init__(self, settings):
        self.settings = settings
        self.table, self.media = _resources(
            settings.region,
            settings.dynamodb_endpoint,
            settings.s3_endpoint,
            settings.table_name,
            settings.media_bucket,
        )

    # Reads

    def check_owner(self, client_id):
        if not self.settings.imprint_enabled:
            raise api_error(404, "IMPRINT_DISABLED", "Imprint is unavailable.")

    def job(self, job_id, client_id=None):
        try:
            uuid.UUID(job_id)
        except ValueError:
            job = None
        else:
            job = self.table.get(f"JOB#{job_id}", "JOB")
        if job is None or (client_id is not None and job["client_id"] != client_id):
            raise api_error(404, "IMPRINT_NOT_FOUND", "Imprint job not found.")
        return job

    def ledger(self):
        return self.table.get("CONTROL", "LEDGER") or {
            "pk": "CONTROL",
            "sk": "LEDGER",
            "settled_microusd": 0,
            "spending_initialized": True,
            "live_session": None,
            "held": [],
            "retention_held": [],
        }

    def get_session(self, session_id):
        try:
            uuid.UUID(session_id)
        except ValueError:
            return None
        return self.table.get(f"SESSION#{session_id}", "SESSION")

    def acquire_provider_lock(self):
        """Outlive the 120 s Lambda invocation so a crashed owner cannot overlap a new one."""
        owner = str(uuid.uuid4())

        def decide(txn):
            item = self.table.get("CONTROL", "PROVIDER_LOCK")
            if item and item["until"] > clock.now():
                return None
            item = item or {"pk": "CONTROL", "sk": "PROVIDER_LOCK"}
            item.update(owner=owner, until=int(clock.now()) + 180)
            txn.put(item)
            return owner

        return optimistic(self.table, decide)

    def release_provider_lock(self, owner):
        def decide(txn):
            item = self.table.get("CONTROL", "PROVIDER_LOCK")
            if item and item["owner"] == owner:
                txn.delete(item["pk"], item["sk"], item["v"])

        optimistic(self.table, decide)

    def gpu_decision(self):
        item = self.table.get("CONTROL", "GPU_SELECTION")
        return json.loads(item["report_json"]) if item else None

    def record_gpu_decision(self, report):
        # Only the selector's sanitized catalogue evidence belongs here, never the endpoint
        # environment or provider responses carrying worker capability tokens.
        def decide(txn):
            ledger = self.ledger()
            if (
                not ledger["live_session"]
                and not ledger["held"]
                and worker_release_changed(self.gpu_decision(), report)
            ):
                ledger.update(capacity_attempts=0, gpu_exclusions=[])
                ledger.pop("capacity_expires_at", None)
                txn.put(ledger)
            else:
                txn.check(ledger["pk"], ledger["sk"], ledger.get("v"))
            # Dispatch is serialized. Commit its report and retry reset together so a
            # crash cannot leave a new release attributed to the old retry chain.
            txn.put_index(
                {
                    "pk": "CONTROL",
                    "sk": "GPU_SELECTION",
                    "report_json": json.dumps(report),
                    "expire_at": int(clock.now() + DIAGNOSTIC_SECONDS),
                }
            )

        optimistic(self.table, decide)

    def refresh_capacity_retries(self):
        """A later workload gets a fresh chain after every original queued intent has expired."""

        def decide(txn):
            ledger = self.ledger()
            if (
                ledger.get("capacity_expires_at", clock.now() + 1) <= clock.now()
                and ledger["live_session"] is None
                and not ledger["held"]
            ):
                ledger.update(capacity_attempts=0, gpu_exclusions=[])
                ledger.pop("capacity_expires_at", None)
                txn.put(ledger)

        optimistic(self.table, decide)

    def admission_blocker(self):
        """The reason a submit cannot queue work now, or None; see ADMISSION_MESSAGES."""
        # Operator spending authority, not a provider-enforced cap.
        if (
            min(
                self.settings.imprint_hourly_microusd,
                self.settings.imprint_rolling_budget_microusd,
                self.settings.imprint_monthly_budget_microusd,
            )
            <= 0
        ):
            return "spending_unauthorized"
        if not self.settings.provider_configured:
            return "endpoint_unconfigured"
        return None

    def dispatch_held(self):
        return bool(self.ledger()["held"])

    def reservation_microusd(self, hourly_microusd, seconds=SESSION_SECONDS):
        return (hourly_microusd * (seconds + SESSION_ALLOWANCE) + 3599) // 3600

    def daily_admissions(self, now):
        """Admissions in the rolling window and when the oldest of them leaves it."""
        window = stamp(now - 86400) + "#~"
        count = self.table.count("ADMIT", window)
        oldest = self.table.query("ADMIT", after=window, limit=1)
        reset = float(oldest[0]["sk"].split("#", 1)[0]) + 86400 if oldest else None
        return count, reset

    def available_budget(self, ledger):
        """Discovery ranks only affordable GPUs; reservation rechecks transactionally."""
        if not ledger.get("spending_initialized"):
            return 0
        now = clock.now()
        month = datetime.fromtimestamp(now, UTC).strftime("%Y-%m")
        monthly = ledger.get("spend_month_microusd", 0) if ledger.get("spend_month") == month else 0
        rolling = sum(
            row["microusd"] for row in self.table.query("SPEND", after=stamp(now - 86400) + "#~")
        )
        remaining = min(
            self.settings.imprint_rolling_budget_microusd - rolling,
            self.settings.imprint_monthly_budget_microusd - monthly,
        )
        if self.settings.imprint_budget_microusd > 0:
            remaining = min(
                remaining, self.settings.imprint_budget_microusd - ledger["settled_microusd"]
            )
        live = self.get_session(ledger["live_session"]) if ledger["live_session"] else None
        return max(0, remaining - (live["reserved_microusd"] if live else 0))

    def spending_limit(self, ledger, reservation, now):
        if not ledger.get("spending_initialized"):
            return True, None
        month = datetime.fromtimestamp(now, UTC).strftime("%Y-%m")
        month_spend = (
            ledger.get("spend_month_microusd", 0) if ledger.get("spend_month") == month else 0
        )
        rows = self.table.query("SPEND", after=stamp(now - 86400) + "#~")
        rolling = sum(row["microusd"] for row in rows)
        rolling_limit = self.settings.imprint_rolling_budget_microusd
        monthly_limit = self.settings.imprint_monthly_budget_microusd
        blocked = rolling + reservation > rolling_limit or month_spend + reservation > monthly_limit
        if not blocked:
            return False, None
        if reservation > min(rolling_limit, monthly_limit):
            return True, None
        retry = 0
        for row in rows:
            if rolling + reservation <= rolling_limit:
                break
            rolling -= row["microusd"]
            retry = max(retry, math.ceil(row["finished_at"] + 86400 - now))
        if month_spend + reservation > monthly_limit:
            date = datetime.fromtimestamp(now, UTC)
            next_month = datetime(
                date.year + (date.month == 12), date.month % 12 + 1, 1, tzinfo=UTC
            )
            retry = max(retry, math.ceil(next_month.timestamp() - now))
        return True, max(1, retry)

    def _record_spend(self, txn, ledger, session):
        at = session["finished_at"]
        amount = session["measured_microusd"]
        txn.put(
            {
                "pk": "SPEND",
                "sk": f"{stamp(at)}#{session['id']}",
                "finished_at": at,
                "microusd": amount,
                "expire_at": int(at + 35 * 86400),
            }
        )
        month = datetime.fromtimestamp(at, UTC).strftime("%Y-%m")
        if ledger.get("spend_month", "") < month:
            ledger.update(spend_month=month, spend_month_microusd=0)
        if ledger.get("spend_month") == month:
            ledger["spend_month_microusd"] += amount

    def initialize_spending(self):
        """One-time migration; old lifetime totals are never reset or credited back."""
        if self.ledger().get("spending_initialized"):
            return {"initialized": True}
        if self.ledger()["live_session"]:
            raise api_error(409, "IMPRINT_SESSION_ACTIVE", "Settle live work before migration.")
        sessions = [
            row
            for row in self.table.scan()
            if row["pk"].startswith("SESSION#")
            and row["sk"] == "SESSION"
            and row["state"] == "finished"
        ]
        for session in sorted(sessions, key=lambda row: row["finished_at"]):

            def seed(txn, session=session):
                ledger = self.ledger()
                key = f"{stamp(session['finished_at'])}#{session['id']}"
                if not self.table.get("SPEND", key):
                    self._record_spend(txn, ledger, session)
                    txn.put(ledger)

            optimistic(self.table, seed)

        def finish(txn):
            ledger = self.ledger()
            ledger["spending_initialized"] = True
            txn.put(ledger)

        optimistic(self.table, finish)
        return {"initialized": True}

    def admission_status(self):
        """What a client can expect right now: admission, dispatch and the first blocker."""
        now = clock.now()
        limit = self.settings.imprint_daily_jobs
        count, reset = self.daily_admissions(now)
        remaining = max(0, limit - count)
        reason = self.admission_blocker()
        retry_after = None
        if reason is None and remaining == 0:
            reason = "daily_limit_reached"
            retry_after = max(1, math.ceil(reset - now)) if reset else None
        ledger = self.ledger()
        if reason is None and not ledger["live_session"]:
            blocked, retry = self.spending_limit(
                ledger, self.reservation_microusd(self.settings.imprint_hourly_microusd), now
            )
            if blocked:
                reason, retry_after = "spending_limit_reached", retry
        accepting = reason is None
        dispatching = accepting
        if dispatching and not self.settings.controller_enabled:
            reason, dispatching = "dispatch_disabled", False
        if dispatching:
            ledger = self.ledger()
            live = self.get_session(ledger["live_session"]) if ledger["live_session"] else None
            held = ledger["settled_microusd"] + (live["reserved_microusd"] if live else 0)
            if ledger["held"]:
                reason, dispatching = "dispatch_held", False
            elif (
                self.settings.imprint_budget_microusd > 0
                and held + self.reservation_microusd(self.settings.imprint_hourly_microusd)
                > self.settings.imprint_budget_microusd
            ):
                reason, dispatching = "budget_exhausted", False
        return {
            "accepting": accepting,
            "dispatching": dispatching,
            "reason_code": reason,
            "retry_after_seconds": retry_after,
            "daily_jobs_limit": limit,
            "daily_jobs_remaining": remaining,
            "observed_at": now,
        }

    def queue_position(self, job):
        """Unexpired queued jobs ahead of this one; the worker leases the oldest first."""
        if job["state"] != "queued":
            return None
        now = clock.now()
        own = f"{stamp(job['created_at'])}#{job['id']}"
        return sum(
            entry["sk"] != own and entry["expires_at"] > now
            for entry in self.table.query("QUEUE", before=own)
        )

    def execution(self, job, queue_position=None):
        """The GPU session serving a job and its worker's measured timings, without identifiers.

        A queued job is served by the live session only while it is first in the queue and that
        session has not taken other work; from its lease on, by its attempt's session.
        """
        attempt = session = None
        if job.get("attempt_id"):
            attempt = self.table.get(f"ATTEMPT#{job['attempt_id']}", "ATTEMPT")
            session = attempt and self.get_session(attempt["session_id"])
        elif job["state"] == "queued" and queue_position == 0:
            live = self.ledger()["live_session"]
            session = live and self.get_session(live)
            if session and (session["state"] != "running" or session["open_attempt"]):
                session = None
        if not session:
            return None
        gpu = seen_at = None
        workers = json.loads(session.get("endpoint_workers_json") or "[]")
        observed = next((w for w in workers if w.get("gpu_id")), None)
        selection = json.loads(session.get("selection_json") or "{}")
        if observed:
            gpu = observed["gpu_id"]
            seen_at = observed.get("first_seen_at", observed["observed_at"])
        elif (
            attempt
            and selection.get("worker_supports_gpu_constraints")
            and len(selection.get("selected_gpu_ids", [])) == 1
        ):
            # The worker checks its GPU against the session's only constraint before leasing.
            gpu = selection["selected_gpu_ids"][0]
        if gpu and attempt:
            seen_at = min(seen_at or attempt["started_at"], attempt["started_at"])
        return {
            "gpu_requested_at": session["created_at"],
            "gpu": gpu,
            "gpu_seen_at": seen_at,
            "started_at": attempt["started_at"] if attempt else None,
            "timings": json.loads(attempt.get("timings_json") or "{}") if attempt else {},
        }

    def list_jobs(self, client_id, *, limit, state=None):
        """The scope's unpruned jobs, newest first; records predate this index are absent."""
        jobs = []
        for entry in self.table.query(f"CLIENT#{client_id}", forward=False):
            if not entry["sk"].startswith("JOB#"):
                continue
            job = self.table.get(f"JOB#{entry['job_id']}", "JOB")
            if job is None or job["client_id"] != client_id:
                continue
            if state == "active" and job["state"] not in ACTIVE:
                continue
            if state not in (None, "active") and job["state"] != state:
                continue
            jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs

    def media_available(self, job, kind):
        now = clock.now()
        if kind == "diagnostics.zip":
            return job["state"] != "cancelled" and (
                job["terminal_at"] is None or job["terminal_at"] + DIAGNOSTIC_SECONDS > now
            )
        if kind in ("reference", "driving"):
            return job["state"] in ACTIVE and (
                job["state"] not in ("uploading", "queued") or job["expires_at"] > now
            )
        return job["state"] == "succeeded" and job["expires_at"] > now

    # Scheduling: each job and finished session has at most one due entry, read by the sweep.

    def _schedule(self, txn, item, kind, due):
        new = None if due is None else f"{stamp(due)}#{kind}#{item['id']}"
        old = item.get("due_sk")
        if old == new:
            return
        if old:
            txn.delete("DUE", old)
        if new:
            txn.put_index({"pk": "DUE", "sk": new, "kind": kind, "id": item["id"]})
        item["due_sk"] = new

    def _end_activity(self, txn, job):
        """Leaving the active states frees the client's one active-job slot."""
        if job["state"] in ACTIVE:
            txn.delete(
                f"CLIENT#{job['client_id']}",
                "ACTIVE",
                condition="job_id = :job",
                values={":job": job["id"]},
            )
        if job["state"] == "queued":
            txn.delete("QUEUE", f"{stamp(job['created_at'])}#{job['id']}")

    def _fail(self, txn, job, now, error, failure_code, failure_reason=None):
        self._end_activity(txn, job)
        job.update(
            state="failed",
            updated_at=now,
            terminal_at=now,
            expires_at=now + MEDIA_SECONDS,
            error=error,
            outcome_kind="failed",
            failure_code=failure_code,
            failure_reason=failure_reason,
        )
        self._schedule(txn, job, "job", now)

    # Client operations

    def _moderation_fail(self, txn, job, code):
        now = clock.now()
        self._fail(
            txn,
            job,
            now,
            moderation.REJECTION_MESSAGE
            if code == "content_rejected"
            else "Content checks could not be completed.",
            code,
        )
        if job["attempt_id"]:
            attempt = self.table.get(f"ATTEMPT#{job['attempt_id']}", "ATTEMPT")
            attempt["finished_at"] = now
            txn.put(attempt)
            session = self.get_session(attempt["session_id"])
            session["open_attempt"] = None
            session["rejected_count"] = session.get("rejected_count", 0) + 1
            txn.put(session)

    def _moderate(self, job_id, stage, check):
        """Claim paid checks outside optimistic retries; cache acceptance on immutable content."""
        token = uuid.uuid4().hex
        expected = "uploading" if stage == "input" else "finishing"
        counter = f"moderation_{stage}_attempts"

        def claim(txn):
            job = self.job(job_id)
            if job["state"] != expected or job.get(f"moderation_{stage}") == "accepted":
                return False
            now = clock.now()
            if job.get("moderation_until", 0) > now:
                raise moderation.unavailable()
            if job.get(counter, 0) >= moderation.MAX_ATTEMPTS:
                self._moderation_fail(txn, job, "moderation_unavailable")
                txn.put(job)
                return False
            job.update(
                moderation_token=token,
                moderation_until=now + moderation.CLAIM_SECONDS,
            )
            job[counter] = job.get(counter, 0) + 1
            self._schedule(txn, job, "job", min(job["expires_at"], job["moderation_until"]))
            txn.put(job)
            return True

        if not optimistic(self.table, claim):
            return
        result = "accepted"
        try:
            check()
        except moderation.Rejected:
            result = "content_rejected"
        except HTTPException as error:
            if error.detail.get("code") != "IMPRINT_MODERATION_UNAVAILABLE":
                raise
            result = "moderation_unavailable"

        def finish(txn):
            job = self.job(job_id)
            if (
                job["state"] != expected
                or job.get("moderation_token") != token
                or job.get("moderation_until", 0) <= clock.now()
            ):
                raise moderation.unavailable()
            job.update(moderation_token="", moderation_until=0)
            if result == "accepted":
                job[f"moderation_{stage}"] = result
            elif result == "content_rejected" or job[counter] >= moderation.MAX_ATTEMPTS:
                self._moderation_fail(txn, job, result)
            txn.put(job)

        optimistic(self.table, finish)
        if result == "moderation_unavailable":
            raise moderation.unavailable()

    def erase_owner(self, client_id):
        digest = hashlib.sha256(client_id.encode()).hexdigest()

        def decide(txn):
            receipt = self.table.get(f"ERASE#{digest}", "ERASE")
            if receipt:
                return receipt
            now = clock.now()
            receipt = {
                "pk": f"ERASE#{digest}",
                "sk": "ERASE",
                "id": digest,
                "scope": client_id,
                "status": "pending",
                "requested_at": now,
                "completed_at": None,
            }
            self._schedule(txn, receipt, "erase", now)
            return txn.put(receipt)

        return optimistic(self.table, decide)

    def _reconcile_erasure(self, digest, due_sk, now):
        receipt = self.table.get(f"ERASE#{digest}", "ERASE")
        if not receipt or receipt["status"] != "pending":
            return
        scope = receipt["scope"]
        for entry in self.table.query(f"CLIENT#{scope}", limit=50):
            if not entry["sk"].startswith("JOB#"):
                continue
            job_id = entry["job_id"]
            self.cancel(job_id)

            def mark(txn, job_id=job_id):
                job = self.job(job_id)
                job["erased"] = True
                txn.put(job)
                return job

            job = optimistic(self.table, mark)
            if not self._blocked(job):
                self._clean_job(job_id, now)

        def finish(txn):
            current = self.table.get(f"ERASE#{digest}", "ERASE")
            if self.table.query(f"CLIENT#{scope}", limit=1):
                self._schedule(txn, current, "erase", now + 60)
            else:
                current.update(
                    status="completed", completed_at=now, expire_at=int(now + RECORD_SECONDS)
                )
                current.pop("scope", None)
                self._schedule(txn, current, "erase", None)
            txn.put(current)

        optimistic(self.table, finish)

    def create(self, client_id, key, request):
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
        digest = replay_digest(client_id, key)
        deadline = request.get("admission_expires_at")

        def decide(txn):
            now = clock.now()
            erasure_key = f"ERASE#{hashlib.sha256(client_id.encode()).hexdigest()}"
            if self.table.get(erasure_key, "ERASE"):
                raise api_error(409, "IMPRINT_OWNER_ERASING", "This owner has requested deletion.")
            txn.check(erasure_key, "ERASE", None)
            replay = self.table.get(f"REPLAY#{digest}", "REPLAY")
            if replay and replay.get("job_id"):
                prior = self.job(replay["job_id"])
                if prior["request_json"] != request_json:
                    raise api_error(
                        409, "IMPRINT_INTENT_CHANGED", "Start a new request for changed inputs."
                    )
                txn.check(prior["pk"], prior["sk"], prior["v"])
                return prior
            if replay:
                raise api_error(
                    410,
                    "IMPRINT_INTENT_EXPIRED",
                    "This intent was retired. It cannot generate again.",
                )
            if self.table.get(f"CLIENT#{client_id}", "ACTIVE"):
                raise api_error(
                    409, "IMPRINT_ACTIVE", "Finish or cancel your existing Imprint first."
                )
            status = self.admission_status()
            if status["reason_code"] == "spending_limit_reached":
                raise api_error(
                    429,
                    "IMPRINT_LIMIT",
                    ADMISSION_MESSAGES["spending_limit_reached"],
                    reason="spending_limit_reached",
                    headers={"Retry-After": str(status["retry_after_seconds"])}
                    if status["retry_after_seconds"]
                    else None,
                )
            admission = self.table.get("CONTROL", "ADMISSION") or {
                "pk": "CONTROL",
                "sk": "ADMISSION",
            }
            # Every admission bumps the version, so this count cannot be raced past the limit.
            count, reset = self.daily_admissions(now)
            if count >= self.settings.imprint_daily_jobs:
                raise api_error(
                    429,
                    "IMPRINT_LIMIT",
                    ADMISSION_MESSAGES["daily_limit_reached"],
                    reason="daily_limit_reached",
                    headers={"Retry-After": str(max(1, math.ceil(reset - now)) if reset else 3600)},
                )
            if deadline is not None and deadline <= now:
                raise api_error(
                    422, "IMPRINT_ADMISSION_EXPIRED", "The admission deadline has already passed."
                )
            job_id = str(uuid.uuid4())
            job = {
                "pk": f"JOB#{job_id}",
                "sk": "JOB",
                "id": job_id,
                "client_id": client_id,
                "idempotency_key": key,
                "request_json": request_json,
                "state": "uploading",
                "created_at": now,
                "updated_at": now,
                "expires_at": min(now + ADMISSION_SECONDS, deadline or math.inf),
                "error": None,
                "result_json": None,
                "terminal_at": None,
                "outcome_kind": None,
                "failure_code": None,
                "failure_reason": None,
                "retention_hold": 0,
                "uploads": {},
                "attempt_id": None,
                "url_until": 0.0,
                "media_cleared": False,
                "diagnostics_cleared": False,
                "list_sk": f"JOB#{stamp(now)}#{job_id}",
            }
            self._schedule(txn, job, "job", job["expires_at"])
            txn.put(admission)
            txn.put_index({"pk": f"CLIENT#{client_id}", "sk": job["list_sk"], "job_id": job_id})
            txn.put_index(
                {
                    "pk": "ADMIT",
                    "sk": f"{stamp(now)}#{digest}",
                    # Garbage collection only; the count filters by time itself.
                    "expire_at": int(now) + 2 * 86400,
                }
            )
            txn.put({"pk": f"REPLAY#{digest}", "sk": "REPLAY", "created_at": now, "job_id": job_id})
            txn.put({"pk": f"CLIENT#{client_id}", "sk": "ACTIVE", "job_id": job_id})
            return txn.put(job)

        return optimistic(self.table, decide)

    def input_ticket(self, job_id, kind, size):
        def decide(txn):
            job = self.job(job_id)
            now = clock.now()
            if job["state"] != "uploading" or job["expires_at"] <= now:
                raise api_error(409, "IMPRINT_SEALED", "Inputs are already sealed.")
            if size > INPUT_LIMITS[kind]:
                raise api_error(413, "IMPRINT_MEDIA_TOO_LARGE", "Media exceeds its byte limit.")
            until = now + UPLOAD_SECONDS
            job["url_until"] = max(job["url_until"], until)
            txn.put(job)
            return json.loads(job["request_json"])[kind + "_sha256"], until

        digest, until = optimistic(self.table, decide)
        url, headers = self.media.presign_put(media_key(job_id, kind), digest, size, UPLOAD_SECONDS)
        return {"method": "PUT", "url": url, "headers": headers, "expires_at": until}

    def submit(self, job_id):
        job = self.job(job_id)
        if job["state"] != "uploading":
            return job
        if job["expires_at"] <= clock.now():
            raise api_error(409, "IMPRINT_SEALED", "The upload window has ended.")
        request = json.loads(job["request_json"])
        uploads = {}
        for kind, limit in INPUT_LIMITS.items():
            stored = self.media.head(media_key(job_id, kind))
            if stored is None:
                raise api_error(
                    409, "IMPRINT_INPUT_REQUIRED", "Upload both inputs before submitting."
                )
            size, digest = stored
            if size > limit:
                raise api_error(413, "IMPRINT_MEDIA_TOO_LARGE", "Media exceeds its byte limit.")
            if digest != request[kind + "_sha256"]:
                raise api_error(
                    422, "IMPRINT_HASH_MISMATCH", "Media did not match the submitted content hash."
                )
            uploads[kind] = {"sha256": digest, "bytes": size}
        blocker = self.admission_blocker()
        if blocker is None and self.admission_status()["reason_code"] == "spending_limit_reached":
            blocker = "spending_limit_reached"
        if blocker:
            raise api_error(
                503, "IMPRINT_BUDGET_BLOCKED", ADMISSION_MESSAGES[blocker], reason=blocker
            )

        inputs = {}
        for kind, limit in INPUT_LIMITS.items():
            try:
                data = self.media.get(media_key(job_id, kind), limit=limit)
            except ValueError:
                raise api_error(
                    413, "IMPRINT_MEDIA_TOO_LARGE", "Media exceeds its byte limit."
                ) from None
            if data is None or hashlib.sha256(data).hexdigest() != uploads[kind]["sha256"]:
                raise api_error(
                    422, "IMPRINT_HASH_MISMATCH", "Stored input changed during validation."
                )
            inputs[kind] = data
        if self.settings.moderation_enabled:
            decoded = preflight.validate(inputs, request, screening=True)
            metadata = decoded["metadata"]
            self._moderate(
                job_id,
                "input",
                lambda: moderation.check(self.settings, decoded["samples"], request["prompt"]),
            )
        else:
            metadata = preflight.validate(inputs, request)

        def decide(txn):
            current = self.job(job_id)
            if current["state"] != "uploading":
                return current
            now = clock.now()
            if current["expires_at"] <= now:
                raise api_error(409, "IMPRINT_SEALED", "The upload window has ended.")
            if self.settings.moderation_enabled and current.get("moderation_input") != "accepted":
                raise moderation.unavailable()
            current.update(state="queued", updated_at=now, uploads=uploads, input_metadata=metadata)
            txn.put_index(
                {
                    "pk": "QUEUE",
                    "sk": f"{stamp(current['created_at'])}#{job_id}",
                    "job_id": job_id,
                    "expires_at": current["expires_at"],
                }
            )
            return txn.put(current)

        return optimistic(self.table, decide)

    def cancel(self, job_id, *, if_not_started=False):
        def decide(txn):
            job = self.job(job_id)
            now = clock.now()
            stopped = False
            if if_not_started and job["attempt_id"]:
                # Decided against the same job version a lease would have changed.
                raise api_error(409, "IMPRINT_STARTED", "A worker already started this job.")
            attempt = (
                self.table.get(f"ATTEMPT#{job['attempt_id']}", "ATTEMPT")
                if job["attempt_id"]
                else None
            )
            if attempt and attempt["finished_at"] is None:
                session = self.get_session(attempt["session_id"])
                if session["state"] != "finished":
                    session["state"] = "stopping"
                    session["stop_requested_at"] = session.get("stop_requested_at") or now
                    stopped = True
                if session["open_attempt"] == attempt["id"]:
                    session["open_attempt"] = None
                txn.put(session)
                attempt["finished_at"] = now
                txn.put(attempt)
            if job["state"] not in ("cancelled", "expired"):
                self._end_activity(txn, job)
                if job["terminal_at"] is None:
                    job.update(terminal_at=now, outcome_kind="cancelled")
                job.update(state="cancelled", updated_at=now, expires_at=now, retention_hold=0)
                self._schedule(txn, job, "job", now)
                job = txn.put(job)
            elif attempt:
                txn.check(job["pk"], job["sk"], job["v"])
            return job, stopped

        return optimistic(self.table, decide)

    def acknowledge(self, job_id):
        def decide(txn):
            job = self.job(job_id)
            now = clock.now()
            if job["state"] == "succeeded" and job["expires_at"] > now + SAVED_SECONDS:
                job["expires_at"] = now + SAVED_SECONDS
                self._schedule(txn, job, "job", now)
                txn.put(job)

        optimistic(self.table, decide)

    def download(self, job, kind):
        key = media_key(job["id"], kind)
        if self.media.head(key) is None:
            return None
        return self.media.presign_get(key, DOWNLOAD_SECONDS, CONTENT_TYPES[kind])

    # Sessions and accounting

    def reserve_session(
        self, hourly_microusd, seconds=SESSION_SECONDS, *, financial_authority=False, selection=None
    ):
        if not financial_authority:
            blocker = self.admission_blocker() or "spending_unauthorized"
            raise api_error(
                503, "IMPRINT_BUDGET_BLOCKED", ADMISSION_MESSAGES[blocker], reason=blocker
            )
        authorized = self.settings.imprint_hourly_microusd
        if not 0 < hourly_microusd <= authorized or not 1 <= seconds <= 3600:
            raise ValueError("Session exceeds the authorized configuration")
        reservation = self.reservation_microusd(hourly_microusd, seconds)
        token = secrets.token_urlsafe(32)
        webhook = secrets.token_urlsafe(32)

        def decide(txn):
            ledger = self.ledger()
            if ledger["held"]:
                raise api_error(
                    503,
                    "IMPRINT_DISPATCH_HELD",
                    "Dispatch requires operator review of the session.",
                )
            if selection and ledger.get("capacity_attempts", 0) >= 3:
                raise api_error(
                    503,
                    "IMPRINT_GPU_FALLBACK_EXHAUSTED",
                    "The GPU allocation retry limit was reached.",
                )
            live = self.get_session(ledger["live_session"]) if ledger["live_session"] else None
            # Live sessions hold their whole reservation; finished ones hold their measured
            # cost, a conservative estimate until provider billing is compared by hand.
            held = ledger["settled_microusd"] + (live["reserved_microusd"] if live else 0)
            if (
                self.settings.imprint_budget_microusd > 0
                and held + reservation > self.settings.imprint_budget_microusd
            ):
                raise api_error(
                    503, "IMPRINT_BUDGET_EXHAUSTED", "The authorized GPU budget is exhausted."
                )
            if live:
                raise api_error(
                    409, "IMPRINT_SESSION_ACTIVE", "Reconcile the current session first."
                )
            now = clock.now()
            blocked, _ = self.spending_limit(ledger, reservation, now)
            if blocked:
                raise api_error(
                    503,
                    "IMPRINT_BUDGET_BLOCKED",
                    ADMISSION_MESSAGES["spending_limit_reached"],
                    reason="spending_limit_reached",
                )
            session_id = str(uuid.uuid4())
            txn.put(
                {
                    "pk": f"SESSION#{session_id}",
                    "sk": "SESSION",
                    "id": session_id,
                    "provider_job_id": None,
                    "state": "running",
                    "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                    "webhook_hash": hashlib.sha256(webhook.encode()).hexdigest(),
                    "created_at": now,
                    "deadline": now + seconds,
                    "hourly_microusd": hourly_microusd,
                    "reserved_microusd": reservation,
                    "measured_microusd": 0,
                    "finished_at": None,
                    "provider_json": None,
                    "selection_json": json.dumps(selection) if selection else None,
                    "queue_timeout_at": (
                        now + self.settings.imprint_gpu_queue_timeout_seconds if selection else None
                    ),
                    "capacity_stop": False,
                    "submission_started": False,
                    "endpoint_workers_json": None,
                    "error": None,
                    "dispatch_hold": 0,
                    "open_attempt": None,
                    "attempt_count": 0,
                    "succeeded_count": 0,
                    "rejected_count": 0,
                    "stop_requested_at": None,
                    "drained": False,
                    "job_ids": [],
                }
            )
            ledger["live_session"] = session_id
            txn.put(ledger)
            return session_id

        return optimistic(self.table, decide), token, webhook

    def _record_provider_job(self, txn, session, provider_job_id):
        if self.table.get(f"PROVIDER#{provider_job_id}", "SESSION"):
            raise RuntimeError("The provider job belongs to another session")
        session["provider_job_id"] = provider_job_id
        txn.put({"pk": f"PROVIDER#{provider_job_id}", "sk": "SESSION", "id": session["id"]})

    def record_provider_job(self, session_id, provider_job_id):
        def decide(txn):
            session = self.get_session(session_id)
            if session["provider_job_id"] is None:
                self._record_provider_job(txn, session, provider_job_id)
                txn.put(session)

        optimistic(self.table, decide)

    def set_session_error(self, session_id, error):
        def decide(txn):
            session = self.get_session(session_id)
            session["error"] = error
            txn.put(session)

        optimistic(self.table, decide)

    def record_endpoint_workers(self, session_id, workers):
        def decide(txn):
            session = self.get_session(session_id)
            session["endpoint_workers_json"] = json.dumps(workers)
            txn.put(session)

        optimistic(self.table, decide)

    def mark_submission_started(self, session_id):
        def decide(txn):
            session = self.get_session(session_id)
            if session["state"] != "running" or session.get("submission_started") is not False:
                raise RuntimeError("Session cannot submit another provider job")
            session["submission_started"] = True
            txn.put(session)
            if session.get("selection_json"):
                ledger = self.ledger()
                if not ledger.get("capacity_attempts"):
                    ledger["capacity_expires_at"] = max(
                        (entry["expires_at"] for entry in self.table.query("QUEUE")),
                        default=clock.now() + 86400,
                    )
                ledger["capacity_attempts"] = ledger.get("capacity_attempts", 0) + 1
                if ledger["capacity_attempts"] > 3:
                    raise RuntimeError("The GPU allocation retry limit was reached")
                txn.put(ledger)

        optimistic(self.table, decide)

    def stop_unleased_session(self, session_id, *, reason="queue_timeout"):
        """Fence an unusable allocation atomically against the worker's first lease."""
        if reason not in {
            "queue_timeout",
            "unhealthy_worker",
            "worker_gpu_incompatible",
            *MODEL_REJECTION_REASONS,
        }:
            raise ValueError("Unknown capacity stop reason")

        def decide(txn):
            session = self.get_session(session_id)
            if session["state"] != "running" or session["attempt_count"]:
                return False
            session.update(
                state="stopping",
                capacity_stop=True,
                capacity_failure_reason=reason,
                error=f"GPU allocation stopped before leasing: {reason}",
            )
            txn.put(session)
            return True

        return optimistic(self.table, decide)

    def recover_capacity(self, session_id, placements):
        """Recover only our confirmed stopped, unleased allocation, within three submissions.

        The serialized provider controller calls this only after authenticated terminal status
        and an empty worker list. Unknown submissions and inference failures cannot use it.
        """
        # Qualification authorizes specific sessions. A failed gate must retain its hold
        # for review instead of buying an automatic replacement allocation.
        if self.settings.imprint_gpu_qualification_enabled:
            return False

        def decide(txn):
            session = self.get_session(session_id)
            ledger = self.ledger()
            evidence = json.loads(session["provider_json"] or "{}")
            selection = json.loads(session.get("selection_json") or "{}")
            confirmed_stop = evidence.get("status") in {"CANCELLED", "TIMED_OUT"}
            confirmed_rejection = (
                session.get("capacity_failure_reason") == "worker_gpu_incompatible"
                and evidence.get("status") in {"COMPLETED", "FAILED"}
                and evidence.get("worker_rejection") == "worker_gpu_incompatible"
            )
            if (
                session["state"] != "finished"
                or not session.get("capacity_stop")
                or not session["provider_job_id"]
                or session["attempt_count"]
                or not session["dispatch_hold"]
                or not (confirmed_stop or confirmed_rejection)
                or ledger.get("capacity_attempts", 0) >= 3
                or ledger["live_session"] is not None
                or ledger["held"] != [session_id]
                or session.get("capacity_failure_reason") in MODEL_REJECTION_REASONS
                or (
                    selection.get("weight_source") == "native-cache"
                    and session.get("capacity_failure_reason") != "worker_gpu_incompatible"
                )
            ):
                return False
            session["dispatch_hold"] = 0
            session["capacity_recovered"] = True
            self._schedule(txn, session, "session", clock.now() + DIAGNOSTIC_SECONDS)
            txn.put(session)
            ledger["held"] = []
            excluded = ledger.get("gpu_exclusions", [])
            ledger["gpu_exclusions"] = excluded + [p for p in placements if p not in excluded]
            txn.put(ledger)
            for job_id in ledger["retention_held"]:
                job = self.table.get(f"JOB#{job_id}", "JOB")
                if job:
                    self._schedule(txn, job, "job", clock.now())
                    txn.put(job)
            return True

        return optimistic(self.table, decide)

    def finish_session(
        self,
        session_id,
        measured_microusd,
        *,
        error=None,
        provider=None,
        retired_before_submission=False,
    ):
        """Settle once, fence held attempts and pause unsafe recovery without losing the queue."""

        def decide(txn):
            session = self.get_session(session_id)
            if session is None or session["state"] == "finished":
                return
            if retired_before_submission and (
                measured_microusd != 0
                or session.get("submission_started") is not False
                or session["provider_job_id"] is not None
                or session["attempt_count"]
                or session["state"] != "running"
            ):
                raise ValueError("Only a proven unsubmitted retirement may settle without a hold")
            now = clock.now()
            selection = json.loads(session.get("selection_json") or "null")
            evidence = provider_evidence(provider, selection) if provider else {}
            delivered = (
                session["attempt_count"]
                and session["succeeded_count"] + session.get("rejected_count", 0)
                == session["attempt_count"]
            )
            # A session the provider ended by itself, with billed timings, has a known cost and
            # a stopped worker; a failed or cancelled job inside it is that job's own outcome,
            # so it never pauses everyone else's queue. Provider COMPLETED alone is not proof
            # of a working worker: it must have leased, or found the queue empty.
            self_ended = (
                evidence.get("status") in {"COMPLETED", "FAILED"}
                and {"delayTime", "executionTime"} <= evidence.keys()
                and "worker_rejection" not in evidence
                and not session.get("capacity_stop")
                and bool(session["attempt_count"] or session.get("drained"))
            )
            if self.settings.imprint_gpu_qualification_enabled:
                # A qualification session must deliver every job it leased.
                self_ended = self_ended and delivered and not session["open_attempt"]
            clean = self_ended or (
                not error
                and session["state"] != "stopping"
                and session["attempt_count"]
                and session["succeeded_count"] == session["attempt_count"]
            )
            hold = not retired_before_submission and not clean
            message = error
            if hold and message is None:
                message = "Session did not finish cleanly; dispatch requires operator review."
            failed = None
            if session["open_attempt"]:
                attempt = self.table.get(f"ATTEMPT#{session['open_attempt']}", "ATTEMPT")
                if attempt["finished_at"] is None:
                    attempt["finished_at"] = now
                    txn.put(attempt)
                    job = self.job(attempt["job_id"])
                    if job["state"] in RUNNING:
                        self._fail(txn, job, now, SESSION_FAILED, "session_ended")
                        txn.put(job)
                        failed = job["id"]
            # Finished sessions release their jobs' cleanup; wake them for the sweep.
            for job_id in session["job_ids"]:
                if job_id == failed:
                    continue
                job = self.table.get(f"JOB#{job_id}", "JOB")
                if job:
                    self._schedule(txn, job, "job", now)
                    txn.put(job)
            session.update(
                state="finished",
                finished_at=now,
                measured_microusd=measured_microusd,
                error=message,
                provider_json=json.dumps(evidence) if provider else None,
                dispatch_hold=int(hold),
                token_hash="",
                webhook_hash="",
                open_attempt=None,
            )
            self._schedule(txn, session, "session", None if hold else now + DIAGNOSTIC_SECONDS)
            txn.put(session)
            ledger = self.ledger()
            if failed:
                ledger["last_failure_at"] = now
            # A single bounded receipt preserves FlashBoot reuse without trusting the
            # image name or provider model-version hashes to identify cached content.
            selection = selection or {}
            if selection.get("worker_supports_model_release"):
                ledger.pop("verified_worker", None)
                if (
                    not hold
                    and evidence.get("status") == "COMPLETED"
                    and evidence.get("worker_id")
                    and evidence.get("boot", {}).get("model_release") == selection["model_release"]
                ):
                    ledger["verified_worker"] = {
                        "worker_id": evidence["worker_id"],
                        "image": selection["worker_image"],
                        "weight_source": selection["weight_source"],
                        "model_release": selection["model_release"],
                    }
            if ledger["live_session"] == session_id:
                ledger["live_session"] = None
            ledger["settled_microusd"] += measured_microusd
            self._record_spend(txn, ledger, session)
            if not hold and session["attempt_count"]:
                ledger.update(capacity_attempts=0, gpu_exclusions=[])
                ledger.pop("capacity_expires_at", None)
            if hold:
                ledger["held"] = [*ledger["held"], session_id]
            txn.put(ledger)

        optimistic(self.table, decide)

    def release_dispatch_hold(self, session_id, *, financial_authority=False):
        """An operator acknowledges one settled session; dispatch still checks its authority."""
        if not financial_authority:
            raise api_error(
                503, "IMPRINT_BUDGET_BLOCKED", "Explicit recovery authorization is required."
            )

        def decide(txn):
            session = self.get_session(session_id)
            if session is None or session["state"] != "finished":
                raise api_error(
                    409, "IMPRINT_SESSION_ACTIVE", "Review a finished session before resuming."
                )
            if not session["dispatch_hold"]:
                return
            now = clock.now()
            session["dispatch_hold"] = 0
            self._schedule(
                txn, session, "session", max(now, session["finished_at"] + DIAGNOSTIC_SECONDS)
            )
            txn.put(session)
            ledger = self.ledger()
            ledger["held"] = [held for held in ledger["held"] if held != session_id]
            ledger.update(capacity_attempts=0, gpu_exclusions=[])
            ledger.pop("capacity_expires_at", None)
            woken = list(session["job_ids"])
            if not ledger["held"]:
                woken += ledger["retention_held"]
            txn.put(ledger)
            for job_id in dict.fromkeys(woken):
                job = self.table.get(f"JOB#{job_id}", "JOB")
                if job:
                    self._schedule(txn, job, "job", now)
                    txn.put(job)

        optimistic(self.table, decide)

    # Worker operations

    def session(self, session_id, token):
        row = self.get_session(session_id)
        if row is None or not secrets.compare_digest(
            row["token_hash"], hashlib.sha256(token.encode()).hexdigest()
        ):
            raise api_error(401, "IMPRINT_WORKER_AUTH", "Invalid worker authorization.")
        if row["state"] != "running" or row["deadline"] <= clock.now():
            raise api_error(409, "IMPRINT_SESSION_ENDED", "The GPU session has ended.")
        return row

    def webhook_session(self, session_id, token):
        row = self.get_session(session_id)
        if row is None or not secrets.compare_digest(
            row.get("webhook_hash", ""), hashlib.sha256(token.encode()).hexdigest()
        ):
            return None
        return row

    def lease(
        self,
        session_id,
        token,
        provider_job_id=None,
        *,
        executor_id,
        weight_source=None,
        model_release=None,
    ):
        def decide(txn):
            now = clock.now()
            session = self.session(session_id, token)
            selection = json.loads(session.get("selection_json") or "{}")
            if selection.get("worker_supports_model_release") and (
                not valid_release(model_release, weight_source)
                or weight_source != selection.get("weight_source")
                or model_release != selection.get("model_release")
            ):
                # An old worker can ignore new provider input. Fence it at the durable
                # lease boundary too, including replay of an already open attempt.
                raise api_error(
                    409,
                    "IMPRINT_MODEL_RELEASE_MISMATCH",
                    "The worker model release is incompatible.",
                )
            dirty = False
            if provider_job_id and not session["provider_job_id"]:
                self._record_provider_job(txn, session, provider_job_id)
                dirty = True
            if session["open_attempt"]:
                attempt = self.table.get(f"ATTEMPT#{session['open_attempt']}", "ATTEMPT")
                if attempt["executor_id"] != executor_id:
                    raise api_error(
                        409,
                        "IMPRINT_ATTEMPT_OWNED",
                        "An unfinished attempt belongs to another worker invocation.",
                    )
                job = self.job(attempt["job_id"])
                if dirty:
                    txn.put(session)
                else:
                    txn.check(session["pk"], session["sk"], session["v"])
                return attempt_view(attempt) | {
                    "request_json": job["request_json"],
                    "state": job["state"],
                }
            # Never start an attempt without its entire 15-minute allowance remaining.
            job = None
            time_left = session["deadline"] - now >= 900
            if time_left:
                for entry in self.table.query("QUEUE"):
                    if entry["expires_at"] > now:
                        job = self.job(entry["job_id"])
                        break
            if job is not None and job["state"] != "queued":
                # The job left the queue after the index was read.
                raise Conflict
            if job is None:
                if time_left and not session.get("drained"):
                    # Proof that the worker could lease: an empty session found nothing to
                    # do, for example after its only job was cancelled before starting.
                    session["drained"] = True
                    dirty = True
                if dirty:
                    txn.put(session)
                return None
            attempt_id = str(uuid.uuid4())
            txn.put(
                {
                    "pk": f"ATTEMPT#{attempt_id}",
                    "sk": "ATTEMPT",
                    "id": attempt_id,
                    "job_id": job["id"],
                    "session_id": session_id,
                    "lease_expires": now + 120,
                    "started_at": now,
                    "finished_at": None,
                    "timings_json": None,
                    "succeeded": 0,
                    "executor_id": executor_id,
                    "result_tickets": {},
                }
            )
            session["open_attempt"] = attempt_id
            session["attempt_count"] += 1
            session["job_ids"] = [*session["job_ids"], job["id"]]
            txn.put(session)
            txn.delete("QUEUE", f"{stamp(job['created_at'])}#{job['id']}")
            job.update(state="preparing", updated_at=now, attempt_id=attempt_id)
            self._schedule(txn, job, "job", None)
            txn.put(job)
            return {
                "id": attempt_id,
                "job_id": job["id"],
                "request_json": job["request_json"],
                "state": "preparing",
            }

        return optimistic(self.table, decide)

    def live_attempt(self, session_id, attempt_id, now=None):
        now = clock.now() if now is None else now
        try:
            uuid.UUID(attempt_id)
            attempt = self.table.get(f"ATTEMPT#{attempt_id}", "ATTEMPT")
        except ValueError:
            attempt = None
        job = self.table.get(f"JOB#{attempt['job_id']}", "JOB") if attempt else None
        if (
            attempt is None
            or attempt["session_id"] != session_id
            or attempt["finished_at"] is not None
            or attempt["lease_expires"] <= now
            or job is None
            or job["state"] not in RUNNING
            or now - attempt["started_at"] > 900
        ):
            raise api_error(409, "IMPRINT_LEASE_LOST", "This attempt can no longer publish.")
        return attempt, job

    def progress(
        self, session_id, attempt_id, stage, timings=None, failure_code=None, failure_reason=None
    ):
        if stage not in {"preparing", "running", "finishing", "failed"}:
            raise ValueError("Unknown worker stage")
        if failure_code not in {None, "worker_attempt_failed", "input_rejected"}:
            raise ValueError("Unknown worker failure code")
        if (failure_code == "input_rejected") != (failure_reason in INPUT_REJECTION_MESSAGES):
            raise ValueError("An input rejection needs exactly one known reason")

        def decide(txn):
            now = clock.now()
            attempt, job = self.live_attempt(session_id, attempt_id, now)
            order = {"preparing": 0, "running": 1, "finishing": 2, "failed": 3}
            if order[stage] < order[job["state"]]:
                raise api_error(409, "IMPRINT_STALE_PROGRESS", "Worker progress is stale.")
            failed = stage == "failed"
            attempt.update(
                lease_expires=now + 120,
                timings_json=json.dumps(timing_evidence(timings)),
                finished_at=now if failed else None,
            )
            txn.put(attempt)
            if failed and failure_code == "input_rejected":
                # The job's own outcome: the worker keeps serving the queue.
                self._fail(
                    txn,
                    job,
                    now,
                    INPUT_REJECTION_MESSAGES[failure_reason],
                    failure_code,
                    failure_reason,
                )
                session = self.get_session(session_id)
                session["open_attempt"] = None
                session["rejected_count"] = session.get("rejected_count", 0) + 1
                txn.put(session)
            elif failed:
                self._fail(
                    txn,
                    job,
                    now,
                    "Inference failed; inspect the retained worker diagnostics.",
                    "worker_attempt_failed",
                )
                session = self.get_session(session_id)
                session["state"] = "stopping"
                session["stop_requested_at"] = session.get("stop_requested_at") or now
                session["open_attempt"] = None
                txn.put(session)
                ledger = self.ledger()
                ledger["last_failure_at"] = now
                txn.put(ledger)
            elif stage != job["state"]:
                # Heartbeats repeat the stage; `updated_at` marks when the stage was entered.
                job.update(state=stage, updated_at=now)
            txn.put(job)
            return failed

        return optimistic(self.table, decide)

    def result_ticket(self, session_id, attempt_id, kind, digest, size):
        def decide(txn):
            now = clock.now()
            attempt, job = self.live_attempt(session_id, attempt_id, now)
            tickets = attempt["result_tickets"]
            # One content per result: a fenced worker can only ever rewrite identical bytes.
            if tickets.get(kind, digest) != digest:
                raise api_error(409, "IMPRINT_RESULT_CHANGED", "This result was already declared.")
            if kind not in tickets:
                attempt["result_tickets"] = tickets | {kind: digest}
                txn.put(attempt)
            until = now + UPLOAD_SECONDS
            job["url_until"] = max(job["url_until"], until)
            txn.put(job)
            return job["id"], until

        job_id, until = optimistic(self.table, decide)
        url, headers = self.media.presign_put(media_key(job_id, kind), digest, size, UPLOAD_SECONDS)
        return {"method": "PUT", "url": url, "headers": headers, "expires_at": until}

    def complete(self, session_id, attempt_id, manifest):
        if self.settings.moderation_enabled:
            prior = self.table.get(f"ATTEMPT#{attempt_id}", "ATTEMPT")
            prior_job = self.job(prior["job_id"]) if prior else None
            if (
                prior_job
                and prior_job["state"] == "succeeded"
                and prior["session_id"] == session_id
                and json.loads(prior_job["result_json"]) == manifest
            ):
                return
            attempt, job = self.live_attempt(session_id, attempt_id)
            if job["state"] != "finishing" or set(manifest) != set(RESULT_NAMES):
                raise api_error(409, "IMPRINT_NOT_FINISHING", "Finish encoding before publication.")
            for name, metadata in manifest.items():
                if attempt["result_tickets"].get(name) != metadata["sha256"] or self.media.head(
                    media_key(job["id"], name)
                ) != (metadata["bytes"], metadata["sha256"]):
                    raise api_error(409, "IMPRINT_RESULT_HASH", "Result integrity check failed.")

            def check_output():
                try:
                    data = self.media.get(
                        media_key(job["id"], "result.mp4"), limit=RESULT_LIMITS["result.mp4"]
                    )
                    if (
                        data is None
                        or hashlib.sha256(data).hexdigest() != manifest["result.mp4"]["sha256"]
                    ):
                        raise ValueError("Result changed")
                except ValueError:
                    raise moderation.unavailable() from None
                duration = (
                    math.ceil(json.loads(job["request_json"])["duration_ms"] * 16 / 1000)
                    * 1000
                    / 16
                )
                images = moderation.output_samples(data, duration)
                moderation.check(self.settings, images)

            self._moderate(job["id"], "output", check_output)
            if self.job(job["id"])["state"] == "failed":
                return

        def decide(txn):
            try:
                uuid.UUID(attempt_id)
                prior = self.table.get(f"ATTEMPT#{attempt_id}", "ATTEMPT")
            except ValueError:
                prior = None
            if prior and prior["session_id"] == session_id:
                job = self.table.get(f"JOB#{prior['job_id']}", "JOB")
                if (
                    job
                    and job["state"] == "succeeded"
                    and json.loads(job["result_json"]) == manifest
                ):
                    return
            now = clock.now()
            attempt, job = self.live_attempt(session_id, attempt_id, now)
            if job["state"] != "finishing":
                raise api_error(409, "IMPRINT_NOT_FINISHING", "Finish encoding before publication.")
            if self.settings.moderation_enabled and job.get("moderation_output") != "accepted":
                raise moderation.unavailable()
            if set(manifest) != set(RESULT_NAMES):
                raise api_error(
                    422, "IMPRINT_RESULT_INVALID", "All three result files are required."
                )
            for name, metadata in manifest.items():
                stored = self.media.head(media_key(job["id"], name))
                if stored is None or stored[0] != metadata["bytes"]:
                    raise api_error(
                        409, "IMPRINT_RESULT_MISSING", "Upload the complete result first."
                    )
                if stored[1] != metadata["sha256"]:
                    raise api_error(409, "IMPRINT_RESULT_HASH", "Result integrity check failed.")
            attempt.update(finished_at=now, succeeded=1)
            txn.put(attempt)
            session = self.get_session(session_id)
            session["open_attempt"] = None
            session["succeeded_count"] += 1
            txn.put(session)
            self._end_activity(txn, job)
            job.update(
                state="succeeded",
                updated_at=now,
                expires_at=now + MEDIA_SECONDS,
                terminal_at=now,
                outcome_kind="ready",
                result_json=json.dumps(manifest),
            )
            self._schedule(txn, job, "job", now)
            txn.put(job)

        optimistic(self.table, decide)

    def put_diagnostics(self, job_id, data):
        self.media.put(media_key(job_id, "diagnostics.zip"), data, hashlib.sha256(data).hexdigest())

    def get_diagnostics(self, job_id):
        return self.media.get(media_key(job_id, "diagnostics.zip"))

    # Housekeeping: the sweep reads due entries instead of scanning every record.

    def reconcile(self, *, time_left=None):
        now = clock.now()
        self._expire_live_lease(now)
        for entry in self.table.query("DUE", before=stamp(now) + "#~", limit=200):
            if time_left is not None and time_left() < 15:
                return
            if entry["kind"] == "erase":
                self._reconcile_erasure(entry["id"], entry["sk"], now)
            elif entry["kind"] == "job":
                self._reconcile_job(entry["id"], entry["sk"], now)
            else:
                self._reconcile_session(entry["id"], entry["sk"], now)

    def _expire_live_lease(self, now):
        def decide(txn):
            ledger = self.ledger()
            if not ledger["live_session"]:
                return
            session = self.get_session(ledger["live_session"])
            if not session["open_attempt"]:
                return
            attempt = self.table.get(f"ATTEMPT#{session['open_attempt']}", "ATTEMPT")
            if attempt["finished_at"] is not None or (
                attempt["lease_expires"] > now and attempt["started_at"] > now - 900
            ):
                return
            # Expiry fails an attempt; it never queues another billable execution.
            job = self.job(attempt["job_id"])
            if job["state"] in RUNNING:
                self._fail(
                    txn, job, now, "Worker lease expired; no automatic retry.", "lease_expired"
                )
                txn.put(job)
            if job["state"] == "failed":
                if session["state"] != "finished":
                    session["state"] = "stopping"
                    session["stop_requested_at"] = session.get("stop_requested_at") or now
                session["open_attempt"] = None
                txn.put(session)
                attempt["finished_at"] = now
                txn.put(attempt)
                ledger["last_failure_at"] = now
                txn.put(ledger)

        optimistic(self.table, decide)

    def _blocked(self, job):
        if job["retention_hold"]:
            return True
        if not job["attempt_id"]:
            return False
        attempt = self.table.get(f"ATTEMPT#{job['attempt_id']}", "ATTEMPT")
        if attempt is None:
            return False
        session = self.get_session(attempt["session_id"])
        return bool(
            attempt["finished_at"] is None
            or session["state"] != "finished"
            # Erasure keeps the financial hold but does not retain private media after
            # the finished session's tokens are fenced and all upload links have lapsed.
            or (session["dispatch_hold"] and not job.get("erased"))
        )

    def _reconcile_job(self, job_id, due_sk, now):
        def transition(txn):
            job = self.table.get(f"JOB#{job_id}", "JOB")
            if job is None:
                txn.delete("DUE", due_sk)
                return None
            if job.get("due_sk") != due_sk:
                txn.delete("DUE", due_sk)
            ledger = self.ledger()
            changed = ledger_changed = False
            moderation_stage = "output" if job["state"] == "finishing" else "input"
            moderation_attempts = job.get(f"moderation_{moderation_stage}_attempts", 0)
            if (
                job["state"] in ("uploading", "finishing")
                and (
                    moderation_attempts >= moderation.MAX_ATTEMPTS
                    or (moderation_attempts > 0 and job["expires_at"] <= now)
                )
                and job.get("moderation_until", 0) <= now
                and job.get(f"moderation_{moderation_stage}") != "accepted"
            ):
                self._moderation_fail(txn, job, "moderation_unavailable")
                changed = True
            if job["state"] in ("uploading", "queued") and job["expires_at"] <= now:
                # A held queue still expires at its original deadline. Its evidence stays until
                # the operator resolves dispatch; release never revives or extends a job.
                hold = job["state"] == "queued" and bool(ledger["held"])
                self._end_activity(txn, job)
                expired = job["expires_at"]
                job.update(
                    retention_hold=int(hold),
                    state="expired",
                    updated_at=expired,
                    terminal_at=expired,
                    outcome_kind="expired",
                    expires_at=expired + MEDIA_SECONDS,
                )
                if hold:
                    ledger["retention_held"] = [*ledger["retention_held"], job_id]
                    ledger_changed = True
                changed = True
            if job["state"] == "succeeded" and job["expires_at"] <= now:
                job["state"] = "expired"
                changed = True
            if job["retention_hold"] and not ledger["held"]:
                job["retention_hold"] = 0
                ledger["retention_held"] = [j for j in ledger["retention_held"] if j != job_id]
                ledger_changed = changed = True
            if ledger_changed:
                txn.put(ledger)
            if changed:
                txn.put(job)
            return job

        job = optimistic(self.table, transition)
        if job is None:
            return
        if job["terminal_at"] is not None and not self._blocked(job):
            self._clean_job(job_id, now)
        self._reschedule_job(job_id, now)

    def _clean_job(self, job_id, now):
        """Media deletion precedes the record change, so an interrupted pass retries safely."""
        job = self.job(job_id)
        upload_closed = job["url_until"] <= now
        if not job["media_cleared"] and job["expires_at"] <= now and upload_closed:
            self.media.delete(media_key(job_id, kind) for kind in MEDIA_KINDS[:-1])

            def media(txn):
                current = self.job(job_id)
                current.update(media_cleared=True, uploads={}, result_json=None)
                txn.put(current)

            optimistic(self.table, media)
            job = self.job(job_id)
        if not job["diagnostics_cleared"] and (
            job["state"] == "cancelled" or job["terminal_at"] + DIAGNOSTIC_SECONDS <= now
        ):
            self.media.delete([media_key(job_id, "diagnostics.zip")])

            def diagnostics(txn):
                current = self.job(job_id)
                current["diagnostics_cleared"] = True
                txn.put(current)
                if current["attempt_id"]:
                    attempt = self.table.get(f"ATTEMPT#{current['attempt_id']}", "ATTEMPT")
                    if attempt:
                        attempt["timings_json"] = None
                        txn.put(attempt)

            optimistic(self.table, diagnostics)
            job = self.job(job_id)
        if (job.get("erased") or job["terminal_at"] + RECORD_SECONDS <= now) and upload_closed:
            prefix = f"jobs/{uuid.UUID(job_id)}/"
            self.media.delete(self.media.keys(prefix))
            if any(True for _ in self.media.keys(prefix)):
                raise OSError("Media remains under a pruned job")
            self._prune(job_id)

    def _prune(self, job_id):
        def decide(txn):
            job = self.table.get(f"JOB#{job_id}", "JOB")
            if job is None:
                return
            txn.delete(job["pk"], job["sk"], job["v"])
            if job.get("due_sk"):
                txn.delete("DUE", job["due_sk"])
            if job.get("list_sk"):
                txn.delete(f"CLIENT#{job['client_id']}", job["list_sk"])
            replay = self.table.get(
                f"REPLAY#{replay_digest(job['client_id'], job['idempotency_key'])}", "REPLAY"
            )
            if replay and replay.get("job_id") == job_id:
                # The permanent ledger keeps only the digest and the admission time.
                replay.pop("job_id")
                txn.put(replay)
            if job["attempt_id"]:
                attempt = self.table.get(f"ATTEMPT#{job['attempt_id']}", "ATTEMPT")
                if attempt:
                    txn.delete(attempt["pk"], attempt["sk"], attempt["v"])
                    session = self.get_session(attempt["session_id"])
                    if session and job_id in session["job_ids"]:
                        session["job_ids"] = [j for j in session["job_ids"] if j != job_id]
                        txn.put(session)

        optimistic(self.table, decide)

    def _reschedule_job(self, job_id, now):
        def decide(txn):
            job = self.table.get(f"JOB#{job_id}", "JOB")
            if job is None:
                return
            times = []
            if job["state"] in ("uploading", "finishing") and job.get("moderation_until", 0) > now:
                times.append(job["moderation_until"])
            if job["state"] in ("uploading", "queued", "succeeded"):
                times.append(job["expires_at"])
            if job["terminal_at"] is not None and not self._blocked(job):
                if not job["media_cleared"]:
                    times.append(max(job["expires_at"], job["url_until"]))
                if not job["diagnostics_cleared"]:
                    times.append(
                        job["terminal_at"]
                        if job["state"] == "cancelled"
                        else job["terminal_at"] + DIAGNOSTIC_SECONDS
                    )
                times.append(max(job["terminal_at"] + RECORD_SECONDS, job["url_until"]))
            due = min(times) if times else None
            if due is not None and due <= now:
                # Retry an unfinished step on the next sweep rather than in a tight loop.
                due = now + 60
            before = job.get("due_sk")
            self._schedule(txn, job, "job", due)
            if job.get("due_sk") != before:
                txn.put(job)

        optimistic(self.table, decide)

    def _reconcile_session(self, session_id, due_sk, now):
        def decide(txn):
            session = self.get_session(session_id)
            if session is None or session.get("due_sk") != due_sk:
                txn.delete("DUE", due_sk)
                return
            if session["state"] == "finished" and not session["dispatch_hold"]:
                if session["finished_at"] + DIAGNOSTIC_SECONDS <= now:
                    session.update(
                        provider_json=None,
                        selection_json=None,
                        endpoint_workers_json=None,
                        error=None,
                        token_hash="",
                        webhook_hash="",
                    )
                    self._schedule(txn, session, "session", None)
                else:
                    self._schedule(
                        txn, session, "session", session["finished_at"] + DIAGNOSTIC_SECONDS
                    )
            else:
                self._schedule(txn, session, "session", None)
            txn.put(session)

        optimistic(self.table, decide)

    # Inspection

    def queued_workload(self):
        """Bounded, owner-free economic snapshot; leasing still checks every live invariant."""
        now = clock.now()
        jobs = []
        for entry in self.table.query("QUEUE", limit=16):
            if entry["expires_at"] <= now:
                continue
            job = self.job(entry["job_id"])
            if job["state"] != "queued" or job["expires_at"] <= now:
                continue
            request = json.loads(job["request_json"])
            jobs.append(
                {
                    "tier": request["tier"],
                    "duration_ms": request["duration_ms"],
                    "model_pixels": job.get("input_metadata", {}).get("model_pixels", 512 * 896),
                }
            )
        return jobs

    def queued_exists(self):
        now = clock.now()
        return any(entry["expires_at"] > now for entry in self.table.query("QUEUE"))


def attempt_view(attempt):
    return {
        key: attempt[key]
        for key in (
            "id",
            "job_id",
            "session_id",
            "lease_expires",
            "started_at",
            "finished_at",
            "timings_json",
            "succeeded",
            "executor_id",
        )
    }


def outcome_view(job):
    """The immutable terminal outcome, derived for records written before it was stored."""
    if job["terminal_at"] is None:
        return None
    kind = job.get("outcome_kind")
    if kind is None:
        if job["state"] == "succeeded" or (job["state"] == "expired" and job["attempt_id"]):
            kind = "ready"
        elif job["state"] == "expired":
            kind = "expired"
        else:
            kind = job["state"]
    return {
        "kind": kind,
        "at": job["terminal_at"],
        "attempt_started": job["attempt_id"] is not None,
        "failure_code": job.get("failure_code"),
        "failure_reason": job.get("failure_reason"),
    }
