"""Serverless worker: boot the private engine once, then serve sessions without any provider key."""

import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import requests

from . import assets
from .client import Client
from .lifecycle import Runtime, deadline
from .media import cpus
from .recipe import TIER_RECIPES
from .runner import Comfy

ENGINE_URL = "http://127.0.0.1:8188"
BOOT_TIMEOUT_SECONDS = 600
LOCAL_MODELS_ROOT = Path("/opt/ComfyUI/models")
MODEL_RELEASE_PATH = Path(__file__).resolve().parents[1] / "model-release.json"
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
GPU_PROBE_EXCEPTION_TYPES = {
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


def diagnostic_hardware(hardware):
    evidence = {}
    for key in ("vram_bytes", "host_memory_bytes", "container_memory_bytes", "container_cpus"):
        value = hardware.get(key)
        if type(value) is int and 0 <= value < 2**60:
            evidence[key] = value
    name = hardware.get("gpu_name")
    if (
        isinstance(name, str)
        and 0 < len(name) <= 120
        and name.isascii()
        and all(char.isalnum() or char in " -_." for char in name)
    ):
        evidence["gpu_name"] = name
    capability = hardware.get("compute_capability")
    if (
        isinstance(capability, list)
        and len(capability) == 2
        and all(type(part) is int and 0 <= part < 100 for part in capability)
    ):
        evidence["compute_capability"] = capability
    cuda = hardware.get("cuda_version")
    if (
        isinstance(cuda, str)
        and len(cuda) <= 16
        and len(cuda.split(".")) == 2
        and all(part.isascii() and part.isdigit() for part in cuda.split("."))
    ):
        evidence["cuda_version"] = cuda
    if type(hardware.get("attention_available")) is bool:
        evidence["attention_available"] = hardware["attention_available"]
    if hardware.get("cgroup_version") in {"v1", "v2"}:
        evidence["cgroup_version"] = hardware["cgroup_version"]
    return evidence


class GpuRejection(ValueError):
    """Only fixed codes and hardware measurements may leave the private probe process."""

    def __init__(self, reason, hardware=None, exception=None):
        reason = reason if reason in GPU_REJECTION_REASONS else "probe_output_invalid"
        super().__init__(reason)
        self.diagnostic = {"reason": reason}
        if exception is not None:
            name = type(exception).__name__
            self.diagnostic["probe_exception_type"] = (
                name if name in GPU_PROBE_EXCEPTION_TYPES else "UnknownError"
            )
        self.diagnostic["hardware"] = diagnostic_hardware(hardware or {})


def container_memory_limit(host_memory):
    """Resolve the process's memory cgroup and its visible ancestor limits, on v1 or v2."""
    try:
        groups = [line.split(":", 2) for line in Path("/proc/self/cgroup").read_text().splitlines()]
        memory = next((row for row in groups if "memory" in row[1].split(",")), None)
        memory = memory or next((row for row in groups if row[:2] == ["0", ""]), None)
        if memory is None:
            raise ValueError("memory controller unavailable")
        version = "v2" if memory[:2] == ["0", ""] else "v1"
        mounts = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            fields, filesystem = before.split(), after.split()
            if (version == "v2" and filesystem[0] == "cgroup2") or (
                version == "v1"
                and filesystem[0] == "cgroup"
                and "memory" in filesystem[2].split(",")
            ):
                mounts.append((Path(fields[3]), Path(fields[4])))
        if len(mounts) != 1 or ".." in Path(memory[2]).parts:
            raise ValueError("memory cgroup mount unresolved")
        root, mount = mounts[0]
        # An inherited mount may expose /.. after entering a private cgroup namespace.
        # Reading that mount's root would mistake an ancestor's allowance for our own.
        if ".." in root.parts:
            raise ValueError("memory cgroup namespace unresolved")
        relative = Path(memory[2]).relative_to(root)
        current = mount / relative
        filename = "memory.max" if version == "v2" else "memory.limit_in_bytes"
        limits = []
        while True:
            # A cgroup root may omit limits; the process cgroup itself must be readable.
            try:
                raw = (current / filename).read_text().strip()
            except FileNotFoundError:
                if current != mount or not limits:
                    raise
            else:
                if version == "v2" and raw == "max":
                    limits.append(host_memory)
                else:
                    limit = int(raw)
                    if limit <= 0:
                        raise ValueError("invalid memory limit")
                    limits.append(min(host_memory, limit))
            if current == mount:
                return min(limits), version
            current = current.parent
    except (OSError, ValueError, IndexError) as error:
        raise GpuRejection("memory_limit_unknown", exception=error) from None


def boot_deadline(seconds=BOOT_TIMEOUT_SECONDS):
    return deadline(seconds, "worker_boot_timeout")


def gpu_runtime():
    """Read hardware in a short-lived process so its CUDA context cannot consume model VRAM."""
    hardware = {}
    stage = "probe_import_failed"
    try:
        from importlib.metadata import version

        import psutil
        import torch
        from comfy_kitchen.sage_attention import is_available

        stage = "probe_device_failed"
        if torch.cuda.device_count() != 1:
            raise GpuRejection("gpu_count_mismatch")
        device = torch.cuda.get_device_properties(0)
        hardware.update(
            gpu_name=device.name,
            compute_capability=list(torch.cuda.get_device_capability(0)),
            vram_bytes=device.total_memory,
            cuda_version=torch.version.cuda,
            torch_arches=torch.cuda.get_arch_list(),
        )
        stage = "probe_memory_failed"
        hardware["host_memory_bytes"] = psutil.virtual_memory().total
        limit, cgroup_version = container_memory_limit(hardware["host_memory_bytes"])
        hardware.update(
            container_memory_bytes=limit, cgroup_version=cgroup_version, container_cpus=cpus()
        )
        stage = "probe_libraries_failed"
        packages = ("torch", "comfy-kitchen", "triton")
        installed = {name: version(name) for name in packages}
        lock = Path(__file__).resolve().parents[1] / "requirements.lock"
        pinned = dict(
            line.split("==", 1)
            for line in lock.read_text().splitlines()
            if line.split("==", 1)[0] in packages
        )
        if installed != pinned:
            raise GpuRejection("library_versions_mismatch")
        hardware["packages"] = installed
        stage = "probe_attention_failed"
        hardware["attention_available"] = is_available(torch.device("cuda:0"))
        return hardware
    except GpuRejection as error:
        error.diagnostic["hardware"] = diagnostic_hardware(hardware)
        raise
    except Exception as error:
        raise GpuRejection(stage, hardware, error) from None


def gpu_probe_result():
    try:
        return {"hardware": gpu_runtime()}
    except GpuRejection as error:
        return {"rejection": error.diagnostic}


def probe_gpu():
    command = (
        "import contextlib,json,sys; "
        "from imprint_worker.handler import gpu_probe_result; "
        "\nwith contextlib.redirect_stdout(sys.stderr): result=gpu_probe_result()"
        "\nprint(json.dumps(result))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", command],
            env=child_environment(),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise GpuRejection("probe_timeout", exception=error) from None
    except (subprocess.CalledProcessError, OSError) as error:
        raise GpuRejection("probe_process_failed", exception=error) from None
    try:
        payload = json.loads(result.stdout)
        if "rejection" in payload:
            diagnostic = payload["rejection"]
            error = GpuRejection(diagnostic["reason"], diagnostic.get("hardware"))
            if diagnostic.get("probe_exception_type") in GPU_PROBE_EXCEPTION_TYPES:
                error.diagnostic["probe_exception_type"] = diagnostic["probe_exception_type"]
            raise error
        return payload["hardware"]
    except (ValueError, TypeError, KeyError) as error:
        if isinstance(error, GpuRejection):
            raise
        raise GpuRejection("probe_output_invalid", exception=error) from None


# The probe reads fixed facts of this container (card, memory limit, library versions), so one
# successful probe serves every later job of the process: a resumed worker spent 2.6 to 3 s
# per job on it before (29 October 2026). A rejection is never cached.
_probed_hardware = None


def check_gpu(constraints):
    global _probed_hardware
    if not isinstance(constraints, list) or not 1 <= len(constraints) <= 3:
        raise GpuRejection("constraints_invalid")
    for candidate in constraints:
        names = candidate["runtime_gpu_names"]
        capability = candidate["compute_capability"]
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or not name for name in names)
            or not isinstance(capability, list)
            or len(capability) != 2
            or any(type(value) is not int or value < 0 for value in capability)
        ):
            raise GpuRejection("constraints_invalid")
        for field in ("min_vram_gib", "min_host_memory_gib"):
            value = candidate[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise GpuRejection("constraints_invalid")

    if _probed_hardware is None:
        hardware = probe_gpu()
        print(json.dumps({"level": "INFO", "message": json.dumps({"gpu": hardware})}), flush=True)
    else:
        hardware = _probed_hardware
    capability = hardware["compute_capability"]
    arch = "sm_" + "".join(map(str, capability))
    if not hardware["attention_available"]:
        raise GpuRejection("attention_unavailable", hardware)
    if arch not in hardware["torch_arches"]:
        raise GpuRejection("torch_arch_unsupported", hardware)
    cuda_version = tuple(map(int, hardware["cuda_version"].split(".")))
    for candidate in constraints:
        if (
            hardware["gpu_name"] in candidate["runtime_gpu_names"]
            and capability == candidate["compute_capability"]
            and hardware["vram_bytes"] >= candidate["min_vram_gib"] * 1024**3
            and hardware["container_memory_bytes"] >= candidate["min_host_memory_gib"] * 1024**3
            and cuda_version >= tuple(map(int, candidate["min_cuda_version"].split(".")))
        ):
            _probed_hardware = hardware
            return {"gpu_id": candidate["gpu_id"], **hardware}
    raise GpuRejection("configuration_mismatch", hardware)


def child_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("RUNPOD_", "HF_", "HUGGING_FACE_", "HUGGINGFACE_"))
    }


def model_configuration():
    try:
        source = os.environ.get("IMPRINT_WEIGHT_SOURCE")
        release = assets.release_identity(
            json.loads(MODEL_RELEASE_PATH.read_text()), source, TIER_RECIPES.values()
        )
        if (
            not assets.release_matches(
                json.loads(os.environ.get("IMPRINT_MODEL_RELEASE", "null")), release
            )
            or os.environ.get("IMPRINT_ENGINE_ARGS") != "--fast-disk"
            or os.environ.get("IMPRINT_MODELS_STAGE")
            or os.environ.get("IMPRINT_WEIGHTS_BUNDLED")
            or (source == "native-cache" and os.environ.get("IMPRINT_MODELS_ROOT"))
            or (
                source == "runtime-hf"
                and os.environ.get("IMPRINT_MODELS_ROOT") != str(LOCAL_MODELS_ROOT)
            )
        ):
            raise assets.ModelError("release_configuration_invalid")
        return source, release
    except (OSError, ValueError, TypeError):
        raise assets.ModelError("release_configuration_invalid") from None


def models_root(source, release):
    """The directory the engine serves, known before verification so startup can overlap it."""
    if source == "native-cache":
        return assets.snapshot_root(release)[0]
    return LOCAL_MODELS_ROOT


def ensure_weights(source, release, scratch):
    # The native cache is provider-owned and read-only. Only runtime-hf creates directories.
    if shutil.disk_usage(scratch).free < 4 * 1024**3:
        raise assets.ModelError("scratch_space_insufficient")
    if source == "native-cache":
        verified, timings = assets.native_models(release, TIER_RECIPES.values())
    else:
        LOCAL_MODELS_ROOT.mkdir(parents=True, exist_ok=True)
        missing_bytes = sum(
            item["bytes"]
            for item in assets.artifacts(TIER_RECIPES.values())
            if not assets.local_path(LOCAL_MODELS_ROOT, item["destination"]).exists()
        )
        if shutil.disk_usage(LOCAL_MODELS_ROOT).free < missing_bytes + 8 * 1024**3:
            raise assets.ModelError("scratch_space_insufficient")
        try:
            verified, timings = assets.fetch(
                LOCAL_MODELS_ROOT, TIER_RECIPES.values(), download=True
            )
        except assets.ModelError:
            raise
        except Exception:
            raise assets.ModelError("weight_acquisition_failed") from None
        timings["cache_resolve_seconds"] = 0.0
        for _, path, _ in verified.entries:
            path.chmod(0o444)
        verified.entries = [
            (item, path, assets.fingerprint(path.lstat())) for item, path, _ in verified.entries
        ]
    if shutil.disk_usage(scratch).free < 4 * 1024**3:
        raise assets.ModelError("scratch_space_insufficient")
    # The handler is root, while ComfyUI reads as UID 10001. Opening every file with that
    # identity catches inaccessible mounts/ancestor directories before any user lease.
    try:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys; "
                "files=json.load(sys.stdin); "
                "\nfor path in files:\n with open(path,'rb') as stream: stream.read(1)",
            ],
            input=json.dumps(
                [
                    str(verified.root / artifact["destination"])
                    for artifact, _, _ in verified.entries
                ]
            ),
            text=True,
            check=True,
            capture_output=True,
            timeout=30,
            env=child_environment(),
            user=10001,
            group=10001,
            extra_groups=(),
        )
    except (OSError, subprocess.SubprocessError):
        raise assets.ModelError("weight_unreadable") from None
    verified.check_unchanged()
    return verified, timings


def serve_boot_failure(reason, *, configuration=False):
    import runpod

    # Boot failed before Client exists. Return one fixed provider error so the authenticated
    # result creates a dispatch hold instead of being treated as a recoverable capacity miss.
    code = "worker_model_configuration_invalid" if configuration else "worker_model_cache_invalid"
    runpod.serverless.start(
        {
            "handler": lambda _: {
                "error": code,
                "model_failure": {"reason": reason},
                "refresh_worker": True,
            }
        }
    )


def stop_engine(process):
    if process.poll() is None:
        # Shutdown and the failing handler can observe the same exiting child.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=30)


def start_engine(models, runtime):
    # The engine runs unprivileged and never sees the provider's worker variables.
    env = child_environment()
    env["HOME"] = str(runtime.engine / "user")
    env["XDG_CACHE_HOME"] = str(runtime.root / "cache")
    env["HF_HUB_OFFLINE"] = "1"
    env["GREMLIN_ENGINE_TELEMETRY"] = str(runtime.engine / "user" / "model-load-timings.json")
    # Loader flags depend on the card and its host memory: on the RTX 5090 `--fast-disk`
    # removed 12 to 15 s of per-job weight staging (see the validation history), so the endpoint
    # template sets them rather than the image.
    extra = os.environ.get("IMPRINT_ENGINE_ARGS", "").split()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "imprint_worker.engine_telemetry",
            "--listen",
            "127.0.0.1",
            "--port",
            "8188",
            "--disable-all-custom-nodes",
            "--offline",
            "--disable-assets",
            "--input-directory",
            str(runtime.engine / "input"),
            "--output-directory",
            str(runtime.engine / "output"),
            "--user-directory",
            str(runtime.engine / "user"),
            "--temp-directory",
            str(runtime.engine),
            "--preview-method",
            "none",
            "--models-directory",
            str(models),
            *extra,
        ],
        cwd="/opt/ComfyUI",
        env=env,
        user=10001,
        group=10001,
        start_new_session=True,
        pass_fds=(runtime.lock.fileno(),),
        # Engine errors and PNG metadata can contain the prompt, graph or filesystem paths.
        # Numeric run diagnostics are delivered explicitly by Client instead.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("ComfyUI exited during startup")
            try:
                if requests.get(ENGINE_URL + "/system_stats", timeout=2).ok:
                    return process
            except requests.RequestException:
                pass
            time.sleep(2)
        raise TimeoutError("ComfyUI did not become ready within five minutes")
    except BaseException:
        stop_engine(process)
        raise


def serve(runtime):
    import psutil

    started = time.monotonic()
    booted_at = time.time()
    try:
        source, release = model_configuration()
    except assets.ModelError as error:
        serve_boot_failure(error.reason, configuration=True)
        return
    with boot_deadline(BOOT_TIMEOUT_SECONDS), ThreadPoolExecutor(1) as pool:
        failure = starting = None
        try:
            root = models_root(source, release)
            # Importing the engine and initializing CUDA takes 8-10 billed seconds. It lists
            # model names only and reads no weight bytes before a lease, so it runs beside the
            # full hash verification instead of after it.
            starting = pool.submit(start_engine, root, runtime)
            models, timings = ensure_weights(source, release, runtime.root)
            if models.root != root:
                raise assets.ModelError("weight_path_invalid")
        except (assets.ModelError, OSError) as error:
            # Register after disarming the boot watchdog. The provider's job execution limit
            # still bounds this failure-only worker, and no user work can ever be leased.
            failure = error.reason if isinstance(error, assets.ModelError) else "weight_unreadable"
            if starting is not None:
                # A failed start already stopped its own process; the model failure is what
                # the provider must see.
                with suppress(Exception):
                    stop_engine(starting.result())
        else:
            weights_seconds = time.monotonic() - started
            engine = starting.result()
    if failure is not None:
        serve_boot_failure(failure)
        return
    try:
        boot = {
            "booted_at": round(booted_at, 1),
            **{
                key: round(value, 3) if type(value) is float else value
                for key, value in timings.items()
            },
            "weights_seconds": round(weights_seconds, 3),
            "stage_seconds": 0.0,
            "engine_ready_seconds": round(time.monotonic() - started, 3),
            "host_memory_gb": round(psutil.virtual_memory().total / 1e9, 1),
            "cpu_count": os.cpu_count(),
            "container_cpus": cpus(),
            "weight_source": source,
            "model_release": release,
        }
        print(json.dumps(boot), flush=True)
        serve_sessions(runtime, engine, boot, models)
    finally:
        stop_engine(engine)


def serve_sessions(runtime, engine, boot, models):
    import runpod

    served = 0
    active = threading.Lock()
    engine_control = Comfy(runtime.engine, ENGINE_URL)

    def handle(job):
        nonlocal served
        if not active.acquire(blocking=False):
            return {"error": "worker_busy"}
        failure = "worker_session_failed"
        try:
            payload = job["input"]
            failure = "worker_model_configuration_invalid"
            if (
                payload.get("weight_source") != boot["weight_source"]
                or not assets.release_matches(payload.get("model_release"), boot["model_release"])
                or model_configuration() != (boot["weight_source"], boot["model_release"])
            ):
                raise assets.ModelError("release_configuration_invalid")
            failure = "worker_model_cache_invalid"
            models.check_unchanged()
            failure = "worker_session_failed"
            if engine.poll() is not None or not engine_control.idle():
                raise RuntimeError("Engine is unavailable or still active")
            # FlashBoot resumes the existing process without main/startup running again.
            engine_control.clear_history()
            runtime.clear_media()
            failure = "worker_gpu_incompatible"
            hardware = check_gpu(payload.get("gpu_constraints"))
            failure = "worker_session_failed"
            client = Client(
                payload["origin"],
                payload["session_id"],
                payload["token"],
                provider_job_id=job["id"],
                jobs_root=runtime.jobs,
                comfy_root=runtime.engine,
                ownership_fd=runtime.lock.fileno(),
                on_lease_lost=lambda: stop_engine(engine),
                check_models=models.check_unchanged,
                weight_source=boot["weight_source"],
                model_release=boot["model_release"],
            )
            served += 1
            session_started = time.monotonic()
            result = {"attempts": client.drain(), "boot": boot, "jobs_served": served}
            result["session_timings"] = {
                **client.session_timings,
                "session_seconds": round(time.monotonic() - session_started, 3),
            }
            result["gpu"] = hardware
            return result
        except Exception as error:
            rejection = None
            if failure == "worker_gpu_incompatible":
                rejection = (
                    error.diagnostic
                    if isinstance(error, GpuRejection)
                    else GpuRejection("constraints_invalid", exception=error).diagnostic
                )
                print(
                    json.dumps(
                        {
                            "level": "ERROR",
                            "message": json.dumps({"error": failure, "gpu_rejection": rejection}),
                        }
                    ),
                    flush=True,
                )
            # Killing and waiting precedes deletion even after a lost engine or API reply.
            # An uncertain attempt is reconciled at the API and is never regenerated here.
            try:
                stop_engine(engine)
                runtime.clear()
            except Exception:
                return {"error": "worker_cleanup_unconfirmed", "refresh_worker": True}
            result = {"error": failure, "refresh_worker": True}
            if isinstance(error, assets.ModelError):
                result["error"] = (
                    "worker_model_configuration_invalid"
                    if failure == "worker_model_configuration_invalid"
                    else "worker_model_cache_invalid"
                )
                result["model_failure"] = {"reason": error.reason}
            if rejection is not None:
                # RunPod promotes `error` to status.error but retains this nonempty output.
                result["gpu_rejection"] = rejection
            return result
        finally:
            active.release()

    try:
        runpod.serverless.start({"handler": handle})
    finally:
        stop_engine(engine)
        # The main thread can receive SIGTERM while a handler is encoding. Keep ownership
        # until that writer exits. If it cannot exit, terminate this process without cleanup;
        # the next startup can then reclaim the scratch without racing a surviving thread.
        if not active.acquire(timeout=30):
            print(json.dumps({"error": "worker_shutdown_incomplete"}), flush=True)
            os._exit(1)
        try:
            runtime.clear()
        finally:
            active.release()


def main():
    def shutdown(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    try:
        with Runtime() as runtime:
            runtime.clear()
            serve(runtime)
    except Exception:
        print(json.dumps({"error": "worker_startup_or_shutdown_failed"}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
