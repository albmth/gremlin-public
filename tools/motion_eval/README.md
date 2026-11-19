# Motion fidelity evaluation

This package compares character-replacement candidates against the original driving clip, which is the ground truth ([ADR 0003](../../docs/adr/0003-motion-fidelity-first.md)).
It never provisions compute and has no production integration.
Run from the repository root with `PYTHONPATH=worker uv run python -m tools.motion_eval COMMAND --out artifacts/motion-eval-<date>`.

Every candidate receives the same inputs: the prepared driving interval at 16 fps on the 512 canvas, the aligned identity still that the Phase A campaign produced with FLUX.2 klein (verified against that run's ledger, never regenerated), and the subject masks of the preserved worker runs where a candidate takes a mask.
Candidates, prices and request bodies live in `matrix.py`; prices are list prices read on 29 October 2026 and every reservation is settled against the provider's posted charges.

| Cell | Endpoint | Architecture | Reserved USD (Kung Fu, Enzo) |
| --- | --- | --- | ---: |
| `KF-S2`, `EN-S2` | fal `fal-ai/scail-2`, replacement mode, end-to-end driving, 40 steps | In-context replacement from the raw driving frames and the aligned still | 0.7125, 1.0125 |
| `KF-VACE`, `EN-VACE` | fal `fal-ai/wan-vace-14b/inpainting`, 480p | Tracked-mask inpainting with the still as reference; untouched pixels are kept | 0.1425, 0.2025 |
| `KF-W27`, `EN-W27` | fal `fal-ai/wan/v2.7/edit-video`, 720p | Reference-guided video edit (reserved at twice the list price because input seconds may be billed) | 0.7125, 1.0125 |

Commands, in order:

1. `prepare --source-run artifacts/hosted-eval-2026-10-29` verifies the fixtures and baselines, copies the stills and the Phase A prompt-driven outputs, converts the tracking masks and writes the model-frame and 896 x 512 canvases.
2. `preflight` validates every planned body against the live fal schemas; `probe-transport` submits an invalid body of full size, cancels it and confirms the inline media are accepted (an invalid request produces no output and no charge).
3. `approve --max-usd N` records the owner's authorization (hard cap USD 6.00); `run [--cells A,B] [--seed N]` executes the fenced calls, one intent per call, never re-posting an uncertain submission.
4. `gallery` normalizes every output and baseline, burns labelled side-by-side comparisons, measures fidelity and writes `gallery/index.html`; `report` writes `report.md`; `settle --export CSV` records posted charges (`cell,request_id,posted_usd`).

The operator supplies `FAL_KEY` in the process environment; the tool never loads `.env`, and the ledger redacts keys and inline media.

## Measurements

Measurements support the owner's review and never replace it.
`background_psnr_db` is the PSNR of the pixels outside the tracked subject against the driving clip: the scene and camera survived when it is high.
`subject_flow_cosine` and `subject_flow_error` compare dense optical flow inside the subject region with the driving clip's flow (direction agreement weighted by the driving flow, and endpoint error over driving magnitude).
`motion_timing_r` correlates the per-frame subject motion energy curves, so it says whether the beats land at the same moments; `subject_energy_ratio` says whether the candidate moves as much as the original.

Baselines measured on 29 October 2026 from the preserved outputs:

| Clip | Background PSNR dB | Subject flow cosine | Subject flow error | Timing r | Energy ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Kung Fu, SCAIL-2 self-hosted RTX 5090 (accepted) | 15.0 | 0.25 | 2.02 | 0.59 | 1.95 |
| Kung Fu, hosted klein + Wan 2.2 i2v (rejected) | 15.8 | 0.04 | 2.01 | 0.18 | 1.36 |
| Enzo, SCAIL-2 self-hosted RTX 5090 (accepted) | 19.4 | 0.91 | 0.38 | 0.96 | 0.99 |
| Enzo, hosted klein + Wan 2.2 i2v (rejected) | 10.9 | 0.03 | 1.22 | 0.26 | 0.65 |

The Kung Fu driver is a dithered, motion-blurred GIF, so its flow is noisy and every candidate scores lower on it; compare candidates with each other and with the baselines on the same fixture.

Offline checks: `PYTHONPATH=worker uv run pytest tools/motion_eval/tests -q`.
These are codec, geometry and spending-fence checks, not inference evidence.
