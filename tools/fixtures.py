"""Verify original fixtures and prepare deterministic inputs without inference or network calls."""

import argparse
import json
from pathlib import Path

from imprint_worker.media import prepare, sha256

ROOT = Path(__file__).resolve().parents[1]
# The fixture media is private and not in Git; tests that need it skip when it is absent.
MEDIA = ROOT / "stickers"


def media_missing():
    manifest = json.loads((ROOT / "tests/fixtures/manifest.json").read_text())
    return any(not (MEDIA / entry["name"]).is_file() for entry in manifest)


def load_fixture(name):
    manifest = json.loads((ROOT / "tests/fixtures/manifest.json").read_text())
    paths = {}
    for entry in manifest:
        path = MEDIA / entry["name"]
        if sha256(path) != entry["sha256"]:
            raise ValueError(f"Fixture checksum mismatch: {path.name}")
        paths[entry["sha256"]] = path
    request = json.loads((ROOT / f"tests/fixtures/requests/{name}.json").read_text())
    return request, paths[request["reference_sha256"]], paths[request["driving_sha256"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=["kungfu", "enzo"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request, reference, driving = load_fixture(args.name)
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = prepare(
        reference,
        driving,
        args.output,
        args.name,
        start_ms=request["start_ms"],
        duration_ms=request["duration_ms"],
    )
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    print(json.dumps({"fixture": args.name, "inference_executed": False, **manifest}, indent=2))


if __name__ == "__main__":
    main()
