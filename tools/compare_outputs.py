"""Offline regression gate: compare a candidate run directory with an accepted run.

Exit 0 requires identical decoded RGB frames and timing, plus identical WebP and poster bytes.
MP4 container hashes are reported but may differ without a delivered-frame regression.
Missing files, decoding errors, extra frames and dimension changes fail closed.
This measures existing media only; it does not run inference or replace owner video review.
"""

import argparse
import hashlib
import json
import math
from itertools import zip_longest
from pathlib import Path

import av
import numpy as np

MEDIA = ("result.mp4", "result.webp", "poster.jpg")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def decoded_frames(path):
    with av.open(str(path)) as container:
        if len(container.streams) != 1 or len(container.streams.video) != 1:
            raise ValueError(f"{path}: expected exactly one stream, containing video")
        aspect_ratio = container.streams.video[0].sample_aspect_ratio
        for frame in container.decode(video=0):
            if frame.pts is None or frame.time_base is None or not frame.duration:
                raise ValueError(f"{path}: missing frame timing")
            yield (
                frame.to_ndarray(format="rgb24"),
                frame.pts * frame.time_base,
                frame.duration * frame.time_base,
                (frame.rotation, aspect_ratio),
            )


def compare_outputs(candidate, accepted):
    hashes = {
        name: {"candidate": sha256(candidate / name), "accepted": sha256(accepted / name)}
        for name in MEDIA
    }
    for item in hashes.values():
        item["identical"] = item["candidate"] == item["accepted"]
    counts = [0, 0]
    different_frames = []
    timing_mismatches = []
    dimension_mismatches = []
    presentation_mismatches = []
    squared_error, samples = 0.0, 0
    candidate_frames = decoded_frames(candidate / "result.mp4")
    accepted_frames = decoded_frames(accepted / "result.mp4")
    try:
        for index, pair in enumerate(zip_longest(candidate_frames, accepted_frames)):
            actual, reference = pair
            for side, frame in enumerate(pair):
                counts[side] += frame is not None
            if actual is None or reference is None:
                continue
            a, a_time, a_duration, a_presentation = actual
            b, b_time, b_duration, b_presentation = reference
            if (a_time, a_duration) != (b_time, b_duration):
                timing_mismatches.append(index)
            if a_presentation != b_presentation:
                presentation_mismatches.append(index)
            if a.shape != b.shape:
                dimension_mismatches.append(index)
                continue
            if not np.array_equal(a, b):
                different_frames.append(index)
            difference = a.astype(np.float64) - b.astype(np.float64)
            squared_error += float(np.sum(difference**2))
            samples += a.size
    finally:
        candidate_frames.close()
        accepted_frames.close()
    matching_count = counts[0] == counts[1] and counts[0] > 0
    comparable = matching_count and not dimension_mismatches and samples > 0
    psnr = (
        "infinity"
        if comparable and squared_error == 0
        else round(10 * math.log10(255**2 * samples / squared_error), 6)
        if comparable
        else None
    )
    decoded_identical = comparable and not different_frames
    passed = (
        decoded_identical
        and not timing_mismatches
        and not presentation_mismatches
        and hashes["result.webp"]["identical"]
        and hashes["poster.jpg"]["identical"]
    )
    return {
        "candidate": str(candidate),
        "accepted": str(accepted),
        "passed": passed,
        "decoded_rgb_identical": decoded_identical,
        "candidate_frames": counts[0],
        "accepted_frames": counts[1],
        "different_frames": different_frames,
        "dimension_mismatches": dimension_mismatches,
        "timing_mismatches": timing_mismatches,
        "presentation_mismatches": presentation_mismatches,
        "psnr_db": psnr,
        "hashes": hashes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "candidate", type=Path, help="directory containing the three delivered files"
    )
    parser.add_argument(
        "accepted", type=Path, help="owner-accepted directory for this fixture/tier"
    )
    args = parser.parse_args()
    try:
        report = compare_outputs(args.candidate, args.accepted)
    except (OSError, ValueError, av.error.FFmpegError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
