"""Contract-suite hooks, mounted only when GREMLIN_TEST_HOOKS is set in the test environment.

The same routes wrap the SQLite reference service (`contract/targets/legacy.py`), so one
black-box suite judges both implementations.
"""

import base64
import json

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import SecretStr

from gremlin_control import clock, controller, doorbell
from gremlin_control.aws import Media, optimistic
from gremlin_control.provider import RunPodEndpoint
from gremlin_control.store import MEDIA_KINDS, ImprintStore

router = APIRouter(prefix="/__test", include_in_schema=False)


def outcome(call):
    try:
        call()
    except HTTPException as error:
        return {
            "raised": "HTTPException",
            "status": error.status_code,
            "code": error.detail["code"],
        }
    except Exception as error:
        return {"raised": type(error).__name__}
    return {"raised": None}


def dispatching(request, changes=None):
    # Only this copy dispatches, as the old in-process tests did; the served settings keep the
    # operator's controller flag.
    update = {"controller_enabled": True} | (changes or {})
    if update.get("imprint_runpod_key"):
        update["imprint_runpod_key"] = SecretStr(update["imprint_runpod_key"])
    return RunPodEndpoint(request.app.state.settings.model_copy(update=update))


@router.post("/clock")
async def set_clock(request: Request):
    body = await request.json()
    if "frozen_at" in body:
        clock.freeze(body["frozen_at"])
    else:
        clock.set_offset(body["offset_seconds"])
    return Response(status_code=204)


@router.post("/settings")
async def settings(request: Request):
    current = request.app.state.settings
    for key, value in (await request.json()).items():
        if key == "imprint_runpod_key" and value is not None:
            value = SecretStr(value)
        setattr(current, key, value)
    return Response(status_code=204)


@router.post("/reconcile")
def reconcile(request: Request):
    return outcome(ImprintStore(request.app.state.settings).reconcile)


@router.post("/tick")
def tick(request: Request):
    return outcome(dispatching(request).tick)


@router.post("/dispatch")
async def dispatch(request: Request):
    changes = await request.json() if await request.body() else {}
    endpoint = dispatching(request, changes)
    return await run_in_threadpool(outcome, endpoint.dispatch)


@router.post("/seed-job")
async def seed_job(request: Request):
    """Set a job's clocks directly, for states only legacy data or a race produces."""
    service = ImprintStore(request.app.state.settings)
    body = await request.json()

    def decide(txn):
        job = service.job(body["job_id"])
        job.update({key: body[key] for key in ("expires_at", "terminal_at") if key in body})
        service._schedule(txn, job, "job", clock.now())
        txn.put(job)

    await run_in_threadpool(optimistic, service.table, decide)
    return Response(status_code=204)


@router.post("/put-media")
async def put_media(request: Request):
    """Store raw bytes as if an older release had written them."""
    body = await request.json()
    data = base64.b64decode(body["data"])
    service = ImprintStore(request.app.state.settings)
    await run_in_threadpool(
        service.media.s3.put_object,
        Bucket=service.media.bucket,
        Key=f"jobs/{body['job_id']}/{body['kind']}",
        Body=data,
    )
    return Response(status_code=204)


@router.post("/fail-next-delete")
async def fail_next_delete(request: Request):
    """Make the next deletion that includes this media name fail once."""
    name = (await request.json())["name"]
    original = Media.delete

    def failing(self, keys):
        keys = list(keys)
        if any(key.endswith("/" + name) for key in keys):
            Media.delete = original
            raise OSError("private filesystem error")
        return original(self, keys)

    Media.delete = failing
    return Response(status_code=204)


@router.post("/sweep")
def sweep(request: Request):
    return outcome(lambda: controller.run(request.app.state.settings))


@router.post("/reserve")
async def reserve(request: Request):
    body = await request.json()
    session_id, token, _ = ImprintStore(request.app.state.settings).reserve_session(
        body["hourly_microusd"], financial_authority=True
    )
    return {"session_id": session_id, "token": token}


@router.post("/finish-session")
async def finish_session(request: Request):
    body = await request.json()
    ImprintStore(request.app.state.settings).finish_session(
        body["session_id"],
        body["measured_microusd"],
        error=body.get("error"),
        provider=body.get("provider"),
    )
    return Response(status_code=204)


@router.post("/release")
async def release(request: Request):
    body = await request.json()
    ImprintStore(request.app.state.settings).release_dispatch_hold(
        body["session_id"], financial_authority=body.get("authorize", False)
    )
    return Response(status_code=204)


@router.get("/records")
def records(request: Request):
    service = ImprintStore(request.app.state.settings)
    jobs, sessions, attempts, replays = [], [], [], []
    for item in service.table.scan():
        kind = item["pk"].split("#", 1)[0]
        if kind == "JOB":
            jobs.append(
                {
                    key: item[key]
                    for key in (
                        "id",
                        "client_id",
                        "state",
                        "error",
                        "created_at",
                        "updated_at",
                        "expires_at",
                        "terminal_at",
                        "retention_hold",
                        "uploads",
                    )
                }
                | {"result": json.loads(item["result_json"] or "null")}
            )
        elif kind == "SESSION" and item["sk"] == "SESSION":
            sessions.append(
                {
                    key: item[key]
                    for key in (
                        "id",
                        "state",
                        "provider_job_id",
                        "created_at",
                        "deadline",
                        "hourly_microusd",
                        "reserved_microusd",
                        "measured_microusd",
                        "finished_at",
                        "dispatch_hold",
                        "error",
                    )
                }
                | {
                    "provider": json.loads(item["provider_json"] or "null"),
                    "token_cleared": item["token_hash"] == "",
                }
            )
        elif kind == "ATTEMPT":
            attempts.append(
                {
                    key: item[key]
                    for key in (
                        "id",
                        "job_id",
                        "session_id",
                        "lease_expires",
                        "started_at",
                        "finished_at",
                        "succeeded",
                        "executor_id",
                    )
                }
                | {"timings": json.loads(item["timings_json"] or "null")}
            )
        elif kind == "REPLAY":
            replays.append(
                {"digest": item["pk"].removeprefix("REPLAY#")}
                | {k: v for k, v in item.items() if k not in {"pk", "sk", "v"}}
            )
    media = {}
    for key in service.media.keys("jobs/"):
        _, job_id, name = key.split("/", 2)
        if name in MEDIA_KINDS:
            media.setdefault(job_id, []).append(name)
    ledger = service.ledger()
    live = service.get_session(ledger["live_session"]) if ledger["live_session"] else None
    return {
        "jobs": sorted(jobs, key=lambda job: job["created_at"]),
        "sessions": sorted(sessions, key=lambda session: session["created_at"]),
        "attempts": sorted(attempts, key=lambda attempt: attempt["started_at"]),
        "replays": sorted(replays, key=lambda replay: replay["created_at"]),
        "media": {job_id: sorted(kinds) for job_id, kinds in media.items()},
        "budget_held_microusd": ledger["settled_microusd"]
        + (live["reserved_microusd"] if live else 0),
        "doorbells": len(doorbell.rings),
    }
