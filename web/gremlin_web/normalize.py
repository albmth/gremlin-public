"""Turn whatever is uploaded into the inputs Gremlin's preflight and worker expect.

Phones produce HEIC photos with EXIF orientation, and HEVC video, often 4K, high frame rate
and stored sideways with a rotation flag that Gremlin ignores (docs/integration.md, preparing
inputs). The browser shows both upright, and the taps and the subject detector work on that
upright view, so normalization applies the same orientation to the pixels:

- reference: first frame, EXIF orientation applied, alpha flattened, at most 1920 px on the
  longer side, re-encoded as a JPEG without metadata;
- driving clip: only the chosen interval, rotation applied, scaled into
  1920 x 1080 (either orientation), at most 60 fps with the original timestamps, encoded as
  8-bit H.264 MP4 starting at 0 ms.

The interval is chosen here too (`choose_interval`): the first continuous shot, 3 to 5 seconds.
"""

from __future__ import annotations

import io
import math
import tempfile
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - the deployed package always includes it
    pass

REFERENCE_MAX_SIDE = 1920
REFERENCE_MAX_PIXELS = 4_147_200
DRIVING_MAX_SIDE = 1920
DRIVING_MAX_PIXELS = 1920 * 1080
MAX_FPS = 60
# Decoded pixels per upload; a 4K source is decoded only for the selected interval.
SOURCE_MAX_PIXELS = 8192 * 8192
# The automatic interval: searched in the first 10 s of the clip, within a bounded frame count.
INTERVAL_WINDOW_S = 10
INTERVAL_MAX_FRAMES = 2400
INTERVAL_MIN_MS = 3000
INTERVAL_MAX_MS = 5000


class MediaRejected(Exception):
    """The upload cannot be used; `reason` is a stable code the interface explains."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _fit(width, height, max_side, max_pixels):
    scale = min(1.0, max_side / max(width, height), math.sqrt(max_pixels / (width * height)))
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def _flatten(image):
    if image.mode in {"RGBA", "LA", "PA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def reference(data):
    """Return (JPEG bytes, (width, height)) for an uploaded still image."""
    Image.MAX_IMAGE_PIXELS = SOURCE_MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            image = ImageOps.exif_transpose(image)
            image = _flatten(image)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MediaRejected("reference_too_large") from None
    except (OSError, ValueError, SyntaxError):
        raise MediaRejected("reference_unreadable") from None
    if min(image.size) < 64:
        raise MediaRejected("reference_too_small")
    size = _fit(*image.size, REFERENCE_MAX_SIDE, REFERENCE_MAX_PIXELS)
    if size != image.size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=93, optimize=True)
    return out.getvalue(), image.size


def _is_animation(path):
    with open(path, "rb") as stream:
        head = stream.read(16)
    return head[:6] in {b"GIF87a", b"GIF89a"} or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


def _animation_frames(path):
    """Yield (source seconds, end seconds, upright image), including the final frame hold."""
    try:
        with Image.open(path) as animation:
            elapsed = 0.0
            for index in range(getattr(animation, "n_frames", 1)):
                animation.seek(index)
                image = _flatten(animation)
                duration = max(1, animation.info.get("duration", 100)) / 1000
                yield elapsed, elapsed + duration, image
                elapsed += duration
    except (OSError, ValueError, SyntaxError):
        raise MediaRejected("driving_unreadable") from None


def _video_frames(path):
    try:
        container = av.open(
            str(path),
            options={
                "protocol_whitelist": "file",
                "format_whitelist": "mov,matroska,webm",
            },
        )
    except (av.error.FFmpegError, OSError, ValueError):
        raise MediaRejected("driving_unreadable") from None
    stream = next(
        (
            s
            for s in container.streams.video
            if not s.disposition & av.stream.Disposition.attached_pic
        ),
        None,
    )
    if stream is None or not stream.width or stream.width * stream.height > SOURCE_MAX_PIXELS:
        container.close()
        raise MediaRejected("driving_unreadable")
    stream.thread_type = "AUTO"
    origin = float(stream.start_time * stream.time_base) if stream.start_time else 0.0
    return container, stream, origin


def _frames(path, start=0):
    if _is_animation(path):
        yield from _animation_frames(path)
        return
    container, stream, origin = _video_frames(path)
    with container:
        if start > 1:
            container.seek(
                int((origin + start - 1) / stream.time_base), stream=stream, backward=True
            )
        try:
            for frame in container.decode(stream):
                if frame.time is None or frame.duration <= 0:
                    raise MediaRejected("driving_timestamps")
                image = frame.to_image()
                if frame.rotation:
                    image = image.rotate(frame.rotation, expand=True)
                time = frame.time - origin
                yield time, time + float(frame.duration * frame.time_base), image
        except av.error.FFmpegError:
            raise MediaRejected("driving_unreadable") from None


def _selected(source, start, end):
    """Clip displayed frame holds to the requested bounds; retain source timing at <=60 fps."""
    pending = None
    previous = None
    final_end = None
    for count, (time, frame_end, image) in enumerate(source):
        if count >= INTERVAL_MAX_FRAMES:
            raise MediaRejected("media_too_complex")
        if previous is not None and time <= previous:
            raise MediaRejected("driving_timestamps")
        previous = time
        if time >= end - 1e-9:
            final_end = min(end, time)
            break
        final_end = min(end, frame_end)
        if time <= start:
            pending = (time, image)
            continue
        if pending is not None:
            shown = max(start, pending[0])
            if time - shown < 1 / MAX_FPS - 1e-9:
                continue
            yield shown, time, pending[1], pending[0]
        elif time > start + 1e-6:
            raise MediaRejected("driving_timestamps")
        pending = (time, image)
    if pending is not None and final_end is not None and final_end > start:
        yield max(start, pending[0]), final_end, pending[1], pending[0]


def driving(path, start_ms, duration_ms):
    """Normalize precisely the selected action, validate the bytes on the engine's timeline.

    No tail from the next shot is included. Packet durations retain the last displayed hold.
    `first_source_ms` distinguishes the frame on screen at the requested start from its PTS.
    """
    start, end = start_ms / 1000, (start_ms + duration_ms) / 1000
    with tempfile.NamedTemporaryFile(suffix=".mp4") as target:
        info = _encode_to(target.name, _frames(path, start), start, end, duration_ms)
        clip = Path(target.name).read_bytes()
    validate_clip(clip, duration_ms)
    return clip, info


def _encode_to(out, source, start, end, duration_ms):
    output = video = None
    times, durations = [], {}
    first_source = final_end = None
    try:
        for time, frame_end, image, source_time in _selected(source, start, end):
            if output is None:
                size = _fit(*image.size, DRIVING_MAX_SIDE, DRIVING_MAX_PIXELS)
                output = av.open(out, "w", format="mp4", options={"movflags": "+faststart"})
                video = output.add_stream("libx264", rate=MAX_FPS)
                video.width, video.height = size
                video.pix_fmt = "yuv420p"
                video.time_base = video.codec_context.time_base = Fraction(1, 90_000)
                # No B-frame DTS reordering: MP4's declared duration must include the final
                # variable-duration display hold, as well as the decoder's final PTS.
                video.options = {"crf": "17", "preset": "veryfast", "profile": "high", "bf": "0"}
                first_source = source_time
            if image.size != (video.width, video.height):
                image = image.resize((video.width, video.height), Image.Resampling.LANCZOS)
            pts = round((time - start) * 90_000)
            durations[pts] = round((frame_end - start) * 90_000) - pts
            frame = av.VideoFrame.from_image(image).reformat(format="yuv420p")
            frame.pts, frame.time_base = pts, Fraction(1, 90_000)
            for packet in video.encode(frame):
                packet.duration = durations[packet.pts]
                output.mux(packet)
            times.append(time - start)
            final_end = frame_end
        if output is None or final_end * 1000 + 0.01 < (start * 1000 + duration_ms):
            raise MediaRejected("driving_too_short")
        for packet in video.encode():
            packet.duration = durations[packet.pts]
            output.mux(packet)
        return {
            "width": video.width,
            "height": video.height,
            "frames": len(times),
            "fps": round((len(times) - 1) / times[-1], 2) if times[-1] else None,
            "source_start_ms": round(start * 1000),
            "source_end_ms": round(end * 1000),
            "first_source_ms": first_source * 1000,
            "duration_ms": duration_ms,
        }
    finally:
        if output is not None:
            output.close()


def _engine_small(image):
    """Exact engine canvas, fitting and cut thumbnail. Cross-checked against media.prepare."""
    w, h = image.size
    size = (512, 512) if 0.8 <= w / h <= 1.25 else ((896, 512) if w > h else (512, 896))
    content = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "black")
    canvas.paste(
        content.convert("RGB"), ((size[0] - content.width) // 2, (size[1] - content.height) // 2)
    )
    return np.asarray(canvas.resize((64, 64))).astype(float) / 255


def _is_cut(a, b):
    ha = np.histogram(a, bins=32, range=(0, 1))[0] / a.size
    hb = np.histogram(b, bins=32, range=(0, 1))[0] / b.size
    return float(np.abs(a - b).mean()) > 0.40 and float(np.abs(ha - hb).sum()) > 0.65


def validate_clip(clip, duration_ms):
    """Enforce the existing worker CPU cut/timestamp/rate boundary on the normalized bytes.

    The worker rounds each 16 Hz target to milliseconds and picks the nearest source PTS,
    preferring the earlier frame on ties. Matching only its thresholds is insufficient.
    """
    times, frames = [], []
    last_end = 0
    with av.open(io.BytesIO(clip)) as container:
        stream = container.streams.video[0]
        if stream.average_rate and stream.average_rate > MAX_FPS:
            raise MediaRejected("driving_timestamps")
        for frame in container.decode(stream):
            ms = float(frame.pts * frame.time_base) * 1000
            if times and ms <= times[-1]:
                raise MediaRejected("driving_timestamps")
            last_end = ms + float(frame.duration * frame.time_base) * 1000
            times.append(ms)
            frames.append(_engine_small(frame.to_image()))
    if not frames or last_end + 1 < duration_ms:
        raise MediaRejected("driving_too_short")
    selected = [
        min(range(len(times)), key=lambda i: abs(times[i] - round(t * 1000 / 16)))
        for t in range(math.ceil(duration_ms * 16 / 1000))
    ]
    for left, right in zip(selected, selected[1:], strict=False):
        if _is_cut(frames[left], frames[right]):
            raise MediaRejected("driving_scene_cut")


def _intervals(path):
    """Try the complete leading interval first, then earlier completed shots.

    Source-frame cuts only suggest alternatives after the engine-timeline check refuses the
    complete interval. A flash the engine never samples must not shorten valid action.
    """
    shot, previous, end = 0.0, None, 0.0
    cuts, completed, offered = False, [], set()

    def interval(start, stop):
        start_ms = math.ceil(start * 1000 - 1e-6)
        length = min(INTERVAL_MAX_MS, math.floor(stop * 1000 + 1e-6) - start_ms)
        return start_ms, length

    for count, (time, frame_end, image) in enumerate(_frames(path)):
        if count >= INTERVAL_MAX_FRAMES:
            raise MediaRejected("media_too_complex")
        if time >= INTERVAL_WINDOW_S:
            break
        size = _fit(*image.size, DRIVING_MAX_SIDE, DRIVING_MAX_PIXELS)
        small = _engine_small(image.resize(size, Image.Resampling.LANCZOS))
        if previous is not None and _is_cut(previous, small):
            cuts = True
            completed.append(interval(shot, time))
            shot = time
        previous = small
        end = min(frame_end, INTERVAL_WINDOW_S)
        if end >= INTERVAL_MAX_MS / 1000:
            proposals = [(0, INTERVAL_MAX_MS), *completed]
            if interval(shot, end)[1] == INTERVAL_MAX_MS:
                proposals.append(interval(shot, end))
            for candidate in proposals:
                if candidate[1] >= INTERVAL_MIN_MS and candidate not in offered:
                    offered.add(candidate)
                    yield candidate
            if interval(shot, end)[1] == INTERVAL_MAX_MS:
                return
    for candidate in [interval(0, end), *completed, interval(shot, end)]:
        if candidate[1] >= INTERVAL_MIN_MS and candidate not in offered:
            offered.add(candidate)
            yield candidate
    if not offered:
        raise MediaRejected("driving_scene_cut" if cuts else "driving_too_short")


def prepare_driving(path, interval=None):
    """Return (source start, duration, normalized bytes, info); bounded automatic or explicit."""
    candidates = [interval] if interval is not None else _intervals(path)
    for start, duration in candidates:
        try:
            clip, info = driving(path, start, duration)
        except MediaRejected as error:
            if interval is not None or error.reason != "driving_scene_cut":
                raise
            continue
        return start, duration, clip, info
    raise MediaRejected("driving_scene_cut")


def choose_interval(path):
    """Earliest technically valid 3-5 second shot in the bounded ten-second automatic scan."""
    start, duration, _, _ = prepare_driving(path)
    return start, duration


def samples(clip, duration_ms, every_ms=250, side=832):
    """Upright stills of a normalized clip: the frame on screen every `every_ms` from 0, and the
    last frame before `duration_ms`, as [(ms, image)] at most `side` pixels across."""
    picked = []
    with av.open(io.BytesIO(clip)) as container:
        due = 0
        last = None
        for frame in container.decode(video=0):
            ms = round(frame.time * 1000)
            if ms >= duration_ms:
                break
            if ms >= due:
                picked.append((ms, _small(frame.to_image(), side)))
                due = (ms // every_ms + 1) * every_ms
                last = None
            else:
                last = frame
        if last is not None:
            picked.append((round(last.time * 1000), _small(last.to_image(), side)))
    return picked


def _small(image, side):
    image = image.convert("RGB")
    image.thumbnail((side, side), Image.Resampling.BILINEAR)
    return image


def jpeg(image, side=640, quality=85):
    image = _small(image, side)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()
