"""Run a real fixture through an already configured ComfyUI; never provisions compute."""

import argparse
from pathlib import Path

from imprint_worker.recipe import TIER_RECIPES, load
from imprint_worker.runner import run

from tools.fixtures import load_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=["kungfu", "enzo"], required=True)
    parser.add_argument("--comfy-root", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--recipe",
        type=Path,
        help="recipe file, for example worker/recipe.json for the H100 reference; "
        "default: the request's tier",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override one recipe value, for example steps=6 or decode=full",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        help="another reference image for the same request, for example an aligned still of "
        "the person in the driving frame; the fixture's sticker otherwise",
    )
    parser.add_argument(
        "--reference-point",
        metavar="X,Y",
        help="normalized point on the person in --reference; the request's point otherwise",
    )
    args = parser.parse_args()
    request, reference, driving = load_fixture(args.name)
    if args.reference_point and not args.reference:
        parser.error("--reference-point needs --reference")
    if args.reference:
        reference = args.reference
        if args.reference_point:
            x, y = (float(value) for value in args.reference_point.split(","))
            if not (0 <= x <= 1 and 0 <= y <= 1):
                parser.error("--reference-point coordinates must be within 0 and 1")
            request = {**request, "reference_point": {"x": x, "y": y}}
    recipe = load(args.recipe or TIER_RECIPES[request["tier"]], args.set)
    run(reference, driving, args.output, args.comfy_root, request, url=args.url, recipe=recipe)


if __name__ == "__main__":
    main()
