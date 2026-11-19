"""Fenced still/video execution and explicit posted-charge probe gates."""

import json
import os
import time
import uuid
from datetime import datetime

from imprint_worker.media import sha256
from PIL import Image

from .budget import Budget, money
from .ledger import Hold, Ledger, write_json
from .matrix import CELLS, MODELS, STILLS, price, still_prompt, video_input
from .prepare import canvas, verify
from .providers import Client, HTTPFailure, data_uri, download, validate


def build(root, cell, model, fixture, seed=42, simulate=False):
    inputs = root / "inputs" / fixture
    if model == "E1":
        position = cell.split("-")[-1]
        files = [inputs / f"{position}.png", inputs / "reference.png"]
        body = {
            "prompt": still_prompt(fixture),
            "image_urls": [data_uri(p) for p in files],
            "image_size": {"width": 512, "height": 512},
            "num_inference_steps": 4,
            "seed": seed,
            "num_images": 1,
            "output_format": "png",
            "enable_safety_checker": True,
        }
    else:
        config = next(c for c in CELLS if c["id"] == cell.split("-seed")[0])
        prefix = "KF" if fixture == "kungfu" else "EN"
        first = (
            inputs / "raw-v1.png"
            if config["route"] == "A0"
            else root
            / "stills"
            / f"S-{prefix}-first"
            / ("v1.png" if model == "V1" else "output.png")
        )
        if simulate and not first.exists():
            first = inputs / "first.png"
        files = [first]
        last = root / "stills" / f"S-{prefix}-last" / "output.png"
        if simulate and not last.exists():
            last = inputs / "last.png"
        if config["route"] == "K2":
            files.append(last)
        prompt = json.loads((root / "inputs/manifest.json").read_text())["fixtures"][fixture][
            "request"
        ]["prompt"]
        body = video_input(
            config,
            prompt,
            data_uri(first),
            data_uri(last) if config["route"] == "K2" else None,
            seed,
        )
    return body, {str(p.relative_to(root)): sha256(p) for p in files}


def metric_cost(model, body):
    if model == "E1":
        # Inputs and output are 512x512. Reservation remains the planned $0.03.
        return 3 * 512 * 512 / 1_000_000 * 0.01
    return price(model, "kungfu" if body.get("duration", 4) == 4 else "enzo")


def timing(record, submitted, received, download_record, transitions):
    def stamp(key):
        value = record.get(key)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None

    created, started, completed = [stamp(k) for k in ("created_at", "started_at", "completed_at")]
    inference = record.get("metrics", {}).get("predict_time")
    inference = inference or record.get("metrics", {}).get("inference_time")
    inference = (
        inference or record.get("output", {}).get("metrics", {}).get("inference_time")
        if isinstance(record.get("output"), dict)
        else inference
    )
    running = next((e["at"] for e in transitions if e["status"] == "IN_PROGRESS"), None)
    return {
        "send_at": submitted,
        "accepted_at": received,
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "upload_s": max(0, created - submitted) if created else None,
        "submission_roundtrip_s": received - submitted,
        "queue_s": max(0, started - created)
        if started and created
        else max(0, running - received)
        if running
        else None,
        "inference_s": inference or (completed - started if completed and started else None),
        "queue_transitions": transitions,
        **download_record,
        "total_s": time.time() - submitted,
    }


def probe_gate(ledger, model):
    completed = {e["attempt"] for e in ledger.select("terminal") if e["status"] == "succeeded"}
    calls = [
        e for e in ledger.select("intent") if e["model"] == model and e["attempt"] in completed
    ]
    if not calls:
        return
    probes = [e for e in ledger.select("probe") if e["model"] == model]
    if not probes or not (probes[-1]["within_tolerance"] or probes[-1].get("owner_accepted")):
        url = (
            "https://replicate.com/account/billing"
            if model != "E1"
            else "https://fal.ai/dashboard/billing"
        )
        raise Hold(
            f"{model} billing probe requires posted charge evidence: {url}; "
            "use probe --model ID --probe-confirmed USD --evidence FILE"
        )


def confirm_probe(root, model, amount, evidence, accept_deviation=False):
    ledger = Ledger(root)
    succeeded = {e["attempt"] for e in ledger.select("terminal") if e["status"] == "succeeded"}
    calls = [
        e for e in ledger.select("intent") if e["model"] == model and e["attempt"] in succeeded
    ]
    if not calls:
        raise Hold("Probe has no completed first call")
    if not evidence.is_file() or not evidence.stat().st_size:
        raise Hold("Nonempty provider billing evidence is required")
    amount = money(amount)
    intent = calls[0]
    expected = money(intent["list_usd"])
    within = abs(amount - expected) <= expected * money("0.10")
    ledger.append(
        "posted",
        cell=intent["cell"],
        attempt=intent["attempt"],
        posted_usd=str(amount),
        evidence_sha256=sha256(evidence),
    )
    ledger.append(
        "probe",
        model=model,
        expected_usd=str(expected),
        posted_usd=str(amount),
        within_tolerance=within,
        owner_accepted=bool(accept_deviation),
        evidence=str(evidence.relative_to(root)),
    )
    if not within and not accept_deviation:
        raise Hold("Billing probe differs by more than 10%; model held for owner decision")


def execute(root, cell, model, fixture, simulate=False, seed=42):
    ledger = Ledger(root)
    verify(root)
    if ledger.select("saved", cell):
        return
    body, files = build(root, cell, model, fixture, seed, simulate=simulate)
    directory = root / ("stills" if model == "E1" else "videos") / cell
    directory.mkdir(parents=True, exist_ok=True)
    if simulate:
        ledger.append(
            "simulation",
            cell=cell,
            model=model,
            simulated=True,
            input=body,
            files=files,
            reserve_usd=price(model, fixture),
        )
        return
    config = MODELS[model]
    token = os.environ.get("FAL_KEY" if config["provider"] == "fal" else "REPLICATE_API_TOKEN")
    client = Client(config["provider"], token, config["endpoint"])
    intents = ledger.select("intent", cell)
    if intents and ledger.select("terminal", cell):
        terminal = ledger.select("terminal", cell)[-1]
        if terminal["status"] == "succeeded" or not terminal.get("retryable"):
            raise Hold(f"{cell} already completed; no automatic regeneration")
        if len(intents) >= 2:
            raise Hold(f"{cell} has exhausted its single provider-failure retry")
        if model == "E1":
            retries = [e for e in ledger.select("intent") if e.get("retry") and e["model"] == "E1"]
            if len(retries) >= 2:
                raise Hold("Campaign still retry allowance exhausted")
        intents = []
    if intents:
        intent = intents[-1]
        accepted = ledger.select("accepted", cell)
        if not accepted:
            raise Hold(f"{cell} has uncertain submission; reconcile manually, never re-POST")
        record = accepted[-1]["record"]
        submitted, received = intent["at"], accepted[-1]["at"]
        attempt = intent["attempt"]
    else:
        probe_gate(ledger, model)
        failures = [
            e
            for e in ledger.select("terminal")
            if MODELS[e["model"]]["provider"] == config["provider"]
        ]
        if len(failures) >= 2 and all(e["status"] != "succeeded" for e in failures[-2:]):
            raise Hold("Two consecutive provider failures; campaign stopped")
        raw_schema, schema, schemas = client.schema()
        validate(body, schema, schemas)
        write_json(directory / "schema.json", raw_schema)
        budget = Budget(root, ledger)
        reserve = price(model, fixture)
        budget.reserve(reserve)
        attempt = str(uuid.uuid4())
        write_json(
            directory / "request.json",
            {
                "model": model,
                "endpoint": config["endpoint"],
                "input": body,
                "files": files,
                "seed": seed,
            },
        )
        intent = ledger.append(
            "intent",
            cell=cell,
            model=model,
            attempt=attempt,
            reserve_usd=reserve,
            list_usd=metric_cost(model, body),
            input=body,
            files=files,
            seed=seed,
            retry=bool(ledger.select("intent", cell)),
        )
        write_json(directory / f"request-{attempt}.json", {"input": body, "files": files})
        submitted = intent["at"]
        # Explicit HTTP responses have one bounded retry; uncertain transport has none.
        try:
            record = client.submit(body)
        except HTTPFailure as error:
            retryable = error.status == 429 or error.status >= 500
            ledger.append(
                "terminal",
                cell=cell,
                model=model,
                attempt=attempt,
                status="rejected",
                metric_usd=None,
                retryable=retryable,
                reason=f"HTTP {error.status}",
            )
            if retryable:
                time.sleep(2)
                return execute(root, cell, model, fixture, seed=seed)
            raise
        received = time.time()
        ledger.append("accepted", cell=cell, attempt=attempt, record=record)
    transitions = []

    def observe(current):
        status = current.get("status", "IN_QUEUE")
        if not transitions or transitions[-1]["status"] != status:
            transitions.append({"status": status, "at": time.time()})
            ledger.append("status", cell=cell, attempt=attempt, status=status)

    record = client.poll(record, observe)
    status = record.get("status")
    success = status in {"succeeded", "COMPLETED"}
    write_json(directory / f"response-{attempt}.json", record)
    write_json(directory / "response.json", record)
    if not success:
        ledger.append(
            "terminal",
            cell=cell,
            model=model,
            attempt=attempt,
            status=status,
            metric_usd=None,
            reason=record.get("error", "Provider failure"),
            retryable=status == "failed" or status == "ERROR",
        )
        return execute(root, cell, model, fixture, seed=seed)
    output = record.get("output")
    if model == "E1":
        flags = output.get("has_nsfw_concepts")
        if not isinstance(flags, list) or len(flags) != 1 or any(flags):
            ledger.append(
                "terminal",
                cell=cell,
                model=model,
                attempt=attempt,
                status="safety_held",
                metric_usd=metric_cost(model, body),
                reason="Missing or refused output safety result",
                retryable=False,
            )
            raise Hold("Still safety check missing or refused output")
        urls = [image["url"] for image in output.get("images", [])]
    else:
        urls = [output] if isinstance(output, str) else output
    if not urls or not isinstance(urls[0], str):
        ledger.append(
            "terminal",
            cell=cell,
            model=model,
            attempt=attempt,
            status="empty_output",
            metric_usd=None,
            retryable=model == "E1",
            reason="Empty provider output",
        )
        if model == "E1":
            return execute(root, cell, model, fixture, seed=seed)
        raise Hold("Provider returned empty media; charge remains reserved")
    path = directory / ("output.png" if model == "E1" else "raw.mp4")
    downloaded = download(urls[0], path, client.hosts)
    if model == "E1":
        with Image.open(path) as image:
            if image.size != (512, 512):
                raise Hold("Still geometry differs from request")
        if sha256(path) in files.values():
            raise Hold("Still is byte-identical to an input")
        canvas(path, directory / "v1.png")
    cost = metric_cost(model, body)
    times = timing(record, submitted, received, downloaded, transitions)
    write_json(directory / "timings.json", times)
    ledger.append(
        "terminal",
        cell=cell,
        model=model,
        attempt=attempt,
        status="succeeded",
        metric_usd=cost,
        timings=times,
        output_sha256=sha256(path),
    )
    ledger.append(
        "saved",
        cell=cell,
        model=model,
        attempt=attempt,
        path=str(path.relative_to(root)),
        sha256=sha256(path),
    )
    # The first successful output is downloaded before asking for the billing read.
    probe_gate(ledger, model)


def run_stills(root, simulate=False):
    for cell in STILLS:
        execute(root, cell, "E1", "kungfu" if "-KF-" in cell else "enzo", simulate)


def run_videos(root, cells=None, simulate=False):
    for config in CELLS:
        if cells and config["id"] not in cells:
            continue
        execute(root, config["id"], config["model"], config["fixture"], simulate)
