"""Blind media aliases and reveal-only key loading for a file:// gallery."""

import json
import os
import shutil
from pathlib import Path

from .ledger import Ledger, write_json
from .matrix import CELLS, MODELS
from .normalize import normalize
from .prepare import verify


def alias(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def build(root):
    manifest, ledger = verify(root), Ledger(root)
    gallery = root / "gallery"
    gallery.mkdir(exist_ok=True)
    fixtures, key = [], {}
    measurements = (
        json.loads((root / "measurements.json").read_text())
        if (root / "measurements.json").exists()
        else {}
    )
    settlement = (
        json.loads((root / "settlement.json").read_text())
        if (root / "settlement.json").exists()
        else {}
    )
    for fixture in ("kungfu", "enzo"):
        info = manifest["fixtures"][fixture]
        driver = root / "inputs" / fixture / "driver.mp4"
        mapping = ledger.select("letters")
        letters = next(e["mapping"] for e in reversed(mapping) if e["fixture"] == fixture)
        entries = []
        for cell, letter in sorted(letters.items(), key=lambda item: item[1]):
            entry = {"letter": letter}
            baseline = cell in manifest["baselines"]
            config = next((c for c in CELLS if c["id"] == cell.split("-seed")[0]), None)
            saved = ledger.select("saved", cell)
            if baseline:
                raw = root / manifest["baselines"][cell] / "result.mp4"
                directory = root / "review-baselines" / fixture / letter
            else:
                directory = root / "videos" / cell
                raw = root / saved[-1]["path"] if saved else None
            if raw and raw.exists():
                if not (directory / "comparison.mp4").exists():
                    normalize(raw, directory, driver, info["output_frames"])
                for name in ("normalized.mp4", "comparison.mp4", "poster.jpg"):
                    dest = gallery / "media" / fixture / letter / name
                    alias(directory / name, dest)
                dest = gallery / "media" / fixture / letter / "raw.mp4"
                alias(raw, dest)
                entry["media"] = f"media/{fixture}/{letter}/"
                entry["status"] = "Available"
            else:
                entry["status"] = "Held or not yet generated"
            entries.append(entry)
            terminal = ledger.select("terminal", cell)
            record = {
                "cell": cell,
                "workflow": "motion-controlled"
                if baseline
                else "keyframe"
                if config["route"] == "K2"
                else "prompt-driven",
                "provider": "RunPod (preserved)"
                if baseline
                else MODELS[config["model"]]["provider"],
                "endpoint": cell if baseline else MODELS[config["model"]]["endpoint"],
                "measurement": measurements.get(cell),
                "settlement": settlement.get(cell),
                "status": terminal[-1]["status"] if terminal else entry["status"],
                "limitations": ["Preserved accepted output, no new inference"]
                if baseline
                else [
                    "No frame-by-frame motion driver",
                    *(["Aspect crop from 16:9"] if config["model"] == "V1" else []),
                    *(["Draft mode, integer duration"] if config["model"] == "V2" else []),
                ],
            }
            if baseline:
                source = root / manifest["baselines"][cell]
                record["provenance"] = {
                    name: json.loads((source / name).read_text())
                    for name in ("result.json", "preparation.json", "job.json")
                    if (source / name).exists()
                }
            record["events"] = [e for e in ledger.events if e.get("cell") == cell]
            request = directory / "request.json"
            if request.exists():
                record["request"] = json.loads(request.read_text())
            if not baseline and config["route"] != "A0":
                prefix = "KF" if fixture == "kungfu" else "EN"
                record["stills"] = []
                record["still_measurements"] = {}
                for position in ("first", "last"):
                    path = root / "stills" / f"S-{prefix}-{position}" / "output.png"
                    if path.exists():
                        record["stills"].append(os.path.relpath(path, gallery))
                        record["still_measurements"][position] = measurements.get(
                            f"S-{prefix}-{position}"
                        )
            key[f"{fixture}:{letter}"] = record
        fixtures.append(
            {
                "id": fixture,
                "name": "Kung Fu" if fixture == "kungfu" else "Enzo",
                "duration_ms": info["duration_ms"],
                "cards": entries,
                "driver": f"../inputs/{fixture}/driver.mp4",
                "reference": f"../inputs/{fixture}/reference.png",
            }
        )
    data = {"run": root.name, "fixtures": fixtures}
    write_json(gallery / "key.json", key)
    (gallery / "key.js").write_text(
        "window.REVEAL_KEY=" + json.dumps(key).replace("</", "<\\/") + ";\n"
    )
    page = Path(__file__).with_name("page.html").read_text()
    (gallery / "index.html").write_text(page.replace("__DATA__", json.dumps(data)))
    return gallery / "index.html"
