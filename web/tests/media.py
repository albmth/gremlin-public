"""Synthetic scenes and optional private media for tests that need real detector inputs."""

from __future__ import annotations

import io
import math
from functools import cache
from pathlib import Path

import av
import pytest
from PIL import Image

STICKERS = Path(__file__).resolve().parents[2] / "stickers"
SIZE = (960, 540)


def fixture(prefix):
    path = next(STICKERS.glob(prefix + "*"), None)
    if path is None:
        pytest.skip(f"private fixture {prefix} missing from stickers/")
    return path


@cache
def _person():
    with Image.open(fixture("Screenshot")) as image:
        return image.convert("RGB")


def scene(people, size=SIZE, background=(70, 110, 90)):
    """A frame with one copy of the person per (centre x, height) pair, standing on the floor."""
    canvas = Image.new("RGB", size, background)
    for centre, height in people:
        person = _person()
        h = int(size[1] * height)
        w = int(person.width * h / person.height)
        canvas.paste(person.resize((w, h)), (int(centre * size[0] - w / 2), size[1] - h))
    return canvas


def png(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def clip(people_at, seconds=5.5, fps=30, size=SIZE, background_at=None):
    """An MP4 whose frame at time t shows `people_at(t)`; `background_at(t)` may cut scenes."""
    out = io.BytesIO()
    with av.open(out, "w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = size[0], size[1], "yuv420p"
        for index in range(int(seconds * fps)):
            t = index / fps
            background = background_at(t) if background_at else (70, 110, 90)
            frame = av.VideoFrame.from_image(scene(people_at(t), size, background))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return out.getvalue()


CLIPS = {
    # One person swaying: chosen automatically.
    "one": lambda t: [(0.5 + 0.03 * math.sin(t * 2), 0.8)],
    # Two equally plausible people: a tap.
    "two": lambda t: [(0.3, 0.75), (0.7, 0.75)],
    # A large person and a small bystander: still automatic.
    "bystander": lambda t: [(0.4, 0.85), (0.88, 0.3)],
    # Nobody at all.
    "empty": lambda t: [],
    # The only person walks out of the frame after about a second.
    "leaves": lambda t: [(0.5 + 0.5 * t, 0.8)] if t < 2 else [],
}
