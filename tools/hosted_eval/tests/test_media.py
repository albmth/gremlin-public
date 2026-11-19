"""Real codec preparation and normalization, without inference."""

import json

import av
import pytest

from tools.hosted_eval.normalize import ffmpeg, normalize
from tools.hosted_eval.prepare import verify


@pytest.mark.parametrize("count", [55, 80])
def test_normalizes_widescreen_24fps_to_exact_square_interval(tmp_path, count):
    raw = tmp_path / "raw.mp4"
    driver = tmp_path / "driver.mp4"
    ffmpeg(
        ["-f", "lavfi", "-i", "testsrc2=size=832x480:rate=24", "-t", "5", "-c:v", "libx264", raw]
    )
    ffmpeg(
        ["-f", "lavfi", "-i", "color=size=512x512:rate=16", "-t", "5", "-c:v", "libx264", driver]
    )
    out = tmp_path / "normalized"
    normalize(raw, out, driver, count)
    with av.open(str(out / "normalized.mp4")) as video:
        stream = video.streams.video[0]
        assert (stream.width, stream.height, stream.average_rate) == (512, 512, 16)
        assert len(list(video.decode(stream))) == count
    assert raw.exists() and (out / "comparison.mp4").exists()


def test_changed_prepared_input_is_detected(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs/manifest.json").write_text(json.dumps({"hashes": {"input": "0" * 64}}))
    (tmp_path / "input").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="changed"):
        verify(tmp_path)
