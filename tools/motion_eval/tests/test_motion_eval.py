"""Offline checks of the measurement, preparation geometry and spending fences."""

import json

import numpy as np
import pytest
from imprint_worker.media import read_video, write_video
from PIL import Image

from tools.hosted_eval.ledger import Hold, Ledger
from tools.motion_eval import fidelity, matrix, run
from tools.motion_eval.prepare import subject_masks, wide_canvas, write_mask_video


def moving_square(frames, size=64, step=2, start=8, background=None):
    clips = []
    for index in range(frames):
        frame = (
            np.full((size, size, 3), 40, dtype=np.uint8)
            if background is None
            else background.copy()
        )
        x = start + step * index
        frame[20:44, x : x + 16] = (220, 180, 90)
        clips.append(frame)
    return np.stack(clips)


def square_masks(frames, size=64, step=2, start=8):
    masks = np.zeros((frames, size, size), dtype=bool)
    for index in range(frames):
        x = start + step * index
        masks[index, 20:44, x : x + 16] = True
    return masks


def test_identical_motion_scores_high_and_unrelated_motion_scores_low():
    driver = moving_square(12)
    masks = square_masks(12)
    same = fidelity.measure(driver, driver.copy(), masks)
    assert same["background_psnr_db"] == 99.0
    assert same["subject_flow_cosine"] > 0.95
    assert same["subject_flow_error"] < 0.1
    assert same["motion_timing_r"] is None or same["motion_timing_r"] > 0.9
    # Same subject moving the opposite way: direction agreement collapses.
    reverse = moving_square(12, step=-2, start=30)
    opposite = fidelity.measure(driver, reverse, masks)
    assert opposite["subject_flow_cosine"] < 0
    # A frozen subject keeps the background but loses the motion energy.
    frozen = np.repeat(driver[:1], 12, axis=0)
    still = fidelity.measure(driver, frozen, masks)
    assert still["subject_energy_ratio"] < 0.2
    assert still["background_psnr_db"] > 40


def test_tracking_masks_round_trip_and_dilate(tmp_path):
    masks = square_masks(5)
    frames = []
    for mask in masks:
        rgb = np.zeros((64, 64, 3), dtype=np.uint8)
        rgb[mask] = (0, 0, 255)
        frames.append(Image.fromarray(rgb))
    write_video(frames, tmp_path / "tracking.mp4", lossless=False)
    decoded = subject_masks(tmp_path / "tracking.mp4", 5)
    assert decoded.shape == (5, 64, 64)
    assert (decoded == masks).mean() > 0.98
    with pytest.raises(Hold):
        subject_masks(tmp_path / "tracking.mp4", 6)
    write_mask_video(decoded, tmp_path / "mask.mp4", dilation=4)
    grown = np.asarray(read_video(tmp_path / "mask.mp4")[0].convert("L")) > 128
    assert grown.sum() > masks[0].sum()
    assert grown[masks[0]].all()
    write_mask_video(decoded, tmp_path / "inverted.mp4", dilation=4, invert=True)
    inverted = np.asarray(read_video(tmp_path / "inverted.mp4")[0].convert("L")) > 128
    assert (inverted == ~grown).mean() > 0.98


def test_wide_canvas_keeps_the_square_scene_in_the_crop_rectangle(tmp_path):
    scene = np.zeros((512, 512, 3), dtype=np.uint8)
    scene[:, :, 1] = np.linspace(0, 255, 512, dtype=np.uint8)[None, :]
    Image.fromarray(scene).save(tmp_path / "scene.png")
    wide_canvas(tmp_path / "scene.png", tmp_path / "wide.png")
    with Image.open(tmp_path / "wide.png") as image:
        assert image.size == matrix.WIDE
        x, y, w, h = matrix.WIDE_CROP
        centre = np.asarray(image.convert("RGB").crop((x, y, x + w, y + h)))
    assert np.abs(centre.astype(int) - scene.astype(int)).mean() < 3


def test_prices_and_reservations_follow_the_verified_list():
    assert matrix.list_usd("S2", 57) == pytest.approx(0.7125)
    assert matrix.list_usd("S2", 81) == pytest.approx(1.0125)
    assert matrix.list_usd("VACE", 57) == pytest.approx(0.1425)
    assert matrix.reserve_usd("W27", 81) == pytest.approx(1.0125)
    total = sum(
        matrix.reserve_usd(c["model"], 57 if c["fixture"] == "kungfu" else 81) for c in matrix.CELLS
    )
    assert total < float(run.HARD_CAP)
    for config in matrix.CELLS:
        media = {name: "data:" for name in matrix.inputs_for(config)}
        body = matrix.body(config, "prompt", media)
        assert body["seed"] == 42 and body["enable_safety_checker"] is True
    square = matrix.body(
        matrix.cell("KF-S2QH"), "prompt", {"still.png": "s", "driver-model.mp4": "d"}
    )
    assert square["image_url"] == "s" and square["subject_type"] == "human"
    assert "inputs" not in square
    assert (
        matrix.body(
            matrix.cell("EN-VACEP"), "p", {n: n for n in matrix.inputs_for(matrix.cell("EN-VACEP"))}
        )["preprocess"]
        is True
    )


def test_campaign_cap_is_six_dollars(tmp_path):
    budget = run.budget(tmp_path)
    with pytest.raises(Hold):
        budget.approve("6.01")
    budget.approve("6.00")
    assert json.loads((tmp_path / "budget.json").read_text())["scope"] == run.SCOPE
    Ledger(tmp_path).append("intent", cell="x", attempt="a", reserve_usd="5.50")
    with pytest.raises(Hold, match="ceiling"):
        run.budget(tmp_path).reserve("0.51")
