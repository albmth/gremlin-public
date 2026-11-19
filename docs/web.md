# The website

`web/` is the public demo: open it, upload a photo and a clip, get a video, no account.
Finished videos open in a private preview popup; closing it deletes the site’s copy.
It calls the Gremlin API as one direct client, exactly like any other client would; nothing in `control/` or `worker/` depends on it.
The site calls itself "Adding to the Slop"; the Terms name Gremlin as the side project behind it.

## What a visitor does

1. Pick a reference photo (a person or an animal) and a driving clip or animation.
   Each file is checked when picked (its bytes match its format, images decode, a clip the browser can read is at least 3 s), then uploads start as soon as both are chosen.
2. Press **Generate**.
   The worker normalizes the media, picks the usable interval and finds the subjects with a small CPU detector.
   If both subjects are clear, the job goes straight to the GPU.
3. Only if something is unclear does the video page ask: tap the intended character or pick a numbered candidate, adjust the interval, edit the description, then **Generate** again.
   Corrections cost nothing from the allowance.
4. Watch six stages fill in from real records: Preparing media, Finding a GPU, Loading models, Generating video, Processing output, Delivering.
   Timers freeze when the connection drops and reloading recovers the current state.
5. Watch the private preview and download the MP4 if you want to keep it.
   Closing the preview deletes the website's copy.

When the allowance is used up or Gremlin is paused or unreachable, the home page says why and takes no uploads.
The API refuses new drafts while Gremlin is closed too, so a paused site doesn't spend a network's upload allowance.
While someone else's video holds the one slot, Generate waits and the page checks back every 20 s.

The header has the shared allowance as ten bars and a count, a status line while Gremlin is busy or closed, and an info button that opens a side panel: what it is, how it works, what was hard, limits, and the Terms.
The hosted demo shows a real photo, driving clip and generated result as an example.
The example media is stored outside Git, so a fresh checkout omits the example and still shows the upload form.

| Choice | How | Correction |
| --- | --- | --- |
| Interval | Earliest valid 3 to 5 s in the first 10 s, preferring the full leading interval over shorter shot alternatives | Pick any 3 to 5 s starting within the first minute |
| Subjects | Pinned YOLOX-Nano on the normalized reference and first driving frame; a confident dominant human or animal with an unambiguous point | Tap the image or press a candidate button |
| Description | Written for each photo and clip by Gemini 2.5 Flash on Replicate (`CaptionModel=google/gemini-2.5-flash`) from the photo and 8 stills, in the shape of the accepted fixture prompts; identical inputs reuse the stored answer for 7 days; any failure, a used-up budget or an unclear action falls back to the generic `FIXED_PROMPT`, which keeps the motion much worse | Edit it |
| Tier | `simple-motion` when the detector and the model both see a person in everyday motion, otherwise `complex-motion` | Either tier |
| Seed | 42 | Fixed |

Detector labels are hints, not requirements: the snow leopard in the owner's test set is detected as a dog and still proposed correctly.
The engine still owns segmentation, tracking and mask checks; passing CPU preparation doesn't promise a good video.

## Architecture

| Part | Where | Role |
| --- | --- | --- |
| `infra/cloudflare/demo-proxy` | Cloudflare Worker on the demo domain | Forwards every request to the API function's URL with a proxy key and the visitor's address |
| `gremlin_web.app` | API Lambda behind a public Function URL, or the local server | Pages and the `/api/v1` JSON API |
| `gremlin_web.visitors` | Both | Anonymous visitor cookies, quotas, the description budget |
| `gremlin_web.service` | Worker Lambda (one-minute schedule plus async invokes), or a thread locally | Advances the active generation, expires pending choices |
| `gremlin_web.generations` | Both | Draft, prepare, review, confirm, call Gremlin, track, deliver |
| `gremlin_web.normalize` | Worker | Phone media into Gremlin's inputs (orientation, size, frame rate) and the automatic interval |
| `gremlin_web.selection` | Worker | The CPU detector and the automatic choices |
| `gremlin_web.assist` | Worker | Per-input scene prompts and the public-figure flag, one Replicate call |
| DynamoDB `gremlin-<env>-web-state` | | Drafts and generations per visitor, the active slot, pending choices, counters |
| S3 `gremlin-<env>-web-media-<account>` | | Raw uploads `u/`, normalized inputs and tap pictures `n/`, delivered results `r/` |

Media never passes through the API function: the browser uploads to and plays from the bucket through presigned links bound to size and type.
The API function carries no media or detector libraries; only the worker package gets them (`web/Makefile`, dependency group `worker`) plus the checksum-pinned detector weights, fetched at build by `devtools.models` and never committed.
Every step is idempotent and resumable: polling, the invoke after submit and the sweep can all advance a generation, and a claim that outlasts the worker's timeout stops expensive work happening twice.
One generation runs at a time across the whole site, matching Gremlin's one active job per client.

The progress page shows only recorded times, never estimates.
The site reads Gremlin every 20 s while queued and every 10 s once started, so a short stage can be missed; the page then places the video by the furthest stage the records prove.

## Visitors, privacy, safety

The first request sets a random 256-bit HttpOnly cookie (30 days); its SHA-256 is the visitor ID that keys that browser's drafts and videos.
Finished videos and posters require the creator’s visitor cookie; there is no public result listing.
The preview’s Close button and Escape wait for deletion before dismissing it; a failed delete stays visible and can be retried.
Navigation, reload and tab closure send a keepalive delete request.
If a crash or lost connection prevents delivery, the retention deadline and minute sweep clean up the private media.
Requests that change anything must carry the site's own `Origin`, and the site answers only to its own host name.
The Gremlin token stays on the server; operator actions go through IAM to the worker function and have no web route.

There is no consent checkbox; generating accepts the Terms (only media you may use, only people who agreed, no public figures, no children, nothing sexual, hateful or deceptive).
Gremlin's own moderation checks every job when enabled, and every MP4 carries an "AI-generated" notice in its metadata.
The description call also answers `public_figure` (a recognized face, or professional footage of a public event such as a televised match); a flagged video ends as `public_figure` before any GPU work and gives its video back.
It is the model's judgement, not face recognition, and it misses some; when the call fails the video goes ahead with the fixed description and no screening.

## Quotas and cost

| Limit | Default |
| --- | --- |
| Videos, shared by everyone (`TotalGenerations`) | 10, first come first served, until `web.sh <env> replenish` |
| Uploads per network address per hour | 12 |
| Videos per network address per UTC day (`AddressDailyVideos`) | 3, given back when no GPU was used |
| Upload size | Photo 15 MB, clip 100 MB, signed into each link |
| Videos at once, whole site | 1 |
| GPU spending | Gremlin's own limits; the site can't raise them |
| Scene descriptions | `CaptionDailyMicrousd`, `CaptionMonthlyMicrousd`, 0 by default (fixed description); about USD 0.0017 a call, 6,200 micro-USD reserved per call |

One video is reserved in the same DynamoDB transaction that commits the generation to the GPU, so concurrent requests can't exceed the allowance.
A generation that ends without a GPU having been used gives its video back; once Gremlin reports a worker on the job, it doesn't.
Idle, the site costs about nothing: the sweep stays in the Lambda free tier and the domain's Worker is on Cloudflare's free plan.
A delivered video costs about USD 0.045 of GPU on a warm worker, 0.11 to 0.35 on a cold one, plus well under a cent of AWS.
Reserved concurrency (5 for the API, 4 for the worker) bounds what a request flood can cost.

## Local use

Local use requires Docker and `uv`.
The browser tests also need Google Chrome.

```sh
./web/dev.sh                        # http://127.0.0.1:8080 on Gremlin's simulated worker, quotas lifted
./web/dev.sh --simulate reject      # every job fails on the simulated GPU
./web/dev.sh --gremlin-url https://<gremlin api> --credential infra/aws/.state/prod-client-gremlin-web-local.json
```

The default starts the site's own DynamoDB Local and MinIO (`web/compose.yaml`, ports 8002 and 9102), a local Gremlin with its simulated worker, and the site in fixed-description mode.
First startup fetches and verifies the pinned CPU detector weights.
History survives restarts; `docker compose -f web/compose.yaml down -v` erases it.
If you have the original example artifacts, run `web/devtools/prepare_showcase.sh` before starting the local site to package them.

`cd web && uv run --frozen pytest` runs detector regressions, HTTP journeys, recovery tests and real Chrome through Playwright.
Cases that use private test media skip when their inputs are absent.

## Deployment

The stack is `infra/aws/web/template.yaml`, deployed by `infra/aws/scripts/web.sh` through the same deployer identity as the API.
The bundled deploy command also prepares the hosted demo's example and requires the original example artifacts.
The custom domain is a Cloudflare Worker ([its README](../infra/cloudflare/demo-proxy/README.md)), not CloudFront, so it adds no AWS resource.
With `PublicOrigin` set to the domain, the Function URL redirects pages there and refuses API calls (421) that skip it; the Worker proves itself with an HMAC of the site's Gremlin token kept only as a Worker secret.

Once per environment:

1. `stack.sh <env> client gremlin-web` mints the site's Gremlin credential; deploy the API so the hash takes effect.
2. The owner stores the token as `/gremlin/<env>/web/gremlin-token` with `infra/aws/admin/put-secret.sh`.
3. For model-written descriptions, the owner stores a dedicated Replicate token as `/gremlin/<env>/web/replicate-token` with `put-secret.sh`, then sets `CaptionModel=google/gemini-2.5-flash` and the two caption caps; deploying with the model set and no parameter stops both functions at start.

Then:

```sh
infra/aws/scripts/web.sh prod params
infra/aws/scripts/web.sh prod set GremlinBaseUrl=https://<gremlin api>
infra/aws/scripts/web.sh prod deploy
infra/aws/scripts/web.sh prod smoke
```

| Task | Command |
| --- | --- |
| Admission, the active generation, videos left | `web.sh <env> state` |
| Recent logs | `web.sh <env> logs [minutes]` |
| Change the allowance or retention | `web.sh <env> set TotalGenerations=10 RetentionHours=6`, then `deploy` |
| Give the whole allowance back | `web.sh <env> replenish` |
| Take one result down now | `web.sh <env> remove-result <id>` |
| Stop new videos | `stack.sh prod close` on Gremlin, or `TotalGenerations=0` and deploy |

Logs are JSON events without files, points or descriptions, kept 7 days.
The worker package must stay under Lambda's 250 MiB unzipped; `web/Makefile` drops onnxruntime's unused parts to keep it there.

## Data

| Data | Kept |
| --- | --- |
| Raw and normalized uploads, tap pictures | Until Gremlin has its own copies, or the video ends, or a pending choice expires (1 hour); bucket rules clean leftovers after 1 and 2 days |
| Photo and stills sent for a description | One Replicate call (Gemini 2.5 Flash); not stored here, kept by Replicate and Google under their terms; the answer is cached 7 days by input hash |
| The description | In Gremlin's job record, 30 days |
| Unsubmitted drafts | Swept after 1 hour |
| Delivered video and poster | Private; deleted when the preview closes, with `RetentionHours` (6, at most 24) as the unattended/crash backstop |
| Upload counters per address | Up to 2 hours |
| Logs | 7 days |
| Gremlin's copies | At most 24 hours after delivery (`/saved`); closing the preview cancels the job to revoke access and schedule earlier cleanup |
