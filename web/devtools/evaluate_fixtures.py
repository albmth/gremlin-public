"""Compare the website's automatic choices with the accepted fixture requests.

    uv run python -m devtools.evaluate_fixtures              # CPU only, nothing billed
    uv run python -m devtools.evaluate_fixtures --caption    # PAID: one Gemini call each

For Kung Fu and Enzo it runs the worker's own preparation (normalize, choose_interval,
selection) on the tracked files and prints the interval, the automatic-or-tap decisions and the
points beside the accepted request (tests/fixtures/requests). Where a tap is needed the accepted
point is used, as a visitor would tap it. Without `--caption` the tier is the fixed policy.

`--caption` makes one real Replicate call per fixture (about USD 0.002 each, needs
GREMLIN_WEB_REPLICATE_TOKEN) and prints the model's prompt beside the accepted one, with the
accepted action beats as a checklist. The accepted prompts are also the instructions' examples,
so a close match here shows the shape works, not that the model generalizes.
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
from pathlib import Path

from gremlin_web import assist, normalize, selection
from gremlin_web.settings import Settings
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = {
    "kungfu": (
        "Kung Fu Fighting GIF.gif",
        [
            "raises both fists in a fighting guard",
            "throws fast punches and swings his arms",
            "lunges forward close to the camera",
            "steps back into his guard",
        ],
    ),
    "enzo": (
        "Enzo",
        [
            "salutes with his right hand at his forehead",
            "purses his lips",
            "cups both hands behind his ears",
            "tilts his head back toward the crowd",
        ],
    ),
}


def _file(prefix):
    return next((ROOT / "stickers").glob(prefix + "*"))


def _decide(choice, accepted_point):
    """(point, source, candidate index) as the site would end up with them."""
    if choice["point"] is not None:
        return choice["point"], "auto", choice["subject"]
    return accepted_point, "tap", None


def evaluate(name, settings):
    prefix, beats = FIXTURES[name]
    accepted = json.loads((ROOT / f"tests/fixtures/requests/{name}.json").read_text())
    reference, _ = normalize.reference(_file("Screenshot").read_bytes())
    with tempfile.NamedTemporaryFile(suffix=Path(prefix).suffix or ".mp4") as raw:
        Path(raw.name).write_bytes(_file(prefix).read_bytes())
        start, interval = normalize.choose_interval(Path(raw.name))
        clip, _ = normalize.driving(Path(raw.name), start, interval)
    with Image.open(io.BytesIO(reference)) as photo:
        photo.load()
        reference_choice = selection.choose_reference(photo)
        samples = normalize.samples(clip, interval)
        target_choice = selection.analyze_clip(samples, interval)
        reference_point, reference_source, reference_index = _decide(
            reference_choice, accepted["reference_point"]
        )
        target_point, target_source, target_index = _decide(target_choice, accepted["target_point"])
        duration = target_choice["duration_ms"]
        candidate = None if target_index is None else target_choice["candidates"][target_index]
        box = (
            None
            if reference_index is None
            else reference_choice["candidates"][reference_index]["box"]
        )
        photo_jpeg, frames = assist.caption_inputs(
            photo,
            box,
            reference_point,
            normalize.samples(clip, duration or interval),
            (candidate or {}).get("box"),
            target_point,
        )
    result = (
        assist.Captioned("fixed")
        if settings.caption_model == assist.FIXED
        else assist.caption(settings, photo_jpeg, frames, auto=target_source == "auto")
    )

    print(f"\n== {name} ==")
    rows = [
        (
            "interval",
            f"{start}-{start + interval} ms",
            f"{accepted['start_ms']}-{accepted['start_ms'] + accepted['duration_ms']} ms",
        ),
        ("clip length", f"{duration} ms", f"{accepted['duration_ms']} ms"),
        (
            "photo",
            f"{reference_source} ({reference_choice['why']}) {reference_point}",
            str(accepted["reference_point"]),
        ),
        (
            "character",
            f"{target_source} ({target_choice['why']}) {target_point}"
            + (f", {candidate['label']}" if candidate else ", free tap"),
            str(accepted["target_point"]),
        ),
    ]
    tier = selection.tier(candidate, result.caption) if result.status == "ok" else "complex-motion"
    rows.append((f"tier ({settings.caption_model})", tier, accepted["tier"]))
    width = max(len(r[0]) for r in rows)
    print(f"{'':{width}}  {'website':52}  accepted")
    for label, ours, theirs in rows:
        print(f"{label:{width}}  {ours:52}  {theirs}")
    if accepted["reference_point"] and box:
        inside = box[0] <= accepted["reference_point"]["x"] <= box[2] and (
            box[1] <= accepted["reference_point"]["y"] <= box[3]
        )
        print(f"accepted photo point inside the chosen box: {inside}")
    if candidate:
        x, y = accepted["target_point"]["x"], accepted["target_point"]["y"]
        b = candidate["box"]
        inside = b[0] <= x <= b[2] and b[1] <= y <= b[3]
        print(f"accepted character point inside the chosen box: {inside}")
    print(f"caption: {result.status}, usage {result.usage}")
    if result.status == "fixed":
        print("Generic description, not scene understanding: " + assist.FIXED_PROMPT)
    if result.status == "ok" and not settings.caption_model.startswith("local"):
        print("\nprompt:\n  " + assist.prompt(result.caption))
        print("accepted:\n  " + accepted["prompt"])
        print(
            "flags: " + json.dumps({k: result.caption[k] for k in (*assist.FLAGS, *assist.KINDS)})
        )
        print("accepted action beats (check each against the prompt):")
        for beat in beats:
            print(f"  [ ] {beat}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--caption",
        action="store_true",
        help="PAID: describe each fixture with the Replicate model",
    )
    parser.add_argument("--model", default="google/gemini-2.5-flash")
    parser.add_argument("fixtures", nargs="*", help=f"some of {', '.join(FIXTURES)} (default all)")
    args = parser.parse_args()
    if unknown := set(args.fixtures) - set(FIXTURES):
        parser.error(f"unknown fixtures: {', '.join(sorted(unknown))}")
    args.fixtures = args.fixtures or list(FIXTURES)
    settings = Settings(caption_model=args.model if args.caption else "fixed")
    if args.caption:
        print(f"Paid Replicate calls: {len(args.fixtures)} x {args.model}")
    for name in args.fixtures:
        evaluate(name, settings)


if __name__ == "__main__":
    main()
