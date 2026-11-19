---
status: accepted
date: 2026-10-22
---

# Run the control API on AWS Lambda, DynamoDB and S3

## Context

The original control plane ran FastAPI, SQLite and temporary media on a Railway service that slept when idle.
A poller woke every 15 seconds to supervise RunPod sessions.
The project needed low idle cost and predictable job recovery without keeping a server or GPU warm.

## Decision

Run two Python functions in eu-north-1.
An API function answers a Lambda Function URL, and a controller function is invoked by API doorbells, RunPod completion webhooks and a one-minute EventBridge sweep.
The doorbells reduce delay, and the sweep recovers missed invocations by reading the same durable state.
The webhook rings the controller, which settles from authenticated provider status.

DynamoDB holds the job, replay and budget ledgers.
Conditional transactions preserve one active job per client, one live GPU session, idempotent admission and settlement, and owner-scoped reads.
Budget reservations and dispatch holds are checked in the same transaction that admits a session.

Clients and workers transfer large inputs and results through short-lived S3 URLs, since buffered Function URLs have a 6 MB payload limit.
Upload links bind the declared size and SHA-256.
A private bucket holds the media, and the retention sweep deletes eligible objects before pruning their records.

Stockholm keeps storage in the EU and shortens the round trip to the GPU workers used during qualification.

## Consequences

The control plane scales to zero between requests and scheduled sweeps.
An expired lease is fenced on the next worker request, even if the controller learns about it on the next sweep.
The migration preserved job and spend history while retiring the Railway deployment and its media transport.

[Operations](../operations.md) covers recovery and deployment.
[Privacy](../privacy.md) covers data flow and retention.
[ADR 0004](0004-return-to-runpod-scail2.md) records why the GPU worker stayed on RunPod.
