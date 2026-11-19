"""Bounded local media preparation and output encoding, independent of GPU code."""

import hashlib
import json
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageFilter, ImageOps
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = 1920 * 1080


class InputRejected(ValueError):
    """These inputs cannot produce a result; the worker and its engine stay healthy.

    `reason` is one of the API's typed input rejection reasons.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def timing(duration_ms):
    if not 3000 <= duration_ms <= 5000:
        raise ValueError("Choose a 3-5 second interval")
    starts = [round(i * 1000 / 16) for i in range(math.ceil(duration_ms * 16 / 1000))]
    durations = [b - a for a, b in zip(starts, starts[1:] + [duration_ms], strict=True)]
    count = math.ceil((len(starts) - 1) / 4) * 4 + 1
    return starts, durations, count


def fit(image, size):
    content = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
    x, y = ((size[0] - content.width) // 2, (size[1] - content.height) // 2)
    canvas = Image.new("RGB", size, "black")
    canvas.paste(content.convert("RGB"), (x, y))
    return canvas, (x, y, content.width, content.height)


def cpus():
    """CPUs this process may use: the container's quota, not the host's count."""
    count = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            count = min(count, int(quota) // int(period))
    except (OSError, ValueError):
        pass
    return max(1, count or 1)


def _threads():
    # The GPU worker bills this CPU time too. PIL filters and numpy reductions release the
    # GIL, so frames are processed side by side on the container's CPUs. The hosts share 120
    # to 256 CPUs with other tenants and restoration took 2.9 to 31 s for the same job on
    # different hosts (October 2026); more runnable threads claim a larger share, and every
    # unit of work is one frame or one row band, so the count never changes a pixel.
    return min(32, cpus())


def _parallel(function, items):
    with ThreadPoolExecutor(_threads()) as pool:
        return list(pool.map(function, items))


def _median(stack):
    """Per-pixel median over the frames, computed in row bands on several CPUs."""
    bands = [rows for rows in np.array_split(np.arange(stack.shape[1]), _threads()) if len(rows)]
    medians = _parallel(lambda rows: np.median(stack[:, rows[0] : rows[-1] + 1], axis=0), bands)
    return np.concatenate(medians, axis=0)


def write_video(frames, path, *, lossless=True):
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1" if lossless else "libx264", rate=16)
        stream.width, stream.height = frames[0].size
        stream.pix_fmt = "bgr0" if lossless else "yuv420p"
        if not lossless:
            stream.options = {"crf": "18", "preset": "medium"}
        for index, image in enumerate(frames):
            frame = av.VideoFrame.from_image(image.convert("RGB"))
            frame.pts, frame.time_base = index, Fraction(1, 16)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def read_video(path):
    with av.open(str(path)) as container:
        return [frame.to_image().convert("RGB") for frame in container.decode(video=0)]


def _blur(array, radius):
    image = Image.fromarray(np.asarray(array).astype(np.uint8))
    return np.asarray(image.filter(ImageFilter.GaussianBlur(radius))).astype(np.int16)


def _steady(frames, radius, tolerance):
    """Pixels that stay within the tolerance of their blurred median in 90% of the frames."""
    blurred = np.stack(_parallel(lambda frame: _blur(frame, radius), frames))
    median = _median(blurred)
    close = _parallel(lambda frame: np.abs(frame - median).max(axis=2) < tolerance, blurred)
    return np.mean(close, axis=0) >= 0.9


def subject_pixels(rgb):
    """Selected-track pixels of a SCAIL-2 colored mask: the palette's blue."""
    return (rgb[:, :, 2] > 225) & (rgb[:, :, 0] < 30) & (rgb[:, :, 1] < 30)


# Components are 4-connected, as PIL's floodfill was before scipy's labelling replaced it on
# 30 October 2026 for speed; the labelled sets are the same, so no restored pixel changed.
def _small_enclosed_holes(region, subject, max_area):
    """Areas fully enclosed by the region that are small and do not touch the subject."""
    labels, count = ndimage.label(np.pad(~region, 1, constant_values=True))
    outside = labels[0, 0]
    labels = labels[1:-1, 1:-1]
    keep = np.bincount(labels.ravel(), minlength=count + 1) <= max_area
    keep[0] = keep[outside] = False
    # A hole at the subject may be the new character or a part the track missed.
    keep[np.unique(labels[subject])] = False
    return keep[labels]


def _components(mask):
    """Connected areas of a boolean mask, each as a boolean array."""
    labels, count = ndimage.label(mask)
    for index in range(1, count + 1):
        yield labels == index


def keep_static_overlays(generated, driving, masks):
    """Restore screen-static driving content, such as broadcast graphics, that diffusion redraws.

    The model regenerates every pixel from a half-resolution scene, which garbles small text.
    A pixel is restored only if it is static through the driving clip, the generator kept
    coarsely the same static content, it lies outside the tracked subject, and nothing passes
    over it in that generated frame. Small enclosed holes away from the subject, such as
    changing digits inside a static box, are filled when they pass the same coarse check.

    Over a moving scene a small static island that carries text or borders is a screen
    overlay, so once the generator kept most of it, it is restored whole, digits included, in
    every frame unless something solid crosses it. fp8 renders draw the Enzo clock a few
    pixels narrower and draw scene streaks through it; the per-pixel checks then dropped its
    leading digit. Flat scenery also looks static under a moving camera, and grain or an
    exposure drift makes a static camera look moving, so neither passes as an overlay.
    """
    if len(generated) != len(driving) or any(
        output.size != source.size for output, source in zip(generated, driving, strict=True)
    ):
        raise ValueError("Generated frames do not match the driving clip")
    rendered = np.stack([np.asarray(frame) for frame in generated]).astype(np.int16)
    source = np.stack([np.asarray(frame) for frame in driving]).astype(np.int16)
    rendered_median = _median(rendered)
    source_median = _median(source)
    # A slight blur keeps source compression noise from speckling static graphics.
    still = _steady(source, 1, 12)
    coarse = np.abs(_blur(source_median, 8) - _blur(rendered_median, 8)).max(axis=2) < 30
    # Blurred values mix across a boundary, so trust the match only 6 px away from a mismatch.
    changed = Image.fromarray((~coarse).astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(13))
    kept = coarse & ~np.asarray(changed).astype(bool)
    subject = np.zeros(still.shape, bool)
    for mask in masks:
        subject |= subject_pixels(np.asarray(mask))
    subject = np.asarray(
        Image.fromarray(subject.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(9))
    ).astype(bool)
    closed = Image.fromarray((still & kept).astype(np.uint8) * 255)
    closed = closed.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    region = np.asarray(closed).astype(bool) & kept & ~subject
    region |= _small_enclosed_holes(region, subject, region.size // 200) & kept
    overlays = np.zeros(region.shape, bool)
    # With a static camera a static area may be scenery the character passes in front of, as
    # Kung Fu's fist does, so only a scene that is at least three-quarters moving qualifies.
    # Grain and exposure drift unsettle single pixels, so the camera is judged on smoothed
    # frames: unsmoothed, grain of sigma 16 made the Kung Fu street look moving.
    calm = _steady(source, 3, 16)
    if (~subject).any() and calm[~subject].mean() <= 0.25:
        # Graphics carry text and borders: strong static edges, over 40 levels across 2 px,
        # inside the island. Flat sky or walls that a moving camera leaves static have none.
        gray = _blur(source_median, 1).mean(axis=2)
        edges = np.zeros(gray.shape, bool)
        edges[:, 1:-1] = np.abs(gray[:, 2:] - gray[:, :-2]) > 40
        edges[1:-1] |= np.abs(gray[2:] - gray[:-2]) > 40
        inner = Image.fromarray((still & ~subject).astype(np.uint8) * 255)
        edges &= np.asarray(inner.filter(ImageFilter.MinFilter(3))).astype(bool)
        for component in _components(still & ~subject):
            # Overlay-sized (at most 2% of the frame), mostly kept and detailed.
            if (
                component.sum() <= component.size // 50
                and coarse[component].mean() >= 0.5
                and edges[component].mean() >= 0.06
            ):
                overlays |= component
        overlays |= _small_enclosed_holes(overlays, subject, overlays.size // 200)
    rendered_reference = _blur(rendered_median, 6)
    crossings = [None] * len(rendered)
    if overlays.any():
        # A render and its driving frame share the scene's motion, so their coarse difference
        # cancels it, and its usual value is what the render always draws differently, such as
        # the narrower fp8 clock. Where a frame departs from that, something solid crosses the
        # overlay, such as a larger character's arm, and stays on top; thin streaks barely move
        # a coarse difference.
        pairs = list(zip(rendered, source, strict=True))
        differences = np.stack(_parallel(lambda pair: _blur(pair[0], 8) - _blur(pair[1], 8), pairs))
        usual = _median(differences)
        rendered_fine = _blur(rendered_median, 1)

        def crossing(item):
            frame, change = item
            # The coarse test also fires around a crossing object and misses its weaker edges;
            # the object is the overlay pixels the render draws unlike usual that connect to
            # where the test fired.
            unusual = overlays & (np.abs(_blur(frame, 1) - rendered_fine).max(axis=2) >= 30)
            labels, count = ndimage.label(unusual)
            crossed = np.zeros(count + 1, bool)
            crossed[np.unique(labels[unusual & (np.abs(change - usual).max(axis=2) >= 30)])] = True
            crossed[0] = False
            return crossed[labels]

        crossings = _parallel(crossing, list(zip(rendered, differences, strict=True)))

    def restore(item):
        frame, original, crossing = item
        passing = np.abs(_blur(frame, 6) - rendered_reference).max(axis=2) >= 25
        active = region & ~passing
        if crossing is not None:
            active |= overlays & ~crossing
        # Feather inward only: the blend never reaches pixels outside the restored region.
        soft = Image.fromarray(active.astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(1.5))
        alpha = (np.asarray(soft) * active)[:, :, None] / 255
        blended = original * alpha + frame * (1 - alpha)
        return Image.fromarray(blended.round().astype(np.uint8))

    return _parallel(restore, list(zip(rendered, source, crossings, strict=True)))


def prepare(reference, driving, directory, prefix, *, start_ms, duration_ms):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    starts, durations, count = timing(duration_ms)
    decoded, timestamps = [], []
    with av.open(
        str(driving),
        options={
            "protocol_whitelist": "file",
            "format_whitelist": "gif,mov,matroska,webm,webp_pipe",
        },
    ) as container:
        stream = next(
            s
            for s in container.streams.video
            if not (s.disposition & av.stream.Disposition.attached_pic)
        )
        if (
            stream.width * stream.height > 1920 * 1080
            or stream.width > 1920
            or stream.height > 1920
        ):
            raise ValueError("Driving resolution exceeds the admission limit")
        if stream.average_rate and stream.average_rate > 60:
            raise ValueError("Source frame rate exceeds 60 fps")
        base = None
        last_end = 0
        for index, frame in enumerate(container.decode(stream)):
            if index > 3600:
                raise ValueError("Source exceeds the decode limit")
            timestamp = float(frame.pts * frame.time_base) * 1000
            if base is None:
                base = timestamp
            timestamp -= base
            last_end = timestamp + float(frame.duration * frame.time_base) * 1000
            if timestamp > start_ms + duration_ms + 100:
                break
            if timestamp >= max(0, start_ms - 100):
                timestamps.append(timestamp)
                decoded.append(frame.to_image().convert("RGB"))
        if not decoded or last_end + 1 < start_ms + duration_ms:
            raise ValueError("The requested interval exceeds the source duration")
    w, h = decoded[0].size
    size = (512, 512) if 0.8 <= w / h <= 1.25 else ((896, 512) if w > h else (512, 896))
    selected = [
        min(range(len(timestamps)), key=lambda i: abs(timestamps[i] - (start_ms + t)))
        for t in starts
    ]
    frames = [fit(decoded[i], size)[0] for i in selected]
    # Large simultaneous histogram and pixel changes flag shot boundaries before paid diffusion.
    for left, right in pairwise(frames):
        a = np.asarray(left.resize((64, 64))).astype(float) / 255
        b = np.asarray(right.resize((64, 64))).astype(float) / 255
        ha = np.histogram(a, bins=32, range=(0, 1))[0] / a.size
        hb = np.histogram(b, bins=32, range=(0, 1))[0] / b.size
        if np.abs(a - b).mean() > 0.40 and np.abs(ha - hb).sum() > 0.65:
            raise InputRejected("driving_scene_cut")
    transform = fit(decoded[0], size)[1]
    frames += [frames[-1]] * (count - len(frames))
    write_video(frames, directory / f"{prefix}-driving.mkv")
    frames[0].save(directory / f"{prefix}-first.png")
    with Image.open(reference) as im:
        if getattr(im, "n_frames", 1) != 1:
            raise ValueError("Select a still identity reference")
        im = ImageOps.exif_transpose(im)
        ref, ref_transform = fit(im, size)
        ref.save(directory / f"{prefix}-reference.png")
        alpha = im.getchannel("A") if "A" in im.getbands() else None
        if alpha is not None and alpha.getextrema()[0] < 255:
            matte = Image.new("L", size, 0)
            x, y, rw, rh = ref_transform
            matte.paste(alpha.resize((rw, rh), Image.Resampling.LANCZOS), (x, y))
            matte.save(directory / f"{prefix}-alpha.png")
    manifest = {
        "duration_ms": duration_ms,
        "start_ms": start_ms,
        "width": size[0],
        "height": size[1],
        "model_frames": count,
        "output_frames": len(starts),
        "frame_starts_ms": starts,
        "frame_durations_ms": durations,
        "source_times_ms": [timestamps[i] for i in selected],
        "content_rect": transform,
        "reference_rect": ref_transform,
        "reference_sha256": sha256(reference),
        "driving_sha256": sha256(driving),
    }
    (directory / f"{prefix}-preparation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def validate_masks(paths, expected_count):
    if len(paths) != expected_count:
        raise ValueError("Tracking frame count mismatch")
    fractions = []
    for path in paths:
        fraction = float(subject_pixels(np.asarray(Image.open(path).convert("RGB"))).mean())
        fractions.append(fraction)
        if not 0.002 <= fraction <= 0.95:
            raise InputRejected("target_not_tracked")
    return fractions


def encode_result(generated, manifest, output, *, ownership_fd=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if len(generated) != manifest["model_frames"]:
        raise ValueError("Generation frame count mismatch")
    if any(im.size != (manifest["width"], manifest["height"]) for im in generated):
        raise ValueError("Generation dimensions mismatch")
    x, y, w, h = manifest["content_rect"]
    frames = [
        im.convert("RGB").crop((x, y, x + w, y + h))
        for im in generated[: manifest["output_frames"]]
    ]
    signatures = {im.resize((32, 32)).tobytes() for im in frames}
    if len(signatures) < 2 or max(np.asarray(im).mean() for im in frames) < 1:
        raise ValueError("Generation is constant or black")
    even = (w // 2 * 2, h // 2 * 2)
    write_video(
        [im.resize(even, Image.Resampling.LANCZOS) for im in frames],
        output / "intermediate.mkv",
        lossless=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(output / "intermediate.mkv"),
            "-t",
            str(manifest["duration_ms"] / 1000),
            "-an",
            "-c:v",
            "libx264",
            # x264 picks its thread count from the frame height (16 at 512 px), as in the
            # accepted encodes; passing the container's 120 CPUs changed every encoded frame.
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output / "result.mp4"),
        ],
        check=True,
        timeout=120,
        # If the worker dies while encoding, a surviving ffmpeg must continue fencing a
        # replacement worker's cleanup until its final scratch write has finished.
        pass_fds=() if ownership_fd is None else (ownership_fd,),
        capture_output=True,
    )
    stickers = []
    for im in frames:
        scaled = ImageOps.contain(im, (512, 512), Image.Resampling.LANCZOS)
        square = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        square.paste(scaled, ((512 - scaled.width) // 2, (512 - scaled.height) // 2))
        stickers.append(square)
    stickers[0].save(
        output / "result.webp",
        save_all=True,
        append_images=stickers[1:],
        duration=manifest["frame_durations_ms"],
        loop=0,
        quality=90,
        # Method 6 held an idle GPU for 45 s per clip to save 2% of file size at the same
        # measured fidelity (41.5 dB PSNR against the frames either way); method 4 takes 2 s.
        method=4,
    )
    frames[0].thumbnail((384, 384))
    frames[0].save(output / "poster.jpg", quality=90)
    with Image.open(output / "result.webp") as check:
        if check.size != (512, 512) or check.n_frames < 2:
            raise ValueError("Invalid animated sticker")
        elapsed = 0
        for i in range(check.n_frames):
            check.seek(i)
            check.load()
            elapsed += check.info["duration"]
        if elapsed != manifest["duration_ms"]:
            raise ValueError("Animated sticker timing drift")
    if (output / "result.webp").stat().st_size > 8 * 1024 * 1024:
        raise ValueError("High-quality sticker exceeds library size limit")
    (output / "intermediate.mkv").unlink()
    return {
        name: {"sha256": sha256(output / name), "bytes": (output / name).stat().st_size}
        for name in ("result.mp4", "result.webp", "poster.jpg")
    }
