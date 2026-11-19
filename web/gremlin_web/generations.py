"""A generation, from upload to download.

States: `draft` (uploads only) -> `submitted` (stages `analyzing`, then `describing`) ->
`processing` (sent to Gremlin, tracked) -> `succeeded` (results copied into the site's bucket),
or `failed` / `cancelled`.

The visitor supplies two files. Before any GPU work the worker proposes the interval,
subjects, description and tier. `awaiting_selection` presents the prepared revision for
review, with taps needed only for uncertain subjects. Media, interval and description can be
corrected before confirmation. Pending review holds no slot, charges no quota again on resume,
and expires after `SELECTION_SECONDS` (the sweep finds it through its `PENDING` item).

The visitor supplies two files and presses Generate once: when both subjects are clear the
generation continues by itself, and it pauses for review only when a subject is uncertain or
preparation needs a correction. One video of the shared allowance is reserved when a generation
is committed to the GPU and returned if it ends before any GPU was used.

Drafts and generations belong to one anonymous visitor (`visitors`): every read and write is
keyed by that visitor's ID, so nobody can cancel or delete another visitor's videos or see their
inputs or results. The `RESULT` partition is only an internal cleanup index.

Every processing step is idempotent and resumable: the browser's polling, the asynchronous
invocation after submit and the one-minute sweep may all advance the same generation, and a
short claim keeps them from doing expensive work twice. One generation runs at a time across the
whole site, matching Gremlin's one active job per client: the single `SLOT` item names it, and
creating that item, together with the quota counts, is what admits a submission, so concurrent
submits cannot both start or exceed a quota.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import re
import secrets
import tempfile
import time
import uuid
from pathlib import Path

from gremlin_web import assist
from gremlin_web.db import Conflict, Txn, optimistic
from gremlin_web.gremlin import GremlinError
from gremlin_web.visitors import LimitReached

log = logging.getLogger("gremlin_web")

RESULTS = {"result.mp4": "video/mp4", "poster.jpg": "image/jpeg"}
RAW_LIMITS = {"reference": 15 * 1024**2, "driving": 100 * 1024**2}
RAW_TYPES = {
    "reference": {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "image/gif"},
    "driving": {
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-matroska",
        "image/gif",
        "image/webp",
    },
}
ACTIVE = ("submitted", "processing")
WAITING = "awaiting_selection"
TERMINAL = ("succeeded", "failed", "cancelled")
GID = re.compile(r"^[0-9a-f]{32}$")
SLOT = {"pk": "SLOT", "sk": "ACTIVE"}
# Longer than the worker's 300 s timeout, so a claim never lapses under a live invocation.
CLAIM_SECONDS = 330
POLL_SECONDS = 10
# A generation that cannot start on a GPU within this time gives up instead of holding the slot.
START_WAIT_SECONDS = 6 * 3600
SEED = 42
# Each analysis is counted before it starts, so a run that a timeout killed counts too.
MAX_ANALYSES = 2
# Description calls per generation, including the one after a tap and a retry.
MAX_CAPTIONS = 2
SELECTION_SECONDS = 3600
# An upload nobody generated from, for example after a file was replaced, is deleted after this.
DRAFT_SECONDS = 3600
# Processing errors in a row before a generation fails as a service error.
MAX_ERRORS = 8
MAX_REVISIONS = 6
SIDES = {"reference": "reference_point", "target": "target_point"}
AI_NOTICE = "AI-generated with Gremlin: a character's likeness was replaced by machine learning."

# Why Gremlin is not taking work, in a visitor's words (capabilities.admission.reason_code).
CLOSED_REASONS = {
    "dispatch_disabled": "generation is paused",
    "dispatch_held": "the GPU queue is waiting for a check",
    "budget_exhausted": "the GPU budget is used up for now",
    "spending_limit_reached": "the GPU budget is used up for now",
    "spending_unauthorized": "the GPU budget is used up for now",
    "daily_limit_reached": "today's job limit is reached",
    "endpoint_unconfigured": "the GPU service is closed",
    "unreachable": "the GPU service can't be reached",
}
# Visitors read "photo" and "video", not the API's names for them.
KIND_WORDS = {"reference": "photo", "driving": "video"}
TYPE_HINTS = {"reference": "JPG, PNG, WebP or HEIC", "driving": "MP4, MOV, WebM or GIF"}


def closed_message(admission):
    """Why Gremlin isn't taking new videos, in a visitor's words, or None when it is."""
    if admission.get("accepting") and admission.get("dispatching"):
        return None
    why = CLOSED_REASONS.get(admission.get("reason_code"), "the GPU service is closed")
    return f"New videos aren't starting right now: {why}."


MESSAGES = {
    "reference_unreadable": "The photo couldn't be opened. Try a JPEG, PNG or HEIC photo.",
    "reference_too_small": "The photo is too small. Both dimensions must be at least 64 pixels.",
    "reference_too_large": "The photo is too large. Use one under 67 megapixels.",
    "driving_unreadable": "The video couldn't be read. Try an MP4 or MOV file.",
    "driving_too_short": "The video needs at least 3 seconds of action from the selected start.",
    "driving_timestamps": "The video's timing information is damaged. Try exporting it again.",
    # Kept for records submitted with a chosen interval.
    "interval": "The selected part runs past the end of the video.",
    "reference_subject_not_found": "The GPU could not segment the chosen reference character. "
    "Choose a clearer photo or a point well inside the character.",
    "target_not_tracked": "The character to replace was lost during the clip. Use a clip where "
    "they stay in view and don't fill the whole frame.",
    "driving_scene_cut": "No usable continuous shot was found in the selected part (automatic "
    "search covers the first 10 seconds). Choose another 3-5 second interval or replace the video.",
    "content_rejected": "The content checks refused this request.",
    "public_figure": "This looks like it shows a public figure or footage of a public event, and "
    "those aren't used here. Try your own photo and clip.",
    "media_too_complex": "Preparation did not finish within its limits. Retry, choose a shorter "
    "interval or replace the video with a smaller file.",
    "selection_expired": "Preparation was not confirmed within an hour, so it was cancelled. "
    "Start it again whenever you like.",
    "quota": "No videos were left when this one was ready to start.",
    "network_quota": "This network had used today's videos when this one was ready to start. "
    "Try again tomorrow (UTC).",
    "capacity": "No GPU started this video within 6 hours of submitting it.",
    "expired": "This video didn't start before its deadline.",
    "cancelled": "Cancelled before a GPU started it.",
    "service": "Something went wrong on our side, not with your files. Try again in a while.",
}


class Rejected(Exception):
    """The request cannot be accepted; `code` is stable, `status` is the HTTP status."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def new_gid(now):
    # Time-ordered, so the history partition reads newest first without an index.
    return f"{int(now * 1000):012x}{secrets.token_hex(10)}"


def gen_pk(visitor):
    return f"GEN#{visitor}"


def draft_pk(visitor):
    return f"DRAFT#{visitor}"


DAY = 86400


def pending_key(gid):
    return {"pk": "PENDING", "sk": gid}


def draft_index_key(gid):
    return {"pk": "DRAFTS", "sk": gid}


def result_key(gid):
    return {"pk": "RESULT", "sk": gid}


class Generations:
    def __init__(self, settings, table, media, gremlin, quotas, budget, clock, invoke):
        self.settings = settings
        self.table = table
        self.media = media
        self.gremlin = gremlin
        self.quotas = quotas
        self.budget = budget
        self.clock = clock
        # `invoke(visitor, gid)` asks a worker to advance a generation soon, asynchronously.
        self.invoke = invoke
        self._capabilities = (0.0, None)

    # Reading

    def get(self, visitor, gid):
        return self.table.get(gen_pk(visitor), gid) if GID.fullmatch(gid) else None

    def draft(self, visitor, gid):
        return self.table.get(draft_pk(visitor), gid) if GID.fullmatch(gid) else None

    def active(self):
        """The slot: which visitor's generation is running, or None."""
        return self.table.get(SLOT["pk"], SLOT["sk"])

    def unfinished(self, visitor):
        """This visitor's generation still running or waiting for review, if any."""
        now = self.clock()
        for gen in self.table.query(gen_pk(visitor), limit=5, forward=False):
            if gen["state"] in ACTIVE or (gen["state"] == WAITING and gen["selection_until"] > now):
                return gen
        return None

    def view(self, gen):
        """The client representation; never exposes storage keys or Gremlin internals."""
        failure = gen.get("failure")
        state = gen["state"]
        view = {
            "id": gen["gid"],
            "version": gen.get("v", 0),
            "state": state,
            "stage": gen.get("stage"),
            "queue_position": (gen.get("gremlin") or {}).get("queue_position"),
            "created_at": gen["created_at"],
            "submitted_at": gen["submitted_at"],
            "finished_at": gen.get("finished_at"),
            "duration_ms": (gen.get("request") or {}).get("duration_ms")
            or (gen.get("selection") or {}).get("duration_ms"),
            "failure": failure
            and {
                "code": failure["code"],
                "message": MESSAGES.get(failure["code"], MESSAGES["service"]),
            },
            "cancellable": (state in ACTIVE and not gen.get("started")) or state == WAITING,
            "files": None,
            "available_until": gen.get("media_until"),
            "selection": None,
            "timeline": self._timeline(gen),
        }
        if state == WAITING:
            analysis = gen.get("analysis") or {}
            chosen = gen.get("selection") or {}
            prepared = gen.get("normalized") or {}
            request = gen.get("request") or {}
            view["selection"] = {
                "revision": _revision(gen),
                "until": gen["selection_until"],
                "required": gen.get("awaiting") or [],
                "source_type": gen["inputs"]["driving"]["type"],
                "error": (
                    {
                        "code": gen["preparation_error"],
                        "message": MESSAGES.get(gen["preparation_error"], MESSAGES["service"]),
                    }
                    if gen.get("preparation_error")
                    else (
                        {
                            "code": "PREPARATION_REQUIRED",
                            "message": "This preparation uses an older format. "
                            "Use Retry preparation to check it again.",
                        }
                        if not gen.get("media_revision")
                        else None
                    )
                ),
                "sides": {
                    side: {
                        "why": analysis[side]["why"],
                        "point": chosen.get(SIDES[side]),
                        "candidate": chosen.get(side),
                        "candidates": [
                            {"id": i, "label": c["label"], "box": c["box"], "point": c["point"]}
                            for i, c in enumerate(analysis[side]["candidates"])
                        ],
                    }
                    for side in SIDES
                    if side in analysis
                },
                "interval": {
                    "start_ms": analysis.get("start_ms", (gen.get("interval") or [None, None])[0]),
                    "duration_ms": analysis.get(
                        "interval_ms", (gen.get("interval") or [None, None])[1]
                    ),
                    "first_source_ms": prepared.get("first_source_ms"),
                    "end_ms": prepared.get("source_end_ms"),
                },
                "description": request.get("prompt") or assist.FIXED_PROMPT,
                "tier": request.get("tier") or "complex-motion",
                "description_source": gen.get("description_source", "fixed"),
                "can_generate": bool(prepared)
                and bool(gen.get("media_revision"))
                and not gen.get("preparation_error"),
                "can_revise": gen.get("revisions", 0) < MAX_REVISIONS,
            }
        if state == "succeeded" and gen.get("media_until", 0) > self.clock():
            view["files"] = {
                name: {"bytes": f["bytes"], "content_type": RESULTS[name]}
                for name, f in gen["results"].items()
                # Results delivered before this release also hold a sticker.
                if name in RESULTS
            }
        return view

    def _timeline(self, gen):
        """What has happened so far, for the progress page: the site's own records and what
        Gremlin reported, as times, sizes, counts and choices; never storage keys, points, the
        description or provider identifiers."""
        trace = gen.get("trace") or {}
        analysis = gen.get("analysis") or {}
        sources = (gen.get("selection") or {}).get("sources") or {}
        normalized = gen.get("normalized") or {}
        gremlin = gen.get("gremlin") or {}
        execution = gremlin.get("execution") or {}

        def subject(side, source):
            found = analysis.get(side)
            return found and {
                "found": len(found["candidates"]),
                "why": found["why"],
                "chosen": sources.get(source),
            }

        return {
            "files": {
                kind: {"bytes": spec["bytes"], "type": spec["type"]}
                for kind, spec in (gen.get("inputs") or {}).items()
            },
            "analysis": {
                "started_at": trace.get("analysis_started"),
                "finished_at": trace.get("analyzed"),
                "attempts": gen.get("analysis_attempts", 0),
                "clip_ms": analysis.get("interval_ms"),
                "width": normalized.get("width"),
                "height": normalized.get("height"),
                "photo": subject("reference", "reference"),
                "clip": subject("target", "target"),
            },
            "choice": {"asked_at": trace.get("asked"), "answered_at": trace.get("answered")},
            "description": {
                "at": trace.get("described"),
                "model_calls": len(gen.get("captions") or []),
                "tier": (gen.get("request") or {}).get("tier"),
            },
            "gremlin": {
                "sent_at": trace.get("sent"),
                "safety_checks": trace.get("safety_checks"),
                "waiting": trace.get("waiting"),
                "stages": gremlin.get("since") or {},
                "queue_position": gremlin.get("queue_position"),
                "gpu_requests": gremlin.get("gpu_requests") or [],
                "gpu": execution.get("gpu"),
                "gpu_seen_at": execution.get("gpu_seen_at"),
                "started_at": execution.get("started_at"),
                "timings": execution.get("timings") or {},
            },
            "delivery": {
                "started_at": trace.get("delivering"),
                "files": {name: f["bytes"] for name, f in (gen.get("results") or {}).items()},
            },
            "retry": {"at": gen["retry_at"], "attempt": gen.get("errors", 0) + 1, "of": MAX_ERRORS}
            if gen.get("retry_at") and gen["state"] in ACTIVE
            else None,
            "ended_stage": gen.get("ended_stage"),
        }

    def preview_url(self, visitor, gid, side, revision=None):
        """Revision-bound prepared pictures/video, plus the original clip for interval editing."""
        gen = self.get(visitor, gid)
        if gen is None or gen["state"] != WAITING:
            return None
        if revision is not None and revision != _revision(gen):
            return None
        if side == "source":
            spec = gen["inputs"]["driving"]
            return self.media.presign_get(spec["key"], 300, spec["type"])
        names = {
            "reference": ("reference.jpg", "image/jpeg"),
            "target": ("frame.jpg", "image/jpeg"),
            "clip": ("driving.mp4", "video/mp4"),
        }
        if not gen.get("normalized") or side not in names:
            return None
        name, kind = names[side]
        return self.media.presign_get(_normal_key(gen, name), 300, kind)

    def file_url(self, visitor, gid, name, download):
        gen = self.get(visitor, gid)
        if (
            gen is None
            or gen["state"] != "succeeded"
            or name not in RESULTS
            or gen.get("media_until", 0) <= self.clock()
        ):
            return None
        extension = name.rsplit(".", 1)[1]
        filename = f"video-{gid[-8:]}.{extension}"
        return self.media.presign_get(
            gen["results"][name]["key"], 300, RESULTS[name], filename if download else None
        )

    # Creating and submitting

    def _check_left(self, address):
        # Refused early to save the upload and the preparation; the reservation itself decides.
        if self.quotas.left() <= 0:
            raise Rejected(429, "LIMIT_REACHED", "No videos are left.")
        if address and self.quotas.network_left(address) <= 0:
            raise Rejected(
                429,
                "LIMIT_REACHED",
                "This network has used today's videos. Try again tomorrow (UTC).",
            )

    def create_draft(self, visitor, ip, files):
        self._check_left(ip)
        # Uploads that can't become a video would only spend the network's upload allowance.
        self._check_open(preparation=False)
        now = self.clock()
        gid = new_gid(now)
        inputs, uploads = {}, {}
        for kind in ("reference", "driving"):
            spec = files.get(kind) or {}
            size, kind_type = spec.get("bytes"), spec.get("type")
            word = KIND_WORDS[kind]
            if not isinstance(size, int) or not 0 < size <= RAW_LIMITS[kind]:
                limit = RAW_LIMITS[kind] // 1024**2
                raise Rejected(413, "UPLOAD_TOO_LARGE", f"The {word} must be under {limit} MB.")
            if kind_type not in RAW_TYPES[kind]:
                raise Rejected(
                    415,
                    "UPLOAD_TYPE",
                    f"That {word} type isn't supported. Use {TYPE_HINTS[kind]}.",
                )
            key = f"u/{gid}/{kind}"
            inputs[kind] = {"key": key, "bytes": size, "type": kind_type}
            uploads[kind] = self.media.presign_put(key, size, kind_type)

        def decide(txn):
            self.quotas.charge(
                txn,
                "drafts",
                ip,
                3600,
                self.settings.address_hourly_drafts,
                "Too many uploads from this network in the last hour. Try again later.",
            )
            txn.put(
                {
                    "pk": draft_pk(visitor),
                    "sk": gid,
                    "gid": gid,
                    "created_at": now,
                    "inputs": inputs,
                    "address": ip,
                    # Unsubmitted drafts disappear; the sweep deletes their uploads first.
                    "expire_at": int(now + DAY),
                }
            )
            txn.put(
                draft_index_key(gid)
                | {"visitor": visitor, "until": now + DRAFT_SECONDS, "expire_at": int(now + DAY)}
            )

        self._admit(decide)
        return {"id": gid, "uploads": uploads}

    def _admit(self, decide):
        try:
            return optimistic(self.table, decide)
        except LimitReached as limit:
            raise Rejected(429, "LIMIT_REACHED", limit.message) from None

    def _uploaded(self, draft):
        for kind, spec in draft["inputs"].items():
            if self.media.size(spec["key"]) != spec["bytes"]:
                raise Rejected(
                    409, "UPLOAD_MISSING", f"The {KIND_WORDS[kind]} hasn't finished uploading."
                )

    def admission(self):
        """Gremlin's live admission, cached for 30 seconds."""
        if self.gremlin is None:
            return {"accepting": False, "dispatching": False, "reason_code": "unconfigured"}
        at, value = self._capabilities
        if self.clock() - at > 30 or value is None:
            try:
                capabilities = self.gremlin.capabilities()
                value = capabilities["admission"] | {
                    "build": capabilities.get("build"),
                    "features": capabilities.get("features") or [],
                }
            except (GremlinError, OSError) as error:
                log.warning("gremlin capabilities unavailable: %s", error)
                value = {"accepting": False, "dispatching": False, "reason_code": "unreachable"}
            self._capabilities = (self.clock(), value)
        return value

    def preparing(self):
        """Whether a new video's scene description can be written and paid for now."""
        # Without budget or model answers, descriptions fall back to the fixed one.
        return bool(self.settings.caption_model)

    def _check_open(self, *, preparation=True):
        # Accepted but not dispatching (paused, held, out of budget) would only queue the job
        # until it expires, so it is refused before anything is sent.
        if message := closed_message(self.admission()):
            raise Rejected(503, "GREMLIN_UNAVAILABLE", message)
        if preparation and not self.preparing():
            raise Rejected(
                503,
                "PREPARATION_UNAVAILABLE",
                "New videos can't be prepared right now. Try again tomorrow (UTC).",
            )

    def _take_slot(self, txn, visitor, gid, now):
        if slot := self.active():
            mine = slot.get("visitor") == visitor
            raise Rejected(
                409,
                "ONE_AT_A_TIME",
                "Your other video is still being made. Wait for it to finish, or cancel it."
                if mine
                else "Someone else's video is being made. Try again in a few minutes.",
            )
        txn.put(SLOT | {"gid": gid, "visitor": visitor, "at": now})

    def _pending(self, visitor):
        now = self.clock()
        return any(
            g["state"] == WAITING and g["selection_until"] > now
            for g in self.table.query(gen_pk(visitor))
        )

    def submit(self, visitor, gid):
        existing = self.get(visitor, gid)
        if existing:
            return existing
        draft = self.draft(visitor, gid)
        if draft is None:
            raise Rejected(404, "NOT_FOUND", "That draft doesn't exist.")
        self._uploaded(draft)
        self._check_open()
        self._check_left(draft.get("address"))
        now = self.clock()

        def decide(txn):
            if current := self.get(visitor, gid):
                return current
            if (current := self.draft(visitor, gid)) is None:
                raise Rejected(404, "NOT_FOUND", "That draft doesn't exist.")
            # The slot first: a generation that moved to a pending choice released it in the
            # same transaction, so it is visible to the query below.
            self._take_slot(txn, visitor, gid, now)
            if self._pending(visitor):
                raise Rejected(
                    409,
                    "PENDING_SELECTION",
                    "Your other video is waiting for review. Finish or cancel it first.",
                )
            txn.delete(current)
            txn.delete(draft_index_key(gid))
            gen = {
                "pk": gen_pk(visitor),
                "sk": gid,
                "gid": gid,
                "visitor": visitor,
                "state": "submitted",
                "stage": "analyzing",
                "created_at": current["created_at"],
                "submitted_at": now,
                "updated_at": now,
                "next_at": now,
                "inputs": current["inputs"],
                "address": current.get("address"),
                "request": {},
                "media_revision": uuid.uuid4().hex,
            }
            txn.put(gen)
            return gen | {"v": 1}

        gen = self._admit(decide)
        self.invoke(visitor, gid)
        return gen

    def _waiting_revision(self, gen, revision):
        if gen is None:
            raise Rejected(404, "NOT_FOUND", "That video doesn't exist.")
        if revision != _revision(gen):
            raise Rejected(409, "STALE", "This media changed. Reload the page and review it again.")
        if gen["state"] != WAITING:
            raise Rejected(409, "NOT_WAITING", "This video isn't waiting for a choice.")
        if self.clock() >= gen["selection_until"]:
            raise Rejected(410, "SELECTION_EXPIRED", MESSAGES["selection_expired"])

    def select(self, visitor, gid, body):
        """Confirm the reviewed revision. Explicit points never go through detector gates."""
        revision, points = _selection_body(body)
        receipt = {k: v for k, v in body.items() if v is not None}
        gen = self.get(visitor, gid)
        if gen and gen.get("selection_receipt") == receipt:
            return gen
        self._waiting_revision(gen, revision)
        if not gen.get("media_revision"):
            raise Rejected(
                409,
                "PREPARATION_REQUIRED",
                "This preparation uses an older format. Use Retry preparation to check it again.",
            )
        if not gen.get("normalized") or gen.get("preparation_error"):
            raise Rejected(
                422, "PREPARATION_REQUIRED", "Correct the media or interval and prepare it again."
            )
        chosen = dict(gen["selection"])
        sources = dict(chosen["sources"])
        for side, point in points.items():
            index = body.get(f"{side}_candidate")
            if index is not None:
                candidates = gen["analysis"][side]["candidates"]
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < len(candidates)
                    or point != candidates[index]["point"]
                ):
                    raise Rejected(
                        422, "INVALID_CANDIDATE", "Choose a current candidate or tap the picture."
                    )
            chosen |= {SIDES[side]: point, side: index}
            sources[side] = "tap"
        if any(chosen.get(field) is None for field in SIDES.values()):
            raise Rejected(
                422, "INVALID_REQUEST", "Choose a subject in each picture that needs one."
            )
        options = dict(gen.get("options") or {})
        if body.get("prompt") is not None:
            prompt = body["prompt"].strip()
            if not 20 <= len(prompt) <= 2000:
                raise Rejected(
                    422, "INVALID_REQUEST", "Use a description between 20 and 2000 characters."
                )
            options["prompt"] = prompt
            options["source"] = "manual"
        if body.get("tier") is not None:
            if body["tier"] not in {"simple-motion", "complex-motion"}:
                raise Rejected(422, "INVALID_REQUEST", "Choose one of the two motion recipes.")
            options["tier"] = body["tier"]
        chosen |= {"sources": sources, "revision": revision, "confirmed": True}
        for name in ("reference.jpg", "driving.mp4"):
            if self.media.size(_normal_key(gen, name)) is None:
                raise Rejected(
                    409, "INPUTS_GONE", "Prepared files are missing. Use Retry preparation."
                )
        self._check_open(preparation=False)
        now = self.clock()

        def decide(txn):
            current = self.get(visitor, gid)
            if current and current.get("selection_receipt") == receipt:
                return current
            self._waiting_revision(current, revision)
            self._take_slot(txn, visitor, gid, now)
            quota_key = current.get("quota_key")
            if not current.get("quota"):
                quota_key = self.quotas.reserve(txn, current.get("address"))
            if pending := self.table.get(**pending_key(gid)):
                txn.delete(pending)
            # A prewritten description survives confirming the automatic proposal. Changing
            # identity requires a new description unless the visitor supplied their own.
            request = current.get("request") or {}
            changed = any(
                current["selection"].get(SIDES[side]) != point for side, point in points.items()
            )
            if request and not changed:
                options.setdefault("prompt", request["prompt"])
                options.setdefault("source", current.get("description_source", "model"))
                options.setdefault("tier", request["tier"])
            return txn.put(
                current
                | _trace(current, answered=now)
                | {
                    "quota": True,
                    "quota_key": quota_key,
                    "state": "submitted",
                    "stage": "describing",
                    "selection": chosen,
                    "selection_receipt": receipt,
                    "options": options,
                    "request": {},
                    "caption": None,
                    "preparation_error": None,
                    "awaiting": None,
                    "selection_until": None,
                    "next_at": now,
                    "busy_until": 0,
                    "updated_at": now,
                }
            )

        gen = self._admit(decide)
        self.invoke(visitor, gid)
        return gen

    def revise(self, visitor, gid, body):
        """Reprepare a bounded correction in the same admission, before any Gremlin intent."""
        revision = body["revision"]
        gen = self.get(visitor, gid)
        receipt = {k: v for k, v in body.items() if v is not None}
        if gen and gen.get("revision_receipt") == receipt:
            return gen
        self._waiting_revision(gen, revision)
        draft_id = body.get("draft_id")
        draft = self.draft(visitor, draft_id) if draft_id else None
        if draft_id:
            if draft is None:
                raise Rejected(404, "NOT_FOUND", "That replacement upload doesn't exist.")
            self._uploaded(draft)
        interval = gen.get("interval")
        if body.get("start_ms") is not None:
            start, duration = body["start_ms"], body.get("duration_ms")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not 0 <= start <= 60_000
                or not isinstance(duration, int)
                or isinstance(duration, bool)
                or not 3000 <= duration <= 5000
            ):
                raise Rejected(
                    422, "INVALID_REQUEST", "Choose 3-5 seconds starting within the first minute."
                )
            interval = [start, duration]
        elif body.get("duration_ms") is not None:
            raise Rejected(422, "INVALID_REQUEST", "Set both the interval start and duration.")
        if draft:
            interval = None
        now = self.clock()

        def decide(txn):
            current = self.get(visitor, gid)
            if current and current.get("revision_receipt") == receipt:
                return current
            self._waiting_revision(current, revision)
            if current.get("revisions", 0) >= MAX_REVISIONS or current.get("gremlin"):
                raise Rejected(
                    409,
                    "REVISION_LIMIT",
                    "This preparation cannot be changed again. Cancel it and start a new one.",
                )
            self._take_slot(txn, visitor, gid, now)
            if pending := self.table.get(**pending_key(gid)):
                txn.delete(pending)
            inputs = current["inputs"]
            if draft:
                current_draft = self.draft(visitor, draft_id)
                if current_draft is None:
                    raise Rejected(409, "STALE", "This replacement upload has already been used.")
                txn.delete(current_draft)
                txn.delete(draft_index_key(draft_id))
                inputs = current_draft["inputs"]
            return txn.put(
                current
                | {
                    "state": "submitted",
                    "stage": "analyzing",
                    "inputs": inputs,
                    "media_revision": uuid.uuid4().hex,
                    "revision_receipt": receipt,
                    "revisions": current.get("revisions", 0) + 1,
                    "interval": interval,
                    "analysis_attempts": 0,
                    "normalized": None,
                    "analysis": None,
                    "selection": None,
                    "selection_receipt": None,
                    "caption": None,
                    "request": {},
                    "preparation_error": None,
                    "awaiting": None,
                    "selection_until": None,
                    "options": {},
                    "errors": 0,
                    "retry_at": None,
                    "trace": {},
                    "next_at": now,
                    "busy_until": 0,
                    "updated_at": now,
                }
            )

        updated = self._admit(decide)
        if draft and updated["inputs"] != gen["inputs"]:
            for prefix in {spec["key"].rsplit("/", 1)[0] + "/" for spec in gen["inputs"].values()}:
                self.media.delete_prefix(prefix)
        self.invoke(visitor, gid)
        return updated

    def cancel(self, visitor, gid):
        gen = self.get(visitor, gid)
        if gen is None:
            raise Rejected(404, "NOT_FOUND", "That video doesn't exist.")
        if gen["state"] == WAITING:
            return self._finish(gen, "cancelled", "cancelled")
        if gen["state"] not in ACTIVE:
            return gen
        if gen.get("started"):
            raise Rejected(409, "ALREADY_STARTED", "A GPU is already making this video.")
        job = (gen.get("gremlin") or {}).get("job_id")
        if job:
            ended = None
            try:
                ended = self.gremlin.cancel(job, if_not_started=True)
            except GremlinError as error:
                if error.code == "IMPRINT_STARTED":
                    self._update(gen, {"started": True})
                    raise Rejected(
                        409, "ALREADY_STARTED", "A GPU is already making this video."
                    ) from None
                if error.code != "IMPRINT_NOT_FOUND":
                    raise
            return self._finish(
                gen,
                "cancelled",
                "cancelled",
                gremlin_state="cancelled",
                observed=ended and _observed(gen["gremlin"], ended),
            )
        # Not yet sent to Gremlin: the processor checks this flag before creating the job.
        marked = self._update(gen, {"cancel_requested": True})
        if marked is None:
            return self.cancel(visitor, gid)
        self.invoke(visitor, gid)
        return self._wait_settled(visitor, gid)

    def _wait_settled(self, visitor, gid, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            gen = self.get(visitor, gid)
            if gen["state"] in TERMINAL:
                return gen
            time.sleep(0.5)
        return self.get(visitor, gid)

    def delete(self, visitor, gid):
        """Remove a finished generation and its files."""
        gen = self.get(visitor, gid)
        if gen is None:
            return
        if gen["state"] in (*ACTIVE, WAITING):
            raise Rejected(409, "IN_PROGRESS", "Cancel the video before deleting it.")
        # Revoke the API copy too; cancellation schedules its media cleanup.
        # Keep our record if that request fails so deletion can be retried.
        job_id = (gen.get("gremlin") or {}).get("job_id")
        if job_id:
            try:
                self.gremlin.cancel(job_id, if_not_started=False)
            except GremlinError as error:
                if error.status != 404:
                    raise
        for prefix in ("r", "u", "n"):
            self.media.delete_prefix(f"{prefix}/{gid}/")
        for prefix in {spec["key"].rsplit("/", 1)[0] + "/" for spec in gen["inputs"].values()}:
            self.media.delete_prefix(prefix)
        txn = Txn(self.table)
        txn.delete(gen)
        txn.delete(result_key(gid))
        if pending := self.table.get(**pending_key(gid)):
            txn.delete(pending)
        txn.commit()

    def remove_result(self, gid):
        """Delete a private result from storage; the operator's action."""
        item = self.table.get(*result_key(gid).values())
        if item is None:
            return False
        self.delete(item["visitor"], gid)
        return True

    def refresh(self, gen):
        """Called when someone looks at a generation: end an expired choice, nudge a due one."""
        now = self.clock()
        if gen["state"] == WAITING and gen["selection_until"] <= now:
            return self._finish(gen, "cancelled", "selection_expired")
        if (
            gen["state"] in ACTIVE
            and gen.get("next_at", 0) <= now
            and gen.get("busy_until", 0) <= now
            and now - gen.get("nudged_at", 0) >= POLL_SECONDS
            and self._update(gen, {"nudged_at": now}) is not None
        ):
            self.invoke(gen["visitor"], gen["gid"])
        return gen

    # Processing

    def _update(self, gen, changes):
        """Replace the generation with `changes` if unchanged since read; None on conflict."""
        txn = Txn(self.table)
        updated = txn.put(gen | changes | {"updated_at": self.clock()})
        try:
            txn.commit()
        except Conflict:
            return None
        return updated

    def process(self, visitor, gid):
        gen = self.get(visitor, gid)
        now = self.clock()
        if gen is None or gen["state"] not in ACTIVE:
            self._release_slot(gid)
            return "inactive"
        if gen.get("busy_until", 0) > now:
            return "busy"
        claim = uuid.uuid4().hex
        gen = self._update(gen, {"busy_until": now + CLAIM_SECONDS, "claim": claim})
        if gen is None:
            return "busy"
        try:
            for _ in range(3):
                try:
                    outcome = self._advance(gen)
                    break
                except Conflict:
                    # A cancel or another writer changed the record mid-step; every step is
                    # idempotent, so continue from a fresh read.
                    gen = self.get(visitor, gid)
                    if gen.get("claim") != claim or gen["state"] not in ACTIVE:
                        return "superseded"
            else:
                raise Conflict
        except Exception:
            log.exception("generation %s: processing error", gid)
            current = self.get(visitor, gid)
            if current and current.get("claim") == claim and current["state"] in ACTIVE:
                attempts = current.get("errors", 0) + 1
                if attempts >= MAX_ERRORS:
                    self._finish(current, "failed", "service")
                else:
                    self._update(
                        current,
                        {
                            "busy_until": 0,
                            "errors": attempts,
                            "next_at": self.clock() + 30 * attempts,
                            "retry_at": self.clock() + 30 * attempts,
                        },
                    )
            return "error"
        current = self.get(visitor, gid)
        if current and current.get("claim") == claim and current["state"] in ACTIVE:
            self._update(current, {"busy_until": 0, "retry_at": None})
        return outcome

    def _advance(self, gen):
        gremlin = gen.get("gremlin") or {}
        if gen.get("cancel_requested") and not gremlin.get("job_id"):
            return self._finish(gen, "cancelled", "cancelled")["state"]
        if not gen.get("normalized"):
            if gen["request"].get("prompt"):
                # Submitted with chosen settings before this release and never started; the
                # deployment drains such records first, so this is only a safety net.
                log.warning("generation %s: unprepared record of the previous format", gen["gid"])
                return self._finish(gen, "failed", "service")["state"]
            gen = self._analyze(gen)
            if gen["state"] != "submitted":
                return gen["state"]
        if not gen["request"].get("prompt"):
            gen = self._describe(gen)
            if gen["state"] != "submitted":
                return gen["state"]
            if not gen["request"].get("prompt"):
                return gen["stage"]
        if not gremlin.get("job_id"):
            gen = self._create_job(gen)
            if gen is None or gen["state"] in TERMINAL or not gen["gremlin"].get("job_id"):
                return "waiting"
            if gen.get("cancel_requested"):
                ended = self.gremlin.cancel(gen["gremlin"]["job_id"], if_not_started=True)
                return self._finish(
                    gen,
                    "cancelled",
                    "cancelled",
                    gremlin_state="cancelled",
                    observed=_observed(gen["gremlin"], ended),
                )["state"]
        job = self.gremlin.job(gen["gremlin"]["job_id"])
        if job["state"] == "uploading":
            job = self._send_inputs(gen, job)
            if job is None:
                return "waiting"
        return self._track(gen, job)

    def _download_raw(self, gen, work):
        reference_raw = self.media.get(gen["inputs"]["reference"]["key"])
        driving_path = work / "driving"
        self.media.download(gen["inputs"]["driving"]["key"], driving_path)
        return reference_raw, driving_path

    def _input(self, gen, name):
        gid = gen["gid"]
        data = self.media.get(_normal_key(gen, name))
        if data is None:
            raise RuntimeError(f"normalized input {name} of {gid} is missing")
        return data

    def _analyze(self, gen):
        """Normalize both files, choose the interval and both subjects, or wait for a tap."""
        # Media and detector libraries load only in the worker, which keeps the API light.
        from PIL import Image

        from gremlin_web import normalize, selection

        gid = gen["gid"]
        attempts = gen.get("analysis_attempts", 0) + 1
        if attempts > MAX_ANALYSES:
            return self._preparation_failed(gen, "media_too_complex")
        started = (
            {}
            if "analysis_started" in (gen.get("trace") or {})
            else {"analysis_started": self.clock()}
        )
        gen = self._update(
            gen, {"analysis_attempts": attempts, "stage": "analyzing"} | _trace(gen, **started)
        )
        if gen is None:
            raise Conflict
        with tempfile.TemporaryDirectory() as work:
            reference_raw, driving_path = self._download_raw(gen, Path(work))
            try:
                reference, _ = normalize.reference(reference_raw)
                start_ms, interval_ms, clip, clip_info = normalize.prepare_driving(
                    driving_path, gen.get("interval")
                )
            except normalize.MediaRejected as error:
                return self._preparation_failed(gen, error.reason)
        with Image.open(io.BytesIO(reference)) as photo:
            reference_choice = selection.choose_reference(photo)
        samples = normalize.samples(clip, interval_ms)
        target_choice = selection.analyze_clip(samples, interval_ms)
        revision = gen.get("media_revision") or uuid.uuid4().hex
        prefix = f"n/{gid}/{revision}"
        self.media.put(f"{prefix}/reference.jpg", reference, "image/jpeg")
        self.media.put(f"{prefix}/driving.mp4", clip, "video/mp4")
        self.media.put(f"{prefix}/frame.jpg", normalize.jpeg(samples[0][1], 832, 90), "image/jpeg")
        hashes = {"reference_sha256": _sha256(reference), "driving_sha256": _sha256(clip)}
        analysis = {
            "revision": revision,
            "start_ms": start_ms,
            "interval_ms": interval_ms,
            "reference": _kept(reference_choice),
            "target": _kept(target_choice),
        }
        chosen = {
            "revision": revision,
            "sources": {},
            "duration_ms": target_choice["duration_ms"],
        }
        awaiting = []
        for side, choice in (("reference", analysis["reference"]), ("target", analysis["target"])):
            if choice["point"] is None:
                awaiting.append(side)
            else:
                chosen |= {SIDES[side]: choice["point"], side: choice["subject"]}
                chosen["sources"][side] = "auto"
        log.info(
            json.dumps(
                {
                    "event": "gremlin_web.analysis",
                    "gid": gid,
                    "start_ms": start_ms,
                    "interval_ms": interval_ms,
                    "duration_ms": target_choice["duration_ms"],
                    "reference": reference_choice["why"],
                    "target": target_choice["why"],
                }
            )
        )
        changes = {
            "normalized": hashes | clip_info | {"prefix": prefix},
            "media_revision": revision,
            "preparation_error": None,
            "analysis": analysis,
            "selection": chosen,
        } | _trace(gen, analyzed=self.clock())
        if awaiting:
            return self._await(gen, awaiting, changes)
        updated = self._update(gen, changes | {"stage": "describing", "next_at": self.clock()})
        if updated is None:
            raise Conflict
        return updated

    def _preparation_failed(self, gen, reason):
        """Retain inputs for bounded correction; no job exists and no quota is charged again."""
        changes = {"preparation_error": reason}
        if not _revision(gen):
            changes["media_revision"] = uuid.uuid4().hex
        return self._await(gen, [], changes)

    def _await(self, gen, sides, changes):
        """Pause for review or correction, releasing the slot in the same transaction."""
        if gen.get("cancel_requested"):
            return self._finish(gen, "cancelled", "cancelled")
        now = self.clock()
        until = min(now + SELECTION_SECONDS, gen["submitted_at"] + START_WAIT_SECONDS - 900)
        if until - now < 300:
            return self._finish(gen, "failed", "capacity")
        txn = Txn(self.table)
        updated = txn.put(
            gen
            | changes
            | _trace(gen | changes, asked=now)
            | {
                "state": WAITING,
                "stage": None,
                "reviewing": True,
                "awaiting": sides,
                "selection_until": until,
                "busy_until": 0,
                "updated_at": now,
            }
        )
        txn.put(pending_key(gen["gid"]) | {"visitor": gen["visitor"], "until": until})
        slot = self.active()
        if slot and slot["gid"] == gen["gid"]:
            txn.delete(slot)
        txn.commit()
        return updated

    def _describe(self, gen):
        """Write the scene description and choose the tier, within the caption budget.

        The model call also screens for public figures, so it runs even when the visitor wrote
        their own description. When it can't run or answer, the fixed description is used.
        """
        if gen["submitted_at"] + START_WAIT_SECONDS - self.clock() < 300:
            return self._finish(gen, "failed", "capacity")
        if self.settings.caption_model == assist.FIXED:
            return self._use_caption(gen, None)
        if gen.get("caption") is None:
            admission = self.admission()
            if not (admission.get("accepting") and admission.get("dispatching")):
                # Gremlin is closed: spend nothing until it opens again or the deadline passes.
                updated = self._update(
                    gen,
                    {"stage": "waiting", "next_at": self.clock() + 120}
                    | _waiting(gen, admission.get("reason_code"), self.clock()),
                )
                if updated is None:
                    raise Conflict
                return updated
            photo, frames = self._caption_inputs(gen)
            key = assist.cache_key(self.settings, photo, frames)
            if cached := self.table.get("CAPTION", key):
                gen = self._update(gen, {"caption": cached["caption"]})
                if gen is None:
                    raise Conflict
                return self._use_caption(gen, gen["caption"])
            # A call still marked claimed never reported back (its invocation died): its
            # reservation stands as the conservative charge.
            captions = [
                a | {"status": "unknown"} if a["status"] == "claimed" else a
                for a in gen.get("captions") or []
            ]
            if len(captions) >= MAX_CAPTIONS:
                return self._use_caption(gen, None)
            now = self.clock()
            txn = Txn(self.table)
            try:
                keys = self.budget.reserve(txn)
            except LimitReached:
                log.warning("generation %s: caption budget used up, fixed description", gen["gid"])
                return self._use_caption(gen, None)
            captions.append({"at": now, "status": "claimed", "keys": keys})
            gen = txn.put(gen | {"captions": captions, "stage": "describing", "updated_at": now})
            txn.commit()
            result = assist.caption(
                self.settings,
                photo,
                frames,
                auto=gen["selection"]["sources"].get("target") == "auto",
            )
            gen = self._settle(gen, result, key)
            if gen.get("caption") is None:
                if len(gen["captions"]) < MAX_CAPTIONS:
                    updated = self._update(gen, {"next_at": self.clock() + 20})
                    if updated is None:
                        raise Conflict
                    return updated
                return self._use_caption(gen, None)
        return self._use_caption(gen, gen["caption"])

    def _caption_inputs(self, gen):
        from PIL import Image

        from gremlin_web import normalize

        chosen = gen["selection"]
        analysis = gen["analysis"]
        samples = normalize.samples(self._input(gen, "driving.mp4"), chosen["duration_ms"])
        reference_box = target_box = None
        if chosen.get("reference") is not None:
            reference_box = analysis["reference"]["candidates"][chosen["reference"]]["box"]
        if chosen.get("target") is not None:
            target_box = analysis["target"]["candidates"][chosen["target"]]["box"]
        with Image.open(io.BytesIO(self._input(gen, "reference.jpg"))) as reference:
            photo, frames = assist.caption_inputs(
                reference,
                reference_box,
                chosen["reference_point"],
                samples,
                target_box,
                chosen["target_point"],
            )
        return photo, frames

    def _settle(self, gen, result, key):
        """Record the call's outcome and correct the budget, on a fresh read, exactly once."""
        visitor, gid = gen["visitor"], gen["gid"]
        cost = assist.cost_microusd(self.settings, result.usage) if result.usage else None

        def decide(txn):
            current = self.get(visitor, gid)
            captions = list(current["captions"])
            if captions[-1]["status"] != "claimed":
                return current
            attempt = captions[-1] | {"status": result.status}
            if result.usage:
                attempt |= {"tokens": list(result.usage), "microusd": cost}
                self.budget.settle(txn, attempt["keys"], cost)
            captions[-1] = attempt
            changes = {"captions": captions, "updated_at": self.clock()}
            if result.status == "ok":
                changes["caption"] = result.caption
                txn.put(
                    {
                        "pk": "CAPTION",
                        "sk": key,
                        "caption": result.caption,
                        "expire_at": int(self.clock() + 7 * DAY),
                    }
                )
            return txn.put(current | changes)

        updated = optimistic(self.table, decide)
        log.info(
            json.dumps(
                {
                    "event": "gremlin_web.caption",
                    "gid": gid,
                    "status": result.status,
                    "input_tokens": result.usage and result.usage[0],
                    "output_tokens": result.usage and result.usage[1],
                    "microusd": cost,
                }
            )
        )
        return updated

    def _use_caption(self, gen, caption):
        """Use a description as a suggestion, never as another authority over explicit intent.

        `caption` is None when no model answer is available: the fixed description is sent.
        """
        from gremlin_web import selection

        if caption and caption["public_figure"]:
            return self._finish(gen, "failed", "public_figure")
        options = gen.get("options") or {}
        if options.get("prompt"):
            prompt, source = options["prompt"], options.get("source", "manual")
        elif caption and caption["action_clear"]:
            prompt, source = assist.prompt(caption), "model"
        else:
            prompt, source = assist.FIXED_PROMPT, "fixed"
        tier = "complex-motion"
        if caption:
            target = gen["selection"].get("target")
            candidate = None if target is None else gen["analysis"]["target"]["candidates"][target]
            tier = selection.tier(candidate, caption)
        return self._compile_request(gen, prompt, tier, source)

    def _compile_request(self, gen, prompt, tier, source):
        if gen.get("cancel_requested"):
            return self._finish(gen, "cancelled", "cancelled")
        chosen, options = gen["selection"], gen.get("options") or {}
        request = {
            "reference_point": chosen["reference_point"],
            "target_point": chosen["target_point"],
            "start_ms": 0,
            "duration_ms": chosen["duration_ms"],
            "seed": SEED,
            "prompt": prompt,
            "tier": options.get("tier") or tier,
        }
        changes = {
            "request": request,
            "caption": None,
            "description_source": source,
            "preparation_error": None,
            "stage": "queued",
            "next_at": self.clock(),
        }
        changes |= _trace(gen, described=self.clock())
        if chosen.get("confirmed"):
            updated = self._update(gen, changes)
            if updated is None:
                raise Conflict
            return updated
        # Once a person is reviewing a generation, only they can send it on.
        if gen.get("reviewing"):
            return self._await(gen, [], changes)
        return self._start_unreviewed(gen, changes)

    def _start_unreviewed(self, gen, changes):
        """Both subjects were clear: reserve a video and continue without a review."""
        txn = Txn(self.table)
        try:
            quota_key = self.quotas.reserve(txn, gen.get("address"))
        except LimitReached as limit:
            return self._finish(gen, "failed", limit.code)
        updated = txn.put(
            gen
            | changes
            | {
                "quota": True,
                "quota_key": quota_key,
                "selection": gen["selection"] | {"confirmed": True},
                "updated_at": self.clock(),
            }
        )
        txn.commit()
        return updated

    def _create_job(self, gen):
        gid = gen["gid"]
        request = gen["request"]
        # Gremlin expires a job that never started by this deadline. Derived from the stored
        # submit time, so a replay after a lost response sends identical fields.
        deadline = gen["submitted_at"] + START_WAIT_SECONDS
        if deadline - self.clock() < 300:
            return self._finish(gen, "failed", "capacity")
        intent = {
            "idempotency_key": f"web-{gid}",
            "reference_sha256": gen["normalized"]["reference_sha256"],
            "driving_sha256": gen["normalized"]["driving_sha256"],
            "reference_point": request["reference_point"],
            "target_point": request["target_point"],
            "start_ms": 0,
            "duration_ms": request["duration_ms"],
            "seed": request["seed"],
            "prompt": request["prompt"],
            "tier": request["tier"],
            "admission_expires_at": deadline,
        }
        try:
            job = self.gremlin.create(intent)
        except GremlinError as error:
            if error.code in {"IMPRINT_LIMIT", "IMPRINT_ACTIVE"} or error.status in {429, 503}:
                reason = "busy" if error.code == "IMPRINT_ACTIVE" else error.reason or "unavailable"
                self._update(
                    gen,
                    {"stage": "waiting", "next_at": self.clock() + 120}
                    | _waiting(gen, reason, self.clock()),
                )
                return None
            raise
        updated = self._update(
            gen,
            {"gremlin": {"job_id": job["id"], "state": job["state"]}}
            | _trace(
                gen,
                sent=self.clock(),
                safety_checks="safety_moderation" in (self.admission().get("features") or []),
            ),
        )
        if updated is None:
            raise Conflict
        return updated

    def _send_inputs(self, gen, job):
        job_id = job["id"]
        try:
            for kind, name in (("reference", "reference.jpg"), ("driving", "driving.mp4")):
                self.gremlin.upload(job_id, kind, self.media.get(_normal_key(gen, name)))
            submitted = self.gremlin.submit(job_id)
        except GremlinError as error:
            if error.code == "IMPRINT_MEDIA_INVALID":
                self.gremlin.cancel(job_id, if_not_started=True)
                self._finish(gen, "failed", _media_reason(error.reason), gremlin_state="cancelled")
                return None
            if error.code == "IMPRINT_SEALED":
                self._discard_inputs(gen)
                return self.gremlin.job(job_id)
            if error.status in {429, 503} or error.code == "IMPRINT_HASH_MISMATCH":
                reason = error.reason or "unavailable"
                self._update(
                    gen,
                    {"stage": "waiting", "next_at": self.clock() + 60}
                    | _waiting(gen, reason, self.clock()),
                )
                return None
            raise
        # Gremlin holds its own copies now, and nothing here can prepare this video again.
        self._discard_inputs(gen)
        return submitted

    def _discard_inputs(self, gen):
        for prefix in {spec["key"].rsplit("/", 1)[0] + "/" for spec in gen["inputs"].values()}:
            self.media.delete_prefix(prefix)
        self.media.delete_prefix(f"n/{gen['gid']}/")

    def _track(self, gen, job):
        state = job["state"]
        stage = {
            "queued": "queued",
            "preparing": "preparing",
            "running": "generating",
            "finishing": "finishing",
        }.get(state)
        started = bool((job.get("outcome") or {}).get("attempt_started")) or state in {
            "preparing",
            "running",
            "finishing",
            "succeeded",
        }
        gremlin = _observed(gen.get("gremlin") or {}, job)
        if state == "succeeded":
            first = {} if "delivering" in (gen.get("trace") or {}) else {"delivering": self.clock()}
            delivering = self._update(
                gen, {"stage": "delivering", "gremlin": gremlin} | _trace(gen, **first)
            )
            if delivering is None:
                raise Conflict
            return self._deliver(delivering, job)
        if state in {"failed", "cancelled", "expired"}:
            outcome = job.get("outcome") or {}
            if outcome.get("kind") == "ready":
                # Ready, then cancelled or expired before we copied it: never delivered.
                code = "service"
            elif state == "failed":
                code = outcome.get("failure_reason") or outcome.get("failure_code") or "service"
                if code not in MESSAGES:
                    code = "service"
            elif state == "expired":
                code = "expired"
            else:
                code = "cancelled" if gen.get("cancel_requested") else "service"
            final = "cancelled" if code == "cancelled" else "failed"
            return self._finish(gen, final, code, gremlin_state=state, observed=gremlin)["state"]
        self._update(
            gen,
            {
                "state": "processing",
                "stage": stage,
                "gremlin": gremlin,
                "started": started,
                "next_at": self.clock() + (POLL_SECONDS if started else 20),
            },
        )
        return stage

    def _deliver(self, gen, job):
        gid = gen["gid"]
        manifest = (job.get("result") or {}).get("files") or {}
        if not set(RESULTS) <= set(manifest):
            return self._finish(gen, "failed", "service")["state"]
        results = {}
        for name in RESULTS:
            expected = manifest[name]
            data = self.gremlin.result(job["id"], name)
            if len(data) != expected["bytes"] or _sha256(data) != expected["sha256"]:
                raise ValueError(f"{name} failed verification")
            if name == "result.mp4":
                data = mark_ai_generated(data)
            key = f"r/{gid}/{name}"
            self.media.put(key, data, RESULTS[name])
            results[name] = {"key": key, "bytes": len(data), "sha256": _sha256(data)}
        # Our copies are durable: shorten Gremlin's own retention of the files.
        self.gremlin.saved(job["id"])
        return self._finish(gen, "succeeded", None, results=results)["state"]

    def _finish(self, gen, state, code, results=None, gremlin_state=None, observed=None):
        """End the generation and free the slot in one transaction, exactly once.

        `gremlin_state` records Gremlin's job state when the caller just read it, and `observed`
        what that read showed (`_observed`).
        """
        visitor, gid = gen["visitor"], gen["gid"]
        now = self.clock()
        retention = self.settings.retention_hours * 3600

        def decide(txn):
            current = self.get(visitor, gid)
            if current["state"] in TERMINAL:
                return current
            changes = {
                "state": state,
                "stage": None,
                "finished_at": now,
                "updated_at": now,
                "busy_until": 0,
                "failure": {"code": code} if code and state != "succeeded" else None,
            }
            if state != "succeeded":
                # Where it stopped, for the progress page; a pending choice has no stage.
                changes["ended_stage"] = current.get("stage") or (
                    "selecting" if current["state"] == WAITING else None
                )
            if gremlin_state or observed:
                changes["gremlin"] = (
                    (current.get("gremlin") or {})
                    | (observed or {})
                    | ({"state": gremlin_state} if gremlin_state else {})
                )
            if state == "succeeded":
                changes["results"] = results
                changes["media_until"] = now + retention
                txn.put(
                    result_key(gid)
                    | {
                        "gid": gid,
                        "visitor": visitor,
                        "until": now + retention,
                        # Only if the sweep stopped; the bucket's backstop takes the files by then.
                        "expire_at": int(now + retention + 2 * DAY),
                    }
                )
            elif current.get("quota") and not (
                current.get("started")
                or (changes.get("gremlin") or current.get("gremlin") or {}).get("gpu_used")
            ):
                # Nothing was billed for this video, so it goes back to everyone.
                self.quotas.release(txn, current.get("quota_key"))
                changes["quota"] = False
            changes["expire_at"] = int(now + retention)
            updated = txn.put(current | changes)
            slot = self.table.get(SLOT["pk"], SLOT["sk"])
            if slot and slot["gid"] == gid:
                txn.delete(slot)
            if pending := self.table.get(**pending_key(gid)):
                txn.delete(pending)
            return updated

        final = optimistic(self.table, decide)
        if final["state"] == state and final.get("finished_at") == now:
            log.info(
                json.dumps(
                    {
                        "event": "gremlin_web.generation_finished",
                        "gid": gid,
                        "state": final["state"],
                        "code": code,
                    }
                )
            )
        if final["state"] != "succeeded":
            self._withdraw(final)
        # Inputs never outlive the generation; results stay until `expire_results`.
        self._discard_inputs(final)
        return final

    def _withdraw(self, gen):
        """End the Gremlin job of a generation that ended here without a delivery.

        Gremlin allows one unfinished job per client, so a job left running after a service-side
        failure would block the next video until its own deadline.
        """
        gremlin = gen.get("gremlin") or {}
        if not gremlin.get("job_id") or gremlin.get("state") in {"failed", "cancelled", "expired"}:
            return
        try:
            self.gremlin.cancel(gremlin["job_id"], if_not_started=False)
        except (GremlinError, OSError) as error:
            log.warning("gremlin job of %s not withdrawn: %s", gen["gid"], error)

    def _release_slot(self, gid):
        slot = self.table.get(SLOT["pk"], SLOT["sk"])
        if slot and slot["gid"] == gid:
            txn = Txn(self.table)
            txn.delete(slot)
            # Another writer already freed or reused the slot.
            with contextlib.suppress(Conflict):
                txn.commit()

    def due(self):
        """(visitor, gid) of the active generation when it is due; frees a stale slot."""
        slot = self.active()
        if slot is None:
            return []
        visitor, gid = slot["visitor"], slot["gid"]
        gen = self.get(visitor, gid)
        now = self.clock()
        if gen is None or gen["state"] not in ACTIVE:
            self._release_slot(gid)
            return []
        if gen.get("next_at", 0) <= now and gen.get("busy_until", 0) <= now:
            return [(visitor, gid)]
        return []

    def expire_selections(self):
        """End every pending choice past its deadline, deleting its inputs; returns how many."""
        now = self.clock()
        ended = 0
        for item in self.table.query("PENDING"):
            if item["until"] > now:
                continue
            gen = self.get(item["visitor"], item["sk"])
            if gen is not None and gen["state"] == WAITING:
                self._finish(gen, "cancelled", "selection_expired")
                ended += 1
            else:
                txn = Txn(self.table)
                txn.delete(item)
                with contextlib.suppress(Conflict):
                    txn.commit()
        return ended

    def expire_results(self):
        """Delete every result past its time from storage; returns how many.

        Only finished generations have a `RESULT` item, so this cannot touch an active job's
        files. Files first: a failure leaves the item for the next sweep to try again.
        """
        now = self.clock()
        ended = 0
        for item in self.table.query("RESULT"):
            if item["until"] > now:
                continue
            gid = item["gid"]
            self.media.delete_prefix(f"r/{gid}/")
            txn = Txn(self.table)
            txn.delete(result_key(gid))
            txn.delete({"pk": gen_pk(item["visitor"]), "sk": gid})
            txn.commit()
            ended += 1
        return ended

    def expire_drafts(self):
        """Delete uploads nobody generated from; returns how many.

        The draft goes first, guarded by its version: once that commits, nobody can submit it,
        so its files cannot belong to a generation. A draft that was submitted first loses the
        race here and keeps its files.
        """
        now = self.clock()
        ended = 0
        for item in self.table.query("DRAFTS"):
            if item["until"] > now:
                continue
            gid = item["sk"]
            draft = self.draft(item["visitor"], gid)
            txn = Txn(self.table)
            txn.delete(draft_index_key(gid))
            if draft:
                txn.delete(draft)
            try:
                txn.commit()
            except Conflict:
                continue
            if draft:
                self.media.delete_prefix(f"u/{gid}/")
                ended += 1
        return ended


def _trace(gen, **events):
    """The change that records `events` (name -> time) in the generation's trace."""
    return {"trace": (gen.get("trace") or {}) | events}


def _waiting(gen, reason, now):
    """The change that records why the generation waits for Gremlin, and since when."""
    previous = (gen.get("trace") or {}).get("waiting") or {}
    since = previous.get("since", now) if gen.get("stage") == "waiting" else now
    return _trace(gen, waiting={"since": since, "reason": reason})


def _observed(gremlin, job):
    """`gremlin` with what this read of the job showed: its state, when each stage began as
    Gremlin recorded it, its place in the queue, and the GPU serving it (`execution`)."""
    progress = job.get("progress") or {}
    since = dict(gremlin.get("since") or {})
    if progress.get("stage") and progress.get("since"):
        since.setdefault(progress["stage"], progress["since"])
    execution = job.get("execution") or {}
    observed = gremlin | {
        "state": job["state"],
        "queue_position": progress.get("queue_position"),
        "since": since,
        # A worker was seen or took the job: from then on the GPU is billed, whatever happens.
        "gpu_used": bool(
            gremlin.get("gpu_used")
            or execution.get("gpu_seen_at")
            or execution.get("started_at")
            or (job.get("outcome") or {}).get("attempt_started")
            or job["state"] in {"preparing", "running", "finishing", "succeeded"}
        ),
    }
    if execution:
        # A replaced request (no GPU started in time) shows as a second request time.
        requests = list(gremlin.get("gpu_requests") or [])
        if execution["gpu_requested_at"] not in requests:
            requests = [*requests, execution["gpu_requested_at"]][-5:]
        observed |= {"execution": execution, "gpu_requests": requests}
    return observed


def _media_reason(reason):
    return {
        "reference_undecodable": "reference_unreadable",
        "reference_not_still": "reference_unreadable",
        "reference_too_large": "reference_too_large",
        "driving_undecodable": "driving_unreadable",
        "driving_resolution": "driving_unreadable",
        "driving_frame_rate": "driving_unreadable",
        "driving_too_short": "driving_too_short",
        "driving_timestamps": "driving_timestamps",
    }.get(reason, "service")


def _point(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"x", "y"}
        or not all(
            isinstance(value[k], int | float)
            and not isinstance(value[k], bool)
            and 0 <= value[k] <= 1
            for k in "xy"
        )
    ):
        raise Rejected(422, "INVALID_REQUEST", "Tap a point inside the picture.")
    return {"x": float(value["x"]), "y": float(value["y"])}


def _selection_body(body):
    """(revision, {side: point}) from a select request."""
    if not isinstance(body, dict) or not isinstance(body.get("revision"), str):
        raise Rejected(422, "INVALID_REQUEST", "Invalid request.")
    points = {side: _point(body[key]) for side, key in SIDES.items() if body.get(key) is not None}
    if any(body.get(f"{side}_candidate") is not None and side not in points for side in SIDES):
        raise Rejected(422, "INVALID_CANDIDATE", "A candidate choice must include its point.")
    return body["revision"], points


def _revision(gen):
    return gen.get("media_revision") or (gen.get("analysis") or {}).get("revision")


def _normal_key(gen, name):
    # Pending records from the deployed version are concrete consumers of the old keys.
    prefix = (gen.get("normalized") or {}).get("prefix", f"n/{gen['gid']}")
    return f"{prefix}/{name}"


def _kept(choice, limit=8):
    """A selection result as stored: at most `limit` candidates, the chosen one first."""
    candidates = choice["candidates"]
    order = list(range(len(candidates)))
    subject = choice.get("subject")
    if subject is not None:
        order = [subject] + [i for i in order if i != subject]
    return {
        "why": choice["why"],
        "point": choice["point"],
        "subject": None if subject is None else 0,
        "candidates": [candidates[i] for i in order[:limit]],
    }


def mark_ai_generated(mp4):
    """Remux the MP4 with an AI-generated notice in its metadata; frames are untouched."""
    import av

    out = io.BytesIO()
    with av.open(io.BytesIO(mp4)) as source, av.open(out, "w", format="mp4") as target:
        target.metadata.update({"comment": AI_NOTICE, "description": AI_NOTICE})
        stream = source.streams.video[0]
        copy = target.add_stream_from_template(stream)
        for packet in source.demux(stream):
            if packet.dts is None:
                continue
            packet.stream = copy
            target.mux(packet)
    return out.getvalue()
