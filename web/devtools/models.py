"""Fetch the worker's subject detector into gremlin_web/models/, verified against its manifest.

`python -m devtools.models` is idempotent: a file with the pinned checksum is kept, anything else
is replaced. The weights are never committed and never downloaded at runtime.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import requests

MODELS = Path(__file__).resolve().parents[1] / "gremlin_web" / "models"


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure():
    manifest = json.loads((MODELS / "detector.json").read_text())
    target = MODELS / manifest["file"]
    if target.is_file() and _sha256(target) == manifest["sha256"]:
        return target
    with (
        tempfile.NamedTemporaryFile(dir=MODELS, delete=False) as partial,
        requests.get(manifest["url"], stream=True, timeout=(10, 120)) as response,
    ):
        response.raise_for_status()
        for block in response.iter_content(1 << 20):
            partial.write(block)
    partial_path = Path(partial.name)
    if _sha256(partial_path) != manifest["sha256"]:
        partial_path.unlink()
        raise SystemExit(f"{manifest['url']} does not match the pinned checksum")
    partial_path.replace(target)
    return target


if __name__ == "__main__":
    print(ensure(), file=sys.stderr)
