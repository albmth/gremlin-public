"""Worker cleanup must fence live writers and never erase an explicit fixture output."""

import json
import subprocess
import sys
import threading
import types
import uuid
import zipfile
from contextlib import nullcontext

import pytest
import requests
from imprint_worker import handler, runner
from imprint_worker.client import Client
from imprint_worker.lifecycle import Runtime
from imprint_worker.media import InputRejected
from PIL import Image

MODEL_BOOT = {
    "weight_source": "runtime-hf",
    "model_release": {
        "version": 1,
        "asset_set_sha256": "a" * 64,
        "repo": None,
        "revision": None,
    },
}


def verified_models(root):
    return types.SimpleNamespace(root=root, check_unchanged=lambda: None)


def test_restart_is_fenced_by_inherited_engine_lock_then_reclaims_abandoned_files(tmp_path):
    root = tmp_path / "runtime"
    fixture = tmp_path / "accepted-fixture"
    fixture.mkdir()
    (fixture / "output.mp4").write_bytes(b"accepted")
    with Runtime(root) as runtime:
        runtime.clear()
        (runtime.jobs / "abandoned").mkdir()
        (runtime.jobs / "abandoned" / "reference").write_bytes(b"personal")
        (runtime.engine / "output" / "link").symlink_to(fixture, target_is_directory=True)
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
            pass_fds=(runtime.lock.fileno(),),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout.readline().strip() == "ready"
    try:
        with pytest.raises(RuntimeError, match="still owned"), Runtime(root):
            pass
        assert (root / "jobs" / "abandoned" / "reference").exists()
    finally:
        child.communicate("", timeout=10)
    with Runtime(root) as runtime:
        runtime.clear()
        assert list(runtime.jobs.iterdir()) == []
        assert list((runtime.engine / "output").iterdir()) == []
    assert (fixture / "output.mp4").read_bytes() == b"accepted"


def test_interrupted_cleanup_is_idempotent_on_next_exclusive_startup(tmp_path, monkeypatch):
    from imprint_worker import lifecycle

    root = tmp_path / "runtime"
    original = lifecycle.remove
    with Runtime(root) as runtime:
        runtime.clear()
        (runtime.jobs / "reference").write_bytes(b"personal")

        def interrupted(path):
            (path / "jobs" / "reference").unlink()
            raise OSError("interrupted")

        monkeypatch.setattr(lifecycle, "remove", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            runtime.clear()
    monkeypatch.setattr(lifecycle, "remove", original)
    with Runtime(root) as runtime:
        runtime.clear()
        assert list(runtime.jobs.iterdir()) == []


def test_orphaned_encoder_keeps_scratch_owned_until_its_last_write(tmp_path, monkeypatch):
    from imprint_worker import media

    children = []

    def encode(_command, **options):
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
            pass_fds=options["pass_fds"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        children.append(child)
        assert child.stdout.readline().strip() == "ready"
        raise InterruptedError("parent interrupted while encoder continues")

    monkeypatch.setattr(media.subprocess, "run", encode)
    root = tmp_path / "runtime"
    with Runtime(root) as runtime:
        runtime.clear()
        output = runtime.jobs / "output"
        with pytest.raises(InterruptedError):
            media.encode_result(
                [Image.new("RGB", (16, 16), color) for color in ("red", "blue", "green")],
                {
                    "model_frames": 3,
                    "output_frames": 3,
                    "width": 16,
                    "height": 16,
                    "content_rect": (0, 0, 16, 16),
                    "duration_ms": 188,
                },
                output,
                ownership_fd=runtime.lock.fileno(),
            )
    try:
        with pytest.raises(RuntimeError, match="still owned"), Runtime(root):
            pass
        assert (output / "intermediate.mkv").exists()
    finally:
        for child in children:
            child.communicate("", timeout=10)
    with Runtime(root) as runtime:
        runtime.clear()
        assert list(runtime.jobs.iterdir()) == []


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt, InputRejected])
@pytest.mark.parametrize("idle", [False, True])
def test_runner_preserves_active_engine_files_and_explicit_fixture_output(
    tmp_path, monkeypatch, failure, idle
):
    engine_root = tmp_path / "engine"
    (engine_root / "input").mkdir(parents=True)
    (engine_root / "output").mkdir()
    output = tmp_path / "fixture"
    monkeypatch.setattr(runner, "peak_memory", lambda: nullcontext({}))
    monkeypatch.setattr(runner.Comfy, "idle", lambda _: idle)
    files = []

    def generate(*args):
        engine, prefix = args[-4:-2]
        source = engine.root / "input" / (prefix + "-reference.png")
        source.write_bytes(b"personal")
        images = engine.root / "output" / prefix
        images.mkdir()
        (images / "frame.png").write_bytes(b"personal")
        output.mkdir()
        (output / "result.mp4").write_bytes(b"fixture evidence")
        files.extend((source, images))
        if failure:
            raise failure("generation stopped")
        return {"result.mp4": {}}, {}

    monkeypatch.setattr(runner, "_run", generate)
    # An input rejection lets the worker continue only from an idle, cleaned engine.
    expected = RuntimeError if failure in (None, InputRejected) and not idle else failure
    if expected:
        with pytest.raises(expected):
            runner.run(None, None, output, engine_root, {}, recipe={"name": "test"})
    else:
        runner.run(None, None, output, engine_root, {}, recipe={"name": "test"})
    assert all(path.exists() is not idle for path in files)
    assert (output / "result.mp4").read_bytes() == b"fixture evidence"


def test_unknown_engine_queue_preserves_files(tmp_path, monkeypatch):
    source = tmp_path / "input" / "attempt-reference.png"
    source.parent.mkdir()
    source.write_bytes(b"personal")

    def unavailable(*_args, **_kwargs):
        raise requests.ConnectionError("engine unavailable")

    monkeypatch.setattr(runner.requests, "get", unavailable)
    with pytest.raises(requests.ConnectionError):
        runner.Comfy(tmp_path).cleanup("attempt")
    assert source.exists()


def test_diagnostics_contains_only_safe_numeric_summary(tmp_path, monkeypatch):
    client = Client("https://example.test", "session", "token")
    (tmp_path / "generation-graph.json").write_text('{"prompt":"private prompt"}')
    (tmp_path / "identity.png").write_bytes(b"personal media")
    uploaded = []
    monkeypatch.setattr(client, "call", lambda *a, **k: uploaded.append(k["data"].read()))
    client.diagnostics(
        "/attempts/example",
        tmp_path,
        {
            "stage": "running",
            "timings": {
                "generation_seconds": 12.5,
                "total_seconds": float("inf"),
                "tracking_seconds": True,
                "preparation_seconds": -1,
                "private prompt": 1,
            },
        },
        failure_code="worker_attempt_failed",
    )
    with zipfile.ZipFile(tmp_path / "diagnostics.zip") as bundle:
        assert bundle.namelist() == ["diagnostics.json"]
        assert json.loads(bundle.read("diagnostics.json")) == {
            "schema_version": 1,
            "stage": "failed",
            "timings": {"generation_seconds": 12.5},
            "failure_code": "worker_attempt_failed",
        }
    assert uploaded


@pytest.mark.parametrize("outcome", ["acknowledged", "lost_reply", "lost_lease", "lost_cache"])
def test_client_cleans_only_after_ack_and_fences_replayed_attempt(tmp_path, monkeypatch, outcome):
    import hashlib

    acknowledged = outcome == "acknowledged"
    stopped = threading.Event()
    if outcome in {"lost_lease", "lost_cache"}:

        class FastHeartbeat(threading.Event):
            def wait(self, timeout=None):
                return super().wait(0.01 if timeout == 25 else timeout)

        monkeypatch.setattr(
            "imprint_worker.client.threading",
            types.SimpleNamespace(
                Event=FastHeartbeat, Thread=threading.Thread, Lock=threading.Lock
            ),
        )
    payload = b"input"
    attempt = {
        "id": str(uuid.uuid4()),
        "state": "preparing",
        "request_json": json.dumps(
            {
                kind + "_sha256": hashlib.sha256(payload).hexdigest()
                for kind in ("reference", "driving")
            }
        ),
    }
    client = Client(
        "https://example.test", "session", "token", jobs_root=tmp_path, on_lease_lost=stopped.set
    )
    calls = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_content(self, _):
            return [payload]

        def json(self):
            return {"method": "PUT", "url": "https://storage.test/result", "headers": {}}

    def call(method, path, **kwargs):
        calls.append(path)
        if outcome == "lost_lease" and path.endswith("/progress") and "timeout" in kwargs:
            raise requests.ConnectionError("heartbeat retries exhausted")
        if path.endswith("/complete"):
            assert (tmp_path / attempt["id"] / "output" / "result.mp4").exists()
            if not acknowledged:
                raise requests.ConnectionError("response lost")
        return Reply()

    def stored():
        response = requests.Response()
        response.status_code = 200
        return response

    generated = []

    def check_models():
        if outcome == "lost_cache" and generated:
            raise handler.assets.ModelError("weight_changed")

    client.check_models = check_models

    def generate(reference, driving, output, *_args, **_kwargs):
        generated.append(True)
        output.mkdir()
        (output / "result.mp4").write_bytes(b"generated")
        if outcome in {"lost_lease", "lost_cache"}:
            assert stopped.wait(2), "Lost heartbeat must stop the engine during generation"
        if outcome == "lost_cache":
            raise requests.ConnectionError("Engine stopped while reading private inputs")
        return {"result.mp4": {}}, {"total_seconds": 1}

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr("imprint_worker.client.run", generate)
    monkeypatch.setattr("imprint_worker.client.requests.request", lambda *a, **k: stored())
    if acknowledged:
        client.execute(attempt)
        assert calls[-1].endswith("/complete")
    elif outcome == "lost_lease":
        with pytest.raises(RuntimeError, match="lease heartbeat failed"):
            client.execute(attempt)
        assert not any(path.endswith("/complete") for path in calls)
    elif outcome == "lost_cache":
        with pytest.raises(handler.assets.ModelError, match="weight_changed"):
            client.execute(attempt)
        assert not any(path.endswith(("/complete", "/upload")) for path in calls)
    else:
        with pytest.raises(requests.ConnectionError):
            client.execute(attempt)
    assert (tmp_path / attempt["id"]).exists() is not acknowledged
    with pytest.raises(RuntimeError, match="cannot be generated twice"):
        client.execute(attempt)
    assert generated == [True]


def test_input_rejection_reports_its_reason_and_keeps_the_session(tmp_path, monkeypatch):
    import hashlib

    payload = b"input"
    attempt = {
        "id": str(uuid.uuid4()),
        "state": "preparing",
        "request_json": json.dumps(
            {
                kind + "_sha256": hashlib.sha256(payload).hexdigest()
                for kind in ("reference", "driving")
            }
        ),
    }
    client = Client("https://example.test", "session", "token", jobs_root=tmp_path)
    posted, archives = [], []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_content(self, _):
            return [payload]

    def call(method, path, **kwargs):
        if path.endswith("/progress"):
            posted.append(kwargs["json"])
        if path.endswith("/diagnostics.zip"):
            with zipfile.ZipFile(kwargs["data"]) as bundle:
                archives.append(json.loads(bundle.read("diagnostics.json")))
        return Reply()

    def reject(*_args, **_kwargs):
        raise InputRejected("reference_subject_not_found")

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr("imprint_worker.client.run", reject)
    # Returning normally is what lets drain() lease the next queued job.
    client.execute(attempt)
    assert posted[-1] == {
        "stage": "failed",
        "timings": posted[-1]["timings"],
        "failure_code": "input_rejected",
        "failure_reason": "reference_subject_not_found",
    }
    assert archives[-1]["failure_code"] == "input_rejected"
    assert not (tmp_path / attempt["id"]).exists()


def test_lease_retry_reuses_executor_nonce_and_new_client_cannot_reuse_it(monkeypatch):
    monkeypatch.setattr("imprint_worker.client.time.sleep", lambda _: None)
    client = Client("https://example.test", "session", "token")
    leases = []

    def request(*_args, **kwargs):
        leases.append(kwargs["json"])
        if len(leases) == 1:
            raise requests.ConnectionError("lease response lost")
        response = requests.Response()
        response.status_code = 200
        response._content = b"null"
        return response

    monkeypatch.setattr(client.http, "request", request)
    assert client.drain() == 0
    assert len(leases) == 2
    assert leases[0]["executor_id"] == leases[1]["executor_id"] == client.executor_id
    assert Client("https://example.test", "session", "token").executor_id != client.executor_id


@pytest.mark.parametrize("failed", [False, True])
def test_handler_flashboot_cleanup_and_error_wait_for_engine_exit(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(handler, "check_gpu", lambda _: {"gpu_id": "verified"})
    events = []
    engine = types.SimpleNamespace(exited=False)
    engine.poll = lambda: 0 if engine.exited else None
    monkeypatch.setattr(handler, "start_engine", lambda *_: engine)
    monkeypatch.setattr(
        handler,
        "model_configuration",
        lambda: (MODEL_BOOT["weight_source"], MODEL_BOOT["model_release"]),
    )
    monkeypatch.setattr(
        handler, "ensure_weights", lambda *_: (verified_models(handler.LOCAL_MODELS_ROOT), {})
    )
    monkeypatch.setattr(handler.Comfy, "idle", lambda _: True)
    monkeypatch.setattr(handler.Comfy, "clear_history", lambda _: events.append("history"))
    monkeypatch.setenv("IMPRINT_MODELS_ROOT", str(tmp_path / "models"))
    monkeypatch.delenv("IMPRINT_MODELS_STAGE", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        types.SimpleNamespace(virtual_memory=lambda: types.SimpleNamespace(total=1024)),
    )

    def stop(_):
        engine.exited = True
        events.append("stopped")

    monkeypatch.setattr(handler, "stop_engine", stop)
    with Runtime(tmp_path / "runtime") as runtime:
        runtime.clear()
        clear = runtime.clear

        def cleanup():
            events.append("cleanup")
            if (runtime.jobs / "inflight").exists():
                assert engine.exited
            clear()

        monkeypatch.setattr(runtime, "clear", cleanup)

        def drain(_):
            assert list(runtime.jobs.iterdir()) == []
            (runtime.engine / "input" / "stale").write_bytes(b"personal")
            if failed:
                (runtime.jobs / "inflight").write_bytes(b"personal")
                raise RuntimeError("secret prompt and token")
            return 1

        monkeypatch.setattr(handler.Client, "drain", drain)

        def start(config):
            job = {
                "id": "provider",
                "input": {
                    **MODEL_BOOT,
                    "origin": "https://example.test",
                    "session_id": "session",
                    "token": "token",
                },
            }
            response = config["handler"](job)
            if failed:
                assert response == {"error": "worker_session_failed", "refresh_worker": True}
                assert list(runtime.jobs.iterdir()) == []
                assert events[-2:] == ["stopped", "cleanup"]
            else:
                assert response["attempts"] == 1
                response = config["handler"](job)
                assert response["jobs_served"] == 2

        monkeypatch.setitem(
            sys.modules,
            "runpod",
            types.SimpleNamespace(serverless=types.SimpleNamespace(start=start)),
        )
        handler.serve(runtime)
        assert list((runtime.engine / "input").iterdir()) == []


def test_shutdown_waits_for_python_writer_before_cleaning_and_releasing_ownership(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(handler, "check_gpu", lambda _: {"gpu_id": "verified"})
    monkeypatch.setattr(
        handler,
        "model_configuration",
        lambda: (MODEL_BOOT["weight_source"], MODEL_BOOT["model_release"]),
    )
    writing, stopped = threading.Event(), threading.Event()
    engine = types.SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(handler, "stop_engine", lambda _: stopped.set())
    monkeypatch.setattr(handler.Comfy, "idle", lambda _: True)
    monkeypatch.setattr(handler.Comfy, "clear_history", lambda _: None)
    threads = []
    with Runtime(tmp_path / "runtime") as runtime:
        runtime.clear()
        clear = runtime.clear

        def cleanup():
            assert not writing.is_set()
            clear()

        monkeypatch.setattr(runtime, "clear", cleanup)

        def drain(_):
            writing.set()
            assert stopped.wait(5)
            # Encoding can outlive engine termination; its last write must still own scratch.
            (runtime.jobs / "last-write").write_bytes(b"personal")
            writing.clear()
            raise RuntimeError("shutdown")

        monkeypatch.setattr(handler.Client, "drain", drain)

        def start(config):
            job = {
                "id": "provider",
                "input": {
                    **MODEL_BOOT,
                    "origin": "https://example.test",
                    "session_id": "session",
                    "token": "token",
                },
            }
            thread = threading.Thread(target=config["handler"], args=(job,))
            threads.append(thread)
            thread.start()
            assert writing.wait(5)
            raise SystemExit(0)

        monkeypatch.setitem(
            sys.modules,
            "runpod",
            types.SimpleNamespace(serverless=types.SimpleNamespace(start=start)),
        )
        with pytest.raises(SystemExit):
            handler.serve_sessions(runtime, engine, MODEL_BOOT, verified_models(tmp_path))
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert list(runtime.jobs.iterdir()) == []
