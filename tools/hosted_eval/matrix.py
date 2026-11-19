"""The approved nine-video, four-still matrix, and nothing from Phase B."""

import json
from pathlib import Path

PRICES = json.loads(Path(__file__).with_name("prices.json").read_text())
MODELS = {k: v for k, v in PRICES["models"].items() if k in {"E1", "V1", "V2", "V3"}}
BASELINES = {
    "kungfu": {
        "RTX 5090": "artifacts/architecture-review-2026-10-26/runs/c12-kungfu",
        "PRO 4500": "artifacts/qualification/boot-tier-20261026-pro4500-kungfu-complex",
        "H100": "artifacts/e2e/download-kungfu",
    },
    "enzo": {
        "RTX 5090": "artifacts/qualification/g-enzo-simple",
        "PRO 4500": "artifacts/qualification/pro4500-enzo-simple",
        "H100": "artifacts/e2e/download-enzo",
    },
}
STILLS = [f"S-{prefix}-{position}" for prefix in ("KF", "EN") for position in ("first", "last")]
CELLS = [
    {
        "id": f"{prefix}-{route}",
        "fixture": fixture,
        "route": route,
        "model": {"A0": "V1", "B1": "V1", "B2": "V2", "B3": "V3", "K2": "V2"}[route],
    }
    for prefix, fixture, routes in [
        ("KF", "kungfu", ["A0", "B1", "B2", "B3", "K2"]),
        ("EN", "enzo", ["B1", "B2", "B3", "K2"]),
    ]
    for route in routes
]
PROBES = {"E1": STILLS[0], "V1": "KF-A0", "V2": "KF-B2", "V3": "KF-B3"}


def price(model, fixture="kungfu"):
    if model == "V2":
        return 0.020 if fixture == "kungfu" else 0.025
    return MODELS[model]["reserve_usd"]


def still_prompt(fixture):
    target = {
        "kungfu": "the orange cat standing upright on the wet night street",
        "enzo": "the footballer in the dark blue shirt saluting in the stadium",
    }[fixture]
    identity = (
        "a man with a brown mullet haircut, a swept fringe and longer hair falling "
        "over the back of his neck, light stubble and a short goatee. He has a "
        "normal-sized head and a lean build. He wears a faded blue denim jacket "
        "with a collar and chest pockets over a black crew-neck t-shirt"
    )
    return (
        f"Replace {target} in the first image with the person in the second image.\n"
        f"Keep the person's face, hair, stubble, goatee and clothing exactly: {identity}.\n"
        "Put the person in the same pose, position, scale and camera as the replaced "
        "character, standing on the same ground, lit only by the scene's light.\n"
        "Keep everything else in the first image unchanged. Photographic, slightly soft, "
        "matching the footage."
    )


def video_input(cell, prompt, first, last=None, seed=42):
    model, kf = cell["model"], cell["fixture"] == "kungfu"
    body = {"prompt": prompt, "image": first, "seed": seed, "resolution": "480p"}
    if model == "V2":
        body.update(
            resolution="720p",
            duration=4 if kf else 5,
            fps=24,
            draft=True,
            prompt_upsampling=False,
            disable_safety_filter=False,
        )
        if cell["route"] == "K2":
            body["last_frame_image"] = last
    else:
        body.update(
            num_frames=81,
            frames_per_second=16,
            go_fast=True,
            disable_safety_checker=False,
        )
        if model == "V1":
            body.update(aspect_ratio="16:9")
        else:
            body.update(interpolate_output=False)
    return body
