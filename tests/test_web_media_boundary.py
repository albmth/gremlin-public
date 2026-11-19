"""Website intervals must pass the actual engine's CPU preparation on identical bytes.

These catch discarded shots, mismatched cut sampling, frame-rate limits and lost final holds.
No inference, services or paid calls.
"""

import io
from fractions import Fraction

import av
import pytest
from gremlin_control.preflight import inspect
from imprint_worker.media import prepare
from PIL import Image

from web.gremlin_web import normalize
from web.tests import media


def boundary(tmp_path, data, *, expected=None, start=None, duration=None, reference_data=None):
    raw = tmp_path / "source"
    raw.write_bytes(data)
    if start is None:
        start, duration = normalize.choose_interval(raw)
    if expected is not None:
        assert (start, duration) == expected
    if reference_data is None:
        reference_data = media.png(Image.new("RGB", (320, 240), "green"))
    reference, _ = normalize.reference(reference_data)
    clip, info = normalize.driving(raw, start, duration)
    inspect(reference, clip, 0, duration)
    photo, video = tmp_path / "photo.jpg", tmp_path / "clip.mp4"
    photo.write_bytes(reference)
    video.write_bytes(clip)
    prepared = prepare(
        photo, video, tmp_path / "prepared", "test", start_ms=0, duration_ms=duration
    )
    assert prepared["duration_ms"] == duration
    return info


@pytest.mark.parametrize(("cut", "expected"), [(2, (2000, 3500)), (3, (0, 3000)), (4, (0, 4000))])
def test_earliest_complete_valid_shot_is_retained(tmp_path, cut, expected):
    data = media.clip(
        lambda _: [],
        size=(320, 240),
        background_at=lambda t: (10, 10, 10) if t < cut else (235, 235, 240),
    )
    boundary(tmp_path, data, expected=expected)


@pytest.mark.parametrize("fps", [24, 30, 60, 75, 120])
def test_frame_rate_normalization_meets_control_and_engine_limits(tmp_path, fps):
    data = media.clip(lambda _: [], seconds=4, fps=fps, size=(320, 240))
    boundary(tmp_path, data, expected=(0, 4000))


def test_a_flash_seen_by_the_engine_is_excluded_from_the_selected_shot(tmp_path):
    data = media.clip(
        lambda _: [],
        size=(320, 240),
        background_at=lambda t: (235, 235, 240) if round(t * 30) == 7 else (10, 10, 10),
    )
    boundary(tmp_path, data)


def test_a_flash_absent_from_the_engine_timeline_does_not_shorten_action(tmp_path):
    data = media.clip(
        lambda _: [],
        size=(320, 240),
        background_at=lambda t: (235, 235, 240) if round(t * 30) == 5 else (10, 10, 10),
    )
    boundary(tmp_path, data, expected=(0, 5000))


def test_gif_final_frame_hold_is_preserved(tmp_path):
    boundary(tmp_path, media.fixture("Kung Fu").read_bytes(), expected=(0, 3400))


def test_a_non_frame_aligned_source_start_keeps_its_timeline(tmp_path):
    data = media.clip(lambda _: [], seconds=5, size=(320, 240))
    info = boundary(tmp_path, data, start=1035, duration=3500)
    assert info["source_start_ms"] == 1035 and info["source_end_ms"] == 4535
    assert info["first_source_ms"] == pytest.approx(1033.3333333333)


def test_variable_rate_and_nonzero_container_origin(tmp_path):
    out = io.BytesIO()
    times = list(range(0, 1000, 40)) + list(range(1000, 4000, 50))
    ends = times[1:] + [4000]
    with av.open(out, "w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=25)
        stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
        stream.time_base = stream.codec_context.time_base = Fraction(1, 1000)
        stream.options = {"bf": "0"}
        durations = {t + 5000: end - t for t, end in zip(times, ends, strict=True)}
        for t in times:
            frame = av.VideoFrame.from_image(Image.new("RGB", (320, 240), (80, 120, 80)))
            frame.pts, frame.time_base = t + 5000, Fraction(1, 1000)
            for packet in stream.encode(frame):
                packet.duration = durations[packet.pts]
                container.mux(packet)
        for packet in stream.encode():
            packet.duration = durations[packet.pts]
            container.mux(packet)
    info = boundary(tmp_path, out.getvalue(), expected=(0, 4000))
    assert info["first_source_ms"] == 0


def test_exif_reference_pixels_match_the_upright_coordinate_system():
    source = Image.new("RGB", (120, 80), "black")
    source.paste("red", (0, 0, 60, 80))
    exif = source.getexif()
    exif[274] = 6
    raw = io.BytesIO()
    source.save(raw, "JPEG", exif=exif)
    data, size = normalize.reference(raw.getvalue())
    with Image.open(io.BytesIO(data)) as upright:
        assert size == (80, 120) and upright.getexif().get(274) is None
        assert upright.getpixel((40, 20))[0] > 200
        assert upright.getpixel((40, 100))[0] < 20


def test_fractional_final_millisecond_is_not_rounded_past_the_media(tmp_path):
    data = media.clip(lambda _: [], seconds=3.5, fps=75, size=(320, 240))
    boundary(tmp_path, data, expected=(0, 3493))


def test_phone_rotation_is_baked_into_the_pixels_and_engine_geometry(tmp_path):
    raw = io.BytesIO()
    with av.open(raw, "w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
        stream.set_display_rotation(90)
        image = Image.new("RGB", (320, 240), "black")
        image.paste("red", (0, 0, 160, 240))
        for _ in range(105):
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    info = boundary(tmp_path, raw.getvalue(), expected=(0, 3500))
    assert (info["width"], info["height"]) == (240, 320)
    with av.open(str(tmp_path / "clip.mp4")) as normalized:
        frame = next(normalized.decode(video=0))
        assert frame.rotation == 0
        assert frame.to_image().getpixel((120, 260))[0] > 200


def test_exact_owner_pair_passes_actual_engine_cpu_preparation(tmp_path):
    from web.tests.benchmark import path

    boundary(
        tmp_path,
        path("driving").read_bytes(),
        expected=(0, 3410),
        reference_data=path("reference").read_bytes(),
    )
