"""Verify a model-free local image and real weights offline without publishing or using a GPU."""

import argparse
import json
import subprocess
from pathlib import Path

VERIFY = r"""
import json
import os
import stat
import sys
from pathlib import Path

from imprint_worker import assets
from imprint_worker.recipe import TIER_RECIPES

recipes = list(TIER_RECIPES.values())
items = assets.artifacts(recipes)
record = json.loads(Path('/opt/worker/model-release.json').read_text())
assets.release_identity(record, 'runtime-hf', recipes)
# Exclude runtime mounts, and count hardlinks only once. Docker Size can report compressed
# containerd blobs, so inspect expanded files as well as all reported image layer bytes.
device = os.stat('/').st_dev
seen = set()
logical = allocated = 0
model_names = {Path(item['destination']).name for item in items}
for root, directories, files in os.walk('/'):
    directories[:] = [name for name in directories
                       if not os.path.islink(os.path.join(root, name))
                       and os.stat(os.path.join(root, name)).st_dev == device]
    if '.parts' in directories:
        raise SystemExit('Unexpected model fragments in worker image')
    for name in files:
        path = os.path.join(root, name)
        info = os.lstat(path)
        identity = (info.st_dev, info.st_ino)
        if info.st_dev != device or not stat.S_ISREG(info.st_mode) or identity in seen:
            continue
        if name in model_names or name.endswith('.safetensors'):
            raise SystemExit('Unexpected model bytes in worker image')
        seen.add(identity)
        logical += info.st_size
        allocated += info.st_blocks * 512
expanded = max(logical, allocated)
weights = sum(item['bytes'] for item in items)
scratch = 8 * 1024**3
if max(expanded, int(sys.argv[2])) + weights + scratch > int(sys.argv[1]) * 1_000_000_000:
    raise SystemExit('Image plus one weight set plus 8 GiB scratch exceeds endpoint disk')
# The real files are mounted read-only as this fixture's blob tree. Snapshot links require
# only a few bytes in tmpfs; no second model copy or origin download exists in this check.
hub = Path('/offline-cache/hub')
release = {'version':1, 'asset_set_sha256':assets.asset_set_sha256(recipes),
           'repo':'offline/release', 'revision':'a'*40}
repo = hub / 'models--offline--release'
snapshot = repo / 'snapshots' / release['revision']
for item in items:
    path = snapshot / item['destination']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(os.path.relpath(repo / 'blobs' / item['destination'], path.parent))
os.setgroups([])
os.setgid(10001)
os.setuid(10001)
runtime, runtime_timing = assets.fetch(repo / 'blobs', recipes, verify=True)
runtime.check_unchanged()
native, native_timing = assets.native_models(release, recipes, hub=hub)
native.check_unchanged()
print(json.dumps({'expanded_image_bytes':expanded, 'weights_bytes':weights,
                  'scratch_reserve_bytes':scratch, 'file_count':len(items),
                  'asset_set_sha256':release['asset_set_sha256'],
                  'runtime_hash_seconds':runtime_timing['hash_seconds'],
                  'native_hash_seconds':native_timing['hash_seconds'],
                  'offline_hashes_verified':True, 'verified_uid':os.getuid(),
                  'snapshot_blob_links':True, 'gpu_executed':False}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="an already-built local image; never pulled")
    parser.add_argument(
        "--models-root",
        type=Path,
        required=True,
        help="existing exact tier files in their ComfyUI directory layout",
    )
    parser.add_argument("--disk-gb", type=int, default=50)
    args = parser.parse_args()
    root = args.models_root.resolve(strict=True)
    if not root.is_dir() or args.disk_gb <= 0:
        raise SystemExit("A model directory and positive disk size are required")
    record = json.loads(subprocess.check_output(["docker", "image", "inspect", args.image]))[0]
    layers = subprocess.check_output(
        ["docker", "image", "history", "--human=false", "--format", "{{.Size}}", args.image],
        text=True,
    )
    layer_bytes = [int(size) for size in layers.splitlines()]
    if max(layer_bytes) >= 10_000_000_000:
        raise SystemExit("Image layer exceeds GHCR's 10 GB limit")
    environment = dict(item.split("=", 1) for item in record["Config"]["Env"])
    if (
        record["Architecture"] != "amd64"
        or environment.get("IMPRINT_WEIGHT_SOURCE") != "runtime-hf"
        or environment.get("IMPRINT_WEIGHTS_BUNDLED")
    ):
        raise SystemExit("Expected the model-free linux/amd64 worker image")
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--platform",
            "linux/amd64",
            "--network=none",
            "--read-only",
            "--user",
            "0:0",
            "--tmpfs",
            "/offline-cache:rw,mode=755,size=16m",
            "--mount",
            f"type=bind,src={root},dst=/offline-cache/hub/models--offline--release/blobs,readonly",
            "--entrypoint",
            "python",
            args.image,
            "-c",
            VERIFY,
            str(args.disk_gb),
            str(sum(layer_bytes)),
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "image_id": record["Id"],
                "docker_reported_bytes": record["Size"],
                "reported_layer_bytes": sum(layer_bytes),
                "largest_reported_layer_bytes": max(layer_bytes),
                "disk_gb": args.disk_gb,
                "network_enabled": False,
                "gpu_executed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
