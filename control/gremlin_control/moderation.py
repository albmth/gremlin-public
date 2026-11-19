"""Bounded managed screening; samples never replace the original media."""

import base64
import io
import json
import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

from gremlin_control.errors import api_error

MAX_ATTEMPTS = 3
CLAIM_SECONDS = 35
REJECTION_MESSAGE = "Content did not pass the safety policy."


class Rejected(Exception):
    """A managed policy refusal, without retaining private assessments."""


def unavailable():
    return api_error(
        503, "IMPRINT_MODERATION_UNAVAILABLE", "Content checks are unavailable. Please try later."
    )


def jpeg(image):
    from PIL import ImageOps

    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((512, 512))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=85)
    return base64.b64encode(output.getvalue()).decode()


def sample(data, start_ms, duration_ms):
    """Sample the frames covering each second and the end of the selected interval."""
    import av
    from PIL import Image, UnidentifiedImageError

    if not 0 <= start_ms <= 60000 or not 0 < duration_ms <= 5100:
        raise ValueError("Invalid screening interval")
    end = start_ms + duration_ms
    targets = [start_ms + offset for offset in range(0, math.ceil(duration_ms), 1000)]
    targets = targets[:5] + [end - 0.001]
    frames = []

    def take(image, start, stop):
        while targets and start <= targets[0] < stop:
            frames.append(jpeg(image))
            targets.pop(0)

    try:
        image = Image.open(io.BytesIO(data))
    except UnidentifiedImageError:
        image = None
    if image is not None:
        with image:
            elapsed = 0
            for index in range(getattr(image, "n_frames", 1)):
                image.seek(index)
                # Animated WebP populates this frame's duration only during load().
                image.load()
                duration = max(1, image.info.get("duration", 100))
                take(image, elapsed, elapsed + duration)
                elapsed += duration
                if not targets:
                    break
    else:
        with av.open(
            io.BytesIO(data),
            options={"protocol_whitelist": "file", "format_whitelist": "mov,matroska,webm"},
        ) as container:
            stream = next(
                s
                for s in container.streams.video
                if not (s.disposition & av.stream.Disposition.attached_pic)
            )
            previous = None
            previous_at = 0
            for frame in container.decode(stream):
                if frame.pts is None or frame.time_base is None:
                    raise ValueError("Missing frame timestamp")
                at = float(frame.pts * frame.time_base) * 1000
                if previous is not None:
                    take(previous.to_image(), previous_at, at)
                previous, previous_at = frame, at
                if not targets:
                    break
            if targets and previous is not None:
                duration = (
                    float(previous.duration * previous.time_base) * 1000
                    if previous.duration
                    else 1000 / float(stream.average_rate)
                )
                take(previous.to_image(), previous_at, previous_at + duration)
    if targets or not frames:
        raise ValueError("Selected interval is incomplete")
    return list(dict.fromkeys(frames))


def managed_check(region, guardrail_id, version, images, prompt):
    config = Config(connect_timeout=1, read_timeout=4, retries={"total_max_attempts": 1})
    session = boto3.Session(region_name=region)
    bedrock = session.client("bedrock-runtime", config=config)
    rekognition = session.client("rekognition", config=config)

    def inspect(image):
        response = rekognition.detect_moderation_labels(
            Image={"Bytes": base64.b64decode(image, validate=True)}, MinConfidence=70
        )
        if not isinstance(response.get("ModerationLabels"), list):
            raise ValueError("Unknown screening result")
        # Rekognition's Graphic Violence parent includes ordinary physical fighting.
        # Refuse its harmful leaf categories without refusing non-graphic martial arts.
        denied = {
            "Explicit",
            "Explicit Nudity",
            "Blood & Gore",
            "Self-Harm",
            "Visually Disturbing",
            "Hate Symbols",
        }
        if any(
            label.get("Name") in denied or label.get("ParentName") in denied
            for label in response["ModerationLabels"]
        ):
            raise Rejected

    def inspect_prompt():
        response = bedrock.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=version,
            source="INPUT",
            content=[{"text": {"text": prompt}}],
        )
        if response.get("action") == "GUARDRAIL_INTERVENED":
            raise Rejected
        if response.get("action") != "NONE":
            raise ValueError("Unknown screening result")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(inspect, image) for image in images]
        if prompt is not None:
            futures.append(pool.submit(inspect_prompt))
        rejected = False
        for future in futures:
            try:
                future.result()
            except Rejected:
                rejected = True
        if rejected:
            raise Rejected


def check(settings, images, prompt=None):
    """A killed child cannot continue spending or publish after the 12-second budget."""
    payload = {
        "region": settings.moderation_region,
        "guardrail_id": settings.moderation_guardrail_id,
        "version": settings.moderation_guardrail_version,
        "images": images,
        "prompt": prompt,
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "gremlin_control.moderation", "check"],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise unavailable() from None
    if result.returncode == 2:
        raise Rejected
    if result.returncode != 0 or result.stdout != b"ok\n":
        raise unavailable()


def output_samples(data, duration_ms):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "gremlin_control.moderation", "sample", str(duration_ms)],
            input=data,
            capture_output=True,
            timeout=8,
            check=False,
        )
        if result.returncode:
            raise ValueError("Screening decode unavailable")
        images = json.loads(result.stdout)
        if not isinstance(images, list) or not 1 <= len(images) <= 6:
            raise ValueError("Screening frames unavailable")
        return images
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise unavailable() from None


def main():
    try:
        if sys.argv[1] == "check":
            managed_check(**json.loads(sys.stdin.buffer.read()))
            print("ok")
        else:
            print(
                json.dumps(sample(sys.stdin.buffer.read(30 * 1024**2 + 1), 0, float(sys.argv[2])))
            )
    except Rejected:
        return 2
    except Exception:
        # Neither native decoder messages nor managed assessments may enter logs.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
