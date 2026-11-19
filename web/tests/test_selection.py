"""The worker's automatic choices on real detector output: synthetic scenes built from a real
photo, and the accepted fixtures. No emulator or network is needed."""

import json
import tempfile
from pathlib import Path

import pytest
from gremlin_web import assist, normalize, selection
from PIL import Image

from tests import media

ROOT = Path(__file__).resolve().parents[2]


def accepted(name):
    request = json.loads((ROOT / f"tests/fixtures/requests/{name}.json").read_text())
    return request


def analyze(data, suffix):
    with tempfile.NamedTemporaryFile(suffix=suffix) as raw:
        Path(raw.name).write_bytes(data)
        start, duration = normalize.choose_interval(Path(raw.name))
        clip, _ = normalize.driving(Path(raw.name), start, duration)
    return (start, duration), selection.analyze_clip(normalize.samples(clip, duration), duration)


def inside(point, box):
    return box[0] <= point["x"] <= box[2] and box[1] <= point["y"] <= box[3]


@pytest.mark.parametrize(
    ("people", "why"),
    [
        ([(0.5, 0.8)], "auto"),
        ([(0.4, 0.85), (0.85, 0.3)], "auto"),
        ([(0.3, 0.75), (0.7, 0.75)], "competing"),
        ([], "no_subject"),
    ],
)
def test_the_photo_subject_is_chosen_only_when_unambiguous(people, why):
    assert selection.choose_reference(media.scene(people))["why"] == why


@pytest.mark.parametrize(
    ("name", "why"),
    [("one", "auto"), ("bystander", "auto"), ("two", "competing"), ("empty", "no_subject")],
)
def test_the_clip_character_is_chosen_only_when_unambiguous(name, why):
    _, result = analyze(media.clip(media.CLIPS[name]), ".mp4")
    assert result["why"] == why
    assert (result["point"] is not None) == (why == "auto")


def test_the_interval_keeps_an_earlier_valid_shot():
    def cut_at(seconds):
        return media.clip(
            lambda t: [] if t < seconds else [(0.5, 0.8)],
            background_at=lambda t: (10, 10, 10) if t < seconds else (235, 235, 240),
        )

    with tempfile.NamedTemporaryFile(suffix=".mp4") as raw:
        Path(raw.name).write_bytes(cut_at(2.0))
        assert normalize.choose_interval(Path(raw.name)) == (2000, 3500)
        Path(raw.name).write_bytes(cut_at(3.0))
        assert normalize.choose_interval(Path(raw.name)) == (0, 3000)


def test_the_accepted_fixtures_get_the_accepted_subjects():
    with Image.open(media.fixture("Screenshot")) as photo:
        reference = selection.choose_reference(photo.convert("RGB"))
    enzo = accepted("enzo")
    assert reference["why"] == "auto"
    assert inside(enzo["reference_point"], reference["candidates"][reference["subject"]]["box"])

    interval, result = analyze(media.fixture("Enzo").read_bytes(), ".mp4")
    assert interval == (0, 5000) and result["why"] == "auto"
    chosen = result["candidates"][result["subject"]]
    # The automatic point and the accepted tap pick out the same person.
    assert inside(enzo["target_point"], chosen["box"]) and inside(result["point"], chosen["box"])
    # Both raised arms belong to the foreground subject through the complete action.
    assert result["duration_ms"] == 5000

    interval, result = analyze(media.fixture("Kung Fu").read_bytes(), ".gif")
    assert interval == (0, 3400)
    assert result["why"] == "auto" and result["point"] is not None
    kungfu = accepted("kungfu")
    assert any(
        c["label"] == "cat" and inside(kungfu["target_point"], c["box"])
        for c in result["candidates"]
    )


CAPTION = {
    "reference_kind": "person",
    "driving_kind": "person",
    "motion": "everyday",
}


def test_caption_tier_suggestion_requires_a_human_with_everyday_motion():
    person = {"label": "person"}
    assert selection.tier(person, CAPTION) == "simple-motion"
    assert selection.tier(person, CAPTION | {"motion": "complex"}) == "complex-motion"
    assert selection.tier(person, CAPTION | {"reference_kind": "animal"}) == "complex-motion"
    assert selection.tier({"label": "cat"}, CAPTION) == "complex-motion"
    assert selection.tier(None, CAPTION) == "complex-motion"


# Hand-written answers, not recorded model output: they pin the validation, not the model.
ANSWER = {
    "public_figure": False,
    "reference_clear": True,
    "reference_kind": "person",
    "subject_visible": True,
    "subject_clear": True,
    "driving_kind": "person",
    "motion": "everyday",
    "action_clear": True,
    "prompt": "A man with short dark hair and a trimmed beard is celebrating on a football "
    "pitch at night. He has a stocky build and wears a navy jacket over a white shirt. He "
    "raises both arms, turns to his left, then jogs two steps forward, moving naturally in "
    "front of the floodlit pitch and the crowd in the stands behind him",
}


def test_a_caption_answer_is_validated_strictly():
    parsed = assist.parse(json.dumps(ANSWER))
    assert assist.prompt(parsed).startswith("A man with short dark hair")
    assert assist.prompt(parsed).endswith("behind him.")
    fenced = "Here it is:\n```json\n" + json.dumps(ANSWER | {"extra": 1}) + "\n```"
    assert assist.parse(fenced) == parsed
    missing = {k: v for k, v in ANSWER.items() if k != "subject_clear"}
    for bad in (
        missing,
        ANSWER | {"motion": "fast"},
        ANSWER | {"subject_clear": "yes"},
        ANSWER | {"public_figure": None},
        ANSWER | {"prompt": "x" * 1801},
        ANSWER | {"prompt": "A man stands on a pitch."},
    ):
        assert assist.parse(json.dumps(bad)) is None
    assert assist.parse("I can't help with that.") is None
