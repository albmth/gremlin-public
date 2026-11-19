# Engine

How a job gets generated and what runs it.
Clients only need the [API reference](api.md).

## Pipeline

A worker leases one queued job, pulls both inputs through presigned links and runs four stages:

1. **Prepare.**
   Decode the selected interval, resample to 16 fps, fit it on a 512 px canvas (512 x 512, 896 x 512 or 512 x 896 by aspect), pad to the model's 4n+1 frames, reject scene cuts.
2. **Track.**
   SAM segments the reference subject at `reference_point` and tracks the target at `target_point` through the clip.
   A mask under 0.2% or over 95% of the frame is rejected.
3. **Generate.**
   SCAIL-2 (fp8, on Wan 2.1 14B) replaces the tracked character with the reference identity, conditioned on the whole driving scene.
4. **Finish.**
   Static overlays like a broadcast clock are copied back from the original clip, then the MP4, animated WebP and poster are encoded and uploaded.

The graphs are pinned ComfyUI graphs built from the tier's recipe in `worker/tiers/`.
The engine stays loaded across stages and jobs; scratch is wiped after each attempt and at startup.
What leaves the worker as diagnostics is a stage name, numeric timings and a fixed failure code: no prompts, paths or exception text.

## Tiers

The client picks one per job.
The API's tier list must equal the files in `worker/tiers/`; a test checks it.

| Tier | Recipe | Use for |
| --- | --- | --- |
| `simple-motion` | DPO, relight and lightx2v distill LoRAs, 8 Euler steps, CFG 1, shift 5 | Real people moving naturally; about 55 s for a 5 s clip on an RTX 5090 |
| `complex-motion` | DPO and relight LoRAs, 12 UniPC steps, CFG 5, shift 3, tiled decode | Animals, cartoons, fights, anything the prompt has to drive; about 75 s for a 3.4 s clip |

Both share the same eight model files (30.8 GB).
`worker/recipe.json` is the original 40-step fp16 H100 recipe: the quality reference, not a tier.

## What runs it

| | |
| --- | --- |
| Worker image | Built from `worker/Dockerfile` in a few parallel-pullable layers; every accepted digest is listed in `control/gremlin_control/gpu_qualifications.json` and nothing else can be selected |
| Weights | One immutable Hugging Face release pinned in `worker/model-release.json`; RunPod fetches it before the container starts and the worker hashes every file at boot ([model release](model-release.md)) |
| GPU | RTX 5090 (preferred: fastest and cheapest per job); RTX PRO 4500 qualified as a fallback on the previous image |
| Endpoint | One RunPod Serverless endpoint, 0 to 1 workers, 5 s idle timeout, FlashBoot, 50 GB container disk, no volume |

Outputs reproduce bit for bit on the same GPU model and image.
They differ slightly between GPU models, so each GPU has its own accepted controls.

## Sessions

The controller starts at most one GPU session at a time:

1. Refuse if dispatch is off, a hold exists, or the budget can't cover a full reservation.
2. Read RunPod's live catalog, keep the qualified image/GPU pairs with stock, estimate each one's cost for the queued work and pick the cheapest (plus alternatives within 10%).
3. Reserve the highest offered hourly rate for 51 minutes and submit one provider job carrying a session token.
   The endpoint is patched only when the GPU set actually changes, because every patch makes RunPod replace the retained worker's container.
4. The worker leases queued jobs one at a time while at least 15 minutes remain of its 40-minute session.
   An input rejection ends only that attempt; any other failure ends the session.
5. RunPod's webhook rings the controller, which settles the session from the provider's timings plus 30 s.
   Missing timings keep the full reservation and hold dispatch for review.

| Bound | Value |
| --- | --- |
| No worker leased within | 660 s per placement, 3 placements, then a hold |
| Lease heartbeat | 120 s |
| Attempt | 900 s, then publication is fenced |
| Session | 2,400 s |
| Boot (hash weights, start the engine) | 600 s |

A worker on a GPU that doesn't match its qualification refuses to lease.
A lost submission response is identified through the worker's first lease and settled once; nothing is resubmitted blindly.

## Cold starts

Three cases, measured on production and staging in late October 2026:

| Host state at dispatch | Submit to first lease |
| --- | --- |
| FlashBoot snapshot of the previous worker still around (minutes since the last job) | 1 to 2 s |
| Host still has the image and weights, new container | 30 to 130 s |
| Fresh host: pull the image, fetch 30.8 GB of weights, boot | 2 to 10 minutes |

So a warm job is roughly 1.5 to 2.5 minutes from create to result, a cold one 4 to 10.
Most fresh starts used to be self-inflicted: the controller patched the endpoint on every dispatch and RunPod answered by replacing the retained worker.
Keeping a GPU idle to avoid the rest was rejected; at this volume idle time costs more than it saves.

## Known limits

- Quality is reviewed on two fixtures; other people, animals, scenes and camera motion are unvalidated.
- Faces drift toward a generic likeness in up-tilted close-ups, and the subject can come out a bit brighter than the scene.
- Overlay restoration can paint over an object that crosses a graphic, or miss a graphic visible in only 90 to 99% of frames.
- Transparent references and clips work but aren't qualified.
- GPU-side rejections (no subject at the point, target lost, scene cut) fail only that job with `input_rejected`; the worker carries on with the next one.
