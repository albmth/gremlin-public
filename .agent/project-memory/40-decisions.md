# Decisions

- The control API runs on Lambda, DynamoDB and S3 with zero idle cost (ADR 0001); media bytes go through presigned links because a Function URL takes at most 6 MB.
- Generation stays on the self-hosted SCAIL-2 worker on RunPod Serverless; hosted model APIs were tried and rejected because they lose the clip's motion (ADR 0002).
- Spending is operator configuration: hourly rate, rolling-day and monthly budgets default to zero; a session reserves its full cost before it starts and settles exactly once; uncertain outcomes hold dispatch for a human.
- Two accepted tiers, `simple-motion` and `complex-motion`, chosen explicitly by the client; the H100 40-step recipe is the preserved reference, not a tier.
- Any change to generated or restored frames is a paired comparison against the accepted controls, adopted only after the owner accepts the videos.
- Prompts are written by the client and passed to the model unchanged; a neutral caption turned a cat-driven clip into a static portrait.
- After diffusion only static driving content is restored; mask composites clipped limbs and were rejected.
- The worker runs serverless, not as a pod, and never holds a provider or AWS credential.
- Weights ship as one immutable Hugging Face release that RunPod fetches before the container starts; the worker hashes every file at boot.
- The controller does not patch the endpoint on routine dispatches, because each patch made RunPod replace the retained worker's container; it keeps a retained worker of the current image and retires anything else.
- No idle GPU retention beyond RunPod's 5 s: FlashBoot resumes for free within minutes, and paid idle buys nothing at this volume.
- Video encoders pick their own thread count; forcing it changed every delivered frame.
- The website is free and anonymous with a shared allowance; model-written descriptions also flag public figures when configured.
