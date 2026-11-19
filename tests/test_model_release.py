"""Release preparation must preserve provenance and bytes without copying or repairing weights."""

import hashlib
import json
from urllib.parse import urlsplit

import pytest

from tools import model_release


def source(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    payload = b"unchanged accepted weights"
    (models / "model.safetensors").write_bytes(payload)
    item = {
        "destination": "model.safetensors",
        "repo": "Comfy-Org/SCAIL-2",
        "revision": "a" * 40,
        "path": "original/model.safetensors",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "license": "mit",
    }
    recipe = tmp_path / "recipe.json"
    recipe.write_text(json.dumps({"assets": [item]}))
    return models, recipe, item


def test_release_links_only_verified_payload_and_preserves_provenance(tmp_path):
    models, recipe, item = source(tmp_path)
    (models / "unrelated.safetensors").write_bytes(b"not part of release")
    output = tmp_path / "release"
    repo = "owner/release"
    record = model_release.prepare(models, output, [recipe, recipe], repo=repo)
    target = output / item["destination"]
    assert target.samefile(models / item["destination"])
    assert list(output.rglob("*.safetensors")) == [target]
    assert {key: record["assets"][0][key] for key in item} == item
    assert (output / "licenses/SAM-LICENSE.txt").is_file()
    card = (output / "README.md").read_text()
    license_link = next(
        line.removeprefix("license_link: ")
        for line in card.splitlines()
        if line.startswith("license_link: ")
    )
    url = urlsplit(license_link)
    assert url.scheme == "https" and url.netloc == "huggingface.co"
    assert url.path.startswith(f"/{repo}/blob/main/")
    assert (output / url.path.removeprefix(f"/{repo}/blob/main/")).is_file()
    with pytest.raises(ValueError, match="new release"):
        model_release.prepare(models, output, [recipe], repo=repo)
    assert target.read_bytes() == b"unchanged accepted weights"


def test_corrupt_or_linked_payload_cannot_create_a_release(tmp_path):
    models, recipe, item = source(tmp_path)
    path = models / item["destination"]
    path.write_bytes(b"x" * item["bytes"])
    output = tmp_path / "release"
    with pytest.raises(ValueError):
        model_release.prepare(models, output, [recipe], repo="owner/release")
    assert not output.exists()
    external = tmp_path / "outside.safetensors"
    external.write_bytes(b"unchanged accepted weights")
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(ValueError):
        model_release.prepare(models, output, [recipe], repo="owner/release")
    assert not output.exists()


def test_native_pin_requires_full_immutable_revision(tmp_path):
    _, recipe, _ = source(tmp_path)
    output = tmp_path / "record.json"
    with pytest.raises(ValueError):
        model_release.pin("owner/release", "main", output, [recipe])
    assert not output.exists()
    model_release.pin(None, None, output, [recipe])
    assert json.loads(output.read_text())["revision"] is None
