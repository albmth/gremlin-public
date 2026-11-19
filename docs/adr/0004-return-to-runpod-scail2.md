---
status: accepted; the owner's decision of 30 October 2026, restored in the tree the same day
date: 2026-10-30
---

# Gremlin returns to the RunPod SCAIL-2 worker as its production architecture

## Context

On 29 October 2026 Gremlin tried to replace its self-hosted GPU route with hosted model APIs ([ADR 0002](0002-hosted-generation.md)): FLUX.2 klein 4B on fal edited the identity into the driving frame and Wan 2.2 A14B on Replicate animated that frame from the prompt.
The owner rejected the result as the product the same evening ([ADR 0003](0003-motion-fidelity-first.md)): the clip's motion was approximated from the prompt, not transferred.
The follow-up evaluation found that hosted motion-conditioned candidates cost more and had weaker motion fidelity.
Fal's SCAIL-2 refused a human reference on an animal clip and cost USD 0.51 for a 5 s clip with 11 minutes end to end, Wan VACE inpainting ignored the mask, and Wan 2.7 lost the action and regenerated the scene.
Measured against the driving clip as ground truth, the self-hosted SCAIL-2 worker remained the only engine that transfers the motion on both fixtures (Enzo subject flow cosine 0.91, timing correlation 0.96) at USD 0.035 to 0.06 per job.
Production kept serving from the GPU route throughout the experiment.
The hosted detour cost USD 2.90 of fal credit and about USD 0.16 of smoke runs and produced no accepted output.

## Decision

- Gremlin uses the RunPod Serverless SCAIL-2 route in production.
- The evaluation runners are in `tools/hosted_eval/` and `tools/motion_eval/`.
- The product requirement of ADR 0003 stands: an Imprint preserves the clip's movement, poses, timing, camera, environment and interactions, judged against the original clip, with the accepted Enzo and Kung Fu outputs as regression controls.
- The next engineering work is the latency of the existing route (cold start, weight preparation, tracking, generation, encoding, delivery), not a provider or model change; a permanently running GPU is not the default answer because the service is low-volume.
- Keep the aligned-still reference experiment as a separate quality follow-up for Kung Fu, with Enzo's accepted motion fidelity as a regression check.

## Consequences

- One architecture is described in the tree: `docs/engine.md`, `docs/operations.md`, `docs/api.md` and the contract suite are those of the production build; `capabilities.features` no longer advertises `prompt_driven_motion`.
- The per-job USD 0.10 ceiling of 29 October is withdrawn; cost is reported beside quality and latency for the owner's decisions.
- Hosted services may still supply a single stage (the alignment still) if the owner accepts its outputs; they do not generate the video.
