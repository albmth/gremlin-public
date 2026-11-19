"""The website on this machine: `./web/dev.sh` (or `python -m devtools.local_stack`).

By default it runs real processes over HTTP with no GPU: persistent DynamoDB Local and MinIO
(web/compose.yaml), Gremlin's own local server with its GPU-free simulated worker, and the
website. With `--gremlin-url` the website calls a deployed Gremlin instead, which spends real
GPU money within that Gremlin's own limits. The tests start the same processes through
`gremlin()` and `web()` on the contract suite's in-memory emulators.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
EMULATORS = {
    "AWS_ACCESS_KEY_ID": "gremlinlocal",
    "AWS_SECRET_ACCESS_KEY": "gremlinlocalsecret",
}
# Quotas for local work with the simulated worker, where no GPU money is at stake.
UNLIMITED = {
    "total_generations": 1000,
    "address_hourly_drafts": 1000,
    "address_daily_videos": 1000,
}
# Match the deployed description policy by default. Tests exercise caption budgets separately.
LOCAL_CAPTION = {"caption_model": "fixed"}
# (DynamoDB, S3): the contract suite's in-memory emulators, and the website's persistent ones.
TEST_STORAGE = (
    os.environ.get("CONTRACT_DYNAMODB_ENDPOINT", "http://127.0.0.1:8001"),
    os.environ.get("CONTRACT_S3_ENDPOINT", "http://127.0.0.1:9100"),
)
DEV_STORAGE = ("http://127.0.0.1:8002", "http://127.0.0.1:9102")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GREMLIN", "AWS_")) and k != "VIRTUAL_ENV"
    }


def storage_up(storage):
    try:
        for endpoint in storage:
            requests.get(endpoint, timeout=1)
        return True
    except requests.RequestException:
        return False


class Process:
    def __init__(self, name, cwd, command, env, log, health):
        self.name, self.cwd, self.command, self.env = name, cwd, command, env
        self.log = log
        self.health = health
        self.process = None

    def start(self):
        stdout = self.log.open("ab") if isinstance(self.log, Path) else None
        # Its own process group: stopping it also stops what `uv run` started.
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if stdout:
            stdout.close()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = self.log.read_text() if isinstance(self.log, Path) else ""
                raise RuntimeError(f"{self.name} exited:\n{detail}")
            try:
                if requests.get(self.health, timeout=1).ok:
                    return self
            except requests.RequestException:
                pass
            time.sleep(0.3)
        raise RuntimeError(f"{self.name} did not start")

    def stop(self):
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(10)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)


def gremlin(log, mode, storage, *, port=None, name=None, client="web-local"):
    """Gremlin's own API with its GPU-free worker; returns (process, url, token)."""
    port = port or free_port()
    token = secrets.token_urlsafe(32)
    name = name or "gremlin-webtest-" + uuid.uuid4().hex[:12]
    env = (
        _clean_env()
        | EMULATORS
        | {
            "GREMLIN_ENVIRONMENT": "local",
            "GREMLIN_LISTEN": f"127.0.0.1:{port}",
            "GREMLIN_LOCAL_WORKER": mode,
            "GREMLIN_DYNAMODB_ENDPOINT": storage[0],
            "GREMLIN_S3_ENDPOINT": storage[1],
            "GREMLIN_TABLE_NAME": name,
            "GREMLIN_MEDIA_BUCKET": name,
            "GREMLIN_IMPRINT_DAILY_JOBS": "200",
            "GREMLIN_IMPRINT_GPU_SELECTION_ENABLED": "false",
            "GREMLIN_CLIENT_TOKEN_HASHES": json.dumps(
                {client: hashlib.sha256(token.encode()).hexdigest()}
            ),
        }
    )
    url = f"http://127.0.0.1:{port}"
    process = Process(
        f"gremlin-{mode}",
        ROOT / "control",
        ["uv", "run", "--frozen", "python", "-m", "gremlin_control.local"],
        env,
        log,
        url + "/health",
    )
    return process.start(), url, token


def web(log, gremlin_url, gremlin_token, storage, *, port=None, name=None, **settings):
    """The website's local server; returns (process, origin)."""
    port = port or free_port()
    name = name or "gremlin-web-test-" + uuid.uuid4().hex[:12]
    origin = f"http://127.0.0.1:{port}"
    values = {
        "public_origin": origin,
        "dynamodb_endpoint": storage[0],
        "s3_endpoint": storage[1],
        "table_name": name,
        "media_bucket": name,
        "gremlin_base_url": gremlin_url,
        "gremlin_token": gremlin_token,
    } | settings
    values = {k: v for k, v in values.items() if v is not None}
    env = _clean_env() | EMULATORS | {f"GREMLIN_WEB_{k.upper()}": str(v) for k, v in values.items()}
    env |= {"GREMLIN_WEB_LISTEN": f"127.0.0.1:{port}", "GREMLIN_WEB_SWEEP_SECONDS": "1"}
    process = Process(
        "web",
        ROOT / "web",
        ["uv", "run", "--frozen", "python", "-m", "gremlin_web.local"],
        env,
        log,
        origin + "/health",
    )
    return process.start(), origin


def _start_storage():
    if storage_up(DEV_STORAGE):
        return
    print("Starting the website's local storage (Docker)...", flush=True)
    compose = ["docker", "compose", "-f", str(ROOT / "web/compose.yaml"), "up", "-d"]
    subprocess.run([*compose, "--quiet-pull"], check=True)
    deadline = time.monotonic() + 60
    while not storage_up(DEV_STORAGE):
        if time.monotonic() > deadline:
            raise SystemExit("The local storage did not start; is Docker running?")
        time.sleep(0.5)


def _token(args):
    if args.credential:
        return json.loads(Path(args.credential).read_text())["token"]
    token = os.environ.get("GREMLIN_WEB_GREMLIN_TOKEN", "")
    if not token:
        raise SystemExit("--gremlin-url needs --credential FILE or GREMLIN_WEB_GREMLIN_TOKEN")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    parser.add_argument(
        "--simulate",
        choices=["succeed", "reject", "fail"],
        default="succeed",
        help="what the simulated GPU worker does with every job (without --gremlin-url)",
    )
    parser.add_argument(
        "--gremlin-url", help="a deployed Gremlin API; real GPU jobs within its own limits"
    )
    parser.add_argument(
        "--credential", help="that Gremlin's client credential file (infra/aws/.state)"
    )
    args = parser.parse_args()
    if args.gremlin_url and not args.gremlin_url.startswith("https://"):
        raise SystemExit("--gremlin-url must be an https:// origin")

    _start_storage()
    processes = []
    try:
        if args.gremlin_url:
            gremlin_url, token = args.gremlin_url, _token(args)
            # Real videos are kept apart from simulated ones.
            name, caption, quotas = "gremlin-web", {}, {}
            mode = f"Gremlin at {gremlin_url}: real GPU jobs, within its own spending limits."
        else:
            gremlin_process, gremlin_url, token = gremlin(
                None, args.simulate, DEV_STORAGE, name="gremlin-sim"
            )
            processes.append(gremlin_process)
            name, caption, quotas = "gremlin-web-sim", LOCAL_CAPTION, UNLIMITED
            mode = f"Gremlin's simulated worker ({args.simulate}); no GPU is used."
        web_process, origin = web(
            None,
            gremlin_url,
            token,
            DEV_STORAGE,
            port=args.port,
            name=name,
            **caption,
            **quotas,
        )
        processes.append(web_process)
        lines = [
            "",
            f"  Gremlin web is running at {origin}",
            f"  Generation: {mode}",
            "  Scene descriptions: "
            + (
                "an editable generic instruction (fixed mode), no model call."
                if caption
                else "none configured, so Generate is refused (no paid model runs from here)."
            ),
            "  History is kept in Docker volumes (web/compose.yaml). Ctrl-C stops.",
            "",
        ]
        print("\n".join(lines), flush=True)
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for process in reversed(processes):
            process.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
