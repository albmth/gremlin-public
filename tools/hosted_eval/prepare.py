"""Hash-verified local preparation and immutable baseline copies."""

import json
import random
import shutil
import subprocess
from pathlib import Path

from imprint_worker.media import prepare as media_prepare
from imprint_worker.media import read_video, sha256, write_video

from tools.fixtures import ROOT, load_fixture

from .ledger import Hold, Ledger, write_json
from .matrix import BASELINES, CELLS


def canvas(source, destination):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            "[0:v]split[a][b];[a]scale=832:480,boxblur=30:2[bg];"
            "[b]scale=480:480[fg];[bg][fg]overlay=176:0",
            "-frames:v",
            "1",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )


def prepare(root):
    if (root / "inputs/manifest.json").exists():
        verify(root)
        return
    if (root / "inputs").exists():
        raise Hold("Partial preparation exists; inspect it and choose a new run directory")
    manifest = {
        "fixtures": {},
        "baselines": {},
        "hashes": {},
        "v1_canvas": [832, 480],
        "v1_crop": [176, 0, 480, 480],
    }
    pins = {}
    for line in (ROOT / "docs/reference-outputs.sha256").read_text().splitlines():
        if line and not line.startswith("#"):
            digest, path = line.split(maxsplit=1)
            pins[path] = digest
    for name in ("kungfu", "enzo"):
        request, reference, driving = load_fixture(name)
        directory = root / "inputs" / name
        directory.mkdir(parents=True)
        shutil.copy2(reference, directory / "original-reference.png")
        shutil.copy2(driving, directory / ("original" + driving.suffix))
        info = media_prepare(
            reference,
            driving,
            directory,
            name,
            start_ms=request["start_ms"],
            duration_ms=request["duration_ms"],
        )
        frames = read_video(directory / f"{name}-driving.mkv")[: info["output_frames"]]
        frames[-1].save(directory / "last.png")
        shutil.copy2(directory / f"{name}-first.png", directory / "first.png")
        shutil.copy2(directory / f"{name}-reference.png", directory / "reference.png")
        write_video(frames, directory / "driver.mp4", lossless=False)
        canvas(directory / "reference.png", directory / "raw-v1.png")
        manifest["fixtures"][name] = {"request": request, **info}
        for label, source in BASELINES[name].items():
            source = Path(source)
            selected = {p: d for p, d in pins.items() if Path(p).parent == source}
            if not selected or str(source / "result.mp4") not in selected:
                raise Hold("Baseline has no pinned MP4")
            for path, digest in selected.items():
                if sha256(ROOT / path) != digest:
                    raise Hold(f"Baseline checksum mismatch: {path}")
            destination = root / "baselines" / name / label.replace(" ", "-")
            shutil.copytree(ROOT / source, destination)
            manifest["baselines"][f"{name}:{label}"] = str(destination.relative_to(root))
    for file in (root / "inputs").rglob("*"):
        if file.is_file():
            manifest["hashes"][str(file.relative_to(root))] = sha256(file)
    for file in (root / "baselines").rglob("*"):
        if file.is_file():
            manifest["hashes"][str(file.relative_to(root))] = sha256(file)
    write_json(root / "inputs/manifest.json", manifest)
    ledger = Ledger(root)
    for fixture in ("kungfu", "enzo"):
        ids = [c["id"] for c in CELLS if c["fixture"] == fixture]
        ids += [f"{fixture}:{label}" for label in BASELINES[fixture]]
        random.SystemRandom().shuffle(ids)
        ledger.append(
            "letters", fixture=fixture, mapping={cell: chr(65 + i) for i, cell in enumerate(ids)}
        )
    ledger.append("prepared", manifest_sha256=sha256(root / "inputs/manifest.json"))


def verify(root):
    manifest = json.loads((root / "inputs/manifest.json").read_text())
    for path, digest in manifest["hashes"].items():
        if sha256(root / path) != digest:
            raise Hold(f"Prepared input or baseline changed: {path}")
    return manifest
