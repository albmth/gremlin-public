"""Recipe files name the weights and sampling settings a generation graph is built from."""

import json
from pathlib import Path

REFERENCE_PATH = Path(__file__).resolve().parents[1] / "recipe.json"
# Owner-accepted inference tiers, selected explicitly per job; see docs/engine.md.
TIERS_DIR = Path(__file__).resolve().parents[1] / "tiers"
TIER_RECIPES = {path.stem: path for path in sorted(TIERS_DIR.glob("*.json"))}

GENERATION_KEYS = (
    "diffusion",
    "weight_dtype",
    "loras",
    "steps",
    "cfg",
    "shift",
    "sampler",
    "scheduler",
    "decode",
    "attention",
    "easycache",
)


def load(path=REFERENCE_PATH, overrides=()):
    """Read a recipe; each override is KEY=VALUE with a dotted path and a JSON value.

    `steps=6`, `loras.2.strength=0.8` and `easycache=null` are all valid; a value that is
    not JSON is kept as a string, so `attention=comfy kitchen attention` needs no quotes.
    """
    recipe = json.loads(Path(path).read_text())
    for item in overrides:
        key, separator, raw = item.partition("=")
        if not separator:
            raise ValueError(f"Override must be KEY=VALUE: {item}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        target = recipe
        parts = key.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        if isinstance(target, list):
            target[int(parts[-1])] = value
        else:
            target[parts[-1]] = value
    return recipe


def recipe_paths():
    """Every recipe file: the preserved reference and the accepted tiers."""
    return [REFERENCE_PATH, *TIER_RECIPES.values()]


def generation_settings(recipe):
    """The subset of a recipe that shapes the generation graph, for run records."""
    return {key: recipe[key] for key in GENERATION_KEYS if recipe.get(key) is not None}
