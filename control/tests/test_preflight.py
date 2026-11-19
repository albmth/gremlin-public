"""Both preserved inputs must remain admissible before any paid worker starts."""

import io
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from gremlin_control import preflight
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "tests/fixtures/manifest.json").read_text())
# The fixture media is private and not in Git.
MEDIA_PRESENT = all((ROOT / "stickers" / item["name"]).is_file() for item in MANIFEST)


@pytest.mark.skipif(not MEDIA_PRESENT, reason="private fixture media (stickers/) absent")
@pytest.mark.parametrize("name", ["kungfu", "enzo"])
def test_accepted_fixture_inputs_pass_cpu_preflight(name):
    request = json.loads((ROOT / f"tests/fixtures/requests/{name}.json").read_text())
    paths = {item["sha256"]: ROOT / "stickers" / item["name"] for item in MANIFEST}
    inputs = {
        kind: paths[request[kind + "_sha256"]].read_bytes() for kind in ("reference", "driving")
    }
    assert preflight.validate(inputs, request)["model_pixels"] == 512 * 512


@pytest.mark.parametrize(("width", "accepted"), [(2000, True), (2100, False)])
def test_reference_preflight_preserves_existing_worker_pixel_limit(width, accepted):
    reference = io.BytesIO()
    Image.new("RGB", (width, 2000)).save(reference, format="PNG")
    driving = io.BytesIO()
    Image.new("RGB", (32, 32)).save(driving, format="GIF", duration=5000)
    inputs = {"reference": reference.getvalue(), "driving": driving.getvalue()}
    request = {"start_ms": 0, "duration_ms": 3000}
    if accepted:
        assert preflight.validate(inputs, request)["model_pixels"] == 512 * 512
    else:
        with pytest.raises(HTTPException) as error:
            preflight.validate(inputs, request)
        assert error.value.status_code == 422
        assert error.value.detail["code"] == "IMPRINT_MEDIA_INVALID"
        assert error.value.detail["reason"] == "reference_too_large"


def test_stalled_native_validation_fails_closed_without_logging_private_media(monkeypatch):
    def stalled(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"private media")

    monkeypatch.setattr(preflight.subprocess, "run", stalled)
    with pytest.raises(HTTPException) as error:
        preflight.validate(
            {"reference": b"private", "driving": b"private"}, {"start_ms": 0, "duration_ms": 3000}
        )
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "IMPRINT_PREFLIGHT_UNAVAILABLE"
    assert "private" not in str(error.value.detail)
