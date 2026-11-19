import json
import shutil
import subprocess
import sys
from pathlib import Path

import av
import numpy as np
import pytest
from PIL import Image

from tools.check_docs import check_document
from tools.compare_outputs import compare_outputs

ROOT = Path(__file__).resolve().parents[1]


def test_docs_checks_links_fragments_and_prose_without_flagging_examples(tmp_path):
    target = tmp_path / "target space.md"
    target.write_text("# A `code` heading\n# A `code` heading\n")
    document = tmp_path / "README.md"
    document.write_text(
        "# 1. Heading\n"
        "A sentence. Another sentence.\n"
        'A quoted prompt: "First sentence. Another sentence."\n'
        "Use e.g. Python for the client.\n"
        "| Two sentences. Stay together in a table. |\n"
        "```md\n[example](missing.md)\nTwo sentences. Code is excluded.\n```\n"
        "[good](<target space.md#a-code-heading-1>)\n"
        "[broken fragment](target%20space.md#wrong)\n"
        "[broken file](missing.md)\n"
        "[external](https://example.invalid/no-network-check)\n"
        "[missing reference][absent]\n"
        "[defined reference][ref]\n"
        "[ref]: <target space.md#a-code-heading>\n"
        "An em dash: \u2014\n"
    )
    errors = check_document(document, tmp_path)
    assert len(errors) == 5, errors
    assert any(":2: put each prose sentence" in error for error in errors)
    assert any("missing Markdown anchor" in error for error in errors)
    assert any("missing local link target: missing.md" in error for error in errors)
    assert any("undefined link reference [absent]" in error for error in errors)
    assert any("em dash" in error for error in errors)


def write_run(directory, *, frames=3, color=64, fps=16, size=16):
    directory.mkdir()
    with av.open(str(directory / "result.mp4"), "w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width = stream.height = size
        stream.pix_fmt = "yuv420p"
        for _ in range(frames):
            frame = av.VideoFrame.from_ndarray(
                np.full((size, size, 3), color, dtype=np.uint8), format="rgb24"
            )
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    image = Image.new("RGB", (size, size), (color, color, color))
    image.save(directory / "result.webp")
    image.save(directory / "poster.jpg")


def test_comparison_accepts_identity_and_rejects_a_changed_poster(tmp_path):
    accepted, candidate = tmp_path / "accepted", tmp_path / "candidate"
    write_run(accepted)
    shutil.copytree(accepted, candidate)
    report = compare_outputs(candidate, accepted)
    assert report["passed"]
    assert report["psnr_db"] == "infinity"
    Image.new("RGB", (16, 16), (255, 0, 0)).save(candidate / "poster.jpg")
    report = compare_outputs(candidate, accepted)
    assert report["decoded_rgb_identical"]
    assert not report["passed"]
    assert not report["hashes"]["poster.jpg"]["identical"]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"color": 128}, "different_frames"),
        ({"fps": 8}, "timing_mismatches"),
        ({"size": 32}, "dimension_mismatches"),
        ({"frames": 2}, "candidate_frames"),
    ],
)
def test_comparison_rejects_changed_frames_geometry_timing_and_length(tmp_path, changes, field):
    accepted, candidate = tmp_path / "accepted", tmp_path / "candidate"
    write_run(accepted)
    write_run(candidate, **changes)
    # Isolate the video change so image checks cannot conceal a faulty video gate.
    for name in ("result.webp", "poster.jpg"):
        shutil.copyfile(accepted / name, candidate / name)
    report = compare_outputs(candidate, accepted)
    assert not report["passed"]
    if field == "candidate_frames":
        assert report[field] < report["accepted_frames"]
        assert report["psnr_db"] is None
    else:
        assert report[field]
    if field == "different_frames":
        assert isinstance(report["psnr_db"], float)


def test_comparison_cli_fails_closed_on_missing_media(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/compare_outputs.py"), str(tmp_path), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["passed"] is False
    assert "error" in report
