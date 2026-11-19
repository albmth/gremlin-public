"""Real fixture media through preflight and the worker's codec path; no inference runs.

The API publishes result geometry predicted at submit time; this pins that prediction to what
the worker's preparation and encoders actually produce for both accepted fixtures.
"""

import json

import av
import pytest
from gremlin_control.preflight import inspect
from imprint_worker.media import encode_result, prepare, read_video
from PIL import Image

from tools.fixtures import load_fixture, media_missing

pytestmark = pytest.mark.skipif(media_missing(), reason="private fixture media (stickers/) absent")


@pytest.mark.parametrize("name", ["kungfu", "enzo"])
def test_predicted_manifest_matches_real_encoding_and_review(tmp_path, name, monkeypatch):
    request, reference, driving = load_fixture(name)
    predicted = inspect(
        reference.read_bytes(), driving.read_bytes(), request["start_ms"], request["duration_ms"]
    )
    manifest = prepare(
        reference,
        driving,
        tmp_path / "prepared",
        name,
        start_ms=request["start_ms"],
        duration_ms=request["duration_ms"],
    )
    assert predicted["model_pixels"] == manifest["width"] * manifest["height"]
    # Re-encode the original driving frames solely to exercise the real codec boundary.
    # These bytes are never saved as inference evidence or displayed as a generated result.
    frames = read_video(tmp_path / "prepared" / f"{name}-driving.mkv")
    output = tmp_path / "codec-only"
    files = encode_result(frames, manifest, output)
    with av.open(str(output / "result.mp4")) as video:
        stream = video.streams.video[0]
        decoded = len(list(video.decode(video=0)))
        mp4 = predicted["result.mp4"]
        assert (stream.width, stream.height) == (mp4["width"], mp4["height"])
        assert decoded == mp4["frames"]
        assert abs(video.duration / 1000 - mp4["duration_ms"]) <= 1
    with Image.open(output / "result.webp") as image:
        webp = predicted["result.webp"]
        assert image.is_animated and image.size == (webp["width"], webp["height"])
        elapsed = 0
        for index in range(image.n_frames):
            image.seek(index)
            image.load()
            elapsed += image.info["duration"]
        assert elapsed == webp["duration_ms"] == request["duration_ms"]
    with Image.open(output / "poster.jpg") as image:
        poster = predicted["poster.jpg"]
        assert image.size == (poster["width"], poster["height"])

    # The review page encodes from the same artifacts, labelled as codec evidence only.
    from imprint_worker.review import main as review

    (output / "preparation.json").write_text(json.dumps(manifest))
    (output / "result.json").write_text(
        json.dumps(
            {
                "recipe": "scail2-native-full-quality-v1",
                "files": files,
                "purpose": "CODEC TEST ONLY: original driving pixels, no inference",
            }
        )
    )
    review_output = tmp_path / "codec-only-review"
    monkeypatch.setattr(
        "sys.argv",
        [
            "review",
            "--reference",
            str(reference),
            "--driving",
            str(driving),
            "--result",
            str(output),
            "--output",
            str(review_output),
        ],
    )
    review()
    with av.open(str(review_output / "comparison.mp4")) as video:
        assert (video.streams.video[0].width, video.streams.video[0].height) == (1024, 512)
        assert len(list(video.decode(video=0))) == manifest["output_frames"]
