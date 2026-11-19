"""Only the pinned, confined bytes may initialize a model source, with no cache repair."""

import hashlib
import json
import os
import sys
import types
from pathlib import Path

import pytest
from imprint_worker import assets


@pytest.fixture
def model(tmp_path):
    data = b"pinned model bytes"
    artifact = {
        "destination": "diffusion_models/model.safetensors",
        "repo": "org/model",
        "revision": "a" * 40,
        "path": "model",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    recipe = tmp_path / "recipe.json"
    recipe.write_text(json.dumps({"assets": [artifact]}))
    release = {
        "version": 1,
        "asset_set_sha256": assets.asset_set_sha256([recipe]),
        "repo": "org/release",
        "revision": "b" * 40,
    }
    hub = tmp_path / "hub"
    repo = hub / "models--org--release"
    snapshot = repo / "snapshots" / release["revision"]
    path = snapshot / artifact["destination"]
    path.parent.mkdir(parents=True)
    blob = repo / "blobs" / artifact["sha256"]
    blob.parent.mkdir()
    blob.write_bytes(data)
    path.symlink_to(os.path.relpath(blob, path.parent))
    return types.SimpleNamespace(
        data=data,
        artifact=artifact,
        recipe=recipe,
        release=release,
        hub=hub,
        repo=repo,
        snapshot=snapshot,
        path=path,
        blob=blob,
    )


def test_native_reads_snapshot_blob_links_without_copy_or_network(model, monkeypatch):
    monkeypatch.setattr(assets, "fetch", lambda *a, **k: pytest.fail("Runtime download"))
    monkeypatch.setattr(assets.requests, "get", lambda *a, **k: pytest.fail("Network access"))
    verified, timing = assets.native_models(model.release, [model.recipe], hub=model.hub)
    assert verified.root == model.snapshot
    assert model.path.is_symlink()
    assert timing["weights_fetched"] is False and timing["download_seconds"] == 0
    verified.check_unchanged()
    # Same-size edits to a warm cache cannot reuse the previously verified engine.
    model.blob.write_bytes(b"X" * len(model.data))
    with pytest.raises(assets.ModelError):
        verified.check_unchanged()
    with pytest.raises(assets.ModelError, match="integrity"):
        assets.native_models(model.release, [model.recipe], hub=model.hub)


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "wrong_revision",
        "external",
        "broken",
        "directory",
        "other_snapshot",
        "linked_snapshot",
        "truncated",
        "unreadable",
    ],
)
def test_native_cache_rejects_missing_conflicting_and_escaping_files(
    model, tmp_path, monkeypatch, damage
):
    if damage == "missing":
        model.blob.unlink()
    elif damage == "wrong_revision":
        model.release["revision"] = "c" * 40
    elif damage in {"external", "broken", "directory", "other_snapshot"}:
        model.path.unlink()
        target = tmp_path / "outside"
        if damage == "other_snapshot":
            target = model.repo / "snapshots" / ("c" * 40) / "file"
            target.parent.mkdir()
        if damage == "directory":
            target.mkdir()
        elif damage != "broken":
            target.write_bytes(model.data)
        model.path.symlink_to(target)
    elif damage == "linked_snapshot":
        other = model.snapshot.with_name("c" * 40)
        model.snapshot.rename(other)
        model.snapshot.symlink_to(other)
    elif damage == "truncated":
        model.blob.write_bytes(b"short")
    else:
        original = os.open

        def open_file(path, *args, **kwargs):
            if Path(path).name == model.blob.name:
                raise PermissionError("sensitive mount location")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(assets.ModelError) as failure:
        assets.native_models(model.release, [model.recipe], hub=model.hub)
    assert str(tmp_path) not in str(failure.value)


def test_regular_snapshot_files_are_supported_and_local_symlinks_are_not(model, tmp_path):
    model.path.unlink()
    model.path.write_bytes(model.data)
    verified, _ = assets.native_models(model.release, [model.recipe], hub=model.hub)
    verified.check_unchanged()
    local = tmp_path / "models"
    path = local / model.artifact["destination"]
    path.parent.mkdir(parents=True)
    path.symlink_to(model.path)
    with pytest.raises(assets.ModelError, match="path_invalid"):
        assets.fetch(local, [model.recipe], download=True)


def test_runtime_download_hashes_once_and_uses_pinned_revision(model, tmp_path, monkeypatch):
    local = tmp_path / "models"
    requests = []

    def download(repo, path, *, revision, local_dir):
        requests.append((repo, path, revision))
        local_dir.mkdir(parents=True)
        downloaded = local_dir / "downloaded"
        downloaded.write_bytes(model.data)
        return str(downloaded)

    monkeypatch.setitem(
        sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=download)
    )
    verified, timing = assets.fetch(local, [model.recipe], download=True)
    assert requests == [("org/model", "model", "a" * 40)]
    assert timing["weights_fetched"] is True
    verified.check_unchanged()
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(hf_hub_download=lambda *a, **k: pytest.fail("Download")),
    )
    # Pre-existing weights are fully checked even if a stale historical marker is present.
    (local / ".verified-old").write_text("trusted once")
    (local / model.artifact["destination"]).write_bytes(b"X" * len(model.data))
    with pytest.raises(assets.ModelError, match="integrity"):
        assets.fetch(local, [model.recipe], download=True)


def test_release_identity_requires_pinned_native_release_and_original_asset_contract(model):
    assert assets.release_identity(model.release, "native-cache", [model.recipe]) == model.release
    runtime = assets.release_identity(model.release, "runtime-hf", [model.recipe])
    assert runtime["repo"] is None and runtime["revision"] is None
    for changed in [
        runtime,
        model.release | {"revision": "main"},
        model.release | {"repo": "../release"},
        model.release | {"asset_set_sha256": "0" * 64},
    ]:
        with pytest.raises(assets.ModelError):
            assets.release_identity(changed, "native-cache", [model.recipe])
    duplicate = json.loads(model.recipe.read_text())
    duplicate["assets"][0]["revision"] = "c" * 40
    other = model.recipe.with_name("other.json")
    other.write_text(json.dumps(duplicate))
    with pytest.raises(assets.ModelError, match="contract_invalid"):
        assets.asset_set([model.recipe, other])


def test_warm_cache_loses_permission_or_mount_identity_before_another_lease(model):
    verified, _ = assets.native_models(model.release, [model.recipe], hub=model.hub)
    original = model.snapshot.stat().st_mode
    model.snapshot.chmod(0o700)
    try:
        with pytest.raises(assets.ModelError, match="changed"):
            verified.check_unchanged()
    finally:
        model.snapshot.chmod(original)


def test_resolved_path_cannot_escape_through_replaced_parent(model, tmp_path):
    original = model.blob.parent
    saved = original.with_name("saved-blobs")
    original.rename(saved)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / model.blob.name).write_bytes(model.data)
    original.symlink_to(outside)
    with pytest.raises(assets.ModelError, match="unreadable"):
        assets.verify_artifact(model.blob, model.artifact)


def test_warm_cache_tracks_snapshot_category_permissions_not_only_blob_permissions(model):
    verified, _ = assets.native_models(model.release, [model.recipe], hub=model.hub)
    original = model.path.parent.stat().st_mode
    model.path.parent.chmod(0o700)
    try:
        with pytest.raises(assets.ModelError, match="changed"):
            verified.check_unchanged()
    finally:
        model.path.parent.chmod(original)
