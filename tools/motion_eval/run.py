"""Fenced paid execution: one intent per call, no blind re-POST, every call under the cap."""

import base64
import os
import sys
import time
import uuid

import requests
from imprint_worker.media import sha256

from tools.hosted_eval.budget import Budget
from tools.hosted_eval.ledger import Hold, Ledger, write_json
from tools.hosted_eval.providers import Client, HTTPFailure, download, validate

from .matrix import MODELS, body, cell, inputs_for, list_usd, reserve_usd
from .prepare import verify

HARD_CAP = "6.00"
SCOPE = "Motion fidelity evaluation of 29 October 2026"
MEDIA_LIMIT = 6_000_000
POLL_TIMEOUT = 1500


def media_uri(path):
    data = path.read_bytes()
    if len(data) > MEDIA_LIMIT:
        raise Hold(f"{path.name} exceeds the inline media limit")
    kind = "image/png" if path.suffix == ".png" else "video/mp4"
    return f"data:{kind};base64," + base64.b64encode(data).decode()


def budget(root):
    return Budget(root, Ledger(root), hard_cap=HARD_CAP, scope=SCOPE)


def build(root, cell_id, seed=42):
    config = cell(cell_id)
    motion = verify(root)
    fixture = config["fixture"]
    directory = root / "inputs" / fixture
    names = inputs_for(config)
    media = {name: media_uri(directory / name) for name in names}
    files = {
        f"inputs/{fixture}/{name}": motion["hashes"][f"inputs/{fixture}/{name}"] for name in names
    }
    frames = motion["fixtures"][fixture]["model_frames"]
    return (
        body(config, motion["fixtures"][fixture]["prompt"], media, seed),
        files,
        list_usd(config["model"], frames),
        reserve_usd(config["model"], frames),
    )


def client_for(model):
    return Client("fal", os.environ.get("FAL_KEY"), MODELS[model]["endpoint"])


def transport_probe(root, cell_id):
    """Submit the full-size body with an invalid enum, then cancel it at once.

    fal accepts the queue submission before validating it, so the probe records the request,
    cancels it immediately and reads its final status; an invalid request never produces an
    output and is not billed, but the record stays in the ledger for reconciliation.
    """
    config = cell(cell_id)
    request, _, _, _ = build(root, cell_id)
    request["resolution"] = "not-a-resolution"
    client = client_for(config["model"])
    ledger = Ledger(root)
    try:
        record = client.submit(request)
    except HTTPFailure as error:
        ledger.append("transport_probe", cell=cell_id, status=error.status)
        if error.status == 422:
            return {"http": error.status}
        raise Hold(f"Transport probe for {cell_id} answered HTTP {error.status}") from None
    ledger.append("transport_probe", cell=cell_id, status=200, record=record)
    outcome = {"http": 200, "request_id": record.get("request_id")}
    try:
        client.http("PUT", record["cancel_url"])
        outcome["cancelled"] = True
    except (HTTPFailure, KeyError) as error:
        outcome["cancelled"] = str(error)
    time.sleep(3)
    try:
        status = client.http("GET", record["status_url"])
    except HTTPFailure as error:
        status = {"status": f"HTTP {error.status}"}
    outcome["status"] = status.get("status")
    if status.get("status") == "COMPLETED":
        # fal validates inside the model app: an invalid body completes at once with a 422
        # response and no output. A 200 response here would mean the invalid body ran.
        try:
            client.http("GET", record["response_url"])
            outcome["response"] = 200
        except HTTPFailure as error:
            outcome["response"] = error.status
    ledger.append("transport_probe_result", cell=cell_id, **outcome)
    if outcome.get("response") == 200:
        raise Hold(f"Transport probe for {cell_id} produced an output from an invalid body")
    return outcome


def preflight(root, cells=None):
    drifts = []
    for config in [c for c in cell_ids(cells)]:
        client = client_for(config["model"])
        raw, schema, schemas = client.schema()
        write_json(root / "schemas" / f"{config['model']}.json", raw)
        request, _, _, _ = build(root, config["id"])
        try:
            validate(request, schema, schemas)
        except Hold as error:
            drifts.append(config["id"] + ": " + str(error))
    if drifts:
        raise Hold("; ".join(drifts))
    Ledger(root).append("preflight", cells=[c["id"] for c in cell_ids(cells)])


def cell_ids(cells):
    from .matrix import CELLS, FIRST_PASS

    return [c for c in CELLS if (c["id"] in cells if cells else c["id"] in FIRST_PASS)]


def timing(record, submitted, received, downloaded, transitions):
    running = next((e["at"] for e in transitions if e["status"] == "IN_PROGRESS"), None)
    metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
    completed = next((e["at"] for e in transitions if e["status"] == "COMPLETED"), None)
    return {
        "send_at": submitted,
        "accepted_at": received,
        "submission_roundtrip_s": received - submitted,
        "queue_s": max(0.0, running - received) if running else None,
        "inference_s": metrics.get("inference_time")
        or (completed - running if completed and running else None),
        "queue_transitions": transitions,
        **downloaded,
        "total_s": time.time() - submitted,
    }


def execute(root, cell_id, *, simulate=False, seed=42):
    ledger = Ledger(root)
    config = cell(cell_id)
    if ledger.select("saved", cell_id):
        return
    request, files, listed, reserve = build(root, cell_id, seed)
    directory = root / "videos" / cell_id
    directory.mkdir(parents=True, exist_ok=True)
    if simulate:
        ledger.append(
            "simulation",
            cell=cell_id,
            model=config["model"],
            simulated=True,
            input=request,
            files=files,
            list_usd=listed,
            reserve_usd=reserve,
        )
        return
    intents = ledger.select("intent", cell_id)
    terminals = ledger.select("terminal", cell_id)
    if intents and terminals and len(terminals) >= len(intents):
        last = terminals[-1]
        if last["status"] == "succeeded" or not last.get("retryable") or len(intents) >= 2:
            raise Hold(f"{cell_id} already ended ({last['status']}); no automatic regeneration")
        intents = []
    client = client_for(config["model"])
    if intents:
        intent = intents[-1]
        accepted = ledger.select("accepted", cell_id)
        if not accepted or accepted[-1]["attempt"] != intent["attempt"]:
            raise Hold(f"{cell_id} has an uncertain submission; reconcile the fal request log")
        record, submitted, received = accepted[-1]["record"], intent["at"], accepted[-1]["at"]
        attempt = intent["attempt"]
    else:
        if not ledger.select("preflight"):
            raise Hold("Run preflight before any paid call")
        recent = [e for e in ledger.select("terminal")][-2:]
        if len(recent) == 2 and all(e["status"] != "succeeded" for e in recent):
            raise Hold("Two consecutive provider failures; campaign stopped")
        raw, schema, schemas = client.schema()
        validate(request, schema, schemas)
        write_json(directory / "schema.json", raw)
        budget(root).reserve(reserve)
        attempt = str(uuid.uuid4())
        write_json(
            directory / "request.json",
            {
                "model": config["model"],
                "endpoint": MODELS[config["model"]]["endpoint"],
                "input": request,
                "files": files,
                "seed": seed,
            },
        )
        intent = ledger.append(
            "intent",
            cell=cell_id,
            model=config["model"],
            attempt=attempt,
            reserve_usd=reserve,
            list_usd=listed,
            input=request,
            files=files,
            seed=seed,
            retry=bool(terminals),
        )
        submitted = intent["at"]
        try:
            record = client.submit(request)
        except HTTPFailure as error:
            retryable = error.status == 429 or error.status >= 500
            ledger.append(
                "terminal",
                cell=cell_id,
                model=config["model"],
                attempt=attempt,
                status="rejected",
                metric_usd=None,
                retryable=retryable,
                reason=f"HTTP {error.status}",
            )
            if retryable:
                time.sleep(5)
                return execute(root, cell_id, seed=seed)
            raise
        received = time.time()
        ledger.append("accepted", cell=cell_id, attempt=attempt, record=record)
    transitions = []

    def observe(current):
        status = current.get("status", "IN_QUEUE")
        if not transitions or transitions[-1]["status"] != status:
            transitions.append({"status": status, "at": time.time()})
            ledger.append("status", cell=cell_id, attempt=attempt, status=status)

    try:
        record = client.poll(record, observe, timeout=POLL_TIMEOUT)
    except HTTPFailure as error:
        if error.status < 400 or error.status >= 500:
            raise
        # fal completes an input the model app refuses with a 4xx response and no output.
        detail = rejection_detail(client, record)
        ledger.append(
            "terminal",
            cell=cell_id,
            model=config["model"],
            attempt=attempt,
            status="input_rejected",
            metric_usd=None,
            reason=f"HTTP {error.status}: {detail}",
            retryable=False,
        )
        write_json(directory / f"response-{attempt}.json", {"http": error.status, "detail": detail})
        raise Hold(f"{cell_id} rejected by the provider: {detail}") from None
    write_json(directory / f"response-{attempt}.json", record)
    write_json(directory / "response.json", record)
    if record.get("status") != "COMPLETED":
        ledger.append(
            "terminal",
            cell=cell_id,
            model=config["model"],
            attempt=attempt,
            status=record.get("status"),
            metric_usd=None,
            reason=str(record.get("error", "Provider failure"))[:300],
            retryable=True,
        )
        return execute(root, cell_id, seed=seed)
    output = record.get("output", {})
    video = output.get("video") if isinstance(output, dict) else None
    url = video.get("url") if isinstance(video, dict) else None
    if not isinstance(url, str):
        ledger.append(
            "terminal",
            cell=cell_id,
            model=config["model"],
            attempt=attempt,
            status="empty_output",
            metric_usd=listed,
            reason="No video in output",
            retryable=False,
        )
        raise Hold(f"{cell_id} returned no video; the charge stays reserved")
    path = directory / "raw.mp4"
    downloaded = download(url, path, client.hosts)
    times = timing(record, submitted, received, downloaded, transitions)
    write_json(directory / "timings.json", times)
    ledger.append(
        "terminal",
        cell=cell_id,
        model=config["model"],
        attempt=attempt,
        status="succeeded",
        metric_usd=listed,
        timings=times,
        output_sha256=sha256(path),
        provider_seed=output.get("seed"),
    )
    ledger.append(
        "saved",
        cell=cell_id,
        model=config["model"],
        attempt=attempt,
        path=str(path.relative_to(root)),
        sha256=sha256(path),
    )


def rejection_detail(client, record):
    """The provider's own message for a refused input, without the echoed request."""
    try:
        response = client.session.get(record["response_url"], timeout=(15, 60))
        detail = response.json().get("detail")
    except (requests.RequestException, ValueError, AttributeError):
        return "no detail"
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg", d)) for d in detail if isinstance(d, dict))
    return str(detail)[:300]


def run(root, cells=None, simulate=False, seed=42):
    """Run each requested cell; a cell the provider rejects does not stop the others."""
    held = []
    for config in cell_ids(cells):
        try:
            execute(
                root,
                config["id"] if seed == 42 else f"{config['id']}-seed{seed}",
                simulate=simulate,
                seed=seed,
            )
        except Hold as error:
            held.append(f"{config['id']}: {error}")
            print("HELD " + held[-1], file=sys.stderr)
    if held:
        raise Hold("; ".join(held))


def settle(root, export):
    """Record posted fal charges from a CSV of cell,request_id,posted_usd beside its export."""
    import csv

    ledger = Ledger(root)
    with export.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise Hold("Empty export cannot be treated as zero cost")
    for row in rows:
        if not {"cell", "request_id", "posted_usd"} <= row.keys():
            raise Hold("Settlement CSV requires cell, request_id, posted_usd")
        matches = [
            e
            for e in ledger.select("accepted", row["cell"])
            if e["record"].get("request_id") == row["request_id"]
        ]
        if not matches:
            raise Hold(f"No accepted submission matches {row['cell']} {row['request_id']}")
        ledger.append(
            "posted",
            cell=row["cell"],
            attempt=matches[-1]["attempt"],
            posted_usd=str(row["posted_usd"]),
            basis=str(export.name),
        )
    write_json(
        root / "settlement.json",
        {e["cell"]: e["posted_usd"] for e in ledger.select("posted")},
    )
