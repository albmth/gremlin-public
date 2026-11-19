"""Prepare an offline, byte-identical model release; this tool never publishes or downloads."""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote

from imprint_worker import assets
from imprint_worker.recipe import TIER_RECIPES

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "worker/notices/models"
NOTICE_FILES = {
    "Comfy-Org/SCAIL-2": ["MIT-terms.txt", "SCAIL-repack-README.md", "SCAIL-original-README.md"],
    "Comfy-Org/Wan_2.1_ComfyUI_repackaged": ["Apache-2.0.txt", "Wan-repack-README.md"],
    "Comfy-Org/sam3.1": ["SAM-LICENSE.txt", "SAM-README.md"],
    "lightx2v/Wan2.1-Distill-Loras": ["Apache-2.0.txt", "Lightx2v-README.md"],
}


def manifest(recipe_paths):
    records = []
    for item in sorted(assets.artifacts(recipe_paths), key=lambda row: row["destination"]):
        notices = list(NOTICE_FILES[item["repo"]])
        if item["destination"].startswith("text_encoders/"):
            notices += ["UMT5-README.md"]
        if item["destination"].startswith("clip_vision/"):
            notices += ["CLIP-README.md", "MIT-terms.txt", "OpenCLIP-LICENSE.txt"]
        records.append({**item, "notices": ["licenses/" + name for name in notices]})
    return {
        "version": 1,
        "asset_set_sha256": assets.asset_set_sha256(recipe_paths),
        "total_bytes": sum(row["bytes"] for row in records),
        "assets": records,
    }


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def check_notices():
    for row in json.loads((NOTICES / "sources.json").read_text()):
        if hashlib.sha256((NOTICES / row["file"]).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("A preserved upstream notice changed")
    for names in NOTICE_FILES.values():
        for name in names:
            if not (NOTICES / name).is_file():
                raise ValueError("Required release notice missing")


def model_card(repo):
    # HF validates license_link as an absolute URI. This metadata link does not select
    # worker weights; the deployed model release still requires its immutable commit pin.
    license_url = f"https://huggingface.co/{quote(repo, safe='/')}/blob/main/licenses/NOTICE.md"
    return (
        "---\nlicense: other\nlicense_name: mixed-model-licenses\n"
        f"license_link: {license_url}\ntags:\n- comfyui\n- gremlin\n---\n\n"
        "# Gremlin immutable tier weights\n\n"
        "Eight original model files for the two accepted Gremlin tiers.\n"
        "The model bytes are unchanged; only the recorded ComfyUI directory placement applies.\n"
        "See [manifest.json](manifest.json) for original revisions, paths, hashes and licenses.\n"
        "See [notices](licenses/NOTICE.md) and the complete "
        "[SAM agreement](licenses/SAM-LICENSE.txt).\n"
        "This repository has mixed licenses and is not licensed as a whole under MIT or Apache.\n"
        "Publish once, verify the remote file inventory and hashes, then pin its full HF commit.\n"
        "Gremlin must not use a mutable branch or accept this manifest "
        "as a new source of trusted hashes.\n"
    )


def prepare(models, output, recipe_paths, *, repo):
    """Verify first, then hard-link the exact files; never copy another full model set."""
    models, output = Path(models).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Use a new release directory")
    record = manifest(recipe_paths)
    check_notices()
    witnesses = []
    for item in record["assets"]:
        path = assets.local_path(models, item["destination"])
        witnesses.append((path, assets.verify_artifact(path, item), item))
    # Hard links require the same filesystem. Fail rather than silently copying 30.8 GB.
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.stat().st_dev != models.stat().st_dev:
        raise ValueError("Release and models must share a filesystem for zero-copy preparation")
    output.mkdir()
    for source, witness, item in witnesses:
        target = output / item["destination"]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target, follow_symlinks=False)
        if assets.fingerprint(target.lstat()) != witness:
            # Creating a hard link legitimately changes ctime; every other identity field
            # must remain stable, and source and target must still be the same inode.
            current = assets.fingerprint(source.lstat())
            if (
                current[:4] != witness[:4]
                or current[5:] != witness[5:]
                or not os.path.samefile(source, target)
            ):
                raise ValueError("Model changed during release preparation")
    shutil.copytree(NOTICES, output / "licenses")
    write_json(output / "manifest.json", record)
    (output / ".gitattributes").write_text("*.safetensors filter=lfs diff=lfs merge=lfs -text\n")
    (output / "README.md").write_text(model_card(repo))
    return record


def pin(repo, revision, output, recipe_paths):
    record = {
        "version": 1,
        "asset_set_sha256": assets.asset_set_sha256(recipe_paths),
        "repo": repo,
        "revision": revision,
    }
    assets.release_identity(record, "native-cache" if repo else "runtime-hf", recipe_paths)
    write_json(output, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_args = commands.add_parser("prepare", help="full hashes and a zero-copy local package")
    prepare_args.add_argument("--models-root", type=Path, required=True)
    prepare_args.add_argument("--output", type=Path, required=True)
    prepare_args.add_argument("--repo", required=True, help="private destination OWNER/RELEASE")
    manifest_args = commands.add_parser("manifest", help="export metadata without model files")
    manifest_args.add_argument("--output", type=Path, required=True)
    pin_args = commands.add_parser("pin", help="generate image record after remote verification")
    pin_args.add_argument("--repo")
    pin_args.add_argument("--revision")
    pin_args.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    recipes = list(TIER_RECIPES.values())
    if args.command == "prepare":
        result = prepare(args.models_root, args.output, recipes, repo=args.repo)
    elif args.command == "manifest":
        result = manifest(recipes)
        write_json(args.output, result)
    else:
        result = pin(args.repo, args.revision, args.output, recipes)
    print(json.dumps({"asset_set_sha256": result["asset_set_sha256"], "published": False}))


if __name__ == "__main__":
    main()
