"""Real codecs exercise timing and integrity; these are transport fixtures, not AI output."""

import av
import numpy as np
import pytest
from imprint_worker.media import (
    InputRejected,
    encode_result,
    keep_static_overlays,
    prepare,
    timing,
    validate_masks,
    write_video,
)
from PIL import Image, ImageDraw


def test_broadcast_clock_redrawn_narrower_is_restored_whole_but_never_over_the_subject():
    size, rng = (256, 192), np.random.default_rng(0)
    driving, generated, masks = [], [], []
    for i in range(20):
        # A panning scene changes every frame, so only the broadcast clock is static.
        x = np.arange(size[0])[None, :] + 6 * i
        stripes = (((x // 8) % 2) * 120 + 60).astype(np.uint8).repeat(size[1], axis=0)
        scene = np.stack([stripes] * 3, axis=2)
        original = Image.fromarray(scene)
        draw = ImageDraw.Draw(original)
        draw.rectangle((4, 4, 48, 24), fill="white")
        draw.text((12, 8), f"85:1{i // 5}", fill="black")
        draw.rectangle((150, 60, 190, 180), fill="#eeaa55")
        noise = rng.integers(-6, 7, scene.shape)
        output = Image.fromarray(np.clip(scene.astype(int) + noise, 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(output)
        # fp8 renders drew the box 7 px narrower, which dropped the leading digit, garbled
        # the text differently in every frame and drew thin scene streaks through the box.
        draw.rectangle((11, 4, 48, 24), fill="white")
        draw.text((14, 8), "#&%@?"[i % 5 :] + "#&%@?"[: i % 5], fill="black")
        draw.line((0, 10 + i % 10, 60, 14 + i % 10), fill="#3355ff", width=2)
        if i == 10:
            # Something solid crossing the clock, such as a larger character's arm.
            draw.rectangle((24, 12, 40, 40), fill="#3b5b8f")
        if i == 15:
            # A hand crossing the left end, where the render drew no box to compare against.
            draw.rectangle((4, 12, 18, 40), fill="#c8906c")
        draw.rectangle((144, 54, 196, 186), fill="#3b5b8f")
        mask = Image.new("RGB", size, "white")
        ImageDraw.Draw(mask).rectangle((150, 60, 190, 180), fill=(0, 0, 255))
        driving.append(original)
        generated.append(output)
        masks.append(mask)
    result = [np.asarray(frame) for frame in keep_static_overlays(generated, driving, masks)]
    # Inside the restored box, clear of the blend that feathers its edge.
    clock = (slice(9, 20), slice(9, 44))
    subject = (slice(54, 187), slice(144, 197))
    for i in range(20):
        if i not in (10, 15):
            assert np.array_equal(result[i][clock], np.asarray(driving[i])[clock])
        assert np.array_equal(result[i][subject], np.asarray(generated[i])[subject])
    arm = (slice(14, 21), slice(27, 38))
    assert np.array_equal(result[10][arm], np.asarray(generated[10])[arm])
    hand = (slice(14, 39), slice(6, 17))
    assert np.array_equal(result[15][hand], np.asarray(generated[15])[hand])


def test_flat_scenery_under_a_moving_camera_is_never_restored_over_a_passing_character():
    # A pan leaves a flat wall or sky patch as static as a broadcast graphic, but it carries no
    # text or borders; restoring it whole would cut the arm of a character larger than the cat.
    size, rng = (256, 192), np.random.default_rng(0)
    driving, generated, masks = [], [], []
    for i in range(20):
        x = np.arange(size[0])[None, :] + 6 * i
        stripes = (((x // 8) % 2) * 120 + 60).astype(np.uint8).repeat(size[1], axis=0)
        scene = np.stack([stripes] * 3, axis=2)
        original = Image.fromarray(scene)
        draw = ImageDraw.Draw(original)
        draw.rectangle((205, 20, 229, 44), fill="#2a2d33")
        draw.rectangle((150, 60, 190, 180), fill="#eeaa55")
        noise = rng.integers(-6, 7, scene.shape)
        output = Image.fromarray(np.clip(scene.astype(int) + noise, 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(output)
        draw.rectangle((205, 20, 229, 44), fill="#2a2d33")
        draw.rectangle((144, 54, 196, 186), fill="#3b5b8f")
        if 10 <= i <= 12:
            draw.rectangle((190, 28, 240, 36), fill="#3b5b8f")
        mask = Image.new("RGB", size, "white")
        ImageDraw.Draw(mask).rectangle((150, 60, 190, 180), fill=(0, 0, 255))
        driving.append(original)
        generated.append(output)
        masks.append(mask)
    result = [np.asarray(frame) for frame in keep_static_overlays(generated, driving, masks)]
    arm = (slice(30, 35), slice(208, 227))
    for i in (10, 11, 12):
        assert np.array_equal(result[i][arm], np.asarray(generated[i])[arm])


def test_static_camera_scenery_is_never_restored_over_a_passing_character():
    # With a static camera a small static corner is scenery the character may pass in front
    # of: Kung Fu's fist crossed one, and restoring it there would clip the fist.
    size, driving, generated, masks = (256, 192), [], [], []
    x = np.arange(size[0])[None, :]
    stripes = (((x // 8) % 2) * 120 + 60).astype(np.uint8).repeat(size[1], axis=0)
    scene = np.stack([stripes] * 3, axis=2)
    for i in range(20):
        # The subject sweeps an L-shaped area, which cuts the top-right corner off.
        track = (150, 0, 220, 191) if i < 10 else (220, 30, 255, 191)
        original = Image.fromarray(scene)
        ImageDraw.Draw(original).rectangle(track, fill="#eeaa55")
        output = Image.fromarray(scene)
        draw = ImageDraw.Draw(output)
        left, top, right, bottom = track
        draw.rectangle((left - 4, max(top - 4, 0), min(right + 4, 255), bottom), fill="#3b5b8f")
        if 10 <= i <= 12:
            draw.rectangle((228, 5, 250, 35), fill="#3b5b8f")
        mask = Image.new("RGB", size, "white")
        ImageDraw.Draw(mask).rectangle(track, fill=(0, 0, 255))
        driving.append(original)
        generated.append(output)
        masks.append(mask)
    result = [np.asarray(frame) for frame in keep_static_overlays(generated, driving, masks)]
    # The fist's interior inside the corner; its blurred edge is softened as before.
    fist = (slice(9, 24), slice(231, 246))
    for i in (10, 11, 12):
        assert np.array_equal(result[i][fist], np.asarray(generated[i])[fist])


@pytest.mark.parametrize("rendered_background", [102, 160])
def test_static_scene_never_clips_a_larger_subject_or_restores_missed_original(
    rendered_background,
):
    # A static camera and a static border ring: everything inside the ring is enclosed.
    size, driving, generated, masks = (128, 96), [], [], []
    for _ in range(12):
        original = Image.new("RGB", size, (100, 100, 100))
        draw = ImageDraw.Draw(original)
        draw.rectangle((0, 0, 127, 95), outline="black", width=4)
        draw.rectangle((56, 40, 70, 70), fill="#eeaa55")
        output = Image.new("RGB", size, (rendered_background,) * 3)
        draw = ImageDraw.Draw(output)
        draw.rectangle((0, 0, 127, 95), outline="black", width=4)
        draw.rectangle((50, 34, 78, 76), fill="#3b5b8f")
        # The track misses the original subject's right edge (columns 68-70).
        mask = Image.new("RGB", size, "white")
        ImageDraw.Draw(mask).rectangle((56, 40, 67, 70), fill=(0, 0, 255))
        driving.append(original)
        generated.append(output)
        masks.append(mask)
    result = [np.asarray(frame) for frame in keep_static_overlays(generated, driving, masks)]
    subject = (slice(34, 77), slice(50, 79))
    interior = (slice(10, 30), slice(10, 40))
    for frame, output in zip(result, generated, strict=True):
        assert np.array_equal(frame[subject], np.asarray(output)[subject])
        if rendered_background == 160:
            assert np.array_equal(frame[interior], np.asarray(output)[interior])


def moving_frames(count, size=(128, 96)):
    frames = []
    for i in range(count):
        frame = Image.new("RGB", size, "#35424a")
        ImageDraw.Draw(frame).rectangle(
            (size[0] // 3 + i % 90, 20, size[0] // 3 + i % 90 + 20, 70), fill="#eeaa55"
        )
        frames.append(frame)
    return frames


def test_subsecond_fixture_duration_survives_model_padding_and_webp_encoding(tmp_path):
    source = tmp_path / "source.mkv"
    reference = tmp_path / "reference.png"
    Image.new("RGB", (100, 160), "white").save(reference)
    write_video(moving_frames(56), source)
    manifest = prepare(
        reference, source, tmp_path / "prepared", "test", start_ms=0, duration_ms=3400
    )
    assert sum(manifest["frame_durations_ms"]) == 3400
    assert len(manifest["source_times_ms"]) == 55
    assert manifest["model_frames"] == 57
    # The generated-frame boundary is supplied codec test frames, never attributed to SCAIL.
    frames = moving_frames(57, (manifest["width"], manifest["height"]))
    output = tmp_path / "encoded"
    result = encode_result(frames, manifest, output)
    with Image.open(output / "result.webp") as image:
        assert image.size == (512, 512) and image.n_frames >= 2
        elapsed = 0
        for i in range(image.n_frames):
            image.seek(i)
            image.load()
            elapsed += image.info["duration"]
        assert elapsed == 3400
        assert "A" in image.getbands()
    with av.open(str(output / "result.mp4")) as video:
        assert abs(video.duration / 1_000_000 - 3.4) <= 1 / 16
        assert len(list(video.decode(video=0))) >= 54
    assert result["result.webp"]["bytes"] < 8 * 1024**2


def test_requested_interval_cannot_read_past_the_source(tmp_path):
    source = tmp_path / "source.mkv"
    reference = tmp_path / "reference.png"
    Image.new("RGB", (64, 64), "white").save(reference)
    write_video(moving_frames(48), source)
    with pytest.raises(ValueError, match="exceeds the source"):
        prepare(
            reference,
            source,
            tmp_path / "prepared",
            "test",
            start_ms=0,
            duration_ms=5000,
        )


def test_missing_target_track_stops_before_generation(tmp_path):
    paths = []
    for i in range(5):
        image = Image.new("RGB", (32, 32), "white")
        if i != 3:
            ImageDraw.Draw(image).rectangle((5, 5, 20, 20), fill="blue")
        path = tmp_path / f"{i}.png"
        image.save(path)
        paths.append(path)
    with pytest.raises(InputRejected) as rejected:
        validate_masks(paths, 5)
    assert rejected.value.reason == "target_not_tracked"


def test_integer_webp_timestamps_have_no_cumulative_drift():
    starts, durations, count = timing(5000)
    assert starts[-1] == 4938 and sum(durations) == 5000 and count == 81
    assert set(durations) == {62, 63}
