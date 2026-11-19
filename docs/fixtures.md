# Fixtures and quality review

Two fixtures carry all quality decisions: a screenshot of a person on the Kung Fu Fighting cat GIF (`complex-motion`) and the same person on a football celebration clip (`simple-motion`).
The media is private and not in Git; `tests/fixtures/manifest.json` pins its names and SHA-256, and the requests in `tests/fixtures/requests/` fix seed 42, the interval and the subject points.
Tests that need the media skip when `stickers/` is missing.
Accepted outputs live outside Git under `artifacts/`, with their hashes in `docs/reference-outputs.sha256`:

```sh
rg -v '^#' docs/reference-outputs.sha256 | shasum -a 256 -c
```

| Request | Driving source | Interval | Output / model frames |
| --- | --- | --- | --- |
| `kungfu.json` | GIF, 480 x 476 | 0 to 3400 ms | 55 / 57 |
| `enzo.json` | MP4, 498 x 498 | 0 to 5000 ms | 80 / 81 |

## Accepted outputs

Each GPU model has its own control per tier, because outputs differ slightly between GPU models.

| Fixture | Control | Directory under `artifacts/` |
| --- | --- | --- |
| Kung Fu, `complex-motion` | RTX 5090, 12-step tier | `architecture-review-2026-10-26/runs/c12-kungfu` |
| Enzo, `simple-motion` | RTX 5090 | `qualification/g-enzo-simple` |
| Kung Fu, `complex-motion` | RTX PRO 4500, 12-step tier | `qualification/boot-tier-20261026-pro4500-kungfu-complex` |
| Enzo, `simple-motion` | RTX PRO 4500 | `qualification/pro4500-enzo-simple` |
| Both, H100 40-step recipe | The original reference | `e2e/download-kungfu`, `e2e/download-enzo` |

Never regenerate, re-encode or overwrite an accepted artifact.
A change that is meant to alter frames gets a new run compared beside these, and the owner watches both videos before anything is adopted.

## Local preparation (no GPU)

```sh
PYTHONPATH=worker uv run python -m tools.fixtures --name kungfu --output artifacts/prepared-kungfu
PYTHONPATH=worker uv run python -m tools.fixtures --name enzo --output artifacts/prepared-enzo
```

This verifies the hashes, resamples, fits the canvas and writes the preparation manifest; the MKVs still show the original scene.
`uv run pytest -q` also runs both fixtures through the preflight and the worker's encoders and checks the published result geometry, without any model.

## Genuine inference

The normal path is the hosted service: run the [example client](../examples/gremlin_client.py) against an environment with a fixture credential, a fixture request and a new idempotency key, under a bounded spending authorization ([operations](operations.md#configuration)).

For quality iteration over SSH, create a RunPod pod from `runpod/pytorch:1.0.7-cu1300-torch291-ubuntu2404-cluster`, sync the repository (no weights, no artifacts) to `/opt/gremlin` and run `bash /opt/gremlin/worker/dev-pod-setup.sh`.
It mirrors `worker/Dockerfile`, downloads and verifies the weights and starts ComfyUI; about 12 minutes.
Run `tools/gpu_watchdog.py --pod ID --max-minutes N --max-usd X` on your own machine with the key from `.env` so the pod can't outlive its allowance; the pod itself never holds a credential.
Then:

```sh
PYTHONPATH=worker uv run python -m tools.generate_fixture --name kungfu \
  --comfy-root artifacts/ComfyUI --url http://127.0.0.1:8188 --output artifacts/inference-kungfu
```

The run follows the request's tier; `--recipe worker/recipe.json` runs the H100 reference recipe instead, and `--reference PATH --reference-point X,Y` swaps the reference image while keeping everything else.
The runner needs the ComfyUI input/output filesystem, not just the URL.

## Review

```sh
PYTHONPATH=worker uv run python -m tools.review_fixture --name kungfu \
  --result artifacts/inference-kungfu --output artifacts/review-kungfu
uv run python tools/compare_outputs.py artifacts/inference-kungfu artifacts/architecture-review-2026-10-26/runs/c12-kungfu
```

Review verifies the hashes and builds `index.html` with a synchronized original/generated comparison.
Watch the whole thing: identity, body consistency, motion, timing, bystanders, occlusions, lighting, background stability.
The comparator exits nonzero on any changed frame, dimension, timing, WebP or poster byte; it tells you whether something changed, not whether it's better.

`cd web && uv run python -m devtools.evaluate_fixtures` runs the website's automatic interval and subject choices on the same fixtures and prints them beside the accepted requests.
