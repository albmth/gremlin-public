"""One controller pass: the sweep, a doorbell and the operator actions all start here."""

import json
import logging

from gremlin_control import clock
from gremlin_control.provider import RunPodEndpoint
from gremlin_control.store import ImprintStore

logger = logging.getLogger(__name__)


def run(settings, *, allow_dispatch=True, time_left=None):
    failed = False
    try:
        ImprintStore(settings).reconcile(time_left=time_left)
    except Exception:
        # A failed deletion stays due for the next pass; settle live work but buy nothing new
        # while the state and cleanup pass is incomplete.
        logger.error("gremlin.retention_cleanup_unconfirmed")
        failed = True
        allow_dispatch = False
    if time_left is not None and time_left() < 90:
        # Discovery and submission share a 75 s request allowance, with at most one final
        # bounded request in flight. Leave room to persist an ambiguous provider answer.
        allow_dispatch = False
    # Closing admission must not strand cancellation, settlement or a billed worker.
    if settings.imprint_runpod_key and (
        settings.controller_enabled or ImprintStore(settings).ledger()["live_session"]
    ):
        try:
            endpoint = RunPodEndpoint(settings)
            if allow_dispatch and settings.controller_enabled:
                endpoint.tick()
            else:
                endpoint.reconcile()
        except Exception:
            # Keep settling live sessions on later passes; never log provider secrets.
            logger.error("gremlin.provider_reconciliation_unconfirmed")
            failed = True
    if settings.monitoring_enabled:
        report_health(settings, failed=failed)


def report_health(settings, *, failed=False):
    """One low-cardinality heartbeat; missing samples also alert if the sweep crashes."""
    service = ImprintStore(settings)
    ledger = service.ledger()
    live = service.get_session(ledger["live_session"]) if ledger["live_session"] else None
    held = ledger["settled_microusd"] + (live["reserved_microusd"] if live else 0)
    oldest = service.table.query("QUEUE", limit=1)
    age = 0
    if oldest:
        job = service.job(oldest[0]["job_id"])
        age = max(0, int(clock.now() - job["updated_at"]))
    reasons = []
    if failed:
        reasons.append("reconciliation_failed")
    if ledger["held"] or ledger["retention_held"]:
        reasons.append("operator_hold")
    # A service-side job failure no longer holds the queue, so it raises the alarm instead.
    if clock.now() - ledger.get("last_failure_at", 0) < 900:
        reasons.append("recent_failure")
    if age > 900:
        reasons.append("queue_stalled")
    # Zero is the deliberately closed state, not a budget overrun of imported history.
    if settings.imprint_budget_microusd > 0 and held >= settings.imprint_budget_microusd:
        reasons.append("spending_limit_reached")
    # Keep the EMF envelope at the JSON root, outside Lambda's logging.message wrapper.
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(clock.now() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "Gremlin/Operations",
                            "Dimensions": [["Environment"]],
                            "Metrics": [{"Name": "Healthy", "Unit": "Count"}],
                        }
                    ],
                },
                "Environment": settings.environment,
                "Healthy": int(not reasons),
                "reasons": reasons,
                "oldest_queued_seconds": age,
                "budget_held_microusd": held,
                "budget_microusd": settings.imprint_budget_microusd,
            }
        ),
        flush=True,
    )


def state(settings):
    """A read-only report without tokens, prompts, keys or media."""
    service = ImprintStore(settings)
    jobs, sessions = {}, []
    replays = 0
    for item in service.table.scan():
        kind = item["pk"].split("#", 1)[0]
        if kind == "JOB":
            jobs[item["state"]] = jobs.get(item["state"], 0) + 1
        elif kind == "REPLAY":
            replays += 1
        elif kind == "SESSION" and item["sk"] == "SESSION":
            sessions.append(
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "state",
                        "provider_job_id",
                        "created_at",
                        "deadline",
                        "finished_at",
                        "hourly_microusd",
                        "reserved_microusd",
                        "measured_microusd",
                        "dispatch_hold",
                        "error",
                        "attempt_count",
                        "succeeded_count",
                        "rejected_count",
                        "drained",
                    )
                }
                | {
                    "provider": json.loads(item["provider_json"] or "null"),
                    "gpu_selection": json.loads(item.get("selection_json") or "null"),
                    "queue_timeout_at": item.get("queue_timeout_at"),
                    "capacity_stop": item.get("capacity_stop", False),
                    "capacity_recovered": item.get("capacity_recovered", False),
                    "capacity_failure_reason": item.get("capacity_failure_reason"),
                    "endpoint_workers": json.loads(item.get("endpoint_workers_json") or "null"),
                }
            )
    ledger = service.ledger()
    live = next((s for s in sessions if s["id"] == ledger["live_session"]), None)
    return {
        "jobs_by_state": jobs,
        "replay_ledger_entries": replays,
        "sessions": sorted(sessions, key=lambda s: s["created_at"]),
        "live_session": ledger["live_session"],
        "dispatch_holds": ledger["held"],
        "retention_held_jobs": len(ledger["retention_held"]),
        "settled_microusd": ledger["settled_microusd"],
        "budget_held_microusd": ledger["settled_microusd"]
        + (live["reserved_microusd"] if live else 0),
        "controller_enabled": settings.controller_enabled,
        "hourly_microusd": settings.imprint_hourly_microusd,
        "budget_microusd": settings.imprint_budget_microusd,
        "rolling_budget_microusd": settings.imprint_rolling_budget_microusd,
        "monthly_budget_microusd": settings.imprint_monthly_budget_microusd,
        "available_budget_microusd": service.available_budget(ledger),
        "spending_initialized": ledger.get("spending_initialized", False),
        "spend_month": ledger.get("spend_month"),
        "spend_month_microusd": ledger.get("spend_month_microusd", 0),
        "daily_jobs": settings.imprint_daily_jobs,
        "gpu_selection_enabled": settings.imprint_gpu_selection_enabled,
        "gpu_qualification_enabled": settings.imprint_gpu_qualification_enabled,
        "gpu_qualification_target": settings.imprint_gpu_qualification_target,
        "gpu_decision": service.gpu_decision(),
        "gpu_capacity_attempts": ledger.get("capacity_attempts", 0),
        "gpu_capacity_expires_at": ledger.get("capacity_expires_at"),
        "gpu_exclusions": ledger.get("gpu_exclusions", []),
        "verified_worker": ledger.get("verified_worker"),
        "last_failure_at": ledger.get("last_failure_at"),
    }


def operate(settings, event):
    action = event.get("gremlin_operator")
    if action == "initialize_spending":
        return ImprintStore(settings).initialize_spending()
    if action == "state":
        return state(settings)
    if action == "gpu_plan":
        return RunPodEndpoint(settings).discover(
            allow_trial=event.get("allow_trial") is True, record=False
        )
    if action == "close_capacity":
        return RunPodEndpoint(settings).close()
    if action == "release_dispatch_hold":
        if event.get("authorize_recovery") is not True:
            raise ValueError("Pass authorize_recovery: true after reviewing the session")
        service = ImprintStore(settings)
        owner = service.acquire_provider_lock()
        if owner is None:
            raise ValueError("The provider controller is active; retry the reviewed release")
        try:
            service.release_dispatch_hold(str(event["session_id"]), financial_authority=True)
        finally:
            service.release_provider_lock(owner)
        return {"released": event["session_id"]}
    raise ValueError("Unknown operator action")
