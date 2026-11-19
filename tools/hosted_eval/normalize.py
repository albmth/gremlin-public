"""Preserve raw media and create consistent review derivatives."""

import subprocess

import av

from .ledger import Hold


def ffmpeg(args):
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], capture_output=True)
    if result.returncode:
        raise Hold("ffmpeg failed; inspect input geometry and codec")


def normalize(raw, directory, driver, frames):
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "normalized.mp4"
    ffmpeg(
        [
            "-i",
            raw,
            "-an",
            "-vf",
            "fps=16,crop='min(iw,ih)':'min(iw,ih)',scale=512:512,setsar=1",
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
    with av.open(str(output)) as video:
        stream = video.streams.video[0]
        if (stream.width, stream.height, stream.average_rate) != (512, 512, 16) or sum(
            1 for _ in video.decode(stream)
        ) != frames:
            raise Hold("Normalized output geometry or frame count mismatch")
    ffmpeg(["-i", output, "-frames:v", 1, directory / "poster.jpg"])
    ffmpeg(
        [
            "-i",
            driver,
            "-i",
            output,
            "-filter_complex",
            "[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]hstack",
            "-an",
            "-frames:v",
            frames,
            "-c:v",
            "libx264",
            "-crf",
            18,
            "-pix_fmt",
            "yuv420p",
            directory / "comparison.mp4",
        ]
    )
