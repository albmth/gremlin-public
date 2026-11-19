---
status: accepted; direction set by the owner on 29 October 2026; the video stage question was settled by ADR 0004 on 30 October 2026 (the self-hosted SCAIL-2 worker)
date: 2026-10-29
---

# Imprint is character replacement with motion fidelity, not prompt-driven image-to-video

## Context

[ADR 0002](0002-hosted-generation.md) moved generation to a hosted two-stage route: FLUX.2 klein 4B edits the reference person into the driving clip's first frame and Wan 2.2 A14B animates that frame from the prompt.
The owner reviewed its outputs on 29 October 2026 and rejected the route as the product: it does not fulfil Imprint's purpose.
An Imprint takes an existing GIF or video and replaces its character with the reference person while faithfully preserving the original movement, actions, poses, timing, camera, environment and interactions; the replacement must look realistic and naturally integrated.
Ground-truth measurements against the driving clips ([the motion fidelity evaluation](../../tools/motion_eval/README.md)) quantify the loss: on Enzo the self-hosted SCAIL-2 output follows the driving motion with a subject flow cosine of 0.91 and a timing correlation of 0.96, the prompt-driven output 0.03 and 0.26.
The earlier turn had been driven by a USD 0.10 per job ceiling that no hosted motion-controlled API met; the owner now prefers hosted services, low cost and simple infrastructure but will not trade the core function for them.

## Decision

- The product requirement is motion-faithful character replacement; a visually pleasing generated video that approximates the clip is a failure.
- Architectures are compared on both fixtures against the original clips as ground truth, with synchronized comparisons, measured cost and latency; the owner judges real outputs, and no model, provider or router is committed in advance.
- The reference-alignment still (the klein edit, the one owner-liked element of the rejected route) is retained as the first stage of every candidate; the video stage is the open question.
- Candidates must condition on the driving pixels themselves (in-context replacement, masked inpainting with a reference, reference-guided video editing); skeleton-driven and prompt-driven routes are excluded by evidence.
- Production continues on the self-hosted SCAIL-2 route while staging keeps dispatch closed.
- Paid evaluation runs only under an explicit, capped authorization recorded in the campaign ledger.

## Consequences

- `tools/motion_eval/` holds the harness: hash-verified inputs, the tracked subject masks of the preserved worker runs, fenced provider calls under a hard cap, normalized outputs, labelled synchronized comparisons and the fidelity measurements.
- In the 29 October 2026 evaluation, the hosted candidates had weaker motion fidelity and cost more than the self-hosted output.
  [ADR 0004](0004-return-to-runpod-scail2.md) kept the self-hosted SCAIL-2 worker as the video stage.
- The USD 0.10 ceiling of 29 October is no longer a hard gate; cost is reported beside quality and decided by the owner.
