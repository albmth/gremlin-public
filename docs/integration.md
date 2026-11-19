# Integrating with Gremlin

For a developer adding Gremlin to a backend.
The [API reference](api.md) has the exact shapes and codes; this is the how.

## What you get from the operator

| Item | Notes |
| --- | --- |
| Origin | The environment's HTTPS origin; staging and production differ |
| Credential file | JSON with a `token`; keep it in your backend's secret store, never on devices or in logs |
| Client kind | Direct (one scope for your whole client) or delegated (one scope per end user via `X-Gremlin-Owner`) |
| Admission terms | The daily job limit and when spending is authorized; hosted generation costs real money |

A multi-user product should be a delegated client: each user gets an isolated job list, idempotency scope and active-job slot under one service token.
Gremlin needs no credential of yours and you never get a GPU-provider or AWS credential.

## Run Gremlin locally

A checkout runs the real API on DynamoDB Local and MinIO with a simulated worker that publishes clearly labelled synthetic results of the right size and timing.
Nothing contacts a GPU or AWS; the CPU preflight is real.

```sh
# From the repository root; Docker must be running.
docker compose -f contract/compose.yaml up -d
TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
printf '{"token": "%s"}\n' "$TOKEN" > /tmp/gremlin-local-token.json
HASH=$(printf %s "$TOKEN" | shasum -a 256 | cut -d' ' -f1)
cd control
GREMLIN_LOCAL_WORKER=succeed GREMLIN_LISTEN=127.0.0.1:8765 \
GREMLIN_DYNAMODB_ENDPOINT=http://127.0.0.1:8001 GREMLIN_S3_ENDPOINT=http://127.0.0.1:9100 \
AWS_ACCESS_KEY_ID=gremlinlocal AWS_SECRET_ACCESS_KEY=gremlinlocalsecret \
GREMLIN_TABLE_NAME=gremlin-dev GREMLIN_MEDIA_BUCKET=gremlin-dev \
GREMLIN_CLIENT_TOKEN_HASHES="{\"my-app\":\"$HASH\"}" \
uv run --frozen python -m gremlin_control.local
```

- `GREMLIN_LOCAL_WORKER=succeed` publishes results; `reject` fails every job with `input_rejected`; `fail` makes every attempt fail with `worker_attempt_failed`.
- Add `GREMLIN_DELEGATED_CLIENTS='["my-app"]'` to develop as a delegated client.
- `GREMLIN_IMPRINT_DAILY_JOBS` raises the default of 8 jobs per rolling day.
- Presigned URLs point at `GREMLIN_S3_ENDPOINT`, so a device or emulator must reach that host (an Android emulator sees it as `10.0.2.2`).

Then run the example client against it:

```sh
python3 examples/gremlin_client.py --base http://127.0.0.1:8765 \
  --token-file /tmp/gremlin-local-token.json --request request.json \
  --reference reference.png --driving driving.mp4 --output out/
```

`request.json` is a create body; the script fills in both hashes.
[`examples/gremlin_client.py`](../examples/gremlin_client.py) depends only on `requests`, and the contract suite runs this exact command against both simulator modes, so it stays correct.

## Preparing inputs

The CPU preflight rejects malformed media before any GPU spend, but tracking and shot checks happen on the GPU and cost money when they fail (`failure_code: input_rejected` with a `failure_reason`).
Normalize the same way every time, then hash the normalized bytes:

- **Reference:** apply EXIF orientation, downscale to at most 1920 px on the long side, re-encode as PNG or JPEG without an orientation tag, and take `reference_point` on that upright image.
  HEIC is not readable.
- **Driving clip:** re-encode as upright 8-bit H.264 MP4 with rotation applied to the pixels, at most 1920 x 1080, 60 fps and 30 MiB, and take `target_point` on that upright frame.
  Gremlin ignores rotation metadata, so an unnormalized portrait phone video comes out sideways.
- **Interval:** trim to the action, set `start_ms` to 0 and keep the last frame's display duration so the clip covers the whole requested interval.
  One continuous shot only.

Preparation resamples to 16 fps and fits the scene on a 512 x 512 canvas when the aspect ratio is between 0.8 and 1.25, otherwise 896 x 512 or 512 x 896.

## Writing the prompt

Gremlin passes the prompt to the model unchanged and never captions inputs.
The model takes its motion from the prompt; with a neutral caption a cat-driven clip came out as a static portrait.
Write one English paragraph in the present tense:

1. The reference subject's visible appearance: face, build, fur or markings, clothing.
2. The target's action beats in order: gestures, strikes, turns, camera moves.
3. The scene: setting, lighting, props, bystanders, what the subject moves in front of.

Describe the actual reference, including an animal when that's what it is.
Keep names and contact details out; the prompt is stored with the job for 30 days.
The accepted examples are in `tests/fixtures/requests/`.

## Choosing the tier

| | `simple-motion` | `complex-motion` |
| --- | --- | --- |
| Use for | Real people moving naturally | Animals, cartoons, fights, stunts: anything the prompt must drive |
| Recipe | Distilled, 8 steps | 12 guided steps |
| On the RTX 5090 | About 55 s for a 5 s clip | About 75 s for a 3.4 s clip |

Gremlin never classifies motion; the client chooses.

## The client state machine

Persist an intent record before the first call and update it at each step:

1. Generate a random idempotency key, normalize the inputs, hash them, save the complete create body.
2. `POST /jobs`; save the returned job ID before any upload.
3. Upload both inputs, then submit.
4. Poll every few seconds while `queued` or active; back off to 15 s after five minutes.
5. On `succeeded`: download, verify, decode and durably store all three files, then call `/saved`.
6. On any other terminal state, record the `outcome` and stop.

Never create a new job to recover a lost response: replay the saved create body and you get the same job back.

| Situation | Gremlin guarantees | Do |
| --- | --- | --- |
| Create, upload-link, submit or poll response lost | Replays are safe and return the current state | Retry the same call |
| Storage upload response lost | A new link accepts the same bytes again | Request a new link and resend |
| Client restarted | Jobs persist | Resume from your records; `GET /jobs?state=active` lists unfinished jobs |
| `409 IMPRINT_ACTIVE` | One unfinished job per scope | Find it with `GET /jobs?state=active`, resume or cancel it |
| Job stays `queued` | It waits for a GPU or the operator until its deadline | Read `capabilities.admission`; tell the user; never replace the job |
| `422 IMPRINT_MEDIA_INVALID` after create | The declared hashes can't change | Cancel, fix normalization, new intent |
| `failed` with `input_rejected` | The same inputs fail the same way | Tell the user what to change; never resubmit automatically |
| Known ID answers 404 | Wrong origin or identity, pruning, or lost data look the same | Replay the saved create body: 410 means retired, same ID recovers it, a different ID means stop and ask the operator |
| `succeeded` with `result: null` | The media window ended | Report delivery unavailable |
| Crash after storing results, before `/saved` | `/saved` only shortens retention | Retry the acknowledgement |

Retry connection errors, timeouts, 502, 504 and any 429, 503 or 500 without `detail.code` with backoff; the example client does.

`outcome` is immutable, so a consumer allowance can be settled from it exactly once: consume on `ready`, release on `input_rejected`, other failures, unstarted cancels and expiry; a cancel after `attempt_started` is your product's call, because GPU time was spent.
Use `DELETE ?if_not_started=true` when a user cancels while waiting.

## Latency

Small samples, not service levels.

| Case | Create to result |
| --- | --- |
| Worker resumed from a FlashBoot snapshot (a job within minutes of the last) | Job time plus about 10 s |
| Host still has the image and weights | About 2 to 2.5 minutes |
| Fresh host | About 4 to 10 minutes |
| Each further queued job in the same session | 1 to 2 minutes |
| Capacity stall before a retry on another GPU | Up to 11 minutes |

Show "usually 5 to 10 minutes", switch to a delayed message after about 15, keep polling and never cancel or regenerate automatically.

## Acceptance checklist

1. No bearer token reaches storage, storage URLs never appear in logs, and every file is size-, hash- and decode-verified before use.
2. Lost create, upload, submit, poll and download responses each recover the same job with no second generation.
3. A restart after create and after submit resumes the same job.
4. `IMPRINT_MEDIA_INVALID`, `IMPRINT_LIMIT`, `IMPRINT_BUDGET_BLOCKED` and platform errors are handled without new keys.
5. For a delegated client, two owners cannot see or affect each other's jobs.
6. Consumer accounting settles exactly once from `outcome`.
7. One real request reaches durable storage in staging, then production, under explicit authorization.

Automated tests should use a local Gremlin; hosted generation spends real money.
