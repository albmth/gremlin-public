"""Client and worker HTTP routes; media bytes move through presigned storage URLs.

Client routes under /api/imprint are the published contract (docs/api.md, docs/openapi.json).
Worker and provider routes are internal and hidden from the OpenAPI document.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import secrets
import zipfile
import zlib
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gremlin_control import clock, doorbell
from gremlin_control.errors import api_error
from gremlin_control.limits import (
    ADMISSION_REASONS,
    ADMISSION_SECONDS,
    CONTENT_TYPES,
    CONTRACT_VERSION,
    DIAGNOSTIC_SECONDS,
    DOWNLOAD_SECONDS,
    DRIVING_FORMATS,
    DRIVING_MAX_FPS,
    DRIVING_MAX_PIXELS,
    DRIVING_MAX_SIDE,
    DURATION_MS,
    FAILURE_CODES,
    IDEMPOTENCY_KEY_CHARS,
    INPUT_LIMITS,
    INPUT_REJECTION_REASONS,
    MEDIA_SECONDS,
    OUTCOMES,
    OUTPUT_FPS,
    OWNER_CHARS,
    POSTER_MAX_SIDE,
    PROMPT_CHARS,
    RECORD_SECONDS,
    REFERENCE_MAX_PIXELS,
    RESULT_LIMITS,
    RESULT_NAMES,
    SAVED_SECONDS,
    SEED_MAX,
    START_MS,
    STATES,
    STICKER_SIDE,
    TIERS,
    UPLOAD_SECONDS,
)
from gremlin_control.store import ImprintStore, media_key, outcome_view

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/imprint", tags=["jobs"])
internal = APIRouter(prefix="/api/imprint", include_in_schema=False)
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Tier = Literal[TIERS]
State = Literal[STATES]
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


def valid_timings(value):
    return (
        isinstance(value, dict)
        and not value.keys() - TIMING_KEYS
        and all(
            type(number) in {int, float} and math.isfinite(number) and number >= 0
            for number in value.values()
        )
    )


# Request models


class Point(BaseModel):
    """A normalized position from the top-left corner of the image or frame."""

    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


CREATE_EXAMPLE = {
    "idempotency_key": "app-7f3c2a9e1b4d4c0f",
    "reference_sha256": "4c7ee30279b06ac0b01074a681908cdd46ff7b5db8b5d70e049e9b9c6a375928",
    "driving_sha256": "fa6bb5341bd829c2226cff9a3528b45fd68e5078b1ccb506be2aa199104665cf",
    "reference_point": {"x": 0.5, "y": 0.7},
    "target_point": {"x": 0.42, "y": 0.68},
    "start_ms": 0,
    "duration_ms": 5000,
    "seed": 42,
    "prompt": "A man with a brown mullet haircut and a short goatee, in a faded blue denim "
    "jacket over a black t-shirt, salutes with his right hand, then cups both hands behind "
    "his ears and tilts his head back, in front of a floodlit football pitch and crowded "
    "stands at night.",
    "tier": "simple-motion",
}
JOB_EXAMPLE = {
    "id": "22e8e10a-5f8d-4a6a-b943-65a22fc2f846",
    "state": "succeeded",
    "tier": "simple-motion",
    "created_at": 1792472875.29,
    "updated_at": 1792473035.98,
    "admission_expires_at": None,
    "media_expires_at": 1792732235.98,
    "progress": None,
    "execution": {
        "gpu_requested_at": 1792472890.04,
        "gpu": "NVIDIA GeForce RTX 5090",
        "gpu_seen_at": 1792472931.6,
        "started_at": 1792472962.18,
        "timings": {
            "engine_first_use": 1,
            "input_transfer_seconds": 0.4,
            "preparation_seconds": 1.9,
            "tracking_seconds": 14.2,
            "generation_seconds": 41.7,
            "model_file_load_seconds": 9.8,
            "model_device_load_seconds": 6.1,
            "restoration_seconds": 3.3,
            "encoding_seconds": 1.6,
            "total_seconds": 62.9,
            "delivery_seconds": 1.2,
            "attempt_seconds": 66.0,
        },
    },
    "outcome": {
        "kind": "ready",
        "at": 1792473035.98,
        "attempt_started": True,
        "failure_code": None,
        "failure_reason": None,
    },
    "error": None,
    "result": {
        "version": 1,
        "files": {
            "result.mp4": {
                "sha256": "ab05f7f6bef23520c386f2f0db4e76f6564bbddb1a4fce543c20022640a341ae",
                "bytes": 1296445,
                "content_type": "video/mp4",
                "width": 512,
                "height": 512,
                "frames": 80,
                "duration_ms": 5000.0,
            },
            "result.webp": {
                "sha256": "1bdee0ee6bcc2329720f3e478e3eab82ff83c6fb137a9d9fb7b0e755a54ab323",
                "bytes": 2675624,
                "content_type": "image/webp",
                "width": 512,
                "height": 512,
                "duration_ms": 5000.0,
            },
            "poster.jpg": {
                "sha256": "d2ba314ed9fc58ce96682b4edac7b8840b18a238fa693310ea2737d52ba2a1b9",
                "bytes": 30991,
                "content_type": "image/jpeg",
                "width": 384,
                "height": 384,
            },
        },
    },
}


class CreateJob(BaseModel):
    """An immutable generation intent; the same key with the same fields replays the job."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [CREATE_EXAMPLE]})
    idempotency_key: str = Field(
        min_length=IDEMPOTENCY_KEY_CHARS[0],
        max_length=IDEMPOTENCY_KEY_CHARS[1],
        pattern=r"^[A-Za-z0-9_-]+$",
        description="The client's own key for this intent, unique within its owner scope.",
    )
    reference_sha256: Digest = Field(description="SHA-256 of the exact reference image bytes.")
    driving_sha256: Digest = Field(description="SHA-256 of the exact driving clip bytes.")
    reference_point: Point = Field(
        description="A point on the person in the upright (EXIF-applied) reference image."
    )
    target_point: Point = Field(
        description="A point on the character to replace in the driving frame at start_ms, "
        "in the frame as stored (rotation metadata is ignored)."
    )
    start_ms: int = Field(default=START_MS[0], ge=START_MS[0], le=START_MS[1])
    duration_ms: int = Field(ge=DURATION_MS[0], le=DURATION_MS[1])
    seed: int = Field(default=42, ge=0, le=SEED_MAX)
    # Describes the identity's appearance, the action and the driving scene. SCAIL-2 takes
    # motion semantics from it: a neutral caption left a cat-driven clip as a static portrait.
    prompt: str = Field(
        min_length=PROMPT_CHARS[0],
        max_length=PROMPT_CHARS[1],
        description="One present-tense paragraph: the identity's appearance, the selected "
        "character's action beats in order, then the scene. Sent to the model unchanged.",
    )
    # simple-motion is the distilled recipe for human-driven clips; complex-motion keeps the
    # guidance that prompt-driven action needs, which the distilled recipe lost on Kung Fu.
    tier: Tier = Field(description="The generation recipe; the client chooses.")
    admission_expires_at: float | None = Field(
        default=None,
        description="Unix time by which the job must have started, at most 24 hours ahead; "
        "default 24 hours after creation. Replays keep the original deadline.",
    )


class Upload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bytes: int = Field(gt=0, description="Exact size of the file whose SHA-256 was declared.")


class ResultUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: Digest
    bytes: int = Field(gt=0)


class WorkerProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["preparing", "running", "finishing", "failed"]
    timings: dict[str, float] = Field(default_factory=dict, max_length=20)
    failure_code: Literal["worker_attempt_failed", "input_rejected"] | None = None
    failure_reason: Literal[INPUT_REJECTION_REASONS] | None = None

    @model_validator(mode="after")
    def codes_only_for_failures(self):
        if self.failure_code and self.stage != "failed":
            raise ValueError("Only a failed stage carries a failure code")
        if (self.failure_code == "input_rejected") != (self.failure_reason is not None):
            raise ValueError("An input rejection carries exactly one reason")
        return self

    @field_validator("timings", mode="before")
    @classmethod
    def approved_timings(cls, value):
        try:
            if valid_timings(value):
                return value
        except OverflowError:
            pass
        raise ValueError("Only approved finite nonnegative timings are accepted")


class PublishedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: Digest
    bytes: int = Field(gt=0, le=RESULT_LIMITS["result.mp4"])


class Lease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executor_id: UUID
    provider_job_id: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    weight_source: Literal["runtime-hf", "native-cache"] | None = None
    model_release: dict | None = Field(default=None, max_length=5)


# Response models


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable code, for example IMPRINT_ACTIVE.")
    message: str
    reason: str | None = Field(
        default=None,
        description="A finer typed reason for IMPRINT_MEDIA_INVALID, IMPRINT_BUDGET_BLOCKED "
        "and IMPRINT_LIMIT.",
    )


class Error(BaseModel):
    """Every Gremlin application error. Validation errors use FastAPI's list-shaped detail."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "detail": {
                        "code": "IMPRINT_MEDIA_INVALID",
                        "message": "Use a valid still reference and driving clip within media "
                        "and trim limits.",
                        "reason": "driving_too_short",
                    }
                }
            ]
        }
    )

    detail: ErrorDetail


class Admission(BaseModel):
    accepting: bool = Field(description="Creates and submits are accepted right now.")
    dispatching: bool = Field(description="Queued jobs are being started on a GPU.")
    reason_code: Literal[ADMISSION_REASONS] | None = Field(
        description="The first blocker, or null when dispatching."
    )
    retry_after_seconds: int | None = Field(
        description="When the daily limit is the blocker, seconds until a slot frees."
    )
    daily_jobs_limit: int
    daily_jobs_remaining: int
    observed_at: float


class DrivingLimits(BaseModel):
    max_bytes: int
    max_side_px: int
    max_pixels: int
    max_fps: int
    formats: list[str]


class ReferenceLimits(BaseModel):
    max_bytes: int
    max_pixels: int


class RequestLimits(BaseModel):
    duration_ms: list[int] = Field(description="Inclusive [minimum, maximum].")
    start_ms: list[int]
    prompt_chars: list[int]
    idempotency_key_chars: list[int]
    seed_max: int
    reference: ReferenceLimits
    driving: DrivingLimits


class OutputLimits(BaseModel):
    fps: int
    sticker_side_px: int
    poster_max_side_px: int
    max_bytes: dict[str, int]


class Retention(BaseModel):
    admission_seconds: int = Field(description="An unstarted job expires this long after create.")
    media_seconds: int = Field(description="Results stay downloadable this long after success.")
    saved_media_seconds: int = Field(description="At most this long after /saved.")
    diagnostics_seconds: int
    record_seconds: int = Field(description="The job record is readable this long after ending.")
    upload_link_seconds: int
    download_link_seconds: int


class Capabilities(BaseModel):
    contract_version: int
    build: str = Field(description="The deployed source commit, or 'local'.")
    tiers: list[str]
    features: list[str]
    admission: Admission
    limits: RequestLimits
    outputs: OutputLimits
    retention: Retention


class UploadLink(BaseModel):
    method: Literal["PUT"]
    url: str = Field(description="Presigned storage URL; send the bytes here, no bearer token.")
    headers: dict[str, str] = Field(description="Send exactly these headers; both are signed.")
    expires_at: float


class JobProgress(BaseModel):
    stage: Literal["uploading", "queued", "preparing", "running", "finishing"]
    since: float = Field(description="When the job entered this stage.")
    queue_position: int | None = Field(
        description="Unexpired queued jobs ahead of this one, while queued."
    )


class JobExecution(BaseModel):
    gpu_requested_at: float = Field(
        description="When Gremlin requested the GPU session that serves this job."
    )
    gpu: str | None = Field(
        description="The GPU model of that session's worker once known, for example "
        "`NVIDIA GeForce RTX 5090`: seen on the provider, or verified by the worker against the "
        "session's only allowed GPU when it took the job."
    )
    gpu_seen_at: float | None = Field(description="When that GPU model was first known.")
    started_at: float | None = Field(description="When a worker took this job.")
    timings: dict[str, float] = Field(
        description="What the worker has measured so far, in seconds unless noted: "
        "`input_transfer_seconds`, `preparation_seconds`, `tracking_seconds`, "
        "`generation_seconds`, `restoration_seconds`, `encoding_seconds`, `delivery_seconds`, "
        "`total_seconds`, `attempt_seconds`, `model_file_load_seconds`, "
        "`model_device_load_seconds` (subsets of tracking and generation) and "
        "`engine_first_use` (1 when the attempt is the worker's first, 0 when warm)."
    )


class Outcome(BaseModel):
    kind: Literal[OUTCOMES] = Field(
        description="ready: results were published; failed: screening or an attempt failed; "
        "cancelled: "
        "cancelled before any result; expired: never started before its deadline."
    )
    at: float = Field(description="When the job first ended; later expiry never changes it.")
    attempt_started: bool
    failure_code: Literal[FAILURE_CODES] | None = Field(
        description="Why a failed job failed. input_rejected: the inputs cannot produce a "
        "result (see failure_reason); content_rejected: safety policy refusal; "
        "the others are service-side."
    )
    failure_reason: Literal[INPUT_REJECTION_REASONS] | None = Field(
        description="For input_rejected only: which input check failed."
    )


class ResultFile(BaseModel):
    """One result file. Geometry is absent for jobs submitted before contract v3."""

    sha256: str
    bytes: int
    content_type: str
    width: int | None = None
    height: int | None = None
    frames: int | None = Field(default=None, description="MP4 only.")
    duration_ms: float | None = Field(
        default=None,
        description="MP4: frames at 16 fps, up to one frame longer than requested; "
        "WebP: exactly the requested duration; absent for the poster.",
    )


class ResultManifest(BaseModel):
    version: Literal[1]
    files: dict[str, ResultFile] = Field(
        description="Exactly result.mp4, result.webp and poster.jpg."
    )


class Job(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [JOB_EXAMPLE]})
    id: str
    state: State
    tier: str
    created_at: float
    updated_at: float
    admission_expires_at: float | None = Field(
        description="While uploading or queued: when the job expires unstarted."
    )
    media_expires_at: float | None = Field(
        description="While succeeded: when the results stop being downloadable."
    )
    progress: JobProgress | None = Field(description="Present while the job is active.")
    execution: JobExecution | None = Field(
        description="The GPU serving the job: while it is first in the queue with a GPU "
        "requested, and from the moment a worker takes it."
    )
    outcome: Outcome | None = Field(description="Present once the job has ended; immutable.")
    error: str | None = Field(description="Human-readable failure text; branch on outcome.")
    result: ResultManifest | None = Field(description="Present while results are downloadable.")


class JobList(BaseModel):
    jobs: list[Job]


class Acknowledged(BaseModel):
    acknowledged: Literal[True]


ERRORS = {401: {"model": Error}, 404: {"model": Error}, 409: {"model": Error}}


# Authentication and views


def store(request: Request):
    return ImprintStore(request.app.state.settings)


def owner(
    request: Request,
    # Documented once as the bearer security scheme of the published document.
    authorization: str | None = Header(default=None, include_in_schema=False),
    x_gremlin_owner: str | None = Header(
        default=None,
        alias="X-Gremlin-Owner",
        description="Required for delegated clients: the opaque end-user identifier that "
        "scopes this request. Forbidden for direct clients.",
    ),
):
    """The owner scope: a direct client's ID, or `<client>:<owner>` for a delegated client."""
    settings = request.app.state.settings
    if not authorization or not authorization.startswith("Bearer "):
        raise api_error(401, "CLIENT_AUTH_REQUIRED", "Client authorization is required.")
    digest = hashlib.sha256(authorization.removeprefix("Bearer ").encode()).hexdigest()
    client_id = next(
        (
            client
            for client, expected in settings.client_token_hashes.items()
            if secrets.compare_digest(digest, expected)
        ),
        None,
    )
    if client_id is None:
        raise api_error(401, "CLIENT_AUTH_INVALID", "Client authorization is invalid.")
    store(request).check_owner(client_id)
    if client_id in settings.delegated_clients:
        if x_gremlin_owner is None or not (
            OWNER_CHARS[0] <= len(x_gremlin_owner) <= OWNER_CHARS[1]
            and all(c.isascii() and (c.isalnum() or c in "_-") for c in x_gremlin_owner)
        ):
            raise api_error(
                400, "CLIENT_OWNER_REQUIRED", "Delegated clients must send X-Gremlin-Owner."
            )
        return f"{client_id}:{x_gremlin_owner}"
    if x_gremlin_owner is not None:
        raise api_error(400, "CLIENT_OWNER_UNSUPPORTED", "This client is not delegated.")
    return client_id


def manifest_view(service, job):
    if not job["result_json"] or not service.media_available(job, "result.mp4"):
        return None
    geometry = job.get("input_metadata", {})
    files = {}
    for name, stored in json.loads(job["result_json"]).items():
        entry = {
            "sha256": stored["sha256"],
            "bytes": stored["bytes"],
            "content_type": CONTENT_TYPES[name],
        }
        entry |= geometry.get(name, {})
        files[name] = entry
    return {"version": 1, "files": files}


def snapshot(service, job):
    active = job["state"] in ("uploading", "queued", "preparing", "running", "finishing")
    position = service.queue_position(job)
    return {
        "id": job["id"],
        "state": job["state"],
        "tier": json.loads(job["request_json"])["tier"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "admission_expires_at": (
            job["expires_at"] if job["state"] in ("uploading", "queued") else None
        ),
        "media_expires_at": job["expires_at"] if job["state"] == "succeeded" else None,
        "progress": (
            {
                "stage": job["state"],
                "since": job["updated_at"],
                "queue_position": position,
            }
            if active
            else None
        ),
        "execution": service.execution(job, position),
        "outcome": outcome_view(job),
        "error": job["error"],
        "result": manifest_view(service, job),
    }


def redirect(url):
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


# Client routes


@router.get("/capabilities", response_model=Capabilities, responses={401: {"model": Error}})
def capabilities(request: Request, client_id: str = Depends(owner)):
    """What this deployment accepts right now: version, tiers, admission, limits, retention."""
    settings = request.app.state.settings
    return {
        "contract_version": CONTRACT_VERSION,
        "build": settings.build_commit or "local",
        "tiers": list(TIERS),
        "features": ["owner_erasure", "spending_limits"]
        + (["safety_moderation"] if settings.moderation_enabled else []),
        "admission": store(request).admission_status(),
        "limits": {
            "duration_ms": list(DURATION_MS),
            "start_ms": list(START_MS),
            "prompt_chars": list(PROMPT_CHARS),
            "idempotency_key_chars": list(IDEMPOTENCY_KEY_CHARS),
            "seed_max": SEED_MAX,
            "reference": {
                "max_bytes": INPUT_LIMITS["reference"],
                "max_pixels": REFERENCE_MAX_PIXELS,
            },
            "driving": {
                "max_bytes": INPUT_LIMITS["driving"],
                "max_side_px": DRIVING_MAX_SIDE,
                "max_pixels": DRIVING_MAX_PIXELS,
                "max_fps": DRIVING_MAX_FPS,
                "formats": list(DRIVING_FORMATS),
            },
        },
        "outputs": {
            "fps": OUTPUT_FPS,
            "sticker_side_px": STICKER_SIDE,
            "poster_max_side_px": POSTER_MAX_SIDE,
            "max_bytes": {name: RESULT_LIMITS[name] for name in RESULT_NAMES},
        },
        "retention": {
            "admission_seconds": ADMISSION_SECONDS,
            "media_seconds": MEDIA_SECONDS,
            "saved_media_seconds": SAVED_SECONDS,
            "diagnostics_seconds": DIAGNOSTIC_SECONDS,
            "record_seconds": RECORD_SECONDS,
            "upload_link_seconds": UPLOAD_SECONDS,
            "download_link_seconds": DOWNLOAD_SECONDS,
        },
    }


class OwnerErasure(BaseModel):
    status: Literal["pending", "completed"]
    requested_at: float
    completed_at: float | None


@router.delete("/owner", response_model=OwnerErasure, responses=ERRORS)
def erase_owner(request: Request, client_id: str = Depends(owner)):
    """Erase one verified delegated owner, with durable asynchronous cleanup."""
    if ":" not in client_id:
        raise api_error(400, "IMPRINT_ERASURE_UNSUPPORTED", "Use a delegated owner identity.")
    receipt = store(request).erase_owner(client_id)
    doorbell.ring(request.app.state.settings)
    return receipt


@router.post(
    "/jobs",
    response_model=Job,
    responses=ERRORS | {410: {"model": Error}, 422: {"model": Error}, 429: {"model": Error}},
)
def create_job(body: CreateJob, request: Request, client_id: str = Depends(owner)):
    """Create a job in `uploading`, or replay the job this key already created."""
    service = store(request)
    data = body.model_dump(exclude={"idempotency_key"})
    # Omitted, the deadline stays out of the stored intent, so replays of intents created
    # before it existed still compare equal.
    deadline = data.pop("admission_expires_at")
    if deadline is not None:
        data["admission_expires_at"] = deadline
    if deadline is not None and deadline > clock.now() + ADMISSION_SECONDS + 1:
        raise api_error(
            422, "IMPRINT_ADMISSION_TOO_LATE", "The admission deadline is more than 24 hours away."
        )
    return snapshot(service, service.create(client_id, body.idempotency_key, data))


@router.get("/jobs", response_model=JobList, responses={401: {"model": Error}})
def list_jobs(
    request: Request,
    client_id: str = Depends(owner),
    limit: int = Query(default=20, ge=1, le=100),
    state: Literal[(*STATES, "active")] | None = Query(
        default=None, description="Only jobs in this state, or `active` for unfinished ones."
    ),
):
    """This owner scope's jobs, newest first, for recovery after losing local state."""
    service = store(request)
    jobs = service.list_jobs(client_id, limit=limit, state=state)
    return {"jobs": [snapshot(service, job) for job in jobs]}


@router.get("/jobs/{job_id}", response_model=Job, responses=ERRORS)
def get_job(job_id: str, request: Request, client_id: str = Depends(owner)):
    """The job's current state, progress, outcome and result manifest."""
    service = store(request)
    return snapshot(service, service.job(job_id, client_id))


@router.post(
    "/jobs/{job_id}/inputs/{kind}/upload",
    response_model=UploadLink,
    responses=ERRORS | {413: {"model": Error}},
)
def input_upload(
    job_id: str,
    kind: Literal["reference", "driving"],
    body: Upload,
    request: Request,
    client_id: str = Depends(owner),
):
    """A presigned link that accepts exactly the declared bytes of this input."""
    service = store(request)
    service.job(job_id, client_id)
    return service.input_ticket(job_id, kind, body.bytes)


@router.post(
    "/jobs/{job_id}/submit",
    response_model=Job,
    responses=ERRORS | {413: {"model": Error}, 422: {"model": Error}, 503: {"model": Error}},
)
def submit(job_id: str, request: Request, client_id: str = Depends(owner)):
    """Verify both stored inputs, run the CPU preflight and queue the job."""
    service = store(request)
    service.job(job_id, client_id)
    job = service.submit(job_id)
    if job["state"] == "queued":
        doorbell.ring(request.app.state.settings)
    return snapshot(service, job)


@router.delete("/jobs/{job_id}", response_model=Job, responses=ERRORS)
def cancel(
    job_id: str,
    request: Request,
    client_id: str = Depends(owner),
    if_not_started: bool = Query(
        default=False,
        description="Cancel only if no worker has started the job; otherwise 409 IMPRINT_STARTED.",
    ),
):
    """Cancel the job, fence any running attempt and withdraw its results."""
    service = store(request)
    service.job(job_id, client_id)
    job, stopped = service.cancel(job_id, if_not_started=if_not_started)
    if stopped:
        # A running attempt was fenced; stop its provider job without waiting for the sweep.
        doorbell.ring(request.app.state.settings)
    return snapshot(service, job)


@router.post("/jobs/{job_id}/saved", response_model=Acknowledged, responses=ERRORS)
def saved(job_id: str, request: Request, client_id: str = Depends(owner)):
    """Results are stored durably on the client's side; shorten Gremlin's retention."""
    service = store(request)
    service.job(job_id, client_id)
    service.acknowledge(job_id)
    return {"acknowledged": True}


@router.get(
    "/jobs/{job_id}/results/{name}",
    status_code=307,
    response_class=RedirectResponse,
    responses=ERRORS | {307: {"description": "Redirect to a presigned storage URL"}},
)
def result(
    job_id: str,
    name: Literal[RESULT_NAMES],
    request: Request,
    client_id: str = Depends(owner),
):
    """Redirect to a short-lived storage URL for one result file."""
    service = store(request)
    job = service.job(job_id, client_id)
    manifest = json.loads(job["result_json"] or "{}")
    url = None
    if name in manifest and service.media_available(job, name):
        url = service.download(job, name)
    if url is None:
        raise api_error(404, "IMPRINT_RESULT_UNAVAILABLE", "The result is not available.")
    return redirect(url)


def diagnostic_bytes(data):
    """Only a small, structured record crosses the worker diagnostic boundary."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if (
                len(members) != 1
                or members[0].filename != "diagnostics.json"
                or members[0].file_size > 16 * 1024
            ):
                raise ValueError("Invalid diagnostic archive")
            with archive.open(members[0]) as stream:
                body = stream.read(16 * 1024 + 1)
            if len(body) > 16 * 1024:
                raise ValueError("Diagnostic record is too large")
            record = json.loads(body)
        if (
            not isinstance(record, dict)
            or not {"schema_version", "stage", "timings"} <= record.keys()
            or record.keys() - {"schema_version", "stage", "timings", "failure_code"}
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["stage"] not in {"preparing", "running", "finishing", "failed"}
            or not valid_timings(record["timings"])
            or (
                "failure_code" in record
                and record["failure_code"] not in {"worker_attempt_failed", "input_rejected"}
            )
        ):
            raise ValueError("Invalid diagnostic record")
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RuntimeError,
        zipfile.BadZipFile,
        zlib.error,
    ) as error:
        raise api_error(
            422, "IMPRINT_DIAGNOSTICS_INVALID", "Diagnostics must contain only approved metrics."
        ) from error
    # Repacking removes ZIP comments, extra fields, prefixes and trailing bytes too.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("diagnostics.json"),
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )
    return buffer.getvalue()


@router.get(
    "/jobs/{job_id}/diagnostics",
    response_class=Response,
    responses=ERRORS | {200: {"content": {"application/zip": {}}}},
)
def diagnostics(job_id: str, request: Request, client_id: str = Depends(owner)):
    """A ZIP holding `diagnostics.json`: stage, numeric timings and an optional failure code."""
    service = store(request)
    job = service.job(job_id, client_id)
    data = (
        service.get_diagnostics(job_id) if service.media_available(job, "diagnostics.zip") else None
    )
    # Older archives can contain private media, prompts and raw engine errors. Preserve held
    # operator evidence but never expose it through the contract.
    try:
        valid = data is not None and len(data) <= 32 * 1024 and data == diagnostic_bytes(data)
    except Exception:
        valid = False
    if not valid:
        raise api_error(404, "IMPRINT_DIAGNOSTICS_UNAVAILABLE", "No retained diagnostics.")
    return Response(data, media_type="application/zip", headers={"Cache-Control": "no-store"})


# Internal routes: the GPU worker and the provider's completion webhook


@internal.post("/provider/{session_id}/{token}")
def provider_webhook(session_id: str, token: str, request: Request):
    """RunPod's completion notice. Its body is never trusted or stored; it only rings."""
    if store(request).webhook_session(session_id, token) is None:
        raise api_error(404, "IMPRINT_NOT_FOUND", "Unknown session.")
    doorbell.ring(request.app.state.settings)
    return {"acknowledged": True}


def worker(request: Request, session_id: str, authorization: str | None = Header(default=None)):
    token = (authorization or "").removeprefix("Bearer ")
    store(request).session(session_id, token)
    return token


@internal.post("/worker/{session_id}/lease")
def lease(session_id: str, request: Request, body: Lease, token: str = Depends(worker)):
    return store(request).lease(
        session_id,
        token,
        body.provider_job_id,
        executor_id=str(body.executor_id),
        weight_source=body.weight_source,
        model_release=body.model_release,
    )


@internal.post("/worker/{session_id}/attempts/{attempt_id}/progress")
def progress(
    session_id: str,
    attempt_id: str,
    body: WorkerProgress,
    request: Request,
    token: str = Depends(worker),
):
    if store(request).progress(
        session_id, attempt_id, body.stage, body.timings, body.failure_code, body.failure_reason
    ):
        doorbell.ring(request.app.state.settings)
    return {"acknowledged": True}


@internal.get("/worker/{session_id}/attempts/{attempt_id}/inputs/{kind}")
def worker_input(
    session_id: str,
    attempt_id: str,
    kind: Literal["reference", "driving"],
    request: Request,
    token: str = Depends(worker),
):
    service = store(request)
    _, job = service.live_attempt(session_id, attempt_id)
    key = media_key(job["id"], kind)
    if service.media.head(key) is None:
        raise api_error(404, "IMPRINT_INPUT_UNAVAILABLE", "The input is not available.")
    return redirect(service.media.presign_get(key, DOWNLOAD_SECONDS, "application/octet-stream"))


@internal.post("/worker/{session_id}/attempts/{attempt_id}/results/{kind}/upload")
def worker_result_upload(
    session_id: str,
    attempt_id: str,
    kind: Literal[RESULT_NAMES],
    body: ResultUpload,
    request: Request,
    token: str = Depends(worker),
):
    if body.bytes > RESULT_LIMITS[kind]:
        raise api_error(413, "IMPRINT_MEDIA_TOO_LARGE", "Media exceeds its byte limit.")
    return store(request).result_ticket(session_id, attempt_id, kind, body.sha256, body.bytes)


@internal.put("/worker/{session_id}/attempts/{attempt_id}/results/diagnostics.zip")
async def worker_diagnostics(
    session_id: str, attempt_id: str, request: Request, token: str = Depends(worker)
):
    service = store(request)
    limit = RESULT_LIMITS["diagnostics.zip"]
    _, job = service.live_attempt(session_id, attempt_id)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise api_error(413, "IMPRINT_MEDIA_TOO_LARGE", "Media exceeds its byte limit.")
    if not data:
        raise api_error(
            422, "IMPRINT_HASH_MISMATCH", "Media did not match the submitted content hash."
        )
    clean = diagnostic_bytes(bytes(data))
    service.live_attempt(session_id, attempt_id)
    service.put_diagnostics(job["id"], clean)
    service.live_attempt(session_id, attempt_id)
    return {"sha256": hashlib.sha256(clean).hexdigest(), "bytes": len(clean)}


@internal.post("/worker/{session_id}/attempts/{attempt_id}/complete")
def complete(
    session_id: str,
    attempt_id: str,
    body: dict[str, PublishedFile],
    request: Request,
    token: str = Depends(worker),
):
    store(request).complete(
        session_id, attempt_id, {key: value.model_dump() for key, value in body.items()}
    )
    return {"acknowledged": True}


DESCRIPTION = """\
Gremlin replaces one character in a short driving clip with the person in a reference image.
Create a job with the hashes of your normalized inputs, upload both files through presigned
links, submit, poll until the job ends, then download and verify the three results.
All client routes need `Authorization: Bearer <token>`; media bytes never pass through the API.
See docs/api.md and docs/integration.md in the Gremlin repository.
"""


def create_app(settings):
    app = FastAPI(
        title="Gremlin",
        version=f"{CONTRACT_VERSION}.0.0",
        description=DESCRIPTION,
        openapi_tags=[{"name": "jobs", "description": "Generation jobs and their results."}],
    )
    app.state.settings = settings
    app.include_router(router)
    app.include_router(internal)

    @app.get("/health", tags=["service"])
    def health() -> dict[str, str]:
        """Liveness only: the API answers. It does not check storage, the GPU or admission."""
        return {"service": "gremlin", "status": "ok"}

    if settings.test_hooks:
        from gremlin_control.hooks import router as hooks

        app.include_router(hooks)
    return app
