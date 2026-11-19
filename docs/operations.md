# Operations

Deploying, configuring, watching and recovering the API and its GPU endpoint.
[`infra/aws/README.md`](../infra/aws/README.md) explains the IAM setup these scripts assume.
Use `state` and `inspect` to check a deployment's current configuration.

## Ground rules

- Deployments, IAM, SSM, pushes and any GPU spending need the owner's go-ahead each time.
- Routine commands use the `gremlin-dev` profile through `infra/aws/scripts/stack.sh`, which assumes the deployer role and checks account, identity and region first.
- Secrets travel through standard input or private files, never command arguments, chat or logs.

## What runs

| Component | Resource | Idle cost |
| --- | --- | --- |
| API | Lambda `gremlin-<env>-control-api` (arm64, 1769 MB, 30 s) behind a public Function URL; bearer tokens checked by the app | Per request |
| Controller | Lambda `gremlin-<env>-control-controller` (120 s), run every minute, by API doorbells and by the provider webhook | Cents a month |
| State | DynamoDB `gremlin-<env>-control-state`, on demand, 7-day point-in-time recovery, deletion protection | Under a cent |
| Media | S3 `gremlin-<env>-control-media-<account>`, private, SSE-S3, reachable only by the two function roles | Per job |
| Provider key | SSM SecureString `/gremlin/<env>/runpod-key`, readable only by the controller | None |
| Alerts | Two CloudWatch alarms and an SNS topic (production only) | About USD 0.50 a month |
| GPU | One RunPod Serverless endpoint, 0 to 1 workers | Only while a worker runs |

Staging has the same shape with no alarms and a disposable table and bucket.

## Commands

All take the environment first: `infra/aws/scripts/stack.sh <staging|prod> <command>`.

| Command | Effect |
| --- | --- |
| `state` | Jobs by state, sessions, holds, settled and held spending, configuration; no tokens, prompts or media |
| `params` | Writes the environment's parameter file from its client file; new files start closed |
| `client ID [--delegated]` | Mints a client identity for this environment; see [clients](#clients) |
| `set KEY=VALUE...` | Changes stack parameters locally; `deploy` applies them |
| `deploy` | Refuses uncommitted `control/` or template changes, then validates, builds and deploys with the commit as `BuildCommit` |
| `smoke` | `/health` and the controller's state report |
| `contract` | Staging only: the hook-free contract tests against the live origin |
| `gpu-plan [--trial]` | Fresh GPU candidates and estimates without reserving anything |
| `release SESSION` | Releases one reviewed dispatch hold |
| `close` | Deploys controller off and zero spending, then confirms the endpoint at 0/0 with no workers |
| `alarms`, `test-alert` | Alarm state; a deliberate notification drill |
| `inspect`, `logs [minutes]` | Resources and function configuration without secrets; sanitized recent logs |
| `teardown`, `verify-gone` | Staging only |

## Configuration

| Parameter | Meaning | Closed value |
| --- | --- | --- |
| `ControllerEnabled` | The controller may start GPU sessions | `false` |
| `HourlyMicrousd` | Highest hourly GPU rate a session may reserve | `0` |
| `RollingBudgetMicrousd`, `MonthlyBudgetMicrousd` | GPU spend per rolling 24 hours and per UTC month, enforced on reservations plus settlements | `0` |
| `BudgetMicrousd` | Optional lifetime cap for bounded trials; zero means none | `0` |
| `DailyJobs` | Creates per rolling 24 hours across all clients | `0` |
| `EndpointId` | The RunPod endpoint | |
| `ImprintEnabled` | `false` answers 404 `IMPRINT_DISABLED` to everyone | `true` |
| `SweepEnabled` | The one-minute sweep; keep it on so retention and settlement run | `true` |
| `GpuSelectionEnabled` | Automatic selection among qualified image/GPU pairs | `true` |
| `GpuQualificationEnabled`, `GpuQualificationTarget` | Staging-only trial of an unaccepted pair | `false` |
| `ClientTokenHashes`, `DelegatedClients` | Written by `params` from the client file | |
| `ModerationEnabled`, `ModerationGuardrailId`, `ModerationGuardrailVersion` | Fail-closed prompt and sampled image checks through Bedrock Guardrails and Rekognition in eu-west-1 | `false` |

Clients see the first closed control in `capabilities.admission.reason_code`.
Spending limits are software guards; RunPod does not enforce them.

To run something paid: confirm `state` shows no live session and no hold, `set` the controller on with the authorized rates and budgets, `deploy`, run the job (the example client with a fixture request works), then `close` and reconcile the posted charge against the settled amount.
Start `tools/worker_logs.py` in the background first; RunPod deletes a worker's logs with the worker.

## Clients

Each environment has an ignored client file, `infra/aws/.state/<env>-clients.json`, with SHA-256 hashes and the delegated list.
`stack.sh <env> client ID [--delegated]` writes the bearer token to `infra/aws/.state/<env>-client-ID.json` (mode 0600) and adds its hash; deliver that file privately, then `deploy`.
Use `--delegated` for a backend serving many end users; it must then send `X-Gremlin-Owner` on every call.
Never rename a client ID: job ownership and replay protection include it.
To rotate a token, replace the hash deliberately, run `params` and deploy.

## Monitoring

- `...-api-failures` fires on any Function URL 5xx within five minutes.
- `...-controller-health` fires when three of five one-minute heartbeats are unhealthy or missing: a failed reconciliation, a hold, a service-side job failure in the last 15 minutes, a queue older than 15 minutes, or a reached budget.
  Input rejections are the client's outcome and don't alert.
- Notifications carry only resource names and aggregate health.
- `logs` shows `gremlin.retention_cleanup_unconfirmed` and `gremlin.provider_reconciliation_unconfirmed` when a sweep could not confirm its work.

## Holds and recovery

A job's own failure never holds the queue: an input rejection fails that job and the worker moves on, and a session the provider ended with billed timings settles on its own.
A hold stays for sessions whose cost or worker state is uncertain: a provider job that had to be cancelled or timed out, missing timings, a model-cache or GPU rejection, an empty session whose worker never asked for work, three failed placements, or an undelivered qualification session.
Queued jobs keep their IDs, inputs and deadlines; clients see `dispatch_held`.

1. `state`: read the session, its attempts and provider evidence; confirm the uncertain work has stopped.
2. Check spending and queued deadlines.
3. `release <session-id>`; it never refunds a reservation or extends a deadline.
4. Check state and alarms.

To stop everything: `close` (waits for live sessions to settle), or set the endpoint to 0/0 in RunPod first for an immediate stop.
Rollback is deploying the previous commit with dispatch off; the table and bucket stay.
Never re-import into a used table or point a dispatching service at an older ledger: that loses replay protection and the budget guard.

## Updating the worker image

Every new digest needs a paired qualification on each GPU and the owner's acceptance of any changed frames before it goes into `gpu_qualifications.json`.

```sh
./tools/check.sh
docker build --platform linux/amd64 --target runtime -t ghcr.io/<you>/gremlin-imprint:$(git rev-parse --short HEAD) worker
uv run python tools/check_worker_image.py ghcr.io/<you>/gremlin-imprint:$(git rev-parse --short HEAD) --disk-gb 50 --models-root worker/models
rg -v '^#' docs/reference-outputs.sha256 | shasum -a 256 -c
uv run python tools/compare_outputs.py <candidate-run> <accepted-run>
```

The image check never pulls or runs inference.
The comparator exits nonzero on changed frames, dimensions, timing, WebP or poster bytes; [fixtures](fixtures.md) lists the accepted runs.
RunPod refuses submissions for about a minute after an image change (`ENDPOINT_PAUSED`), so switch images while nothing is queued.

## Security

- Clients authenticate with bearer tokens compared by SHA-256; only hashes are deployed.
- Every route checks the scope and answers 404 otherwise, so a job ID is never authorization.
- Workers use a per-session token; the provider webhook carries a per-session secret and only rings the controller.
- The worker never holds a provider or AWS credential; RunPod starts and stops it.
- Media moves only through presigned links bound to exact size and SHA-256; the bucket denies every other principal.
- Diagnostics and logs exclude prompts, media, tokens and raw exception text; [privacy](privacy.md) has the inventory.
- The deployer role cannot read table items or media objects.

## Moderation

Optional, fail-closed: a CLASSIC Bedrock Guardrail (`infra/aws/admin/moderation.yaml`, eu-west-1) checks the prompt, and Rekognition checks the reference, six driving frames and six result frames.
The API role gets only `bedrock:ApplyGuardrail` on that exact guardrail and `rekognition:DetectModerationLabels` in eu-west-1; no model invocation, logging or face indexing.
Pin a numbered guardrail version after every policy change, never `DRAFT`.
The policy refuses explicit sexual content, gore, self-harm, disturbing imagery, hate symbols, deceptive impersonation and incitement; ordinary sport, martial arts and swimwear pass.
Each stage gets three durable attempts; an unavailable service answers `IMPRINT_MODERATION_UNAVAILABLE` and nothing unchecked is published.
