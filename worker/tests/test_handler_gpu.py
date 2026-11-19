"""Changed pool membership must never lease work on an unqualified GPU."""

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from imprint_worker import handler
from imprint_worker.lifecycle import Runtime

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


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    # The handler caches one successful probe for the process; tests start without one.
    monkeypatch.setattr(handler, "_probed_hardware", None)


@pytest.fixture
def constraint():
    return {
        "gpu_id": "NVIDIA GeForce RTX 5090",
        "runtime_gpu_names": ["NVIDIA GeForce RTX 5090"],
        "compute_capability": [12, 0],
        "min_vram_gib": 30,
        "min_host_memory_gib": 59127 / 1024,
        "min_cuda_version": "13.0",
    }


@pytest.fixture
def hardware():
    return {
        "gpu_name": "NVIDIA GeForce RTX 5090",
        "compute_capability": [12, 0],
        "vram_bytes": 32111 * 1024**2,
        "host_memory_bytes": 515605 * 1024**2,
        "container_memory_bytes": 59128 * 1024**2,
        "cuda_version": "13.2",
        "torch_arches": ["sm_80", "sm_120"],
        "attention_available": True,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (None, None),
        ("missing_constraints", None),
        ("gpu_name", "NVIDIA GeForce RTX 5090 Unqualified Variant"),
        ("compute_capability", [8, 9]),
        ("vram_bytes", 24 * 1024**3),
        ("container_memory_bytes", 48 * 1024**3),
        ("cuda_version", "12.8"),
        ("torch_arches", ["sm_80", "sm_90"]),
        ("attention_available", False),
    ],
)
def test_runtime_qualification_gates_leases_and_shuts_down_rejected_worker(
    tmp_path, monkeypatch, constraint, hardware, field, value
):
    monkeypatch.setattr(
        handler,
        "model_configuration",
        lambda: (MODEL_BOOT["weight_source"], MODEL_BOOT["model_release"]),
    )
    if field is not None and field != "missing_constraints":
        hardware[field] = value
    monkeypatch.setattr(handler, "probe_gpu", lambda: hardware)
    engine = types.SimpleNamespace(exited=False)
    engine.poll = lambda: 0 if engine.exited else None
    monkeypatch.setattr(handler, "stop_engine", lambda _: setattr(engine, "exited", True))
    monkeypatch.setattr(handler.Comfy, "idle", lambda _: True)
    monkeypatch.setattr(handler.Comfy, "clear_history", lambda _: None)
    drained = []
    monkeypatch.setattr(handler.Client, "drain", lambda _: drained.append(True) or 1)
    response = None

    def start(config):
        nonlocal response
        response = config["handler"](
            {
                "id": "provider-job",
                "input": {
                    **MODEL_BOOT,
                    "origin": "https://example.test",
                    "session_id": "session",
                    "token": "token",
                    **({} if field == "missing_constraints" else {"gpu_constraints": [constraint]}),
                },
            }
        )
        if field is not None:
            assert engine.exited

    monkeypatch.setitem(
        sys.modules, "runpod", types.SimpleNamespace(serverless=types.SimpleNamespace(start=start))
    )
    with Runtime(tmp_path / "runtime") as runtime:
        runtime.clear()
        handler.serve_sessions(runtime, engine, MODEL_BOOT, verified_models(tmp_path))
    if field is None:
        assert drained == [True]
        assert response["gpu"] == {"gpu_id": constraint["gpu_id"], **hardware}
    else:
        assert drained == []
        assert response["error"] == "worker_gpu_incompatible"
        assert response["refresh_worker"] is True
        assert response["gpu_rejection"]["hardware"] == (
            {} if field == "missing_constraints" else handler.diagnostic_hardware(hardware)
        )
        assert response["gpu_rejection"]["reason"] in {
            "configuration_mismatch",
            "constraints_invalid",
            "attention_unavailable",
            "torch_arch_unsupported",
        }


@pytest.mark.parametrize("invalid", [None, [], {}, "5090", float("nan")])
def test_supplied_empty_or_malformed_gpu_constraints_cannot_disable_guard(monkeypatch, invalid):
    def probe():
        pytest.fail("Malformed constraints should be rejected before probing")

    monkeypatch.setattr(handler, "probe_gpu", probe)
    with pytest.raises(ValueError):
        handler.check_gpu(invalid)


def test_nan_memory_bound_cannot_match_a_qualified_worker(monkeypatch, constraint):
    constraint["min_host_memory_gib"] = float("nan")
    monkeypatch.setattr(handler, "probe_gpu", lambda: pytest.fail("Unexpected probe"))
    with pytest.raises(ValueError):
        handler.check_gpu([constraint])


def test_runtime_probe_uses_container_limit_and_checks_image_library_pins(monkeypatch):
    installed = {"torch": "2.9.1+cu130", "comfy-kitchen": "0.2.39", "triton": "3.5.1"}
    monkeypatch.setattr("importlib.metadata.version", installed.__getitem__)
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        types.SimpleNamespace(virtual_memory=lambda: types.SimpleNamespace(total=512 * 1024**3)),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(
            cuda=types.SimpleNamespace(
                device_count=lambda: 1,
                get_device_properties=lambda _: types.SimpleNamespace(
                    name="NVIDIA GeForce RTX 5090", total_memory=32111 * 1024**2
                ),
                get_device_capability=lambda _: (12, 0),
                get_arch_list=lambda: ["sm_120"],
            ),
            device=lambda value: value,
            version=types.SimpleNamespace(cuda="13.0"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "comfy_kitchen.sage_attention",
        types.SimpleNamespace(is_available=lambda _: True),
    )
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path("/proc/self/cgroup"):
            return "0::/"
        if path == Path("/proc/self/mountinfo"):
            return "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw"
        if path == Path("/sys/fs/cgroup/memory.max"):
            return str(59128 * 1024**2)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    evidence = handler.gpu_runtime()
    assert evidence["container_memory_bytes"] == 59128 * 1024**2
    assert evidence["host_memory_bytes"] == 512 * 1024**3
    installed["torch"] = "2.10.0+cu130"
    with pytest.raises(handler.GpuRejection, match="library_versions_mismatch"):
        handler.gpu_runtime()


def test_probe_uses_bounded_subprocess_without_provider_key(monkeypatch, hardware):
    monkeypatch.setenv("RUNPOD_AI_API_KEY", "private")

    def run(command, **options):
        assert command[:2] == [sys.executable, "-c"]
        assert options["timeout"] == 30
        assert "RUNPOD_AI_API_KEY" not in options["env"]
        return types.SimpleNamespace(stdout=json.dumps({"hardware": hardware}))

    monkeypatch.setattr(handler.subprocess, "run", run)
    assert handler.probe_gpu() == hardware


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_memory_probe_resolves_process_and_stricter_ancestor_limit(tmp_path, monkeypatch, version):
    mount = tmp_path / "cgroup"
    group = mount / "parent" / "worker"
    group.mkdir(parents=True)
    filename = "memory.max" if version == "v2" else "memory.limit_in_bytes"
    (mount / filename).write_text("max" if version == "v2" else str(2**63 - 4096))
    (group.parent / filename).write_text(str(62_000_000_000))
    (group / filename).write_text(str(128 * 1024**3))
    group_line = "0::/parent/worker" if version == "v2" else "5:memory:/parent/worker"
    filesystem = "cgroup2 cgroup rw" if version == "v2" else "cgroup cgroup rw,memory"
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path("/proc/self/cgroup"):
            return group_line
        if path == Path("/proc/self/mountinfo"):
            return f"1 0 0:1 / {mount} rw - {filesystem}"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert handler.container_memory_limit(512 * 1024**3) == (62_000_000_000, version)
    # A missing controller cannot be mistaken for unlimited host memory.
    (group / filename).unlink()
    with pytest.raises(handler.GpuRejection, match="memory_limit_unknown") as failure:
        handler.container_memory_limit(512 * 1024**3)
    assert failure.value.diagnostic["probe_exception_type"] == "FileNotFoundError"
    assert str(tmp_path) not in json.dumps(failure.value.diagnostic)


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("private_namespace", [False, True])
def test_memory_probe_resolves_mounted_container_root_and_explicit_unlimited(
    tmp_path, monkeypatch, version, private_namespace
):
    mount = tmp_path / "cgroup"
    mount.mkdir()
    filename = "memory.max" if version == "v2" else "memory.limit_in_bytes"
    (mount / filename).write_text("max" if version == "v2" else str(2**63 - 4096))
    path = "/" if private_namespace else "/docker/worker"
    root = "/" if private_namespace else "/docker/worker"
    group_line = f"0::{path}" if version == "v2" else f"5:memory:{path}"
    filesystem = "cgroup2 cgroup rw" if version == "v2" else "cgroup cgroup rw,memory"
    original_read = Path.read_text

    def read(file, *args, **kwargs):
        if file == Path("/proc/self/cgroup"):
            return group_line
        if file == Path("/proc/self/mountinfo"):
            return f"1 0 0:1 {root} {mount} rw - {filesystem}"
        return original_read(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert handler.container_memory_limit(512 * 1024**3) == (512 * 1024**3, version)


@pytest.mark.parametrize("root", ["/..", "/docker/worker"])
def test_memory_probe_rejects_unresolved_namespace_instead_of_reading_host_limit(monkeypatch, root):
    def read(path, *_, **__):
        if path == Path("/proc/self/cgroup"):
            return "0::/"
        if path == Path("/proc/self/mountinfo"):
            return f"1 0 0:1 {root} /sys/fs/cgroup rw - cgroup2 cgroup rw"
        pytest.fail("An unresolved namespace must not read an ancestor's memory limit")

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(handler.GpuRejection, match="memory_limit_unknown"):
        handler.container_memory_limit(512 * 1024**3)


def test_probe_failure_keeps_sanitized_partial_hardware_across_subprocess(monkeypatch, hardware):
    secret = "private prompt and token must not leave probe"
    failure = handler.GpuRejection(
        "memory_limit_unknown", hardware | {"prompt": secret}, FileNotFoundError(secret)
    )
    monkeypatch.setattr(handler, "gpu_runtime", lambda: (_ for _ in ()).throw(failure))
    child = handler.gpu_probe_result()
    monkeypatch.setattr(
        handler.subprocess, "run", lambda *_, **__: types.SimpleNamespace(stdout=json.dumps(child))
    )
    with pytest.raises(handler.GpuRejection) as propagated:
        handler.probe_gpu()
    assert propagated.value.diagnostic == failure.diagnostic
    assert propagated.value.diagnostic["probe_exception_type"] == "FileNotFoundError"
    assert secret not in json.dumps(child)


@pytest.mark.parametrize("failure", ["timeout", "crash", "invalid_output"])
def test_probe_execution_failures_never_persist_raw_process_output(monkeypatch, failure):
    secret = "private prompt and token must not leave probe"

    def run(*_, **__):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(secret, 30, output=secret, stderr=secret)
        if failure == "crash":
            raise subprocess.CalledProcessError(1, secret, output=secret, stderr=secret)
        return types.SimpleNamespace(stdout=secret)

    monkeypatch.setattr(handler.subprocess, "run", run)
    with pytest.raises(handler.GpuRejection) as error:
        handler.probe_gpu()
    assert error.value.diagnostic["reason"] in {
        "probe_timeout",
        "probe_process_failed",
        "probe_output_invalid",
    }
    assert secret not in json.dumps(error.value.diagnostic)


@pytest.mark.parametrize("stalled", [False, True])
def test_boot_watchdog_kills_stalled_download_but_is_disarmed_for_sessions(tmp_path, stalled):
    script = """
import os, sys, time, types
from imprint_worker import handler
sys.modules['psutil'] = types.SimpleNamespace(
    virtual_memory=lambda: types.SimpleNamespace(total=1)
)
handler.BOOT_TIMEOUT_SECONDS = 0.05
os.environ['IMPRINT_MODELS_ROOT'] = sys.argv[1]
os.environ.pop('IMPRINT_MODELS_STAGE', None)
handler.model_configuration = lambda: ('runtime-hf', {})
models = types.SimpleNamespace(root=handler.LOCAL_MODELS_ROOT)
handler.ensure_weights = lambda *_: (time.sleep(10 if sys.argv[2] == 'stall' else 0) or models, {})
handler.start_engine = lambda *_: object()
handler.stop_engine = lambda *_: None
handler.serve_sessions = lambda *_: time.sleep(0.15)
handler.serve(types.SimpleNamespace(root=sys.argv[1]))
print('session finished', flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "models"), "stall" if stalled else "ready"],
        env={**os.environ, "PYTHONPATH": str(Path(handler.__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        timeout=15,
    )
    if stalled:
        assert result.returncode == 1
        assert result.stderr.strip() == '{"error":"worker_boot_timeout"}'
        assert "session finished" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert "session finished" in result.stdout


def test_attempt_watchdog_bounds_stalled_work_outside_the_graph(tmp_path):
    script = """
import time
from imprint_worker import client, lifecycle
client.deadline = lambda _seconds, code: lifecycle.deadline(0.05, code)
worker = client.Client('https://example.test', 'session', 'token')
worker._execute = lambda _: time.sleep(10)
worker.execute({})
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(Path(handler.__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == '{"error":"worker_attempt_timeout"}'


@pytest.mark.parametrize("configuration", [False, True])
def test_model_boot_failure_registers_sanitized_failure_without_engine_or_lease(
    tmp_path, monkeypatch, configuration
):
    def failure(*_):
        raise handler.assets.ModelError(
            "release_configuration_invalid" if configuration else "weight_integrity_invalid"
        )

    monkeypatch.setattr(
        handler,
        "model_configuration",
        failure
        if configuration
        else lambda: (MODEL_BOOT["weight_source"], MODEL_BOOT["model_release"]),
    )
    monkeypatch.setattr(handler, "ensure_weights", failure)
    # The engine starts beside verification; a failed verification must stop it before the
    # failure registers, and a configuration failure never starts it.
    engine = types.SimpleNamespace(poll=lambda: None)
    events = []
    monkeypatch.setattr(
        handler,
        "start_engine",
        (lambda *_: pytest.fail("Engine started"))
        if configuration
        else (lambda *_: events.append("started") or engine),
    )
    monkeypatch.setattr(handler, "stop_engine", lambda process: events.append(("stopped", process)))
    monkeypatch.setattr(handler, "Client", lambda *a, **k: pytest.fail("Client created"))
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace())
    responses = []

    def register(config):
        events.append("registered")
        responses.append(config["handler"]({"input": {"secret": "private"}}))

    monkeypatch.setitem(
        sys.modules,
        "runpod",
        types.SimpleNamespace(serverless=types.SimpleNamespace(start=register)),
    )
    with Runtime(tmp_path / "runtime") as runtime:
        runtime.clear()
        handler.serve(runtime)
    assert responses[0]["error"] == (
        "worker_model_configuration_invalid" if configuration else "worker_model_cache_invalid"
    )
    assert responses[0]["refresh_worker"] is True
    assert "private" not in json.dumps(responses)
    assert events == (
        ["registered"] if configuration else ["started", ("stopped", engine), "registered"]
    )


@pytest.mark.parametrize("verified_root", ["engine", "elsewhere"])
def test_engine_starts_during_verification_and_serves_only_the_verified_root(
    tmp_path, monkeypatch, verified_root
):
    import threading

    started = threading.Event()
    engine = types.SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(
        handler,
        "model_configuration",
        lambda: (MODEL_BOOT["weight_source"], MODEL_BOOT["model_release"]),
    )

    def start_engine(root, _runtime):
        assert root == handler.LOCAL_MODELS_ROOT
        started.set()
        return engine

    def ensure_weights(*_):
        assert started.wait(5), "the engine start waited for verification"
        root = handler.LOCAL_MODELS_ROOT if verified_root == "engine" else tmp_path
        return verified_models(root), {}

    monkeypatch.setattr(handler, "start_engine", start_engine)
    monkeypatch.setattr(handler, "ensure_weights", ensure_weights)
    stopped, served, responses = [], [], []
    monkeypatch.setattr(handler, "stop_engine", lambda process: stopped.append(process))
    monkeypatch.setattr(
        handler, "serve_sessions", lambda _runtime, process, _boot, _models: served.append(process)
    )
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        types.SimpleNamespace(virtual_memory=lambda: types.SimpleNamespace(total=1)),
    )
    monkeypatch.setitem(
        sys.modules,
        "runpod",
        types.SimpleNamespace(
            serverless=types.SimpleNamespace(
                start=lambda config: responses.append(config["handler"]({"input": {}}))
            )
        ),
    )
    with Runtime(tmp_path / "runtime") as runtime:
        runtime.clear()
        handler.serve(runtime)
    if verified_root == "engine":
        assert served == [engine] and stopped == [engine] and responses == []
    else:
        assert served == [] and stopped == [engine]
        assert responses[0]["error"] == "worker_model_cache_invalid"
        assert responses[0]["model_failure"] == {"reason": "weight_path_invalid"}


@pytest.mark.parametrize("change", ["release", "source", "missing", "cache", "boolean_version"])
def test_flashboot_rechecks_release_and_cache_before_any_new_client(tmp_path, monkeypatch, change):
    monkeypatch.setattr(handler, "check_gpu", lambda _: {"gpu_id": "verified"})
    monkeypatch.setattr(
        handler,
        "model_configuration",
        lambda: (MODEL_BOOT["weight_source"], MODEL_BOOT["model_release"]),
    )
    engine = types.SimpleNamespace(exited=False)
    engine.poll = lambda: 0 if engine.exited else None
    monkeypatch.setattr(handler, "stop_engine", lambda _: setattr(engine, "exited", True))
    monkeypatch.setattr(handler.Comfy, "idle", lambda _: True)
    monkeypatch.setattr(handler.Comfy, "clear_history", lambda _: None)
    clients = []
    monkeypatch.setattr(handler.Client, "drain", lambda _: clients.append(True) or 1)
    models = verified_models(tmp_path)
    responses = []

    def start(config):
        payload = {
            **MODEL_BOOT,
            "origin": "https://example.test",
            "session_id": "session",
            "token": "token",
        }
        job = {"id": "provider", "input": payload}
        responses.append(config["handler"](job))
        if change == "release":
            payload["model_release"] = {**MODEL_BOOT["model_release"], "asset_set_sha256": "b" * 64}
        elif change == "source":
            payload["weight_source"] = "native-cache"
        elif change == "missing":
            del payload["model_release"]
        elif change == "boolean_version":
            payload["model_release"] = {**MODEL_BOOT["model_release"], "version": True}
        else:

            def lost():
                raise handler.assets.ModelError("weight_changed")

            models.check_unchanged = lost
        responses.append(config["handler"](job))
        assert engine.exited

    monkeypatch.setitem(
        sys.modules, "runpod", types.SimpleNamespace(serverless=types.SimpleNamespace(start=start))
    )
    with Runtime(tmp_path / "runtime") as runtime:
        runtime.clear()
        handler.serve_sessions(runtime, engine, MODEL_BOOT, models)
    assert clients == [True]
    assert responses[0]["attempts"] == 1
    assert responses[1]["error"] == (
        "worker_model_cache_invalid" if change == "cache" else "worker_model_configuration_invalid"
    )


def test_low_scratch_cannot_begin_native_verification_or_runtime_download(tmp_path, monkeypatch):
    monkeypatch.setattr(handler.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=1024))
    monkeypatch.setattr(handler.assets, "native_models", lambda *a, **k: pytest.fail("Cache read"))
    monkeypatch.setattr(handler.assets, "fetch", lambda *a, **k: pytest.fail("Download"))
    for source in ("native-cache", "runtime-hf"):
        with pytest.raises(handler.assets.ModelError, match="scratch_space_insufficient"):
            handler.ensure_weights(source, {}, tmp_path)


def test_engine_environment_excludes_acquisition_credentials(monkeypatch):
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN", "RUNPOD_API_KEY"):
        monkeypatch.setenv(name, "secret")
    assert "secret" not in handler.child_environment().values()


def test_runtime_preflight_reserves_all_missing_weights_and_eight_gib_scratch(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(handler, "LOCAL_MODELS_ROOT", tmp_path / "models")
    monkeypatch.setattr(
        handler.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=8 * 1024**3)
    )
    monkeypatch.setattr(handler.assets, "fetch", lambda *a, **k: pytest.fail("Download"))
    with pytest.raises(handler.assets.ModelError, match="scratch_space_insufficient"):
        handler.ensure_weights("runtime-hf", MODEL_BOOT["model_release"], tmp_path)


def test_deployment_model_configuration_must_match_image_pin_and_source(tmp_path, monkeypatch):
    record = {
        "version": 1,
        "asset_set_sha256": handler.assets.asset_set_sha256(handler.TIER_RECIPES.values()),
        "repo": "org/release",
        "revision": "a" * 40,
    }
    path = tmp_path / "model-release.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(handler, "MODEL_RELEASE_PATH", path)
    monkeypatch.setenv("IMPRINT_WEIGHT_SOURCE", "native-cache")
    monkeypatch.setenv("IMPRINT_MODEL_RELEASE", json.dumps(record))
    monkeypatch.setenv("IMPRINT_ENGINE_ARGS", "--fast-disk")
    for name in ("IMPRINT_MODELS_ROOT", "IMPRINT_MODELS_STAGE", "IMPRINT_WEIGHTS_BUNDLED"):
        monkeypatch.delenv(name, raising=False)
    assert handler.model_configuration() == ("native-cache", record)
    monkeypatch.setenv("IMPRINT_MODEL_RELEASE", json.dumps(record | {"revision": "b" * 40}))
    with pytest.raises(handler.assets.ModelError, match="configuration_invalid"):
        handler.model_configuration()
    runtime = record | {"repo": None, "revision": None}
    monkeypatch.setenv("IMPRINT_WEIGHT_SOURCE", "runtime-hf")
    monkeypatch.setenv("IMPRINT_MODEL_RELEASE", json.dumps(runtime))
    monkeypatch.setenv("IMPRINT_MODELS_ROOT", str(handler.LOCAL_MODELS_ROOT))
    assert handler.model_configuration() == ("runtime-hf", runtime)
    monkeypatch.setenv("IMPRINT_MODELS_STAGE", "/unapproved-copy")
    with pytest.raises(handler.assets.ModelError, match="configuration_invalid"):
        handler.model_configuration()


def test_boot_permission_probe_opens_engine_aliases_not_only_readable_blob_targets(
    tmp_path, monkeypatch
):
    import hashlib

    blob = tmp_path / "blob"
    blob.write_bytes(b"weights")
    snapshot = tmp_path / "snapshot"
    alias = snapshot / "diffusion_models" / "model"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(blob)
    artifact = {
        "destination": "diffusion_models/model",
        "bytes": 7,
        "sha256": hashlib.sha256(b"weights").hexdigest(),
    }
    verified = handler.assets.VerifiedModels(
        snapshot, [(artifact, blob, handler.assets.verify_artifact(blob, artifact))]
    )
    monkeypatch.setattr(handler.assets, "native_models", lambda *a, **k: (verified, {}))
    monkeypatch.setattr(
        handler.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=16 * 1024**3)
    )
    original_run = subprocess.run
    alias.parent.chmod(0)

    def run(command, **kwargs):
        assert kwargs["user"] == kwargs["group"] == 10001
        assert json.loads(kwargs["input"]) == [str(alias)]
        # Exercise real permission denial as the test's unprivileged identity. The Linux
        # image gate separately checks the production UID 10001 against the real weights.
        if os.getuid() != 0:
            for name in ("user", "group", "extra_groups"):
                kwargs.pop(name)
        return original_run(command, **kwargs)

    monkeypatch.setattr(handler.subprocess, "run", run)
    try:
        with pytest.raises(handler.assets.ModelError, match="weight_unreadable"):
            handler.ensure_weights("native-cache", {}, tmp_path)
    finally:
        alias.parent.chmod(0o755)
