"""Execute the pinned native SCAIL-2 pipeline against a private local ComfyUI process."""

import argparse
import json
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from . import engine_telemetry
from .graph import generation_graph, tracking_graph, validate_graph
from .lifecycle import remove
from .media import (
    InputRejected,
    encode_result,
    keep_static_overlays,
    prepare,
    read_video,
    validate_masks,
    write_video,
)
from .recipe import TIER_RECIPES, generation_settings, load


def gpu_memory_mib():
    used = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.split()
    return max(map(int, used))


def container_memory_gib():
    return round(int(Path("/sys/fs/cgroup/memory.current").read_text()) / 1024**3, 2)


@contextmanager
def peak_memory(interval=2):
    """Peak GPU memory and container memory while the block runs, for the run record.

    The tiers peak near 29.5 GB on a 32 GB card, so each attempt keeps its own headroom
    evidence. A value whose source fails at any point stays None, so a peak cut short never
    reads as a measurement.
    """
    peaks = {"gpu_mib": None, "container_gib": None}
    done = threading.Event()

    def sample():
        live = {"gpu_mib": gpu_memory_mib, "container_gib": container_memory_gib}
        while live:
            for name, read in list(live.items()):
                try:
                    peaks[name] = max(peaks[name] or 0, read())
                except (OSError, ValueError, subprocess.SubprocessError):
                    del live[name]
                    peaks[name] = None
            if done.wait(interval):
                return

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        yield peaks
    finally:
        done.set()
        thread.join(timeout=15)


class Comfy:
    def __init__(self, root, url="http://127.0.0.1:8188"):
        self.root = Path(root)
        self.url = url

    def idle(self):
        response = requests.get(self.url + "/queue", timeout=20)
        response.raise_for_status()
        queue = response.json()
        return queue["queue_running"] == [] and queue["queue_pending"] == []

    def clear_history(self):
        requests.post(self.url + "/history", json={"clear": True}, timeout=20).raise_for_status()

    def cleanup(self, prefix):
        # A lost /prompt response may still have started inference. An HTTP failure or a
        # nonempty queue preserves the files until the owning handler stops the engine.
        if not self.idle():
            return False
        for path in (self.root / "input").glob(prefix + "-*"):
            remove(path)
        remove(self.root / "output" / prefix)
        return True

    def execute(self, graph, deadline):
        schemas = requests.get(self.url + "/object_info", timeout=30)
        schemas.raise_for_status()
        validate_graph(graph, schemas.json())
        reply = requests.post(
            self.url + "/prompt",
            json={"prompt": graph, "client_id": str(uuid.uuid4())},
            timeout=30,
        )
        reply.raise_for_status()
        prompt_id = reply.json()["prompt_id"]
        while time.monotonic() < deadline:
            response = requests.get(self.url + "/history/" + prompt_id, timeout=20)
            response.raise_for_status()
            history = response.json().get(prompt_id)
            if history:
                if history["status"]["status_str"] != "success":
                    raise RuntimeError("Comfy execution failed")
                requests.post(
                    self.url + "/history", json={"delete": [prompt_id]}, timeout=20
                ).raise_for_status()
                return history
            time.sleep(1)
        requests.post(self.url + "/interrupt", json={}, timeout=10).raise_for_status()
        raise TimeoutError("Inference exceeded its reserved attempt duration")

    def files(self, history, node_id):
        result = []
        for item in history["outputs"][node_id]["images"]:
            root = (self.root / item["type"]).resolve()
            path = (root / item["subfolder"] / item["filename"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Engine returned an invalid output path")
            result.append(path)
        return result


def run(
    reference,
    driving,
    output,
    comfy_root,
    request,
    *,
    url="http://127.0.0.1:8188",
    report=lambda *_: None,
    recipe=None,
    ownership_fd=None,
    retain_review=True,
):
    # The request's tier decides the recipe; fixture experiments pass one explicitly.
    recipe = recipe or load(TIER_RECIPES[request["tier"]])
    prefix = str(uuid.uuid4())
    engine = Comfy(comfy_root, url)
    try:
        with peak_memory() as peaks:
            result = _run(
                reference,
                driving,
                output,
                request,
                url,
                report,
                recipe,
                peaks,
                engine,
                prefix,
                ownership_fd,
                retain_review,
            )
    except InputRejected:
        # The worker serves its next job only after the engine is idle and this job's files
        # are gone; anything else ends the session.
        if not engine.cleanup(prefix):
            raise RuntimeError("Engine is still active after an input rejection") from None
        raise
    except BaseException:
        with suppress(OSError, requests.RequestException):
            engine.cleanup(prefix)
        raise
    if not engine.cleanup(prefix):
        raise RuntimeError("Engine is still active after execution")
    return result


def _run(
    reference,
    driving,
    output,
    request,
    url,
    report,
    recipe,
    peaks,
    engine,
    prefix,
    ownership_fd,
    retain_review=True,
):
    started = time.monotonic()
    started_at = time.time()
    deadline = started + 900
    load_start, timings = engine_telemetry.begin_attempt(engine.root)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    inputs = engine.root / "input"
    # The engine's flags (loader, attention) shape the timings, so the record keeps them.
    stats = requests.get(url + "/system_stats", timeout=30)
    stats.raise_for_status()
    system = stats.json()["system"]
    engine_record = {key: system.get(key) for key in ("argv", "comfyui_version", "pytorch_version")}
    engine_record["devices"] = [device.get("name") for device in stats.json().get("devices", [])]
    report("preparing", timings)
    manifest = prepare(
        reference,
        driving,
        inputs,
        prefix,
        start_ms=request["start_ms"],
        duration_ms=request["duration_ms"],
    )
    timings["preparation_seconds"] = time.monotonic() - started
    (output / "preparation.json").write_text(json.dumps(manifest, indent=2))

    def point(value, rect):
        x, y, w, h = rect
        return {
            "x": round(x + value["x"] * (w - 1)),
            "y": round(y + value["y"] * (h - 1)),
        }

    graph = tracking_graph(
        prefix,
        point(request["reference_point"], manifest["reference_rect"]),
        point(request["target_point"], manifest["content_rect"]),
    )
    alpha = inputs / f"{prefix}-alpha.png"
    if alpha.exists():
        graph["alpha"] = {"class_type": "LoadImage", "inputs": {"image": alpha.name}}
        graph["ref_detect"] = {
            "class_type": "ImageToMask",
            "inputs": {"image": ["alpha", 0], "channel": "red"},
        }
    (output / "tracking-graph.json").write_text(json.dumps(graph, indent=2))
    stage = time.monotonic()
    history = engine.execute(graph, deadline)
    timings["tracking_seconds"] = time.monotonic() - stage
    timings.update(engine_telemetry.elapsed(engine.root, load_start))
    (output / "tracking-history.json").write_text(json.dumps(history, indent=2))
    masks = engine.files(history, "save_drive_masks")
    manifest["mask_area_fractions"] = validate_masks(masks, manifest["model_frames"])
    (output / "preparation.json").write_text(json.dumps(manifest, indent=2))
    mask_images = [Image.open(path).convert("RGB") for path in masks]
    write_video(mask_images, inputs / f"{prefix}-masks.mkv")
    # Retained evidence lets a reviewer inspect the selected subject through motion.
    if retain_review:
        write_video(mask_images, output / "tracking.mp4", lossless=False)
    ref_mask = Image.open(engine.files(history, "save_ref_mask")[0]).convert("RGB")
    ref_mask.save(inputs / f"{prefix}-reference-mask.png")
    ref = np.asarray(Image.open(inputs / f"{prefix}-reference.png").convert("RGB"))
    selected = np.asarray(ref_mask).max(axis=2) > 25
    if selected.mean() < 0.002:
        raise InputRejected("reference_subject_not_found")
    Image.fromarray(ref * selected[:, :, None]).save(inputs / f"{prefix}-masked-reference.png")
    if retain_review:
        Image.fromarray(ref * selected[:, :, None]).save(output / "identity.png")
    # Keep the engine and available model cache across stages/jobs. ComfyUI still offloads
    # within the accepted 32 GiB cards; all models need not fit simultaneously.
    graph = generation_graph(
        prefix,
        manifest["width"],
        manifest["height"],
        manifest["model_frames"],
        request["seed"],
        request["prompt"],
        recipe,
    )
    (output / "generation-graph.json").write_text(json.dumps(graph, indent=2))
    report("running", timings)
    stage = time.monotonic()
    history = engine.execute(graph, deadline)
    timings["generation_seconds"] = time.monotonic() - stage
    timings.update(engine_telemetry.elapsed(engine.root, load_start))
    (output / "generation-history.json").write_text(json.dumps(history, indent=2))
    report("finishing", timings)
    stage = time.monotonic()
    generated = [Image.open(path).convert("RGB") for path in engine.files(history, "save")]
    # The raw render beside the restored result shows what static-content restoration changed
    # on a real job, for about a second of encoding.
    if retain_review:
        write_video(generated, output / "generated.mp4", lossless=False)
    driving_frames = read_video(inputs / f"{prefix}-driving.mkv")
    restoring = time.monotonic()
    generated = keep_static_overlays(generated, driving_frames, mask_images)
    # Part of encoding_seconds: CPU work the GPU worker is billed for.
    timings["restoration_seconds"] = time.monotonic() - restoring
    result = encode_result(generated, manifest, output, ownership_fd=ownership_fd)
    timings["encoding_seconds"] = time.monotonic() - stage
    timings["total_seconds"] = time.monotonic() - started
    (output / "result.json").write_text(
        json.dumps(
            {
                "files": result,
                "timings": timings,
                "tier": request.get("tier"),
                "recipe": recipe["name"],
                "generation": generation_settings(recipe),
                "engine": engine_record,
                "peak_memory": peaks,
                "started_at": started_at,
                "finished_at": time.time(),
            },
            indent=2,
        )
    )
    return result, timings


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for arg in ("reference", "driving", "output", "comfy-root", "request"):
        parser.add_argument("--" + arg, required=True, type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    args = parser.parse_args()
    run(
        args.reference,
        args.driving,
        args.output,
        args.comfy_root,
        json.loads(args.request.read_text()),
        url=args.url,
        report=lambda stage, timings: print(
            json.dumps({"stage": stage, "timings": timings}), flush=True
        ),
    )
