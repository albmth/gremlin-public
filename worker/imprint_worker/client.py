"""Drain backend leases once; never regenerate an attempt after an uncertain completion."""

import json
import math
import threading
import time
import uuid
import zipfile
from contextlib import suppress
from pathlib import Path

import requests

from .assets import ModelError
from .lifecycle import deadline, remove
from .media import InputRejected, sha256
from .runner import run

TIMING_KEYS = {
    "preparation_seconds",
    "tracking_seconds",
    "generation_seconds",
    "restoration_seconds",
    "encoding_seconds",
    "total_seconds",
    "model_file_load_seconds",
    "model_device_load_seconds",
    "engine_first_use",
    "input_transfer_seconds",
    "delivery_seconds",
    "attempt_seconds",
}


class Client:
    def __init__(
        self,
        origin,
        session_id,
        token,
        *,
        provider_job_id=None,
        jobs_root="/work/imprint-runtime/jobs",
        comfy_root="/work/imprint-runtime/engine",
        ownership_fd=None,
        on_lease_lost=lambda: None,
        check_models=lambda: None,
        weight_source=None,
        model_release=None,
    ):
        origin = origin.rstrip("/")
        if not origin.startswith("https://"):
            raise ValueError("Worker control plane requires HTTPS")
        self.base = origin + "/api/imprint/worker/" + session_id
        self.provider_job_id = provider_job_id
        self.executor_id = str(uuid.uuid4())
        self.jobs_root = Path(jobs_root)
        self.comfy_root = Path(comfy_root)
        self.ownership_fd = ownership_fd
        self.on_lease_lost = on_lease_lost
        self.check_models = check_models
        self.weight_source = weight_source
        self.model_release = model_release
        self.session_timings = {"completion_seconds": 0.0, "attempt_seconds": 0.0}
        self.started = set()
        self.http = requests.Session()
        self.http.headers["Authorization"] = "Bearer " + token

    def call(self, method, path, **kwargs):
        timeout = kwargs.pop("timeout", (10, 30))
        for retry in range(3):
            data = kwargs.get("data")
            if hasattr(data, "seek"):
                data.seek(0)
            try:
                response = self.http.request(method, self.base + path, timeout=timeout, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                if retry == 2 or (exc.response is not None and exc.response.status_code < 500):
                    raise
                time.sleep(2**retry)

    def diagnostics(self, base, directory, current, *, failure_code=None):
        archive = directory / "diagnostics.zip"
        record = {
            "schema_version": 1,
            "stage": "failed" if failure_code else current["stage"],
            "timings": {
                name: value
                for name, value in current["timings"].items()
                if name in TIMING_KEYS
                and type(value) in (int, float)
                and math.isfinite(value)
                and value >= 0
            },
        }
        if failure_code:
            record["failure_code"] = failure_code
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("diagnostics.json", json.dumps(record))
        with archive.open("rb") as stream:
            self.call(
                "PUT",
                base + "/results/diagnostics.zip",
                data=stream,
                headers={"Content-Type": "application/octet-stream"},
            )

    def store(self, base, name, path):
        """Send a result straight to storage through a link bound to these exact bytes."""
        digest, size = sha256(path), path.stat().st_size
        for retry in range(3):
            ticket = self.call(
                "POST", f"{base}/results/{name}/upload", json={"sha256": digest, "bytes": size}
            ).json()
            try:
                with path.open("rb") as stream:
                    # The signed link is the authorization; the session token stays here.
                    response = requests.request(
                        ticket["method"],
                        ticket["url"],
                        data=stream,
                        headers=ticket["headers"],
                        timeout=(10, 120),
                    )
                response.raise_for_status()
                return
            except requests.RequestException as exc:
                if retry == 2 or (exc.response is not None and exc.response.status_code < 500):
                    raise
                time.sleep(2**retry)

    def execute(self, attempt):
        # Publication already expires at 15 minutes. Bound the whole attempt to the same
        # allowance, including native input decoding, encoding and slow streaming transfers.
        with deadline(900, "worker_attempt_timeout"):
            self._execute(attempt)

    def _execute(self, attempt):
        attempt_started = time.monotonic()
        attempt_id = attempt["id"]
        uuid.UUID(attempt_id)
        base = f"/attempts/{attempt_id}"
        request = json.loads(attempt["request_json"])
        if attempt_id in self.started:
            raise RuntimeError("An attempt cannot be generated twice")
        self.started.add(attempt_id)
        directory = self.jobs_root / attempt_id
        directory.mkdir(parents=True, exist_ok=False)
        output = directory / "output"
        lock = threading.Lock()
        done = threading.Event()
        heartbeat_failure = []
        current = {
            "stage": attempt["state"],
            "timings": json.loads(attempt.get("timings_json") or "{}"),
        }

        def progress(stage, timings):
            with lock:
                current["stage"] = stage
                current["timings"].update(timings)
                self.call("POST", base + "/progress", json=current)

        def heartbeat():
            while not done.wait(25):
                try:
                    self.check_models()
                    with lock:
                        self.call("POST", base + "/progress", json=current, timeout=(5, 10))
                except (requests.RequestException, OSError, ValueError) as exc:
                    heartbeat_failure.append(
                        exc if isinstance(exc, ModelError) else type(exc).__name__
                    )
                    # Do not spend the rest of an inference after losing ownership. The
                    # handler stops its engine; the controller still reconciles the bill.
                    if not done.is_set():
                        self.on_lease_lost()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self.check_models()
            transfer_started = time.monotonic()
            for kind, limit in [
                ("reference", 10 * 1024**2),
                ("driving", 30 * 1024**2),
            ]:
                path = directory / kind
                with (
                    self.call("GET", base + "/inputs/" + kind, stream=True) as response,
                    path.open("wb") as stream,
                ):
                    size = 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("Input exceeds its byte limit")
                        stream.write(chunk)
                if sha256(path) != request[kind + "_sha256"]:
                    raise ValueError("Input hash mismatch")
            current["timings"]["input_transfer_seconds"] = time.monotonic() - transfer_started
            files, timings = run(
                directory / "reference",
                directory / "driving",
                output,
                self.comfy_root,
                request,
                report=progress,
                ownership_fd=self.ownership_fd,
                retain_review=False,
            )
            if heartbeat_failure:
                raise RuntimeError("Worker lease heartbeat failed")
            progress("finishing", timings)
            delivery_started = time.monotonic()
            for name in files:
                self.check_models()
                self.store(base, name, output / name)
            # Stop heartbeats before final publication, so a late heartbeat cannot regress state.
            done.set()
            thread.join(timeout=70)
            if thread.is_alive() or heartbeat_failure:
                raise RuntimeError("Worker lease heartbeat did not finish cleanly")
            self.check_models()
            current["timings"]["delivery_seconds"] = time.monotonic() - delivery_started
            current["timings"]["attempt_seconds"] = time.monotonic() - attempt_started
            progress("finishing", current["timings"])
            self.diagnostics(base, directory, current)
            self.check_models()
            completion_started = time.monotonic()
            self.call("POST", base + "/complete", json=files)
            self.session_timings["completion_seconds"] += time.monotonic() - completion_started
            self.session_timings["attempt_seconds"] += time.monotonic() - attempt_started
            remove(directory)
        except InputRejected as rejection:
            # A job-level outcome: report it and keep serving the queue with the same engine.
            done.set()
            thread.join(timeout=70)
            if thread.is_alive() or heartbeat_failure:
                raise RuntimeError("Worker lease heartbeat did not finish cleanly") from None
            with suppress(requests.RequestException):
                self.diagnostics(base, directory, current, failure_code="input_rejected")
            self.call(
                "POST",
                base + "/progress",
                json={
                    "stage": "failed",
                    "timings": current["timings"],
                    "failure_code": "input_rejected",
                    "failure_reason": rejection.reason,
                },
            )
            remove(directory)
        except Exception:
            with suppress(requests.RequestException):
                self.diagnostics(base, directory, current, failure_code="worker_attempt_failed")
            done.set()
            thread.join(timeout=70)
            with suppress(requests.RequestException):
                self.call(
                    "POST",
                    base + "/progress",
                    json={"stage": "failed", "timings": current["timings"]},
                )
            if heartbeat_failure and isinstance(heartbeat_failure[0], ModelError):
                # Stopping the engine can surface a transport error first. Preserve the
                # fixed cache reason so the provider receipt identifies the root failure.
                raise heartbeat_failure[0] from None
            raise
        finally:
            done.set()
            thread.join(timeout=70)

    def drain(self):
        """Run queued attempts until the service has none; returns how many were executed.

        An input rejection ends only its own attempt; any other failure ends the session.
        """
        completed = 0
        while True:
            self.check_models()
            # The provider job identity lets the service settle a session whose submission
            # response was lost.
            identity = {"provider_job_id": self.provider_job_id, "executor_id": self.executor_id}
            if self.weight_source is not None:
                identity.update(weight_source=self.weight_source, model_release=self.model_release)
            attempt = self.call("POST", "/lease", json=identity).json()
            if attempt is None:
                return completed
            self.execute(attempt)
            completed += 1
