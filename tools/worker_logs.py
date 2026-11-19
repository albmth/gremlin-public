"""Capture a serverless endpoint's worker list, health and worker logs while a session runs.

RunPod deletes a worker's logs when the worker is removed, so a session that never leases
(image pull or model preparation outlasting the pre-lease bound) leaves no evidence unless the
logs are read while the candidates still exist. Runs on the operator's machine with the
service-side provider key from `.env`; nothing inside a worker holds a credential.
"""

import argparse
import json
import time
from pathlib import Path

import requests
from gpu_watchdog import provider_key

API = "https://api.runpod.io/v2"
JOBS = "https://api.runpod.ai/v2"


def read_log_lines(session, endpoint, worker, seconds):
    """Read the backfilled SSE log stream for a bounded time and return parsed lines."""
    lines = []
    deadline = time.monotonic() + seconds
    try:
        with session.get(
            f"{API}/serverless/{endpoint}/workers/{worker}/logs",
            params={"tail": 5000},
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=(10, 5),
        ) as response:
            response.raise_for_status()
            for raw in response.iter_lines(decode_unicode=True):
                if raw and raw.startswith("data:"):
                    payload = raw[5:].strip()
                    try:
                        lines.append(json.loads(payload))
                    except ValueError:
                        lines.append({"raw": payload})
                if time.monotonic() > deadline:
                    break
    except requests.RequestException as error:
        lines.append({"capture_error": type(error).__name__, "detail": str(error)[:200]})
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--read-seconds", type=float, default=6)
    parser.add_argument("--max-minutes", type=float, default=60)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    http = requests.Session()
    http.headers.update(
        {"Authorization": "Bearer " + provider_key(), "User-Agent": "gremlin-tools"}
    )
    seen = {}
    started = time.time()

    def record(name, entry):
        with (args.output / name).open("a") as stream:
            stream.write(json.dumps({"at": round(time.time(), 3), **entry}) + "\n")

    while (time.time() - started) / 60 < args.max_minutes:
        try:
            workers = http.get(f"{API}/serverless/{args.endpoint}/workers", timeout=30).json()
            health = http.get(f"{JOBS}/{args.endpoint}/health", timeout=30).json()
            # Each endpoint release replaces the retained worker's container after its job,
            # so the release list is read beside the workers to attribute every re-creation.
            releases = http.get(f"{API}/serverless/{args.endpoint}/releases", timeout=30).json()
        except (requests.RequestException, ValueError) as error:
            record("workers.jsonl", {"error": type(error).__name__})
            time.sleep(min(args.interval, 15))
            continue
        rows = releases.get("releases", releases) if isinstance(releases, dict) else releases
        latest = max(rows, key=lambda row: row.get("version", 0), default={}) if rows else {}
        release = {
            "count": len(rows),
            "version": latest.get("version"),
            "created_at": latest.get("createdAt"),
            "fields": sorted({item.get("field") for item in latest.get("diff", [])}),
        }
        record("workers.jsonl", {"health": health, "workers": workers, "release": release})
        summary = {
            w["id"]: (w.get("status"), w.get("version"), w.get("isStale"))
            for w in workers.get("workers", [])
        }
        print(
            json.dumps(
                {
                    "at": round(time.time()),
                    "jobs": health.get("jobs"),
                    "release": release["version"],
                    "workers": summary,
                }
            ),
            flush=True,
        )
        for worker in workers.get("workers", []):
            new = []
            for line in read_log_lines(http, args.endpoint, worker["id"], args.read_seconds):
                key = json.dumps(line, sort_keys=True)
                if key not in seen.setdefault(worker["id"], set()):
                    seen[worker["id"]].add(key)
                    new.append(line)
            if new:
                record(
                    f"worker-{worker['id']}.jsonl", {"status": worker.get("status"), "lines": new}
                )
                print(
                    json.dumps({"worker": worker["id"], "new_lines": len(new), "last": new[-1]}),
                    flush=True,
                )
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
