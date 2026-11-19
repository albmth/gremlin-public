"""Regression expectations label user intent independently of detector decisions."""

import io

import pytest
from gremlin_web import normalize, selection
from PIL import Image

from tests.benchmark import LABELS, path


def test_owner_subjects_are_automatic_and_the_whole_action_is_preserved():
    reference, _ = normalize.reference(path("reference").read_bytes())
    with Image.open(io.BytesIO(reference)) as photo:
        ref = selection.choose_reference(photo)
    start, duration = normalize.choose_interval(path("driving"))
    clip, _ = normalize.driving(path("driving"), start, duration)
    target = selection.analyze_clip(normalize.samples(clip, duration), duration)
    for side, choice in (("reference", ref), ("driving", target)):
        assert choice["why"] == "auto", choice
        x0, y0, x1, y1 = LABELS[side]["interior"]
        assert x0 <= choice["point"]["x"] <= x1
        assert y0 <= choice["point"]["y"] <= y1
    assert (start, duration) == (0, LABELS["driving"]["duration_ms"])
    assert target["duration_ms"] == duration


@pytest.mark.parametrize("later", ["duplicate", "occlusion", "overlap"])
def test_later_detection_uncertainty_cannot_invent_a_departure(monkeypatch, later):
    main = {"label": "person", "score": 0.9, "box": [0.1, 0.1, 0.9, 0.95]}
    other = {"label": "person", "score": 0.7, "box": [0.4, 0.1, 0.9, 0.95]}
    detections = [[main] for _ in range(21)]
    detections[6:9] = {
        "duplicate": [[main, other]] * 3,
        "occlusion": [[]] * 3,
        "overlap": [[other, main]] * 3,
    }[later]
    frames = iter(detections)
    monkeypatch.setattr(selection, "detect", lambda _: next(frames))
    choice = selection.analyze_clip([(i * 250, None) for i in range(21)], 5000)
    assert choice["why"] == "auto"
    assert choice["duration_ms"] == 5000
