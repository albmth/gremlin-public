# Architecture

The API and controller are one Python package (`control/gremlin_control`) on two AWS Lambdas in eu-north-1, with one DynamoDB table and one S3 bucket; media moves only through presigned links.
A one-minute sweep, API doorbells and RunPod's completion webhook drive the controller.
The worker is a Linux/CUDA image on one RunPod Serverless endpoint running pinned ComfyUI SAM and SCAIL-2 graphs from the job's tier recipe; it publishes MP4, animated WebP and poster.
The controller holds the only provider credential, runs one GPU session at a time within the spending limits, and holds dispatch for review when cost or worker state is uncertain.
Clients are direct (one scope) or delegated (`<client>:<owner>` scopes).
The local server can run a GPU-free simulator (`GREMLIN_LOCAL_WORKER`) for client development and the contract suite.
The website (`web/gremlin_web`) is a direct client on its own two Lambdas, table and bucket, with anonymous visitors scoped by a random cookie; `docs/web.md` owns it.
