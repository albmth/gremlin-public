"""Start the service in its own process and drive it over HTTP only.

Media bytes never pass through the API: `Api.upload_input` and `Worker.upload` ask for a
presigned storage link and send the bytes there, exactly as real clients and workers do.
"""

from __future__ import annotations

import base64
import hashlib
import os
import signal
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 30
EMULATOR_KEYS = {"AWS_ACCESS_KEY_ID": "gremlinlocal", "AWS_SECRET_ACCESS_KEY": "gremlinlocalsecret"}


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Backend:
    def __init__(self, workdir, runpod_url, settings):
        self.target = "local"
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = workdir / "service.log"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GREMLIN_", "AWS_")) and key != "VIRTUAL_ENV"
        }
        env |= {
            "GREMLIN_ENVIRONMENT": "test",
            "GREMLIN_TEST_HOOKS": "1",
            "GREMLIN_LISTEN": f"127.0.0.1:{self.port}",
            "GREMLIN_RUNPOD_MANAGEMENT_URL": runpod_url + "/mgmt/v2",
            "GREMLIN_RUNPOD_JOBS_URL": runpod_url + "/jobs/v2",
        }
        env |= {f"GREMLIN_{key.upper()}": value for key, value in settings.items()}
        name = "gremlin-test-" + uuid.uuid4().hex[:16]
        env |= EMULATOR_KEYS | {
            # Shared contract cases exercise the fixed provider boundary; GPU discovery has
            # its own cases with the catalogue enabled.
            "GREMLIN_IMPRINT_GPU_SELECTION_ENABLED": "false",
            "GREMLIN_DYNAMODB_ENDPOINT": os.environ.get(
                "CONTRACT_DYNAMODB_ENDPOINT", "http://127.0.0.1:8001"
            ),
            "GREMLIN_S3_ENDPOINT": os.environ.get("CONTRACT_S3_ENDPOINT", "http://127.0.0.1:9100"),
            "GREMLIN_TABLE_NAME": name,
            "GREMLIN_MEDIA_BUCKET": name,
        }
        self.cwd = ROOT / "control"
        self.env = env
        self.command = ["uv", "run", "--frozen", "python", "-m", "gremlin_control.local"]
        self.process = None

    def start(self):
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                self.command, cwd=self.cwd, env=self.env, stdout=log, stderr=subprocess.STDOUT
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.target} exited during startup:\n{self.log.read_text()}")
            try:
                if requests.get(self.url + "/health", timeout=1).ok:
                    return self
            except requests.RequestException:
                time.sleep(0.1)
        raise RuntimeError(f"{self.target} did not start:\n{self.log.read_text()}")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def restart(self):
        self.stop()
        return self.start()


@dataclass
class Outcome:
    """A transport-neutral answer: the API's status and JSON, or the storage service's."""

    status: int
    body: object = None
    code: str | None = None

    @property
    def ok(self):
        return self.status == 200


def outcome(response):
    try:
        body = response.json()
    except ValueError:
        body = response.text
    code = None
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        code = body["detail"].get("code")
    return Outcome(response.status_code, body, code)


def storage_put(ticket, data):
    """Send bytes to a presigned URL; the API's bearer credential never goes to storage."""
    response = requests.request(
        ticket["method"], ticket["url"], data=data, headers=ticket["headers"], timeout=TIMEOUT
    )
    if response.status_code == 200:
        return Outcome(200, {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    code = (
        response.text.split("<Code>", 1)[-1].split("</Code>", 1)[0]
        if "<Code>" in response.text
        else None
    )
    return Outcome(response.status_code, response.text, code)


@dataclass
class Api:
    url: str
    offset: float = 0.0
    frozen: float | None = None
    tokens: dict = field(default_factory=dict)
    applied: dict = field(default_factory=dict)

    def __post_init__(self):
        self.http = requests.Session()
        self.http.trust_env = False

    def request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", TIMEOUT)
        return self.http.request(method, self.url + path, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)

    # Hooks

    def now(self):
        return self.frozen if self.frozen is not None else time.time() + self.offset

    def clock(self, offset):
        self.offset, self.frozen = offset, None
        assert self.post("/__test/clock", json={"offset_seconds": offset}).status_code == 204

    def freeze(self, at):
        self.frozen = at
        assert self.post("/__test/clock", json={"frozen_at": at}).status_code == 204

    def advance(self, seconds):
        if self.frozen is not None:
            self.freeze(self.frozen + seconds)
        else:
            self.clock(self.offset + seconds)

    def settings(self, **values):
        self.applied |= values
        assert self.post("/__test/settings", json=values).status_code == 204

    def reapply(self):
        """After a restart, restore the in-memory configuration and clock the test set."""
        self.settings(**self.applied)
        if self.frozen is not None:
            self.freeze(self.frozen)
        elif self.offset:
            self.clock(self.offset)

    def seed_job(self, job_id, **clocks):
        response = self.post("/__test/seed-job", json={"job_id": job_id} | clocks)
        assert response.status_code == 204, response.text

    def put_media(self, job_id, kind, data):
        body = {"job_id": job_id, "kind": kind, "data": base64.b64encode(data).decode()}
        assert self.post("/__test/put-media", json=body).status_code == 204

    def fail_next_delete(self, name):
        assert self.post("/__test/fail-next-delete", json={"name": name}).status_code == 204

    def register(self, client="a"):
        token = "test-client-" + client * 32
        self.tokens[client] = hashlib.sha256(token.encode()).hexdigest()
        self.settings(client_token_hashes=self.tokens)
        return {"Authorization": "Bearer " + token}

    def configure_provider(self):
        self.settings(imprint_runpod_key="provider-test-key", imprint_endpoint_id="endpoint123")

    def hook(self, name):
        response = self.post(f"/__test/{name}")
        assert response.status_code == 200, response.text
        return response.json()

    def reconcile(self):
        return self.hook("reconcile")

    def tick(self):
        return self.hook("tick")

    def dispatch(self):
        return self.hook("dispatch")

    def reserve(self, hourly=650_000):
        response = self.post("/__test/reserve", json={"hourly_microusd": hourly})
        assert response.status_code == 200, response.text
        return response.json()["session_id"], response.json()["token"]

    def finish_session(self, session_id, measured, **extra):
        body = {"session_id": session_id, "measured_microusd": measured} | extra
        assert self.post("/__test/finish-session", json=body).status_code == 204

    def release(self, session_id, authorize=True):
        return self.post("/__test/release", json={"session_id": session_id, "authorize": authorize})

    def records(self):
        response = self.get("/__test/records")
        assert response.status_code == 200, response.text
        return response.json()

    def job_row(self, job_id):
        return next((job for job in self.records()["jobs"] if job["id"] == job_id), None)

    def session_row(self, session_id):
        return next(s for s in self.records()["sessions"] if s["id"] == session_id)

    def media(self, job_id):
        return set(self.records()["media"].get(job_id, ()))

    # Client transport

    def upload_input(self, job_id, kind, data, headers):
        path = f"/api/imprint/jobs/{job_id}/inputs/{kind}"
        ticket = outcome(self.post(path + "/upload", json={"bytes": len(data)}, headers=headers))
        if not ticket.ok:
            return ticket
        return storage_put(ticket.body, data)

    def download(self, job_id, kind, headers):
        """Follow the result link; a redirect target must not receive the client's token."""
        return self.get(f"/api/imprint/jobs/{job_id}/results/{kind}", headers=headers)


@dataclass
class Worker:
    api: Api
    session_id: str
    token: str
    executor_id: str = "00000000-0000-4000-8000-000000000001"

    @property
    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    def prefix(self, attempt_id=None):
        base = f"/api/imprint/worker/{self.session_id}"
        return base + (f"/attempts/{attempt_id}" if attempt_id else "")

    def lease(self, provider_job_id=None, executor_id=None):
        body = {"executor_id": executor_id or self.executor_id}
        if provider_job_id:
            body["provider_job_id"] = provider_job_id
        return outcome(self.api.post(self.prefix() + "/lease", json=body, headers=self.headers))

    def progress(self, attempt_id, stage, timings=None, **failure):
        body = {"stage": stage} | ({"timings": timings} if timings is not None else {}) | failure
        return outcome(
            self.api.post(self.prefix(attempt_id) + "/progress", json=body, headers=self.headers)
        )

    def input(self, attempt_id, kind):
        return self.api.get(self.prefix(attempt_id) + f"/inputs/{kind}", headers=self.headers)

    def upload(self, attempt_id, kind, data):
        path = self.prefix(attempt_id) + f"/results/{kind}"
        if kind == "diagnostics.zip":
            return outcome(self.api.put(path, data=data, headers=self.headers))
        ticket = outcome(
            self.api.post(
                path + "/upload",
                json={"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
                headers=self.headers,
            )
        )
        if not ticket.ok:
            return ticket
        return storage_put(ticket.body, data)

    def complete(self, attempt_id, manifest):
        return outcome(
            self.api.post(
                self.prefix(attempt_id) + "/complete", json=manifest, headers=self.headers
            )
        )

    def publish(self, attempt_id, payload=b"transport fixture, not generated media"):
        """Simulate only the worker protocol, never inference."""
        assert self.progress(attempt_id, "finishing").ok
        manifest = {}
        for name in ("result.mp4", "result.webp", "poster.jpg"):
            stored = self.upload(attempt_id, name, payload)
            assert stored.ok, stored
            manifest[name] = stored.body
        completed = self.complete(attempt_id, manifest)
        assert completed.ok, completed
        return manifest
