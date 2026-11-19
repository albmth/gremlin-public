"""Review-only work cannot alter delivered bytes; the graph executor is a local stub."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

from imprint_worker import runner
from imprint_worker.media import read_video, sha256
from PIL import Image, ImageDraw


def test_skipping_review_encodes_preserves_delivery_and_keeps_fixture_evidence(
    tmp_path, monkeypatch
):
    reference = tmp_path / "reference.png"
    driving = tmp_path / "driving.gif"
    Image.new("RGB", (32, 32), "white").save(reference)
    frames = [Image.new("RGB", (32, 32), (30 + i * 3, 60, 100)) for i in range(30)]
    frames[0].save(driving, save_all=True, append_images=frames[1:], duration=100, loop=0)
    root = tmp_path / "engine"
    (root / "input").mkdir(parents=True)
    (root / "output").mkdir()
    monkeypatch.setattr(runner, "peak_memory", lambda: nullcontext({}))
    monkeypatch.setattr(runner.Comfy, "idle", lambda _: True)
    monkeypatch.setattr(
        runner.requests,
        "get",
        lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"system": {}, "devices": []},
        ),
    )

    def execute(_self, graph, _deadline):
        save = graph["save_drive_masks"] if "save_drive_masks" in graph else graph["save"]
        return save["inputs"]["filename_prefix"].split("/")[0]

    def files(self, prefix, node):
        manifest = json.loads((self.root / "input" / f"{prefix}-preparation.json").read_text())
        size = manifest["width"], manifest["height"]
        if node == "save":
            images = read_video(self.root / "input" / f"{prefix}-driving.mkv")
        else:
            mask = Image.new("RGB", size, "black" if node == "save_ref_mask" else "white")
            ImageDraw.Draw(mask).rectangle((100, 100, 300, 300), fill=(0, 0, 255))
            images = [mask] * (1 if node == "save_ref_mask" else manifest["model_frames"])
        folder = self.root / "output" / prefix / node
        folder.mkdir(parents=True)
        paths = []
        for i, image in enumerate(images):
            path = folder / f"{i}.png"
            image.save(path)
            paths.append(path)
        return paths

    monkeypatch.setattr(runner.Comfy, "execute", execute)
    monkeypatch.setattr(runner.Comfy, "files", files)
    request = {
        "tier": "simple-motion",
        "start_ms": 0,
        "duration_ms": 3000,
        "seed": 42,
        "prompt": "A person moves through the scene.",
        "reference_point": {"x": 0.5, "y": 0.5},
        "target_point": {"x": 0.5, "y": 0.5},
    }
    manifests = []
    for retain in (True, False):
        output = tmp_path / str(retain)
        manifest, _ = runner.run(reference, driving, output, root, request, retain_review=retain)
        manifests.append(manifest)
        for name in ("tracking.mp4", "generated.mp4", "identity.png"):
            assert (output / name).exists() is retain
        for name, expected in manifest.items():
            assert sha256(output / name) == expected["sha256"]
    assert manifests[0] == manifests[1]
