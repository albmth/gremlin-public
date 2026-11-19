"""Two exact model sources: immutable runtime downloads or a verified native snapshot."""

import argparse
import hashlib
import json
import os
import re
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .recipe import REFERENCE_PATH

CACHE_HUB = Path("/runpod-volume/huggingface-cache/hub")
ASSET_FIELDS = ("destination", "repo", "revision", "path", "bytes", "sha256")
RELEASE_FIELDS = {"version", "asset_set_sha256", "repo", "revision"}


class ModelError(ValueError):
    """A fixed diagnostic code, never a cache path, URL or provider exception."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def artifacts(recipe_paths):
    """The pinned assets of every listed recipe, each destination once."""
    found = {}
    for recipe_path in recipe_paths:
        for artifact in json.loads(Path(recipe_path).read_text())["assets"]:
            destination = Path(artifact["destination"])
            if (
                destination.is_absolute()
                or ".." in destination.parts
                or not destination.parts
                or destination.as_posix() != artifact["destination"]
            ):
                raise ModelError("asset_contract_invalid")
            prior = found.setdefault(artifact["destination"], artifact)
            if any(prior[key] != artifact[key] for key in ASSET_FIELDS):
                raise ModelError("asset_contract_invalid")
    return list(found.values())


def asset_set(recipe_paths):
    return [
        {key: item[key] for key in ASSET_FIELDS}
        for item in sorted(artifacts(recipe_paths), key=lambda item: item["destination"])
    ]


def asset_set_sha256(recipe_paths):
    encoded = json.dumps(asset_set(recipe_paths), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def release_identity(record, source, recipe_paths):
    """Validate image-side pins; a native release cannot be invented by deployment env."""
    if (
        source not in {"native-cache", "runtime-hf"}
        or not isinstance(record, dict)
        or set(record) != RELEASE_FIELDS
        or type(record["version"]) is not int
        or record["version"] != 1
        or record["asset_set_sha256"] != asset_set_sha256(recipe_paths)
    ):
        raise ModelError("release_configuration_invalid")
    repo, revision = record["repo"], record["revision"]
    if (repo is None) != (revision is None):
        raise ModelError("release_configuration_invalid")
    if repo is not None and (
        not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", repo)
        or ".." in repo
        or "--" in repo
        or not isinstance(revision, str)
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
    ):
        raise ModelError("release_configuration_invalid")
    if source == "native-cache" and repo is None:
        raise ModelError("release_unpublished")
    return {
        **record,
        "repo": repo if source == "native-cache" else None,
        "revision": revision if source == "native-cache" else None,
    }


def release_matches(actual, expected):
    return (
        isinstance(actual, dict)
        and set(actual) == RELEASE_FIELDS
        and type(actual.get("version")) is int
        and actual == expected
    )


def fingerprint(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_gid,
    )


def open_regular(path):
    # Walk already-resolved paths with directory descriptors. If a provider replaces a
    # directory with a link between resolution and open, no read can escape through it.
    parts = Path(path).absolute().parts
    directory = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def verify_artifact(path, artifact):
    """Open the resolved regular file once and reject replacement or writes during hashing."""
    path = Path(path)
    try:
        descriptor = open_regular(path)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != artifact["bytes"]:
                raise ModelError("weight_integrity_invalid")
            if hashlib.file_digest(stream, "sha256").hexdigest() != artifact["sha256"]:
                raise ModelError("weight_integrity_invalid")
            if fingerprint(before) != fingerprint(os.fstat(stream.fileno())):
                raise ModelError("weight_changed")
        if fingerprint(before) != fingerprint(path.lstat()):
            raise ModelError("weight_changed")
        return fingerprint(before)
    except OSError:
        raise ModelError("weight_unreadable") from None


def local_path(root, destination):
    root = Path(root)
    path = root / destination
    for parent in (root, *path.relative_to(root).parents):
        candidate = parent if parent == root else root / parent
        if candidate.is_symlink():
            raise ModelError("weight_path_invalid")
    if path.is_symlink():
        raise ModelError("weight_path_invalid")
    return path


def snapshot_root(release, hub=CACHE_HUB):
    hub = Path(hub).absolute()
    if any(path.is_symlink() for path in (hub, *hub.parents)):
        raise ModelError("snapshot_path_invalid")
    repo = hub / ("models--" + release["repo"].replace("/", "--"))
    snapshot = repo / "snapshots" / release["revision"]
    try:
        # A link for the snapshot directory could select a different revision.
        if any(path.is_symlink() for path in (repo, repo / "snapshots", snapshot)):
            raise ModelError("snapshot_path_invalid")
        if not snapshot.is_dir():
            raise ModelError("snapshot_missing")
        resolved_repo = repo.resolve(strict=True)
        snapshot = snapshot.resolve(strict=True)
        snapshot.relative_to(resolved_repo)
        return snapshot, resolved_repo
    except ModelError:
        raise
    except (OSError, ValueError):
        raise ModelError("snapshot_path_invalid") from None


def cached_path(snapshot, repo, destination):
    """Permit only snapshot files or snapshot-to-blob links in this one repository."""
    path = snapshot / destination
    try:
        for parent in path.relative_to(snapshot).parents:
            if (snapshot / parent).is_symlink():
                raise ModelError("weight_path_invalid")
        if path.is_symlink():
            target = Path(os.path.abspath(path.parent / path.readlink()))
            if not target.is_relative_to(repo / "blobs"):
                raise ModelError("weight_path_invalid")
            # Hugging Face links directly to regular blob files. Chained links or linked
            # blob directories could traverse untrusted paths and are not that layout.
            if any(
                (repo / "blobs" / part).is_symlink()
                for part in (
                    target.relative_to(repo / "blobs"),
                    *target.relative_to(repo / "blobs").parents,
                )
            ):
                raise ModelError("weight_path_invalid")
        resolved = path.resolve(strict=True)
        if not (resolved.is_relative_to(snapshot) or resolved.is_relative_to(repo / "blobs")):
            raise ModelError("weight_path_invalid")
        if not resolved.is_file():
            raise ModelError("weight_path_invalid")
        # A linked blobs directory could escape or alias another snapshot.
        if (repo / "blobs").is_symlink():
            raise ModelError("weight_path_invalid")
        return resolved
    except (OSError, RuntimeError, ValueError):
        raise ModelError("weight_path_invalid") from None


class VerifiedModels:
    """Boot-verified files whose identities must survive engine and FlashBoot reuse."""

    def __init__(self, root, entries, *, repo=None):
        self.root = Path(root)
        self.entries = entries
        self.repo = repo
        parents = set(self.root.parents) | {self.root}
        for artifact, path, _ in entries:
            parents.update(path.parents)
            parents.update((self.root / artifact["destination"]).parents)
        # Preserve the UID-10001 readability proof across later permission/mount changes.
        self.directories = {path: self.directory_identity(path) for path in parents}

    @staticmethod
    def directory_identity(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid

    def check_unchanged(self):
        try:
            if any(
                self.directory_identity(path) != identity
                for path, identity in self.directories.items()
            ):
                raise ModelError("weight_changed")
            if self.repo is not None and (
                self.root.is_symlink() or self.root.resolve(strict=True) != self.root
            ):
                raise ModelError("weight_changed")
            for artifact, expected_path, expected_fingerprint in self.entries:
                path = (
                    cached_path(self.root, self.repo, artifact["destination"])
                    if self.repo is not None
                    else local_path(self.root, artifact["destination"])
                )
                if path != expected_path or fingerprint(path.lstat()) != expected_fingerprint:
                    raise ModelError("weight_changed")
        except OSError:
            raise ModelError("weight_unreadable") from None


def native_models(release, recipe_paths, *, hub=CACHE_HUB):
    started = time.monotonic()
    snapshot, repo = snapshot_root(release, hub)
    resolved = [
        (item, cached_path(snapshot, repo, item["destination"])) for item in artifacts(recipe_paths)
    ]
    resolved_at = time.monotonic()
    # SHA-256 is single-threaded per file, so one file at a time hashed the 30.8 GB release at
    # 1.2-1.5 GB/s on the qualified hosts; side by side, the billed wait is the largest file.
    with ThreadPoolExecutor(min(8, len(resolved))) as pool:
        fingerprints = list(pool.map(lambda pair: verify_artifact(pair[1], pair[0]), resolved))
    entries = [
        (item, path, identity)
        for (item, path), identity in zip(resolved, fingerprints, strict=True)
    ]
    return VerifiedModels(snapshot, entries, repo=repo), {
        "cache_resolve_seconds": resolved_at - started,
        "hash_seconds": time.monotonic() - resolved_at,
        "download_seconds": 0.0,
        "weights_fetched": False,
    }


def fetch(root, recipe_paths, *, download=False, verify=False):
    root = Path(root)
    staging = root / ".staging"
    timing = {"hash_seconds": 0.0, "download_seconds": 0.0, "weights_fetched": False}
    entries = []
    for artifact in artifacts(recipe_paths):
        path = local_path(root, artifact["destination"])
        if path.exists():
            started = time.monotonic()
            identity = verify_artifact(path, artifact)
            timing["hash_seconds"] += time.monotonic() - started
            entries.append((artifact, path, identity))
            continue
        if verify:
            raise ModelError("weight_missing")
        if not download:
            url = (
                f"https://huggingface.co/{artifact['repo']}/resolve/"
                f"{artifact['revision']}/{artifact['path']}"
            )
            response = requests.get(url, headers={"Range": "bytes=0-7"}, stream=True, timeout=60)
            response.raise_for_status()
            response.close()
            print(f"accessible {artifact['destination']} ({artifact['bytes']} bytes)", flush=True)
            continue
        from huggingface_hub import hf_hub_download

        if staging.is_symlink():
            raise ModelError("weight_path_invalid")
        started = time.monotonic()
        staged = Path(
            hf_hub_download(
                artifact["repo"], artifact["path"], revision=artifact["revision"], local_dir=staging
            )
        )
        timing["download_seconds"] += time.monotonic() - started
        started = time.monotonic()
        verify_artifact(staged, artifact)
        timing["hash_seconds"] += time.monotonic() - started
        path.parent.mkdir(parents=True, exist_ok=True)
        local_path(root, artifact["destination"])
        os.replace(staged, path)
        # Atomic placement moves the verified inode and changes ctime, not its bytes.
        entries.append((artifact, path, fingerprint(path.lstat())))
        timing["weights_fetched"] = True
    return VerifiedModels(root, entries), timing


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--recipe", type=Path, action="append", help="repeatable; default: reference"
    )
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    fetch(args.root, args.recipe or [REFERENCE_PATH], download=args.download, verify=args.verify)
