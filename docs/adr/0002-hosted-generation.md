---
status: superseded by ADR 0003 (rejected as the product, 29 October 2026) and ADR 0004 (removed from the tree, 30 October 2026)
date: 2026-10-29
---

# Imprint generation moves from a self-hosted GPU worker to hosted model APIs

## Context

Until 29 October 2026 every Imprint ran SAM tracking and Wan 2.1 SCAIL-2 character replacement on one RunPod Serverless GPU session controlled by the AWS control plane ([ADR 0001](0001-control-api-on-aws.md)).
It transferred the driving clip's motion frame by frame, cost about USD 0.05 to 0.07 per job, and took 143 s warm and 472 to 589 s cold, with stalls of up to 11 minutes.
The owner's blind review of 29 October scored the accepted cat-driven Kung Fu output 1 of 5 for realism, and the owner set a ceiling of strictly under USD 0.10 per complete job with end-to-end speed as the next differentiator.
No hosted motion-controlled API fit that ceiling.
The initial comparison generated nine hosted candidates.
In the blind review the owner saved one hosted Kung Fu output (FLUX.2 klein 4B edit on fal, then Wan 2.2 A14B fast on Replicate) with identity and smoothness 5 and movement 1, and kept only the preserved SCAIL-2 outputs for Enzo.

## Decision

Imprint generation is a two-stage hosted pipeline run inside the existing controller Lambda:

1. FLUX.2 klein 4B edit on fal places the reference identity into the driving clip's first frame on the 512 canvas.
2. Wan 2.2 A14B image-to-video (fast, 480p, 81 frames) on Replicate animates that frame from the client's prompt.

The control plane keeps the client contract, moderation, budgets, retention and erasure unchanged, reserves a per-job amount before any paid call, settles from the stage prices, and never submits a paid request twice.
The RunPod dispatch, GPU selection and worker protocol are removed from the control plane; the worker source stays in the tree as the retired route.
The product promise changes from motion transfer to prompt-driven motion, and the contract and documentation say so.

## Consequences

- A job completes in tens of seconds with no cold start, at roughly USD 0.07 reserved and USD 0.065 settled, all inside the USD 0.10 ceiling.
- The driving clip's motion is approximated from the prompt; the owner accepted this and asked to preserve room for later motion-fidelity improvements, which the stage design allows (the video stage is one adapter).
- In this design, likeness media would leave AWS for fal and Replicate during a job.
- Quality is accepted by the owner on real outputs only; the hosted route has one saved Kung Fu output and no owner-accepted Enzo output yet.
- Only staging received the hosted build, with dispatch closed.
  [ADR 0004](0004-return-to-runpod-scail2.md) records the return to the self-hosted route.
