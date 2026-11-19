# AWS

The control plane (`admin/`), the API stack (`stack/`), the website stack (`web/`) and the operator scripts (`scripts/`).
[Operations](../../docs/operations.md) and [the web doc](../../docs/web.md) are the runbooks; this explains what the scripts assume.

## Trust chain

```
gremlin-dev (IAM user, no permissions of its own)
  -> sts:AssumeRole only
gremlin-ctl-deployer (1 h session, eu-north-1 only, gremlin-staging-* / gremlin-prod-*, no table or media reads)
  -> hands CloudFormation exactly one role
gremlin-ctl-cfn-exec (creates resources; every role it creates must carry the boundary)
  -> gremlin-ctl-workload-boundary (data-plane ceiling for the function roles)
```

The scripts assume the role per run, keep credentials in process memory, clear ambient `AWS_*` variables and check account, user, role and region before any call.
They read the account ID from `GREMLIN_ACCOUNT` (or `infra/aws/.state/account`), which stays out of Git.
`.state/` is ignored and holds each environment's client hashes, minted credentials (mode 0600) and non-secret stack parameters.

## Files

| File | Run by | Purpose |
| --- | --- | --- |
| `admin/preflight.sh` | The owner, admin profile | Read-only inventory before the bootstrap |
| `admin/bootstrap.yaml` | The owner, admin profile | The control plane above and the SAM artifact bucket; optional boundary statements for Bedrock and Rekognition |
| `admin/moderation.yaml` | The owner, eu-west-1 | The Bedrock guardrail and its numbered version |
| `admin/put-secret.sh` | The owner, admin profile | Stores a secret as an SSM SecureString from stdin: `/gremlin/<env>/runpod-key` and `/gremlin/<env>/web/gremlin-token` |
| `stack/template.yaml` | `stack.sh <env> deploy` | One API per environment: two functions, table, bucket, schedule, log groups; prod retains and protects its table and bucket |
| `web/template.yaml` | `web.sh <env> deploy` | The website: two functions, table, bucket, schedule |
| `scripts/lib.sh`, `stack.sh`, `web.sh` | You, through the deployer | Identity guard, clients, deploy, state, GPU planning, close, alarms, logs, staging teardown |

## What each identity can do

| Identity | Can | Cannot |
| --- | --- | --- |
| `gremlin-dev` | Assume the deployer | Anything else |
| `gremlin-ctl-deployer` | Change sets for `gremlin-staging-*` and `gremlin-prod-*`, artifact uploads, invoke and inspect their functions, read their logs, empty staging buckets | Read table items or media objects, delete production tables or buckets, touch IAM, other regions, other stacks, billing |
| `gremlin-ctl-cfn-exec` | Manage the stacks' resources and boundary-carrying roles | Read or write media, delete production data, create a role without the boundary |
| Function roles | Their environment's table, bucket, `/gremlin/<env>/*` parameters, logs and functions; optionally the exact guardrail, Rekognition and Bedrock actions the owner enables | Anything outside the workload boundary |

The media bucket's policy additionally denies object reads and writes to every principal except the two function roles.

## Cost safeguards

GPU spend is not an AWS charge; it is bounded by the stack's spending parameters, which default to zero with the controller off.
The table has an on-demand ceiling of 100 read and 100 write units per second, logs keep 7 days, each Lambda has a small reserved concurrency, and there is no gateway, VPC, NAT, queue or provisioned capacity.
Account-wide budgets notify; `stack.sh prod close` is the stop.
