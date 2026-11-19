"""Prepare hash-verified inputs: driving interval, tracked masks, aligned stills, wide canvases."""

import json
import shutil
import subprocess
from pathlib import Path

import av
import cv2
import numpy as np
from imprint_worker.media import read_video, sha256, write_video
from PIL import Image

from tools.hosted_eval.ledger import Hold, Ledger, write_json
from tools.hosted_eval.prepare import prepare as hosted_prepare
from tools.hosted_eval.prepare import verify as hosted_verify

from .matrix import FIXTURES, WIDE, WIDE_CROP

# The Phase A hosted outputs of 29 October 2026 that the owner reviewed: the prompt-driven baseline.
PHASE_A_VIDEOS = {"kungfu": "KF-B3", "enzo": "EN-B3"}
MASK_DILATION_PX = 8


def ffmpeg(args):
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], capture_output=True)
    if result.returncode:
        raise Hold("ffmpeg failed: " + result.stderr.decode(errors="replace")[-300:])


def phase_a_file(source_run, cell):
    """A still or video saved by the Phase A campaign, verified against its ledger."""
    saved = Ledger(source_run).select("saved", cell)
    if not saved:
        raise Hold(f"Phase A run has no saved output for {cell}")
    path = source_run / saved[-1]["path"]
    if sha256(path) != saved[-1]["sha256"]:
        raise Hold(f"Phase A output changed since it was saved: {cell}")
    return path


def subject_masks(tracking, count):
    """Boolean masks from the worker's blue-palette tracking video, first `count` frames."""
    masks = []
    with av.open(str(tracking)) as container:
        for frame in container.decode(container.streams.video[0]):
            rgb = frame.to_ndarray(format="rgb24")
            masks.append((rgb[:, :, 2] > 128) & (rgb[:, :, 0] < 100) & (rgb[:, :, 1] < 100))
            if len(masks) == count:
                break
    if len(masks) != count:
        raise Hold("Tracking video is shorter than the model frame count")
    return np.stack(masks)


def write_mask_video(masks, path, dilation=MASK_DILATION_PX, invert=False):
    """White marks the region to regenerate; `invert` writes the complement for polarity probes."""
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilation + 1, 2 * dilation + 1))
    frames = []
    for mask in masks:
        grown = cv2.dilate(mask.astype(np.uint8) * 255, kernel)
        if invert:
            grown = 255 - grown
        frames.append(Image.fromarray(np.repeat(grown[:, :, None], 3, axis=2)))
    write_video(frames, path, lossless=False)


def wide_canvas(source, destination, frames=None):
    """Centre the 512 square scene on a darkened, blurred 896x512 canvas (cropped back later)."""
    x = WIDE_CROP[0]
    args = [
        "-i",
        source,
        "-filter_complex",
        f"[0:v]split[a][b];[a]scale={WIDE[0]}:{WIDE[1]},boxblur=30:2,"
        f"eq=brightness=-0.12[bg];[b]scale=512:512[fg];[bg][fg]overlay={x}:0,setsar=1",
    ]
    if frames:
        args += ["-frames:v", frames, "-c:v", "libx264", "-crf", 14, "-pix_fmt", "yuv420p"]
    else:
        args += ["-frames:v", 1]
    ffmpeg([*args, destination])


def prepare(root, source_run):
    root, source_run = Path(root), Path(source_run)
    hosted_prepare(root)
    manifest = hosted_verify(root)
    motion = {"source_run": str(source_run), "fixtures": {}, "hashes": {}}
    for fixture, config in FIXTURES.items():
        info = manifest["fixtures"][fixture]
        directory = root / "inputs" / fixture
        model_frames, output_frames = info["model_frames"], info["output_frames"]
        still = phase_a_file(source_run, f"S-{config['prefix']}-first")
        shutil.copy2(still, directory / "still.png")
        video = phase_a_file(source_run, PHASE_A_VIDEOS[fixture])
        (root / "baselines" / fixture / "prompt-driven").mkdir(parents=True, exist_ok=True)
        shutil.copy2(video, root / "baselines" / fixture / "prompt-driven" / "result.mp4")
        tracking = root / manifest["baselines"][f"{fixture}:RTX 5090"] / "tracking.mp4"
        masks = subject_masks(tracking, model_frames)
        write_mask_video(masks, directory / "mask.mp4")
        write_mask_video(masks, directory / "mask-inverted.mp4", invert=True)
        np.save(directory / "masks.npy", masks[:output_frames])
        frames = read_video(directory / f"{fixture}-driving.mkv")
        if len(frames) != model_frames:
            raise Hold("Prepared driving video frame count differs from the manifest")
        write_video(frames, directory / "driver-model.mp4", lossless=False)
        wide_canvas(directory / "driver-model.mp4", directory / "driver-wide.mp4", model_frames)
        wide_canvas(directory / "still.png", directory / "still-wide.png")
        motion["fixtures"][fixture] = {
            "model_frames": model_frames,
            "output_frames": output_frames,
            "subject": config["subject"],
            "mask_area_fraction_mean": float(masks.mean()),
            "prompt": info["request"]["prompt"],
        }
        for name in (
            "still.png",
            "still-wide.png",
            "mask.mp4",
            "mask-inverted.mp4",
            "driver-model.mp4",
            "driver-wide.mp4",
            "driver.mp4",
        ):
            motion["hashes"][f"inputs/{fixture}/{name}"] = sha256(directory / name)
        motion["hashes"][f"baselines/{fixture}/prompt-driven/result.mp4"] = sha256(video)
    write_json(root / "motion-manifest.json", motion)
    Ledger(root).append("motion_prepared", manifest_sha256=sha256(root / "motion-manifest.json"))
    return motion


def verify(root):
    root = Path(root)
    hosted_verify(root)
    motion = json.loads((root / "motion-manifest.json").read_text())
    for path, digest in motion["hashes"].items():
        if sha256(root / path) != digest:
            raise Hold(f"Prepared motion input changed: {path}")
    return motion
