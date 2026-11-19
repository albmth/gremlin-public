# Contract suite

Black-box tests of Gremlin's client and worker contract.
They drive a running service over HTTP only: the local target judges `control/` on DynamoDB Local and MinIO, and the hook-free subset judges a deployed stack.
[`docs/api.md`](../docs/api.md) is the contract these tests enforce.

## Targets

| Target | What runs |
| --- | --- |
| `local` (default) | `python -m gremlin_control.local` from `control/`, against DynamoDB Local and MinIO (`compose.yaml`), with a fresh table and bucket per test |
| `url` | An already running origin at `CONTRACT_BASE_URL` with the client token in `CONTRACT_CLIENT_TOKEN`; only tests marked `hookless` run |

Each test starts its own service process with its own configuration and empty state, and an in-process fake RunPod (`gremlin_contract/fake_runpod.py`) that records submissions, cancellations, capacity changes and webhooks and can drop or fail any response.
Restart tests stop the process and start it again on the same state.

```sh
docker compose -f contract/compose.yaml up -d
uv run pytest contract
CONTRACT_BASE_URL=https://<id>.lambda-url.eu-north-1.on.aws CONTRACT_CLIENT_TOKEN=<token> \
  uv run pytest contract --target url
```

`tools/check.sh` runs the local target and `control/`'s own tests; `infra/aws/scripts/stack.sh staging contract` runs the hook-free subset against staging.

| File | Covers |
| --- | --- |
| `test_jobs_and_sessions.py` | Replay, ownership, leases, fencing, sessions, dispatch, failure isolation and accounting |
| `test_media_routes.py` | Presigned transfers, preflight, diagnostics sanitizing, cleanup failures |
| `test_retention.py` | Every retention boundary on a frozen clock |
| `test_contract_v3.py` | Owner scope, admission status, outcomes, conditional cancel, queue position, deadlines, listing |
| `test_example_client.py` | The published example client under lost responses, failures and corrupt delivery |
| `test_simulated_worker.py` | The documented local loop: the example CLI against both simulator modes |
| `test_worker_client.py` | The shipped worker client publishing through storage |
| `test_hookless.py` | Authentication, validation and closed admission, safe against a deployed origin |

## Test hooks

Served only when `GREMLIN_TEST_HOOKS=1` and `GREMLIN_ENVIRONMENT=test`.
Every service clock reads real time plus the offset, or the frozen instant.

| Route | Body | Effect |
| --- | --- | --- |
| `POST /__test/clock` | `{"offset_seconds": n}` or `{"frozen_at": t}` | Moves or freezes the service clock |
| `POST /__test/settings` | Setting names and values | Changes the running service's settings |
| `POST /__test/reconcile` | | One retention and lease-expiry pass; answers `{"raised": <exception name or null>}` |
| `POST /__test/tick`, `/__test/dispatch` | Optional setting overrides for dispatch | One provider tick or dispatch with the controller enabled on a copy of the settings |
| `POST /__test/sweep` | | The production housekeeping pass with the served settings |
| `POST /__test/reserve`, `/__test/finish-session`, `/__test/release` | Session fields | Reserve, settle or release directly, for flows that need no provider |
| `POST /__test/seed-job` | `job_id`, `expires_at`, `terminal_at` | Sets a job's clocks, for states only older data produces |
| `POST /__test/put-media` | `job_id`, `kind`, base64 `data` | Stores raw bytes as an older release would have |
| `POST /__test/fail-next-delete` | `name` | The next deletion of that media name fails once |
| `GET /__test/records` | | Normalized jobs, sessions, attempts, replay ledger, stored media, budget held and doorbell rings |
