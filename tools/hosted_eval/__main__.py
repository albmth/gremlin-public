"""Run the separately authorized, isolated hosted evaluation campaign."""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

from . import gallery, report, run
from .budget import Budget
from .ledger import Hold, Ledger, campaign_lock, write_json
from .matrix import CELLS, MODELS, STILLS
from .prepare import prepare
from .providers import Client, data_uri, validate


def preflight(root):
    """Validate every planned body, including unavailable edited images, before spending."""
    manifest = json.loads((root / "inputs/manifest.json").read_text())
    image = data_uri(root / "inputs/kungfu/reference.png")
    drifts = []
    for model, config in MODELS.items():
        client = Client(
            config["provider"],
            os.environ.get("FAL_KEY" if model == "E1" else "REPLICATE_API_TOKEN"),
            config["endpoint"],
        )
        raw, schema, schemas = client.schema()
        write_json(root / "schemas" / f"{model}.json", raw)
        if model == "E1":
            body, _ = run.build(root, STILLS[0], model, "kungfu")
            try:
                validate(body, schema, schemas)
            except Hold as error:
                drifts.append(model + ": " + str(error))
        else:
            from .matrix import video_input

            for cell in [c for c in CELLS if c["model"] == model]:
                prompt = manifest["fixtures"][cell["fixture"]]["request"]["prompt"]
                try:
                    validate(video_input(cell, prompt, image, image), schema, schemas)
                except Hold as error:
                    drifts.append(cell["id"] + ": " + str(error))
        write_json(root / "schemas" / f"{model}.json", raw)
    if drifts:
        raise Hold("; ".join(drifts))
    Ledger(root).append("preflight", models=list(MODELS))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "approve",
            "preflight",
            "stills",
            "videos",
            "normalize",
            "measure",
            "gallery",
            "settle",
            "report",
            "probe",
            "repeats",
        ],
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-usd", default="2.00")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--cells")
    parser.add_argument("--model", choices=list(MODELS))
    parser.add_argument("--probe-confirmed")
    parser.add_argument("--accept-deviation", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--replicate", type=Path)
    parser.add_argument("--fal", type=Path)
    parser.add_argument("--scores", type=Path)
    args = parser.parse_args()
    root = args.out.resolve()
    repository = Path(__file__).resolve().parents[2]
    if not root.is_relative_to(repository / "artifacts"):
        parser.error("--out must be inside this repository’s ignored artifacts directory")
    try:
        with campaign_lock(root):
            if args.command == "prepare":
                prepare(root)
            elif args.command == "approve":
                Budget(root, Ledger(root)).approve(args.max_usd)
            elif args.command == "preflight":
                preflight(root)
            elif args.command in {"stills", "videos"}:
                if not args.simulate and not Ledger(root).select("preflight"):
                    raise Hold("Run preflight for every model before any paid call")
                if args.command == "stills":
                    run.run_stills(root, args.simulate)
                else:
                    run.run_videos(
                        root, args.cells.split(",") if args.cells else None, args.simulate
                    )
            elif args.command == "probe":
                if not args.model or args.probe_confirmed is None or not args.evidence:
                    raise Hold("Probe needs --model, --probe-confirmed and --evidence")
                run.confirm_probe(
                    root,
                    args.model,
                    args.probe_confirmed,
                    args.evidence.resolve(),
                    args.accept_deviation,
                )
            elif args.command in {"gallery", "normalize"}:
                report.measure(root)
                print(gallery.build(root))
            elif args.command == "measure":
                report.measure(root)
            elif args.command == "settle":
                report.settle(root, args.replicate, args.fal)
            elif args.command == "report":
                report.report(root, args.scores)
            elif args.command == "repeats":
                if not args.scores:
                    raise Hold("Repeats require owner-exported scores")
                scores = json.loads(args.scores.read_text())
                if scores["run"] != root.name:
                    raise Hold("Score export belongs to another campaign")
                ledger = Ledger(root)
                for cell in CELLS:
                    fixture = cell["fixture"]
                    mapping = next(
                        e["mapping"]
                        for e in reversed(ledger.select("letters"))
                        if e["fixture"] == fixture
                    )
                    key = fixture + ":" + mapping[cell["id"]]
                    rating = scores["scores"].get(key, {}).get("ratings", {}).get("realism")
                    if rating not in ("3", "4", "5") or not scores["revealed"].get(fixture):
                        continue
                    repeat = cell["id"] + "-seed7"
                    if repeat not in mapping:
                        mapping = {**mapping, repeat: chr(65 + len(mapping))}
                        ledger.append("letters", fixture=fixture, mapping=mapping)
                    run.execute(root, repeat, cell["model"], fixture, seed=7)
    except (Hold, requests.RequestException) as error:
        # Transport errors can include auth/query strings. Emit only a fixed safe message.
        message = (
            str(error)
            if isinstance(error, Hold)
            else "Transport error; reservation retained. Reconcile submission before retrying."
        )
        print("HELD: " + message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
