"""Motion-conditioned candidates compared against the driving clip as ground truth.

Every candidate receives the same aligned identity still (the Phase A FLUX.2 klein edit of the
driving clip's first frame) and the same prepared driving interval; only the video stage differs.
"""

PRICES = {
    "verified_on": "2026-10-29",
    "models": {
        "S2": {
            "provider": "fal",
            "endpoint": "fal-ai/scail-2",
            "architecture": "in-context character replacement (SCAIL-2, raw driving frames)",
            "usd_per_output_second": 0.20,
            "reserve_multiplier": 1,
            "source": "https://fal.ai/models/fal-ai/scail-2",
            "quote": "$0.20 per second of generated video; billed seconds equal output frames / 16",
        },
        "VACE": {
            "provider": "fal",
            "endpoint": "fal-ai/wan-vace-14b/inpainting",
            "architecture": "tracked-mask inpainting with a reference image (Wan VACE 14B)",
            "usd_per_output_second": 0.04,
            "reserve_multiplier": 1,
            "source": "https://fal.ai/models/fal-ai/wan-vace-14b/inpainting",
            "quote": "$0.04 per video second for 480p; video seconds are calculated at 16 fps",
        },
        "W27": {
            "provider": "fal",
            "endpoint": "fal-ai/wan/v2.7/edit-video",
            "architecture": "reference-guided video edit (Wan 2.7)",
            "usd_per_output_second": 0.10,
            # The October lab observed input seconds billed as well as output seconds.
            "reserve_multiplier": 2,
            "source": "https://fal.ai/models/fal-ai/wan/v2.7/edit-video",
            "quote": "$0.10 per second of output video at 720p",
        },
    },
}
MODELS = PRICES["models"]
FIXTURES = {
    "kungfu": {"prefix": "KF", "subject": "animal", "target": "the orange cat"},
    "enzo": {"prefix": "EN", "subject": "human", "target": "the footballer in the dark blue shirt"},
}
CELLS = [
    {"id": f"{FIXTURES[fixture]['prefix']}-{model}", "fixture": fixture, "model": model}
    for fixture in FIXTURES
    for model in ("S2", "VACE", "W27")
]
# Iteration variants after the first pass: the same endpoint with one changed input or field.
VARIANTS = [
    # fal's SAM3 found no subject in the wide Kung Fu canvas: try the square inputs as they are.
    {"id": "KF-S2Q", "fixture": "kungfu", "model": "S2", "variant": {"inputs": "square"}},
    # ... and, if the cat is still not detected as an animal, the human detection prompt.
    {
        "id": "KF-S2QH",
        "fixture": "kungfu",
        "model": "S2",
        "variant": {"inputs": "square", "subject_type": "human"},
    },
    # VACE returned the source almost untouched: let fal grey out the masked region first.
    {"id": "EN-VACEP", "fixture": "enzo", "model": "VACE", "variant": {"preprocess": True}},
    {"id": "KF-VACEP", "fixture": "kungfu", "model": "VACE", "variant": {"preprocess": True}},
    # Both VACE runs returned the source subject: probe whether fal reads the mask inverted.
    {"id": "EN-VACEI", "fixture": "enzo", "model": "VACE", "variant": {"mask": "inverted"}},
]
CELLS += VARIANTS
FIRST_PASS = [c["id"] for c in CELLS if "variant" not in c]
WIDE = (896, 512)
WIDE_CROP = ((WIDE[0] - 512) // 2, 0, 512, 512)
IDENTITY = (
    "a man with a brown mullet haircut, a swept fringe and longer hair falling over the back "
    "of his neck, light stubble and a short goatee, a lean build, wearing a faded blue denim "
    "jacket with a collar and chest pockets over a black crew-neck t-shirt"
)


def cell(cell_id):
    return next(c for c in CELLS if c["id"] == cell_id.split("-seed")[0])


def seconds(model_frames):
    return model_frames / 16


def list_usd(model, model_frames):
    return round(MODELS[model]["usd_per_output_second"] * seconds(model_frames), 4)


def reserve_usd(model, model_frames):
    config = MODELS[model]
    return round(
        config["usd_per_output_second"] * seconds(model_frames) * config["reserve_multiplier"], 4
    )


def edit_prompt(fixture):
    target = FIXTURES[fixture]["target"]
    return (
        f"Replace {target} with the man in the reference image: {IDENTITY}. "
        "Keep his face, hair and clothing exactly as in the reference. "
        "Keep the camera, framing, background, lighting, every movement and its timing exactly "
        "as in the input video; only the character changes. Photographic, matching the footage."
    )


def body(config, prompt, media, seed=42):
    """The request body for one cell; media maps input names to URLs or data URIs."""
    model, variant = config["model"], dict(config.get("variant", {}))
    square = variant.pop("inputs", "wide") == "square"
    mask = "mask-inverted.mp4" if variant.pop("mask", "tracked") == "inverted" else "mask.mp4"
    if model == "S2":
        request = {
            "prompt": prompt,
            "image_url": media["still.png" if square else "still-wide.png"],
            "video_url": media["driver-model.mp4" if square else "driver-wide.mp4"],
            "mode": "replacement",
            "driving_type": "end_to_end",
            "subject_type": FIXTURES[config["fixture"]]["subject"],
            "resolution": "512p",
            "num_inference_steps": 40,
            "guidance_scale": 5,
            "shift": 3,
            "seed": seed,
            "enable_safety_checker": True,
        }
    elif model == "VACE":
        request = {
            "prompt": prompt,
            "video_url": media["driver-model.mp4"],
            "mask_video_url": media[mask],
            "ref_image_urls": [media["still.png"]],
            "resolution": "480p",
            "aspect_ratio": "1:1",
            "match_input_num_frames": True,
            "match_input_frames_per_second": True,
            "num_inference_steps": 30,
            "guidance_scale": 5,
            "sampler": "unipc",
            "shift": 5,
            "seed": seed,
            "enable_prompt_expansion": False,
            "enable_safety_checker": True,
        }
    elif model == "W27":
        request = {
            "prompt": edit_prompt(config["fixture"]),
            "video_url": media["driver-model.mp4"],
            "reference_image_url": media["still.png"],
            "resolution": "720p",
            "aspect_ratio": "1:1",
            "duration": 0,
            "audio_setting": "auto",
            "seed": seed,
            "enable_safety_checker": True,
        }
    else:
        raise KeyError(model)
    return {**request, **variant}


def inputs_for(config):
    variant = config.get("variant", {})
    square = variant.get("inputs") == "square"
    mask = "mask-inverted.mp4" if variant.get("mask") == "inverted" else "mask.mp4"
    return {
        "S2": ["still.png", "driver-model.mp4"]
        if square
        else ["still-wide.png", "driver-wide.mp4"],
        "VACE": ["driver-model.mp4", mask, "still.png"],
        "W27": ["driver-model.mp4", "still.png"],
    }[config["model"]]
