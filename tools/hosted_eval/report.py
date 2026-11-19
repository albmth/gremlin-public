"""Measured timing and explicitly unresolved billing, without quality judgments."""

import csv
import json
import statistics
from collections import defaultdict

from .budget import Budget, money
from .ledger import Hold, Ledger, write_json
from .matrix import CELLS, MODELS, STILLS


def measure(root):
    ledger, results = Ledger(root), {}
    samples = defaultdict(list)
    for event in ledger.select("terminal"):
        intent = next(e for e in ledger.select("intent") if e["attempt"] == event["attempt"])
        result = {
            "status": event["status"],
            "model": event["model"],
            "list_usd": intent["list_usd"],
            "reserved_usd": intent["reserve_usd"],
            "metric_estimate_usd": event.get("metric_usd"),
            "posted_usd": None,
            "posted_reason": "Awaiting provider usage evidence",
            "timings": event.get("timings"),
        }
        posted = [e for e in ledger.select("posted") if e["attempt"] == event["attempt"]]
        if posted:
            result.update(
                posted_usd=posted[-1]["posted_usd"],
                posted_reason=None,
                posted_basis=posted[-1].get("basis", "Provider billing probe"),
            )
        results[event["cell"]] = result
        if event.get("timings"):
            samples[event["model"]].append(event["timings"])
    write_json(root / "measurements.json", results)
    write_json(
        root / "latency-summary.json",
        {
            model: {
                "samples": len(values),
                "segments": {
                    segment: {
                        "samples": len(observed),
                        "min_s": min(observed),
                        "median_s": statistics.median(observed),
                        "max_s": max(observed),
                    }
                    for segment in ("upload_s", "queue_s", "inference_s", "download_s", "total_s")
                    if (observed := [v[segment] for v in values if v.get(segment) is not None])
                },
                "p95": None,
                "note": "Too few samples for p95; absent segments are unobservable",
            }
            for model, values in samples.items()
        },
    )
    return results


def settle(root, replicate=None, fal=None):
    ledger = Ledger(root)
    for provider, path in [("replicate", replicate), ("fal", fal)]:
        if path is None:
            continue
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        if not rows:
            raise Hold("Empty export cannot be treated as zero cost")
        for row in rows:
            if not {"cell", "request_id", "posted_usd"} <= row.keys():
                raise Hold("Settlement CSV requires cell, request_id, posted_usd")
            accepted = ledger.select("accepted", row["cell"])
            matches = [
                e
                for e in accepted
                if e["record"].get("id", e["record"].get("request_id")) == row["request_id"]
            ]
            if len(matches) != 1:
                raise Hold("Settlement request ID does not match exactly one accepted call")
            event = matches[0]
            intent = next(e for e in ledger.select("intent") if e["attempt"] == event["attempt"])
            if MODELS[intent["model"]]["provider"] != provider:
                raise Hold("Settlement provider mismatch")
            ledger.append(
                "posted",
                cell=row["cell"],
                attempt=event["attempt"],
                posted_usd=str(money(row["posted_usd"])),
                evidence=str(path),
                basis=row.get("basis", "Request-level billing export"),
            )
    results = measure(root)
    write_json(
        root / "settlement.json",
        {
            cell: {
                "posted_usd": value["posted_usd"],
                "reason": value["posted_reason"],
                "basis": value.get("posted_basis"),
            }
            for cell, value in results.items()
        },
    )


def owner_review(root, scores_path, ledger):
    scores = json.loads(scores_path.read_text())
    if scores["run"] != root.name:
        raise Hold("Scores belong to another run")
    criteria = [
        "realism",
        "identity preservation",
        "positioning",
        "lighting",
        "smoothness",
        "movement",
        "overall visual appeal",
    ]
    lines = ["## Owner scores", "", "Missing numeric ratings remain unscored.", ""]
    for fixture in ("kungfu", "enzo"):
        mappings = [e for e in ledger.select("letters") if e["fixture"] == fixture]
        if not mappings:
            continue
        revealed = scores.get("revealed", {}).get(fixture, False)
        lines += [
            f"### {fixture}",
            "",
            "Owner reveal recorded." if revealed else "Model identities remain hidden.",
            "",
            "| Card | Save | Realism | Identity | Position | Lighting | Smoothness | "
            "Movement | Appeal | Notes |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for cell, letter in sorted(mappings[-1]["mapping"].items(), key=lambda item: item[1]):
            score = scores.get("scores", {}).get(f"{fixture}:{letter}", {})
            label = f"{letter} ({cell})" if revealed else letter
            ratings = score.get("ratings", {})
            notes = score.get("notes", "").replace("|", "\\|").replace("\n", "<br>")
            values = [
                label,
                score.get("sticker") or "not answered",
                *(ratings.get(k) or "not scored" for k in criteria),
                notes,
            ]
            lines.append("| " + " | ".join(values) + " |")
        lines.append("")
    return lines


def report(root, scores_path=None):
    results = measure(root)
    ledger = Ledger(root)
    review_lines = owner_review(root, scores_path, ledger) if scores_path else []
    status = (
        "Owner review recorded; missing numeric ratings remain unscored."
        if scores_path
        else "Owner quality judgment is pending."
    )
    lines = [
        "# Phase A architecture comparison",
        "",
        status,
        "No model or architecture is selected.",
        "No Phase B execution or production modification is part of this campaign.",
        "",
        f"Conservative committed cost: USD {Budget(root, ledger).total()}.",
        "Likely total (posted where available, otherwise estimated): "
        f"USD {Budget(root, ledger).likely_total()}.",
        "Unposted charges remain estimates; local simulation is not inference evidence.",
        "Moderation and publication are not executed by this offline evaluation.",
        "Replicate per-cell posted amounts allocate its verified aggregate units at the posted "
        "unit rates; they are not per-prediction invoice lines.",
        "",
        "| Cell | Status | List USD | Metric estimate USD | Posted USD | Total seconds |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    cells = [*STILLS, *(c["id"] for c in CELLS)]
    cells += [cell for cell in results if cell not in cells]
    for cell in cells:
        value = results.get(cell, {})
        lines.append(
            f"| {cell} | {value.get('status', 'held/not generated')} | "
            f"{value.get('list_usd', 'pending')} | "
            f"{value.get('metric_estimate_usd', 'pending')} | "
            f"{value.get('posted_usd') or 'pending'} | "
            f"{(value.get('timings') or {}).get('total_s', 'pending')} |"
        )
    lines += [
        "",
        "Seed-7 repeats require the owner’s exported seed-42 realism scores of 3 or higher.",
        "Provider outputs are copied locally; Replicate API media expire after one hour.",
        "fal retention follows the account setting. No standing compute is provisioned.",
        "Rotate both evaluation keys and remove them from .env after closeout.",
        "",
    ]
    lines += review_lines
    (root / "report.md").write_text("\n".join(lines))
