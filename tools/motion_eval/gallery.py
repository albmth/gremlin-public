"""Labelled review package: normalized candidates, synchronized comparisons and measurements."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
from imprint_worker.media import write_video
from PIL import Image, ImageDraw, ImageFont

from tools.hosted_eval.ledger import Hold, Ledger, write_json

from .fidelity import GLOSSARY, frames_of, measure
from .matrix import CELLS, MODELS, WIDE_CROP
from .prepare import verify

BASELINES = {
    "RTX-5090": "SCAIL-2 self-hosted, RTX 5090 (accepted 26 Oct)",
    "H100": "SCAIL-2 self-hosted, H100 reference (19 Oct)",
    "prompt-driven": "Hosted klein edit + Wan 2.2 i2v (Phase A, 29 Oct)",
}


def ffmpeg(args):
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], capture_output=True)
    if result.returncode:
        raise Hold("ffmpeg failed: " + result.stderr.decode(errors="replace")[-300:])


def labelled(frame, text):
    """Burn a caption with Pillow: this ffmpeg build has no drawtext filter."""
    image = Image.fromarray(frame)
    draw = ImageDraw.Draw(image, "RGBA")
    font = ImageFont.load_default(size=18)
    box = draw.textbbox((8, 8), text, font=font)
    draw.rectangle((box[0] - 6, box[1] - 4, box[2] + 6, box[3] + 4), fill=(0, 0, 0, 140))
    draw.text((8, 8), text, fill=(255, 255, 255, 255), font=font)
    return np.asarray(image)


def normalize(raw, directory, driver, frames, title, crop=None):
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "normalized.mp4"
    cropping = (
        f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]},"
        if crop
        else "crop='min(iw,ih)':'min(iw,ih)',"
    )
    ffmpeg(
        [
            "-i",
            raw,
            "-an",
            "-vf",
            f"fps=16,{cropping}scale=512:512,setsar=1,tpad=stop_mode=clone:stop_duration=1",
            "-frames:v",
            frames,
            "-c:v",
            "libx264",
            "-crf",
            18,
            "-pix_fmt",
            "yuv420p",
            output,
        ]
    )
    ffmpeg(["-i", output, "-frames:v", 1, directory / "poster.jpg"])
    left, right = frames_of(driver, frames), frames_of(output, frames)
    pairs = [
        Image.fromarray(np.concatenate([labelled(a, "original"), labelled(b, title)], axis=1))
        for a, b in zip(left, right, strict=True)
    ]
    write_video(pairs, directory / "comparison.mp4", lossless=False)
    return output


def entries(root, motion, manifest):
    """Every reviewable clip per fixture: baselines first, then campaign candidates."""
    ledger = Ledger(root)
    for fixture in motion["fixtures"]:
        for key, title in BASELINES.items():
            source = root / "baselines" / fixture / key / "result.mp4"
            if source.exists():
                yield fixture, f"{fixture}:{key}", title, source, None, True
        for config in CELLS:
            if config["fixture"] != fixture:
                continue
            for event in ledger.select("saved"):
                if event["cell"].split("-seed")[0] == config["id"]:
                    variant = config.get("variant", {})
                    title = f"{MODELS[config['model']]['endpoint']} seed {event.get('seed', 42)}"
                    if variant:
                        title += " " + ", ".join(f"{k}={v}" for k, v in variant.items())
                    wide = config["model"] == "S2" and variant.get("inputs") != "square"
                    crop = WIDE_CROP if wide else None
                    yield fixture, event["cell"], title, root / event["path"], crop, False


def measure_all(root):
    motion, manifest = verify(root), json.loads((root / "inputs/manifest.json").read_text())
    ledger = Ledger(root)
    results = {}
    for fixture, key, title, source, crop, baseline in entries(root, motion, manifest):
        info = motion["fixtures"][fixture]
        frames = info["output_frames"]
        directory = root / "review" / fixture / key.split(":")[-1]
        driver = root / "inputs" / fixture / "driver.mp4"
        if not (directory / "comparison.mp4").exists():
            normalize(source, directory, driver, frames, title, crop)
        masks = np.load(root / "inputs" / fixture / "masks.npy")
        fidelity = measure(
            frames_of(driver, frames), frames_of(directory / "normalized.mp4", frames), masks
        )
        record = {"fixture": fixture, "title": title, "baseline": baseline, "fidelity": fidelity}
        if not baseline:
            terminal = [e for e in ledger.select("terminal", key) if e["status"] == "succeeded"]
            intent = [e for e in ledger.select("intent", key)]
            posted = ledger.select("posted", key)
            record["cost"] = {
                "list_usd": intent[-1]["list_usd"] if intent else None,
                "reserved_usd": intent[-1]["reserve_usd"] if intent else None,
                "posted_usd": posted[-1]["posted_usd"] if posted else None,
            }
            record["latency"] = terminal[-1].get("timings") if terminal else None
            request = root / "videos" / key / "request.json"
            if request.exists():
                record["request"] = json.loads(request.read_text())
            record["provider_seed"] = terminal[-1].get("provider_seed") if terminal else None
        else:
            record["cost"], record["latency"] = baseline_facts(root, manifest, fixture, key)
        results[key] = record
    write_json(root / "measurements.json", results)
    return results


def baseline_facts(root, manifest, fixture, key):
    """Documented cost and timing of the preserved outputs, by source."""
    label = key.split(":")[-1]
    if label == "prompt-driven":
        return (
            {"posted_usd": "0.0525", "basis": "Phase A posted charges: still 0.0025 + video 0.05"},
            {"total_s": 26.66 if fixture == "kungfu" else 26.06, "basis": "Phase A measurements"},
        )
    source = root / manifest["baselines"][f"{fixture}:{label.replace('-', ' ')}"]
    result = (
        json.loads((source / "result.json").read_text())
        if (source / "result.json").exists()
        else {}
    )
    timings = result.get("timings", {})
    return (
        {
            "posted_usd": "0.035" if label == "RTX-5090" else None,
            "basis": "Posted charge per video, cold session (29 October 2026)",
        },
        {
            "total_s": timings.get("total_seconds"),
            "generation_s": timings.get("generation_seconds"),
            "basis": (
                "worker result.json; excludes the 281 s unbilled pull and the 472 to 589 s "
                "cold submit-to-ready"
            ),
        },
    )


def alias(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def build(root):
    motion = verify(root)
    results = measure_all(root)
    gallery = root / "gallery"
    gallery.mkdir(exist_ok=True)
    fixtures = []
    for fixture, info in motion["fixtures"].items():
        cards = []
        for key, record in results.items():
            if record["fixture"] != fixture:
                continue
            name = key.split(":")[-1]
            directory = root / "review" / fixture / name
            for file in ("normalized.mp4", "comparison.mp4", "poster.jpg"):
                alias(directory / file, gallery / "media" / fixture / name / file)
            cards.append({"id": name, "media": f"media/{fixture}/{name}/", **record})
        for file in ("driver.mp4", "still.png", "reference.png", "mask.mp4"):
            alias(root / "inputs" / fixture / file, gallery / "media" / fixture / file)
        fixtures.append(
            {
                "id": fixture,
                "name": "Kung Fu" if fixture == "kungfu" else "Enzo",
                "frames": info["output_frames"],
                "seconds": round(info["output_frames"] / 16, 3),
                "subject": info["subject"],
                "cards": cards,
            }
        )
    data = {"run": root.name, "fixtures": fixtures, "glossary": GLOSSARY}
    page = Path(__file__).with_name("page.html").read_text()
    (gallery / "index.html").write_text(
        page.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    )
    return gallery / "index.html"
