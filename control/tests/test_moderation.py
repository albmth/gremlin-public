"""Real codec intervals and durable moderation boundaries, without managed or GPU spend."""

import base64
import hashlib
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import av
import pytest
from fastapi import HTTPException
from gremlin_control import clock, moderation, preflight
from gremlin_control.local import provision
from gremlin_control.settings import Settings
from gremlin_control.store import ImprintStore, media_key
from PIL import Image

COLORS = [
    (240, 10, 10),
    (10, 240, 10),
    (10, 10, 240),
    (240, 240, 10),
    (240, 10, 240),
    (10, 240, 240),
]


def clip(format):
    output = io.BytesIO()
    frames = [Image.new("RGB", (32, 32), color) for color in COLORS]
    if format == "MP4":
        with av.open(output, "w", format="mp4") as container:
            stream = container.add_stream("libx264", rate=10)
            stream.width = stream.height = 32
            stream.pix_fmt = "yuv420p"
            for image in frames:
                for _ in range(10):
                    for packet in stream.encode(av.VideoFrame.from_image(image)):
                        container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    else:
        frames[0].save(
            output,
            format=format,
            save_all=True,
            append_images=frames[1:],
            duration=[700, 1300, 900, 1100, 1000, 1000],
            lossless=True,
        )
    return output.getvalue()


def color_index(sample):
    with Image.open(io.BytesIO(base64.b64decode(sample))) as image:
        color = image.convert("RGB").getpixel((16, 16))
    return min(
        range(len(COLORS)),
        key=lambda i: sum((a - b) ** 2 for a, b in zip(COLORS[i], color, strict=True)),
    )


@pytest.mark.parametrize("format", ["GIF", "WEBP", "MP4"])
def test_selected_interval_samples_cover_start_seconds_and_end(format):
    driving = clip(format)
    # The first target lies inside frame 0, not at the next frame's start.
    samples = moderation.sample(driving, 500, 3000)
    assert [color_index(value) for value in samples] == [0, 1, 2, 3]
    reference = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(reference, "PNG")
    result = preflight.validate(
        {"reference": reference.getvalue(), "driving": driving},
        {"start_ms": 500, "duration_ms": 3000},
        screening=True,
    )
    assert [color_index(value) for value in result["samples"][1:]] == [0, 1, 2, 3]
    assert result["metadata"]["result.mp4"]["frames"] == 48
    assert len(moderation.sample(driving, 0, 5000)) <= 6


def test_fractional_output_frame_duration_and_incomplete_interval():
    data = clip("MP4")
    assert moderation.output_samples(data, 3437.5)
    with pytest.raises(ValueError, match="incomplete"):
        moderation.sample(data, 5000, 3000)


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "gremlinlocal")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "gremlinlocalsecret")
    name = "gremlin-moderation-" + uuid.uuid4().hex[:12]
    settings = Settings(
        environment="test",
        dynamodb_endpoint="http://127.0.0.1:8001",
        s3_endpoint="http://127.0.0.1:9100",
        table_name=name,
        media_bucket=name,
        moderation_enabled=True,
        moderation_guardrail_id="test",
        moderation_guardrail_version="1",
        imprint_endpoint_id="simulated",
        imprint_runpod_key="simulated",
        imprint_hourly_microusd=650000,
        imprint_budget_microusd=5000000,
        imprint_rolling_budget_microusd=5000000,
        imprint_monthly_budget_microusd=5000000,
    )
    provision(settings)
    store = ImprintStore(settings)
    store.initialize_spending()
    return store


def uploaded(service):
    reference = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(reference, "PNG")
    inputs = {"reference": reference.getvalue(), "driving": clip("MP4")}
    request = {
        "start_ms": 500,
        "duration_ms": 3400,
        "tier": "simple-motion",
        "prompt": "A person performs non-graphic Kung Fu choreography.",
    }
    request.update(
        {kind + "_sha256": hashlib.sha256(data).hexdigest() for kind, data in inputs.items()}
    )
    job = service.create("owner", "moderation-test-0001", request)
    for kind, data in inputs.items():
        service.media.put(media_key(job["id"], kind), data, request[kind + "_sha256"])
    return job["id"]


def reject(*args, **kwargs):
    raise moderation.Rejected


def unavailable(*args, **kwargs):
    raise moderation.unavailable()


def test_input_refusal_never_queues_or_starts_attempt(service, monkeypatch):
    job_id = uploaded(service)
    monkeypatch.setattr(moderation, "check", reject)
    job = service.submit(job_id)
    assert job["state"] == "failed" and job["failure_code"] == "content_rejected"
    assert job["outcome_kind"] == "failed" and job["attempt_id"] is None
    assert not service.queued_exists()
    assert service.submit(job_id)["moderation_input_attempts"] == 1
    assert service.ledger()["live_session"] is None


def test_unavailable_checks_exhaust_durable_bound_and_release_owner(service, monkeypatch):
    job_id = uploaded(service)
    monkeypatch.setattr(moderation, "check", unavailable)
    for count in range(1, 4):
        with pytest.raises(HTTPException) as error:
            service.submit(job_id)
        assert error.value.status_code == 503
        assert error.value.detail["code"] == "IMPRINT_MODERATION_UNAVAILABLE"
        assert service.job(job_id)["moderation_input_attempts"] == count
    assert service.submit(job_id)["failure_code"] == "moderation_unavailable"
    assert not service.table.get("CLIENT#owner", "ACTIVE")
    assert not service.queued_exists()


def test_concurrent_submit_spends_once_and_cannot_queue_during_check(service, monkeypatch):
    job_id = uploaded(service)
    started, release = Event(), Event()
    calls = []

    def check(*args):
        calls.append(True)
        started.set()
        assert release.wait(10)

    monkeypatch.setattr(moderation, "check", check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.submit, job_id)
        assert started.wait(10)
        try:
            with pytest.raises(HTTPException):
                service.submit(job_id)
            assert not service.queued_exists()
        finally:
            release.set()
        assert first.result()["state"] == "queued"
    assert service.submit(job_id)["state"] == "queued"
    assert len(calls) == 1


def test_cancellation_during_screening_fences_acceptance(service, monkeypatch):
    job_id = uploaded(service)

    def cancel(*args):
        service.cancel(job_id)

    monkeypatch.setattr(moderation, "check", cancel)
    with pytest.raises(HTTPException):
        service.submit(job_id)
    assert service.job(job_id)["state"] == "cancelled"
    assert not service.queued_exists()


def test_abandoned_last_claim_fails_on_sweep(service, monkeypatch):
    job_id = uploaded(service)
    monkeypatch.setattr(moderation, "check", unavailable)
    for _ in range(2):
        with pytest.raises(HTTPException):
            service.submit(job_id)

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(moderation, "check", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.submit(job_id)
    then = clock.now() + 40
    monkeypatch.setattr(clock, "now", lambda: then)
    service.reconcile()
    assert service.job(job_id)["failure_code"] == "moderation_unavailable"


@pytest.mark.parametrize(("refused", "input_outages"), [(True, 0), (False, 0), (False, 2)])
def test_output_screening_precedes_publication_and_success_is_replayable(
    service, monkeypatch, refused, input_outages
):
    job_id = uploaded(service)
    calls = []
    monkeypatch.setattr(moderation, "check", unavailable)
    for _ in range(input_outages):
        with pytest.raises(HTTPException):
            service.submit(job_id)
    monkeypatch.setattr(moderation, "check", lambda *args: calls.append(True))
    service.submit(job_id)
    session_id, token, _ = service.reserve_session(650000, financial_authority=True)
    attempt = service.lease(session_id, token, executor_id="test")
    service.progress(session_id, attempt["id"], "finishing")
    manifest = {}
    for name in ("result.mp4", "result.webp", "poster.jpg"):
        data = clip("MP4") if name == "result.mp4" else b"transport-fixture"
        digest = hashlib.sha256(data).hexdigest()
        service.result_ticket(session_id, attempt["id"], name, digest, len(data))
        service.media.put(media_key(job_id, name), data, digest)
        manifest[name] = {"bytes": len(data), "sha256": digest}
    if refused:
        monkeypatch.setattr(moderation, "check", reject)
    service.complete(session_id, attempt["id"], manifest)
    job = service.job(job_id)
    if refused:
        assert job["failure_code"] == "content_rejected" and job["outcome_kind"] == "failed"
        assert job["result_json"] is None
        assert not service.media_available(job, "result.mp4")
    else:
        assert job["state"] == "succeeded"
        service.complete(session_id, attempt["id"], manifest)
        assert job["outcome_kind"] == "ready"
        assert len(calls) == 2
        assert service.job(job_id)["moderation_input_attempts"] == input_outages + 1
        assert service.job(job_id)["moderation_output_attempts"] == 1
