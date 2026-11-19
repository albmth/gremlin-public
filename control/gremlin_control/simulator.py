"""A GPU-free worker for the local server, so client developers can reach every outcome.

It drives the same store operations as a real worker session: reserve, lease, stage
progress, presigned result uploads, diagnostics and completion, then settles the session
at zero cost. The published results are synthetic frames with the geometry the real worker
would produce, clearly labelled, never generated media.
"""

import io
import json
import logging
import threading
import time
import uuid
import zipfile

import av
import requests
from PIL import Image, ImageDraw

from gremlin_control import clock
from gremlin_control.api import diagnostic_bytes
from gremlin_control.limits import OUTPUT_FPS, RESULT_NAMES
from gremlin_control.store import ImprintStore

logger = logging.getLogger(__name__)
STAGE_SECONDS = 1.0
SIMULATED_GPU = "Simulated GPU"


def frame_durations(duration_ms):
    starts = [round(i * 1000 / OUTPUT_FPS) for i in range(-(-duration_ms * OUTPUT_FPS // 1000))]
    return [b - a for a, b in zip(starts, starts[1:] + [duration_ms], strict=True)]


def synthetic_frames(width, height, count):
    frames = []
    for index in range(count):
        image = Image.new("RGB", (width, height), (40, 40 + index * 3 % 160, 90))
        draw = ImageDraw.Draw(image)
        draw.text((8, 8), f"SIMULATED {index + 1}/{count}", fill="white")
        draw.rectangle((index * 4 % width, height // 2, index * 4 % width + 24, height // 2 + 24))
        frames.append(image)
    return frames


def render(geometry, duration_ms):
    """Encode the three results with the predicted dimensions and timing."""
    mp4 = geometry["result.mp4"]
    frames = synthetic_frames(mp4["width"], mp4["height"], mp4["frames"])
    video = io.BytesIO()
    with av.open(video, "w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=OUTPUT_FPS)
        stream.width, stream.height, stream.pix_fmt = mp4["width"], mp4["height"], "yuv420p"
        for frame in frames:
            for packet in stream.encode(av.VideoFrame.from_image(frame)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    side = geometry["result.webp"]["width"]
    stickers = []
    for frame in frames:
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        scaled = frame.copy()
        scaled.thumbnail((side, side))
        square.paste(scaled, ((side - scaled.width) // 2, (side - scaled.height) // 2))
        stickers.append(square)
    webp = io.BytesIO()
    stickers[0].save(
        webp,
        format="WEBP",
        save_all=True,
        append_images=stickers[1:],
        duration=frame_durations(duration_ms),
        loop=0,
    )
    poster = frames[0].copy()
    poster = poster.resize((geometry["poster.jpg"]["width"], geometry["poster.jpg"]["height"]))
    jpeg = io.BytesIO()
    poster.save(jpeg, format="JPEG", quality=90)
    return dict(
        zip(RESULT_NAMES, (video.getvalue(), webp.getvalue(), jpeg.getvalue()), strict=True)
    )


def diagnostics_archive(stage, failure_code=None):
    record = {"schema_version": 1, "stage": stage, "timings": {"total_seconds": STAGE_SECONDS * 3}}
    if failure_code:
        record["failure_code"] = failure_code
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("diagnostics.json", json.dumps(record))
    return diagnostic_bytes(buffer.getvalue())


def run_session(settings, *, pause=time.sleep):
    """Run every queued job in one zero-cost session; returns how many attempts ran."""
    import hashlib

    service = ImprintStore(settings)
    session_id, token, _ = service.reserve_session(
        settings.imprint_hourly_microusd, financial_authority=True
    )
    executor = str(uuid.uuid4())
    attempts = 0
    status = "COMPLETED"
    # As if the provider had placed a worker, so clients can follow a GPU being assigned.
    pause(STAGE_SECONDS)
    seen = clock.now()
    service.record_endpoint_workers(
        session_id, [{"observed_at": seen, "first_seen_at": seen, "gpu_id": SIMULATED_GPU}]
    )
    pause(STAGE_SECONDS)
    try:
        while (attempt := service.lease(session_id, token, executor_id=executor)) is not None:
            attempts += 1
            request = json.loads(attempt["request_json"])
            job = service.job(attempt["job_id"])
            if settings.local_worker == "reject":
                # Tracking found no subject: only this job fails; the session continues.
                pause(STAGE_SECONDS)
                service.put_diagnostics(job["id"], diagnostics_archive("failed", "input_rejected"))
                service.progress(
                    session_id,
                    attempt["id"],
                    "failed",
                    {},
                    failure_code="input_rejected",
                    failure_reason="reference_subject_not_found",
                )
                continue
            # The simulator's own measured stage times, under the worker's timing names.
            timings = {"engine_first_use": int(attempts == 1)}
            for stage, measured in (
                ("running", "preparation_seconds"),
                ("finishing", "generation_seconds"),
            ):
                started = time.monotonic()
                pause(STAGE_SECONDS)
                timings[measured] = round(time.monotonic() - started, 3)
                service.progress(session_id, attempt["id"], stage, timings)
            if settings.local_worker == "fail":
                service.put_diagnostics(
                    job["id"], diagnostics_archive("failed", "worker_attempt_failed")
                )
                service.progress(
                    session_id, attempt["id"], "failed", {}, failure_code="worker_attempt_failed"
                )
                # A service-side failure ends the worker's session, as on a GPU.
                status = "FAILED"
                break
            manifest = {}
            for name, data in render(job["input_metadata"], request["duration_ms"]).items():
                digest = hashlib.sha256(data).hexdigest()
                ticket = service.result_ticket(session_id, attempt["id"], name, digest, len(data))
                response = requests.request(
                    ticket["method"],
                    ticket["url"],
                    data=data,
                    headers=ticket["headers"],
                    timeout=30,
                )
                response.raise_for_status()
                manifest[name] = {"sha256": digest, "bytes": len(data)}
            service.put_diagnostics(job["id"], diagnostics_archive("finishing"))
            service.complete(session_id, attempt["id"], manifest)
    finally:
        # Settled as the provider reports a session that ended by itself, at zero cost.
        service.finish_session(
            session_id, 0, provider={"status": status, "delayTime": 0, "executionTime": 0}
        )
    return attempts


def loop(settings, stop):
    service = ImprintStore(settings)
    while not stop.wait(1):
        try:
            service.reconcile()
            if service.queued_exists() and not service.ledger()["live_session"]:
                run_session(settings)
        except Exception:
            logger.exception("gremlin.simulated_worker_failed at %s", clock.now())


def start(settings):
    stop = threading.Event()
    threading.Thread(target=loop, args=(settings, stop), daemon=True).start()
    return stop
