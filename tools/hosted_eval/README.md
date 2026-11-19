# Hosted architecture evaluation

This package evaluated the hosted route described in [ADR 0002](../../docs/adr/0002-hosted-generation.md).
The motion-fidelity evaluation in `tools/motion_eval/` reuses its ledger, budget guard, provider clients and fixture preparation.
It has no production integration and never provisions compute.
Run from the repository root with `PYTHONPATH=worker uv run python -m tools.hosted_eval COMMAND --out artifacts/hosted-eval-2026-10-29`.

The operator supplies `REPLICATE_API_TOKEN` and `FAL_KEY` in the process environment.
The tool does not load `.env`.
The owner's existing `FAL_API_KEY` may be mapped to `FAL_KEY` in the launching process.
Never put keys in command arguments, records or chat.

Commands, in order:

1. `prepare` verifies sources, copies preserved baselines, prepares media and assigns blind letters.
2. `stills --simulate` and `videos --simulate` record simulated inputs without network access or fabricated model outputs.
3. `preflight` checks every planned body against authenticated live schemas without inference.
4. `approve --max-usd 2.00` records the owner's authorized campaign ceiling.
5. `stills` stops after the first successful editor call for its billing probe.
6. `probe --model E1 --probe-confirmed AMOUNT --evidence PATH` records the posted charge from a nonempty evidence file inside the run directory.
7. Resume `stills`, then `videos`, confirming the V1, V2 and V3 probes in the same way.
8. `measure`, `settle --replicate CSV --fal CSV`, `gallery` and `report` preserve measurements and build the review package.
9. The owner scores, exports and reveals each fixture in the gallery.
10. `repeats --scores PATH` permits seed 7 only for owner-scored realism of at least 3 in a revealed fixture, then rebuild the gallery and report.

A missing probe or a difference above 10% holds the model.
Only an explicit owner decision permits `probe --accept-deviation`; the mismatch remains in the ledger.
Settlement CSVs require `cell,request_id,posted_usd`; keep the original provider export beside any transcription.
An empty export never establishes zero cost.
Unknown submissions are never re-posted: resume by provider ID if the accepted record exists, otherwise reconcile the provider history before proceeding.
Explicit 429 or 5xx responses and terminal provider failures get at most one retry with a fresh intent and reservation.
Two consecutive provider failures stop the campaign; still retries additionally have a campaign limit of two.
All reservations remain in the conservative total, including failed calls; posted costs can raise that total.
The budget uses decimal arithmetic and an exclusive command lock.

Raw outputs, redacted inputs, file hashes, provider responses, schemas and timings remain in the ignored run directory.
Review derivatives never replace original media.
The gallery opens from `file://`, stores scores in the browser, and loads its key only on the owner's reveal action.
The key is a blind-review aid, not a security boundary against inspecting files.
No automated metric or agent score selects an architecture.

On 29 October the owner approved omitting unsupported V1 `negative_prompt` and `optimize_prompt`, and using 81 frames for the three Kung Fu Wan cells because both endpoints require at least 81.
Review copies still contain exactly 55 Kung Fu frames or 80 Enzo frames at 16 fps.
All nine videos cost USD 0.2275 at the plan's list prices; with four USD 0.03 still reservations the initial matrix is USD 0.3475.
The plan's USD 0.2025 video subtotal excludes optional EN-K2.

Offline checks: `uv run pytest tools/hosted_eval/tests -q` and `./tools/check.sh`.
These are codec, transport and simulated-provider checks, not genuine inference evidence.
