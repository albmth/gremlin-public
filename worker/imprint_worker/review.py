"""Create an offline, explicitly labelled side-by-side review of genuine run artifacts."""

import argparse
import html
import json
import shutil
import subprocess
from pathlib import Path

from .media import sha256


def main():
    parser = argparse.ArgumentParser()
    for name in ("reference", "driving", "result", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    record = json.loads((args.result / "result.json").read_text())
    preparation = json.loads((args.result / "preparation.json").read_text())
    for kind, path in (("reference", args.reference), ("driving", args.driving)):
        if sha256(path) != preparation[kind + "_sha256"]:
            raise ValueError("Review input differs from the inference input")
    for name in ("result.mp4", "result.webp", "poster.jpg"):
        path = args.result / name
        expected = record["files"][name]
        if path.stat().st_size != expected["bytes"] or sha256(path) != expected["sha256"]:
            raise ValueError("Review result integrity failure")
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.reference, args.output / ("reference" + args.reference.suffix))
    shutil.copy2(args.driving, args.output / ("driving" + args.driving.suffix))
    shutil.copy2(args.result / "result.mp4", args.output / "result.mp4")
    shutil.copy2(args.result / "result.webp", args.output / "result.webp")
    # Bound by the recorded frame count: -t rounds fractional 16 fps intervals down
    # after hstack. Hold the final frame to cover that last partial frame interval.
    scale = (
        "fps=16,scale=512:512:force_original_aspect_ratio=decrease,"
        "pad=512:512:(ow-iw)/2:(oh-ih)/2,tpad=stop_mode=clone:stop_duration=0.0625"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            str(preparation["start_ms"] / 1000),
            "-i",
            str(args.driving),
            "-i",
            str(args.result / "result.mp4"),
            "-filter_complex",
            f"[0:v:0]{scale},setpts=PTS-STARTPTS[a];"
            f"[1:v:0]{scale},setpts=PTS-STARTPTS[b];[a][b]hstack=shortest=1[v]",
            "-map",
            "[v]",
            "-frames:v",
            str(preparation["output_frames"]),
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(args.output / "comparison.mp4"),
        ],
        check=True,
        timeout=120,
    )
    a = html.escape("reference" + args.reference.suffix)
    b = html.escape("driving" + args.driving.suffix)
    source = (
        f'<img src="{b}" alt="Driving animation">'
        if args.driving.suffix.lower() in {".gif", ".webp"}
        else f'<video src="{b}" controls loop muted playsinline></video>'
    )
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Imprint visual review</title>
<style>
body{{font:18px system-ui;background:#f6f1e8;color:#242424;margin:28px}}
main{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}}
img,video{{width:100%;max-height:70vh;object-fit:contain}}
h2{{font-size:20px}}pre{{white-space:pre-wrap}}
</style>
<h1>Imprint visual review</h1>
<p>Recipe: {html.escape(str(record["recipe"]))}. Review identity, motion, expression, timing,
interactions, background and lighting through the full clip. Technical completion is not
quality acceptance.</p>
<main><section><h2>Identity A</h2><img src="{a}" alt="Identity reference"></section>
<section><h2>Original B</h2>{source}</section>
<section><h2>SCAIL-2 output</h2>
<video src="result.mp4" controls loop muted playsinline></video>
<p><a href="result.webp">Animated sticker</a></p></section></main>
<h2>Synchronized comparison: original (left), generated (right)</h2>
<video src="comparison.mp4" controls loop muted playsinline></video>
<pre>{html.escape(json.dumps(record, indent=2))}</pre></html>'''
    (args.output / "index.html").write_text(page)


if __name__ == "__main__":
    main()
