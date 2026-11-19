# Privacy and retention

Gremlin's data policy for the API in `control/` and the GPU worker.
Retention periods run from the job's first terminal time and mean deletion eligibility, enforced by a sweep every minute, not physical erasure at an exact instant.

## Policy

| Data | Kept | Notes |
| --- | --- | --- |
| Inputs and results in storage | 72 hours after the job ends; `/saved` shortens this to at most 24 hours | Unfinished attempts, unsettled sessions and holds keep files until resolved |
| Sanitized diagnostics | 7 days after the job ends; gone at once after a cancel | Held sessions keep their evidence until reviewed |
| Job, attempt and upload records (scope, owner ID for delegated clients, prompt, points, hashes, key) | 30 days after the job ends, and only after its files are gone | Pruned whole; the ID then reads 404 |
| Replay ledger (hash of scope and key, admission time) | Indefinite | Stops an old intent buying another generation |
| Session accounting | Indefinite | Keeps the budget guard honest |
| Worker scratch | Wiped after each attempt and at startup | Uncertain work is never regenerated from scratch files |

- Cancellation fences downloads and publication at once; a link issued before it stays usable until it lapses (120 s).
- Clients never get original inputs back.

## Where data goes

| Location | Contents | Cleanup |
| --- | --- | --- |
| S3 `jobs/<job-id>/`, eu-north-1 | `reference`, `driving`, `result.mp4`, `result.webp`, `poster.jpg`, `diagnostics.zip` | The sweep deletes on the deadlines above, after every upload link has lapsed; pruning removes the whole prefix |
| DynamoDB | Job, attempt, session and replay records; scheduling entries without personal data | Pruned on schedule; point-in-time recovery keeps deleted items restorable for 7 days |
| Submit preflight | Input bytes and a decoded first frame in a child process fed through stdin | Nothing hits Lambda `/tmp`; errors carry fixed codes only |
| Moderation, eu-west-1 (when enabled) | The prompt to Bedrock Guardrails; a resized reference and sampled frames to Rekognition | In process memory only; Gremlin stores acceptance, counters and fixed codes, never assessments |
| CloudWatch Logs, 7 days | Fixed operational events | No prompts, media, tokens or provider responses |
| Worker `/work/imprint-runtime/` | Inputs, prepared frames, masks, rendered frames, encoder intermediates | Removed after each attempt and at startup |
| Provider job input and logs | API origin, session ID, session token, boot and status metadata | Provider-controlled lifetime |

The diagnostic ZIP holds only `diagnostics.json`: a schema version, a stage, allowlisted numeric timings and an optional fixed failure code.
The API validates and repacks every archive before storing or serving it.

## Limits

- Deleting an unversioned S3 object removes the only copy, but no provider certifies physical erasure.
- Deleted table items survive in point-in-time recovery for up to 7 days.
- FlashBoot can retain worker state after spin-down; the worker cleans before each attempt completes and before a resumed session rather than relying on scale-to-zero.
- The bucket is in Stockholm; GPU workers run in whichever European RunPod data centre has stock, so processing location is recorded, not guaranteed.

## What clients must do

- Keep user identifiers and contact details out of idempotency keys, owner IDs and prompts.
- Store your own copies under your own retention and deletion policy; Gremlin's sweep can't delete them.
- Protect the bearer token and treat storage URLs as short-lived secrets.

## Owner erasure

A delegated client can request deletion of one owner scope through `DELETE /api/imprint/owner`.
The controller cancels active work and removes that scope's media, prompts, job and attempt records once upload links expire and sessions settle.
The receipt keeps only a hashed scope and timestamps for 30 days; anonymous session totals and replay hashes remain.

## Demo previews

The website exposes completed media only to the visitor who generated it.
Closing the preview deletes the website’s media and generation record.
Tab closure uses a best-effort keepalive request; abandoned media expires after the configured retention period (6 hours by default), with deletion by the minute sweep.
Deletion also cancels the completed generation API job, revoking access and scheduling its media cleanup after outstanding upload links and sessions settle.
Downloaded copies and provider retention remain outside this deletion.
