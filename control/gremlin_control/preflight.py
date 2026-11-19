"""Cheap input rejection before queueing GPU work; never modifies accepted input bytes.

Native decoders run in a bounded child. This checks the reference and driving headers/first
frame, not tracking, shot continuity or output quality, which remain worker responsibilities.
It also derives the output geometry the worker will produce for the admitted driving frame, by
the same rules as `imprint_worker.media.prepare`, `timing` and `encode_result`.
"""

import io
import json
import math
import subprocess
import sys
import warnings

from gremlin_control.limits import (
    DRIVING_MAX_FPS,
    DRIVING_MAX_PIXELS,
    DRIVING_MAX_SIDE,
    INPUT_LIMITS,
    OUTPUT_FPS,
    POSTER_MAX_SIDE,
    REFERENCE_MAX_PIXELS,
    STICKER_SIDE,
)

MESSAGE = "Use a valid still reference and driving clip within media and trim limits."


class Rejection(ValueError):
    """An input the preflight refuses, with a stable reason code and no decoder text."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def canvas(width, height):
    ratio = width / height
    if 0.8 <= ratio <= 1.25:
        return (STICKER_SIDE, STICKER_SIDE)
    return (896, STICKER_SIDE) if width > height else (STICKER_SIDE, 896)


def output_geometry(width, height, duration_ms):
    """Dimensions, frame counts and durations of the three results for an admitted clip."""
    from PIL import Image, ImageOps

    size = canvas(width, height)
    content = ImageOps.contain(Image.new("RGB", (width, height)), size).size
    even = (content[0] // 2 * 2, content[1] // 2 * 2)
    frames = math.ceil(duration_ms * OUTPUT_FPS / 1000)
    # The poster is the first cropped frame before the MP4's even-dimension resize.
    poster = Image.new("RGB", content)
    poster.thumbnail((POSTER_MAX_SIDE, POSTER_MAX_SIDE))
    return {
        "model_pixels": size[0] * size[1],
        "result.mp4": {
            "width": even[0],
            "height": even[1],
            "frames": frames,
            "duration_ms": frames * 1000 / OUTPUT_FPS,
        },
        # The WebP encoder merges identical consecutive frames, so its frame count depends on
        # content and is not published.
        "result.webp": {"width": STICKER_SIDE, "height": STICKER_SIDE, "duration_ms": duration_ms},
        "poster.jpg": {"width": poster.size[0], "height": poster.size[1]},
    }


def inspect(reference, driving, start_ms, duration_ms):
    import av
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = REFERENCE_MAX_PIXELS // 2
    with warnings.catch_warnings():
        # Match the worker's existing hard limit of twice MAX_IMAGE_PIXELS. Promoting its
        # warning to an error would reject previously valid references between 2 and 4 MP.
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(reference)) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise Rejection("reference_not_still")
                image.load()
        except Image.DecompressionBombError:
            raise Rejection("reference_too_large") from None
        except (UnidentifiedImageError, OSError, ValueError):
            raise Rejection("reference_undecodable") from None
    # Pillow supports animated WebP, which libav's webp_pipe decoder cannot reliably read.
    try:
        animation = Image.open(io.BytesIO(driving))
    except UnidentifiedImageError:
        animation = None
    if animation is not None:
        with animation:
            if animation.format not in {"GIF", "WEBP"}:
                raise Rejection("driving_undecodable")
            w, h = animation.size
            if w * h > DRIVING_MAX_PIXELS or max(w, h) > DRIVING_MAX_SIDE:
                raise Rejection("driving_resolution")
            elapsed = 0
            for index in range(getattr(animation, "n_frames", 1)):
                animation.seek(index)
                animation.load()
                duration = max(1, animation.info.get("duration", 100))
                if duration < 1000 / DRIVING_MAX_FPS:
                    raise Rejection("driving_frame_rate")
                elapsed += duration
                if elapsed >= start_ms + duration_ms:
                    return output_geometry(w, h, duration_ms)
            raise Rejection("driving_too_short")
    try:
        container = av.open(
            io.BytesIO(driving),
            options={
                "protocol_whitelist": "file",
                "format_whitelist": "gif,mov,matroska,webm,webp_pipe",
            },
        )
    except (av.error.FFmpegError, OSError, ValueError):
        raise Rejection("driving_undecodable") from None
    with container:
        stream = next(
            (
                s
                for s in container.streams.video
                if not (s.disposition & av.stream.Disposition.attached_pic)
            ),
            None,
        )
        if stream is None:
            raise Rejection("driving_undecodable")
        w, h = stream.width, stream.height
        if not w or not h or w * h > DRIVING_MAX_PIXELS or max(w, h) > DRIVING_MAX_SIDE:
            raise Rejection("driving_resolution")
        if stream.average_rate and stream.average_rate > DRIVING_MAX_FPS:
            raise Rejection("driving_frame_rate")
        # Containers without a reliable duration still get the worker's full trim check.
        duration = (
            float(stream.duration * stream.time_base) * 1000
            if stream.duration is not None and stream.time_base is not None
            else container.duration / 1000
            if container.duration is not None
            else None
        )
        if duration is not None and duration + 1 < start_ms + duration_ms:
            raise Rejection("driving_too_short")
        try:
            frame = next(container.decode(stream), None)
            if frame is None:
                raise Rejection("driving_undecodable")
            if frame.pts is None or frame.time_base is None:
                raise Rejection("driving_timestamps")
            frame.to_image().load()
        except (av.error.FFmpegError, OSError, ValueError):
            raise Rejection("driving_undecodable") from None
    return output_geometry(w, h, duration_ms)


def validate(inputs, request, *, screening=False):
    from gremlin_control.errors import api_error

    # Keep likeness media out of Lambda's warm /tmp, including after a killed invocation.
    data = len(inputs["reference"]).to_bytes(8) + inputs["reference"] + inputs["driving"]
    try:
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "gremlin_control.preflight",
                str(request["start_ms"]),
                str(request["duration_ms"]),
                "screen" if screening else "validate",
            ],
            input=data,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise api_error(
            503,
            "IMPRINT_PREFLIGHT_UNAVAILABLE",
            "Media validation timed out; no GPU work was queued.",
        ) from None
    if process.returncode:
        raise api_error(
            503,
            "IMPRINT_PREFLIGHT_UNAVAILABLE",
            "Media validation stopped; no GPU work was queued.",
        )
    result = json.loads(process.stdout)
    if "error" in result:
        raise api_error(422, "IMPRINT_MEDIA_INVALID", MESSAGE, reason=result["error"])
    return result


def main():
    try:
        size = int.from_bytes(sys.stdin.buffer.read(8))
        if not 0 < size <= INPUT_LIMITS["reference"]:
            raise Rejection("reference_undecodable")
        reference = sys.stdin.buffer.read(size)
        driving = sys.stdin.buffer.read(INPUT_LIMITS["driving"] + 1)
        if len(driving) > INPUT_LIMITS["driving"]:
            raise Rejection("driving_undecodable")
        result = inspect(reference, driving, int(sys.argv[1]), int(sys.argv[2]))
        if len(sys.argv) > 3 and sys.argv[3] == "screen":
            from PIL import Image

            from gremlin_control import moderation

            with Image.open(io.BytesIO(reference)) as image:
                images = [moderation.jpeg(image)]
            images += moderation.sample(driving, int(sys.argv[1]), int(sys.argv[2]))
            result = {"metadata": result, "samples": images}
    except Rejection as rejection:
        # Native decoder messages can include paths or input bytes. Only the code leaves.
        result = {"error": rejection.reason}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
