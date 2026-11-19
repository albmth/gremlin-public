"""One-shot hosted SCAIL-2 comparison, with durable receipts and no POST retries.

Run prepare, submit PROVIDER, then collect PROVIDER. Media and receipts stay ignored.
The submit marker deliberately survives ambiguous HTTP failures: inspect before retrying.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import requests

from tools.hosted_eval.providers import download, validate

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/meimei-scail2-comparison-20261104"
MODELS = {"fal": "fal-ai/scail-2", "replicate": "sprited/scail-2"}


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + "\n")


def read(name):
    return json.loads((OUT / name).read_text())


def credentials():
    # Read only the two authorized credentials; never serialize the environment.
    env = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if key.strip() in {"FAL_API_KEY", "FAL_KEY", "REPLICATE_API_TOKEN"}:
                env.setdefault(key.strip(), value.strip().strip("\"'"))
    return {
        "fal": {"Authorization": "Key " + (env.get("FAL_KEY") or env["FAL_API_KEY"])},
        "replicate": {"Authorization": "Bearer " + env["REPLICATE_API_TOKEN"]},
    }


def api(provider, method, url, **kwargs):
    response = requests.request(
        method,
        url,
        headers=credentials()[provider] | kwargs.pop("headers", {}),
        timeout=(15, 60),
        allow_redirects=False,
        **kwargs,
    )
    # Responses stay local; errors must not echo a credential-bearing request.
    if response.status_code >= 300:
        save(
            f"{provider}-http-{time.time_ns()}.json",
            {
                "method": method,
                "status": response.status_code,
                "response": response.text,
            },
        )
        raise RuntimeError(f"{provider} {method} HTTP {response.status_code}")
    return response


def probe(path):
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def prepare():
    OUT.mkdir(parents=True, exist_ok=True)
    source = ROOT / "artifacts/web-demo-snow-leopard-2026-11-02"
    reference, driving, gremlin = (
        source / n for n in ["reference.png", "driving-source.mp4", "result.mp4"]
    )
    pins = {
        reference: "4d60ebb940421038d6024f7181371480b1e4d4acee4fddf76521c30aad791008",
        driving: "dd1ce7d9ed8d08a206981fe750ed2e71f5a8977ecfdf1e4b86e837890dadb92e",
        gremlin: "6ac5cd1c2042fdb34c9e0b8093d4e03537d59f7153d75c724ecbbb0da5750115",
    }
    for path, expected in pins.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Source hash mismatch: {path.name}")
    prompt = (
        "A cute anthropomorphic snow leopard with large round dark eyes, small rounded ears, "
        "a short feline muzzle, white cheeks, and dense silver-grey fur covered in dark rosettes "
        "celebrates in a packed football stadium. Its furry body has no clothing. "
        "It raises both furry forepaws beside and behind its ears, spreading its elbows outward, "
        "then tilts and turns its head toward the crowd with an expressive face. "
        "It moves naturally at the foreground player's position beside the green football pitch, "
        "with crowded stands, blue advertising boards and other players in blue shirts behind it. "
        "The broadcast camera follows the close-up celebration with the original camera movement."
    )
    for source, name in [
        (reference, "reference.png"),
        (driving, "source-driving.mp4"),
        (gremlin, "gremlin.mp4"),
    ]:
        shutil.copyfile(source, OUT / name)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-n",
            "-i",
            str(driving),
            "-map",
            "0:v:0",
            "-vf",
            "trim=start=0:end=3,setpts=PTS-STARTPTS,fps=16,tpad=stop_mode=clone:stop_duration=0.0625",
            "-frames:v",
            "49",
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "10",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(OUT / "driving.mp4"),
        ],
        check=True,
    )
    save(
        "inputs.json",
        {
            "reference": str(reference.relative_to(ROOT)),
            "driving": str(driving.relative_to(ROOT)),
            "gremlin": str(gremlin.relative_to(ROOT)),
            "interval_ms": [0, 3000],
            "preprocessing": (
                "16 fps, first 3 seconds, repeat final frame once for 49 frames (4n+1); "
                "no crop or restoration"
            ),
            "prompt": prompt,
            "sha256": {
                name: hashlib.sha256((OUT / name).read_bytes()).hexdigest()
                for name in ["reference.png", "driving.mp4", "gremlin.mp4"]
            },
        },
    )
    print("Prepared pinned inputs and unchanged accepted Gremlin video")


def uri(name, mime):
    return f"data:{mime};base64," + base64.b64encode((OUT / name).read_bytes()).decode()


def submit(provider, subject_type="human"):
    prompt = read("inputs.json")["prompt"]
    if provider == "fal":
        body = dict(
            prompt=prompt,
            image_url=uri("reference.png", "image/png"),
            video_url=uri("driving.mp4", "video/mp4"),
            mode="replacement",
            driving_type="end_to_end",
            subject_type=subject_type,
            resolution="512p",
            num_inference_steps=40,
            guidance_scale=5,
            shift=3,
            seed=42,
            enable_safety_checker=True,
        )
        schema = read("research/fal-schema.json")
        validate(
            body, schema["components"]["schemas"]["Scail2Input"], schema["components"]["schemas"]
        )
        price = read("research/fal-price.json")["prices"][0]
        assert price["unit"] == "seconds" and price["unit_price"] <= 0.20
        url = "https://queue.fal.run/fal-ai/scail-2"
        payload, headers = body, {}
    else:
        model = read("research/replicate-model.json")
        body = dict(
            image=uri("reference.png", "image/png"),
            video=uri("driving.mp4", "video/mp4"),
            mode="replacement",
            prompt=prompt,
            preset="fast",
            resolution="512p",
            fps=16,
            num_frames=49,
            seed=42,
            auto_mask=True,
            return_masks=True,
            vae_precision="bf16",
            output_format="mp4",
            output_quality=95,
        )
        schema = model["latest_version"]["openapi_schema"]["components"]["schemas"]
        validate(body, schema["Input"], schema)
        payload = {"version": model["latest_version"]["id"], "input": body}
        headers = {"Cancel-After": "10m"}
        url = "https://api.replicate.com/v1/predictions"
    # Reserve $0.6125 fal + $0.915 Replicate plus ~$0.58 already spent on invalid inputs.
    # The combined bound stays below the owner's original $3 cap.
    # Reserve the whole allowance before sending. Never resubmit automatically.
    marker = OUT / f"{provider}-submission-marker.json"
    with marker.open("x") as stream:
        json.dump(
            {"at": time.time(), "reserved_usd": 0.6125 if provider == "fal" else 0.915}, stream
        )
    save(
        f"{provider}-parameters.json",
        {
            k: ("local:" + ("reference.png" if "image" in k else "driving.mp4"))
            if k in {"image", "video", "image_url", "video_url"}
            else v
            for k, v in body.items()
        },
    )
    response = api(provider, "POST", url, json=payload, headers=headers)
    save(f"{provider}-receipt.json", response.json())
    print(provider, "submitted", response.json().get("id", response.json().get("request_id")))


def collect(provider):
    record = read(f"{provider}-receipt.json")
    url = record["status_url"] if provider == "fal" else record["urls"]["get"]
    while True:
        response = api(provider, "GET", url)
        record = record | response.json()
        save(f"{provider}-latest.json", record)
        with (OUT / f"{provider}-observations.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": time.time(), "record": record}) + "\n")
        status = record["status"]
        print(provider, status, flush=True)
        if status in {"succeeded", "failed", "canceled", "aborted", "COMPLETED"}:
            break
        time.sleep(15)
    if provider == "fal":
        result = api(provider, "GET", record["response_url"])
        save("fal-result-headers.json", dict(result.headers))
        record["output"] = result.json()
    save(f"{provider}-result.json", record)
    output = record.get("output")
    if not output:
        raise RuntimeError(f"{provider}: terminal status {status}, no output")
    for key, value in output.items():
        url = value.get("url") if isinstance(value, dict) else value
        if isinstance(url, str) and url.startswith("https://"):
            extension = {
                "video": ".mp4",
                "metadata": ".json",
                "reference_mask": ".png",
                "driving_mask": ".mkv",
            }.get(key, Path(url.split("?")[0]).suffix)
            name = f"{provider}{'' if key == 'video' else '-' + key}{extension}"
            download(
                url,
                OUT / name,
                ["replicate.delivery", "fal.media", "fal.run", "storage.googleapis.com"],
            )
    save(f"{provider}-probe.json", probe(OUT / f"{provider}.mp4"))
    print(provider, "downloaded", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "submit", "collect", "page"])
    parser.add_argument("provider", nargs="?", choices=list(MODELS))
    parser.add_argument("--fal-subject-type", choices=["human", "animal"], default="human")
    parser.add_argument(
        "--fal-animal-retry",
        action="store_true",
        help="Use the preserved technical-failure retry directory",
    )
    args = parser.parse_args()
    global OUT
    if args.fal_animal_retry:
        if args.provider != "fal":
            parser.error("the technical retry directory is for fal only")
        OUT = OUT / "fal-animal-retry"
    if args.action == "prepare":
        prepare()
    elif args.action == "page":
        html = Path(__file__).with_name("scail2_comparison.html").read_text()
        if (OUT / "fal.mp4").exists():
            fal = (
                '<figure><h2>fal.ai</h2><video id="fal" src="fal.mp4" '
                'muted playsinline preload="auto"></video><p class="meta" data-for="fal"></p>'
                '<a href="fal.mp4">Open original MP4</a></figure>'
            )
        elif (OUT / "fal-preprocessing-failure.json").exists():
            fal = (
                "<figure><h2>fal.ai - no generated video</h2>"
                "<p>The human setting rejected Meimei: no subject detected in the reference.</p>"
                "<p>The animal setting rejected Enzo: no subject detected in the driving video.</p>"
                "<p>The documented API has no separate reference/driver subject settings "
                "or explicit mask inputs. Both attempts are preserved.</p>"
                '<a href="fal-preprocessing-failure.json">Human-setting failure</a> · '
                '<a href="fal-animal-retry/fal-preprocessing-failure.json">'
                "Animal-setting failure</a></figure>"
            )
        else:
            raise RuntimeError(
                "Collect fal output or preserve its failure before building the page"
            )
        (OUT / "index.html").write_text(html.replace("<!-- FAL_RESULT -->", fal))
    else:
        if not args.provider:
            parser.error("provider required")
        if args.action == "submit":
            submit(args.provider, args.fal_subject_type)
        else:
            collect(args.provider)


if __name__ == "__main__":
    main()
