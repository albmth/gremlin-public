"""Time one real job from the client's create call to the downloadable result.

Runs the example client's flow against a deployed environment and records every observable
transition with a wall-clock stamp: capabilities, create, uploads, submit, each state change,
`outcome.at`, the verified download and the diagnostics timings. The record is what a latency
claim must be made from; nothing here provisions, patches or spends on its own, the controller
decides whether and when to dispatch.

    uv run python tools/latency_bench.py --base https://<origin> --token-file <credential.json> \\
        --request tests/fixtures/requests/enzo.json --reference <png> --driving <mp4> \\
        --name e1 --key enzo-bench-20261030-s1 --output artifacts/<campaign>/s1
"""

import argparse
import json
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from gremlin_client import Gremlin, sha256_file  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--driving", type=Path, required=True)
    parser.add_argument("--name", required=True, help="short label written into every event")
    parser.add_argument("--key", required=True, help="a new idempotency key for this run")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--give-up-after", type=int, default=1500)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    events = (args.output / f"{args.name}-events.jsonl").open("a")

    def event(what, **fields):
        record = {"t": round(time.time(), 3), "name": args.name, "event": what, **fields}
        events.write(json.dumps(record) + "\n")
        events.flush()
        print(json.dumps(record), flush=True)

    client = Gremlin(args.base, json.loads(args.token_file.read_text())["token"])
    intent = json.loads(args.request.read_text()) | {
        "idempotency_key": args.key,
        "reference_sha256": sha256_file(args.reference),
        "driving_sha256": sha256_file(args.driving),
    }
    capabilities = client.capabilities()
    event("capabilities", admission=capabilities["admission"], build=capabilities.get("build"))
    if not capabilities["admission"]["accepting"]:
        raise SystemExit(f"not accepting: {capabilities['admission']['reason_code']}")
    event("create", tier=intent["tier"], key=args.key)
    job = client.create(intent)
    event("created", id=job["id"], state=job["state"], created_at=job.get("created_at"))
    if job["state"] == "uploading":
        for kind, path in (("reference", args.reference), ("driving", args.driving)):
            client.upload(job["id"], kind, path)
            event("uploaded", id=job["id"], kind=kind)
        job = client.submit(job["id"])
        event("submitted", id=job["id"], state=job["state"])
    last = None

    def progress(snapshot):
        nonlocal last
        stage = (snapshot.get("progress") or {}).get("stage") or snapshot["state"]
        marker = (snapshot["state"], stage)
        if marker != last:
            last = marker
            event(
                "state",
                id=snapshot["id"],
                state=snapshot["state"],
                stage=stage,
                queue_position=(snapshot.get("progress") or {}).get("queue_position"),
            )

    job = client.wait(job["id"], give_up_after=args.give_up_after, on_progress=progress)
    outcome = job.get("outcome") or {}
    if job["state"] == "succeeded":
        files = client.results(job)
        target = args.output / args.name
        target.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (target / name).write_bytes(data)
        event("stored", id=job["id"], files=sorted(files))
        client.saved(job["id"])
    diagnostics = None
    try:
        response = client.call("GET", f"/api/imprint/jobs/{job['id']}/diagnostics", attempts=2)
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            diagnostics = json.loads(archive.read("diagnostics.json"))
        record = args.output / f"{args.name}-diagnostics.json"
        record.write_text(json.dumps(diagnostics, indent=1))
    except Exception as error:  # noqa: BLE001 - the record is complete without diagnostics
        event("diagnostics_unavailable", error=type(error).__name__)
    ready_at = outcome.get("at")
    event(
        "done",
        id=job["id"],
        state=job["state"],
        outcome=outcome,
        created_at=job.get("created_at"),
        created_to_ready_seconds=(
            round(ready_at - job["created_at"], 1) if ready_at and job.get("created_at") else None
        ),
        timings=(diagnostics or {}).get("timings"),
    )
    (args.output / f"{args.name}-job.json").write_text(json.dumps(job, indent=1))
    events.close()
    raise SystemExit(0 if job["state"] == "succeeded" else 1)


if __name__ == "__main__":
    main()
