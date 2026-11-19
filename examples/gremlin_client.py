"""A minimal, complete Gremlin client: create, upload, submit, wait, verify, store, acknowledge.

Only `requests` is required. Copy this file into your application, or run it directly:

    python gremlin_client.py --base https://<origin> --token-file credential.json \\
        --request request.json --reference reference.png --driving driving.mp4 --output out/

`request.json` is the create body without hashes; this script hashes the files you pass.
The contract is docs/api.md; integration rules are docs/integration.md.
This helper is not a durable queue: persist the intent (with its idempotency key) before the
first create, and the job ID before any upload, so a restart resumes the same job.
"""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import requests

RESULTS = ("result.mp4", "result.webp", "poster.jpg")
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
# Gateway failures in front of the API carry no Gremlin error code and are safe to retry.
RETRYABLE = {502, 504}


class GremlinError(Exception):
    """An answer Gremlin refused; `code` is the stable `detail.code`, None for platform errors."""

    def __init__(self, status, code, message, reason=None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.reason = status, code, message, reason


class Gremlin:
    def __init__(self, base, token, *, owner=None):
        """`owner` is the opaque end-user ID a delegated client must send on every call."""
        self.base = base.rstrip("/")
        self.http = requests.Session()
        self.http.headers["Authorization"] = "Bearer " + token
        if owner is not None:
            self.http.headers["X-Gremlin-Owner"] = owner

    def call(self, method, path, *, attempts=6, **kwargs):
        timeout = kwargs.pop("timeout", (10, 60))
        retried_500 = False
        for attempt in range(attempts):
            try:
                response = self.http.request(method, self.base + path, timeout=timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == attempts - 1:
                    raise
            else:
                if response.ok:
                    return response
                detail = None
                try:
                    body = response.json()
                    detail = body.get("detail") if isinstance(body, dict) else body
                except ValueError:
                    pass
                coded = isinstance(detail, dict) and "code" in detail
                retry_500 = response.status_code == 500 and not retried_500
                retried_500 |= retry_500
                retry = (
                    response.status_code in RETRYABLE
                    or (response.status_code in {429, 503} and not coded)
                    or retry_500
                )
                if not retry or attempt == attempts - 1:
                    if coded:
                        raise GremlinError(
                            response.status_code,
                            detail["code"],
                            detail["message"],
                            detail.get("reason"),
                        )
                    raise GremlinError(
                        response.status_code, None, str(detail or response.text[:500])
                    )
            time.sleep(2**attempt)

    # Every call below is safe to repeat with the same arguments.

    def health(self):
        return self.call("GET", "/health", attempts=5, timeout=(3, 5)).json()

    def capabilities(self):
        return self.call("GET", "/api/imprint/capabilities").json()

    def create(self, intent):
        return self.call("POST", "/api/imprint/jobs", json=intent).json()

    def jobs(self, state=None, limit=20):
        params = {"limit": limit} | ({"state": state} if state else {})
        return self.call("GET", "/api/imprint/jobs", params=params).json()["jobs"]

    def job(self, job_id):
        return self.call("GET", f"/api/imprint/jobs/{job_id}", attempts=3, timeout=(5, 30)).json()

    def upload(self, job_id, kind, path, attempts=4):
        """Send the file to storage through a link bound to its exact size and hash."""
        size = os.path.getsize(path)
        for attempt in range(attempts):
            # A link lasts minutes and accepts only these bytes; ask again after any failure.
            link = self.call(
                "POST", f"/api/imprint/jobs/{job_id}/inputs/{kind}/upload", json={"bytes": size}
            ).json()
            try:
                with open(path, "rb") as stream:
                    # No bearer token goes to storage: the signed link is the authorization.
                    response = requests.request(
                        link["method"],
                        link["url"],
                        data=stream,
                        headers=link["headers"],
                        timeout=(10, 300),
                    )
            except (requests.ConnectionError, requests.Timeout):
                if attempt == attempts - 1:
                    raise
            else:
                if response.ok:
                    return {"sha256": sha256_file(path), "bytes": size}
                if response.status_code < 500 or attempt == attempts - 1:
                    raise GremlinError(response.status_code, None, response.text[:500])
            time.sleep(2**attempt)

    def submit(self, job_id):
        return self.call("POST", f"/api/imprint/jobs/{job_id}/submit").json()

    def cancel(self, job_id, *, if_not_started=False):
        params = {"if_not_started": "true"} if if_not_started else None
        return self.call("DELETE", f"/api/imprint/jobs/{job_id}", params=params).json()

    def saved(self, job_id):
        return self.call("POST", f"/api/imprint/jobs/{job_id}/saved").json()

    def wait(self, job_id, *, give_up_after=1800, on_progress=None):
        """Poll until the job ends; returns the last snapshot, ended or not."""
        started = time.monotonic()
        while True:
            job = self.job(job_id)
            if on_progress:
                on_progress(job)
            if job["state"] in TERMINAL or time.monotonic() - started > give_up_after:
                return job
            time.sleep(3 if time.monotonic() - started < 300 else 15)

    def results(self, job):
        """Download all three files and verify each one's size and SHA-256."""
        manifest = job.get("result")
        if not isinstance(manifest, dict) or set(manifest.get("files", {})) != set(RESULTS):
            raise ValueError("Result manifest unavailable or incomplete; failed verification")
        files = {}
        for name, expected in manifest["files"].items():
            # The API redirects to a short-lived storage link; requests drops the bearer token
            # on that cross-host redirect, as every client must.
            data = self.call("GET", f"/api/imprint/jobs/{job['id']}/results/{name}").content
            if (
                len(data) != expected["bytes"]
                or hashlib.sha256(data).hexdigest() != expected["sha256"]
            ):
                raise ValueError(f"{name} failed verification")
            files[name] = data
        return files


def sha256_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(client, intent, reference_path, driving_path, *, store_results, on_progress=None):
    """One generation from normalized inputs; `intent` is already persisted with its key.

    `store_results(job_id, files)` must durably store all three verified files, idempotently
    by job ID, and raise on failure. Returns `(job, files)` on success, `(job, None)` after
    cancellation or expiry and `(job, {"failure_code": ..., "failure_reason": ...})` after a
    failure; `input_rejected` means these inputs cannot work and the user must change them.
    """
    job = client.create(intent)
    if job["state"] == "uploading":
        for kind, path in (("reference", reference_path), ("driving", driving_path)):
            stored = client.upload(job["id"], kind, path)
            if stored["sha256"] != intent[kind + "_sha256"]:
                raise ValueError(f"{kind} file does not match the persisted intent")
        job = client.submit(job["id"])
    job = client.wait(job["id"], on_progress=on_progress)
    if job["state"] == "succeeded":
        files = client.results(job)
        store_results(job["id"], files)
        # Acknowledge only after durable storage; it shortens Gremlin's retention to 24 hours.
        client.saved(job["id"])
        return job, files
    if job["state"] == "failed":
        outcome = job["outcome"]
        return job, {key: outcome.get(key) for key in ("failure_code", "failure_reason")}
    return job, None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="Gremlin origin, for example https://...")
    parser.add_argument("--token-file", type=Path, required=True, help="JSON with a `token`")
    parser.add_argument("--owner", help="End-user ID, required for delegated clients")
    parser.add_argument("--request", type=Path, required=True, help="Create body without hashes")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--driving", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = Gremlin(args.base, json.loads(args.token_file.read_text())["token"], owner=args.owner)
    intent = json.loads(args.request.read_text())
    intent["reference_sha256"] = sha256_file(args.reference)
    intent["driving_sha256"] = sha256_file(args.driving)
    admission = client.capabilities()["admission"]
    if not admission["accepting"]:
        raise SystemExit(f"Gremlin is not accepting jobs: {admission['reason_code']}")

    def store(job_id, files):
        target = args.output / job_id
        target.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (target / name).write_bytes(data)

    def progress(job):
        stage = (job.get("progress") or {}).get("stage") or job["state"]
        print(json.dumps({"id": job["id"], "state": job["state"], "stage": stage}), flush=True)

    job, outcome = run(
        client, intent, args.reference, args.driving, store_results=store, on_progress=progress
    )
    print(json.dumps({"job": job["id"], "state": job["state"], "outcome": job["outcome"]}))
    if job["state"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
