#!/usr/bin/env bash
# A Gremlin workload stack, every step through gremlin-dev -> gremlin-ctl-deployer.
# Usage: infra/aws/scripts/stack.sh <staging|prod> <command>
#   params | client ID [--delegated] | set KEY=VALUE... | deploy | smoke | contract | state | gpu-plan [--trial] | release ID | close | alarms | test-alert | inspect | logs | teardown | verify-gone
# New parameter files start with zero spending authority and the controller off.
set -euo pipefail
GREMLIN_ENV="${1:-}"
shift || true
export GREMLIN_ENV
source "$(dirname "$0")/lib.sh"

readonly PARAMS="$GREMLIN_STATE/${GREMLIN_ENV}-params.json"
# Client identities of this environment only: {"hashes": {id: sha256}, "delegated": [id]}.
# Hashes only; each bearer token stays in its own 0600 credential file.
readonly CLIENTS="$GREMLIN_STATE/${GREMLIN_ENV}-clients.json"
readonly STAGING_CLIENT="$GREMLIN_STATE/staging-client.json"
# Seed for a new production client file. Client IDs are never renamed: replay digests
# and job ownership include them.
readonly LEGACY_HASHES="$REPO_ROOT/artifacts/railway/client-token-hashes.json"

params() {
  umask 077
  mkdir -p "$GREMLIN_STATE"
  if [[ ! -f "$CLIENTS" ]]; then
    # Seed from the environment's deployed parameters, so existing identities carry over.
    if [[ ! -f "$PARAMS" && "$GREMLIN_ENV" == prod ]]; then
      [[ -f "$LEGACY_HASHES" ]] || die "missing $LEGACY_HASHES"
    fi
    if [[ ! -f "$PARAMS" && "$GREMLIN_ENV" == staging && ! -f "$STAGING_CLIENT" ]]; then
      (cd "$REPO_ROOT" && uv run python tools/client_token.py --client contract-staging --output "$STAGING_CLIENT") >/dev/null
    fi
    python3 - "$GREMLIN_ENV" "$PARAMS" "$LEGACY_HASHES" "$STAGING_CLIENT" "$CLIENTS" <<'PY'
import hashlib, json, os, sys
env, params_path, legacy, staging_client, clients_path = sys.argv[1:]
if os.path.exists(params_path):
    params = json.load(open(params_path))
    hashes = json.loads(params.get("ClientTokenHashes", "{}"))
    delegated = json.loads(params.get("DelegatedClients", "[]"))
elif env == "prod":
    hashes, delegated = json.load(open(legacy)), []
else:
    token = json.load(open(staging_client))["token"]
    hashes, delegated = {"contract-staging": hashlib.sha256(token.encode()).hexdigest()}, []
json.dump({"hashes": hashes, "delegated": delegated}, open(clients_path, "w"), indent=1)
PY
  fi
  python3 - "$GREMLIN_ENV" "$CLIENTS" "$PARAMS" <<'PY'
import json, os, sys
env, clients_path, params_path = sys.argv[1:]
clients = json.load(open(clients_path))
params = {"ControllerEnabled": "false", "HourlyMicrousd": "0", "BudgetMicrousd": "0",
          "SweepEnabled": "false" if env == "prod" else "true",
          "GpuSelectionEnabled": "true", "GpuQueueTimeoutSeconds": "660", "GpuQualificationEnabled": "false",
          "GpuQualificationTarget": "",
          "EndpointId": "", "DailyJobs": "8" if env == "staging" else "0"}
if os.path.exists(params_path):
    params |= json.load(open(params_path))
params["ClientTokenHashes"] = json.dumps(clients["hashes"], separators=(",", ":"), sort_keys=True)
params["DelegatedClients"] = json.dumps(sorted(clients["delegated"]), separators=(",", ":"))
json.dump(params, open(params_path, "w"), indent=1)
print("clients:", ", ".join(f"{c} (delegated)" if c in clients["delegated"] else c
                            for c in sorted(clients["hashes"])))
PY
  echo "wrote $PARAMS (hashes only)"
}

# Mint a new client identity for this environment only; deploy to make it effective.
add_client() {
  local id="${1:?client id}" delegated="${2:-}"
  [[ "$id" =~ ^[A-Za-z0-9_-]{1,100}$ ]] || die "client IDs use letters, digits, _ and -"
  [[ -z "$delegated" || "$delegated" == --delegated ]] || die "usage: client ID [--delegated]"
  params >/dev/null
  local credential="$GREMLIN_STATE/${GREMLIN_ENV}-client-${id}.json"
  python3 -c 'import json, sys; sys.exit(sys.argv[2] in json.load(open(sys.argv[1]))["hashes"])' \
    "$CLIENTS" "$id" || die "client $id already exists in $GREMLIN_ENV; rotate deliberately"
  (cd "$REPO_ROOT" && uv run python tools/client_token.py --client "$id" --output "$credential") >/dev/null
  python3 - "$CLIENTS" "$id" "$credential" "$delegated" <<'PY'
import hashlib, json, sys
path, client, credential, delegated = sys.argv[1:]
clients = json.load(open(path))
token = json.load(open(credential))["token"]
clients["hashes"][client] = hashlib.sha256(token.encode()).hexdigest()
if delegated:
    clients["delegated"] = sorted(set(clients["delegated"]) | {client})
json.dump(clients, open(path, "w"), indent=1)
PY
  params
  echo "credential: $credential (mode 0600); deliver it privately, then deploy"
}

set_params() {
  [[ -f "$PARAMS" ]] || die "run params first"
  python3 - "$PARAMS" "$@" <<'PY'
import json, sys
path, *pairs = sys.argv[1:]
allowed = {"ControllerEnabled", "HourlyMicrousd", "BudgetMicrousd", "RollingBudgetMicrousd", "MonthlyBudgetMicrousd", "EndpointId", "DailyJobs", "ImprintEnabled",
           "GpuSelectionEnabled", "GpuQueueTimeoutSeconds", "GpuQualificationEnabled", "GpuQualificationTarget",
           "AlertEmail", "SweepEnabled", "ModerationEnabled", "ModerationGuardrailId", "ModerationGuardrailVersion"}
params = json.load(open(path))
for pair in pairs:
    key, _, value = pair.partition("=")
    if key not in allowed:
        sys.exit(f"error: {key} is not a settable parameter")
    params[key] = value
json.dump(params, open(path, "w"), indent=1)
print(json.dumps({k: v for k, v in params.items() if k != "ClientTokenHashes"}))
PY
}

deploy() {
  [[ -f "$PARAMS" ]] || die "run params first"
  git -C "$REPO_ROOT" diff --quiet HEAD -- control infra/aws/stack \
    || die "commit control/ and infra/aws/stack/ before deploying"
  local commit build="$REPO_ROOT/artifacts/sam-build-$GREMLIN_ENV"
  commit=$(git -C "$REPO_ROOT" rev-parse HEAD)
  (cd "$REPO_ROOT" && sam validate --lint -t infra/aws/stack/template.yaml)
  (cd "$REPO_ROOT" && sam build -t infra/aws/stack/template.yaml --build-dir "$build")
  gremlin_assume
  # A parameter file, not KEY=VALUE words: SAM rejects an empty value on the command line, and an
  # omitted key would silently keep the stack's previous value (for example an old EndpointId).
  local overrides="$GREMLIN_STATE/${GREMLIN_ENV}-deploy-params.yaml"  # JSON content; SAM reads only YAML or TOML files
  (umask 077 && python3 -c 'import json, sys; p = json.load(open(sys.argv[1])); p |= {"Environment": sys.argv[2], "BuildCommit": sys.argv[3]}; json.dump(p, open(sys.argv[4], "w"))' \
    "$PARAMS" "$GREMLIN_ENV" "$commit" "$overrides")
  (cd "$REPO_ROOT" && sam deploy -t "$build/template.yaml" \
    --stack-name "$GREMLIN_STACK" --region "$GREMLIN_REGION" \
    --s3-bucket "$GREMLIN_ARTIFACT_BUCKET" --s3-prefix "$GREMLIN_STACK" \
    --role-arn "$GREMLIN_CFN_ROLE" --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --tags project=gremlin env="$GREMLIN_ENV" \
    --parameter-overrides "file://$overrides" \
    --no-fail-on-empty-changeset --no-confirm-changeset)
  echo "ApiUrl: $(stack_output ApiUrl)"
}

smoke() {
  gremlin_assume
  local url
  url=$(stack_output ApiUrl)
  curl -fsS "${url}health"; echo
  controller '{"gremlin_operator": "state"}'
}

contract() {
  # The hook-free subset creates and cancels jobs, so it never runs against production.
  [[ "$GREMLIN_ENV" == staging ]] || die "the contract subset runs only against staging"
  gremlin_assume
  # This suite submits a valid job to check closed admission. Refuse before it can queue
  # work under an accidentally open campaign budget, even when dispatch is disabled.
  controller '{"gremlin_operator": "state"}' | python3 -c 'import json, sys
state = json.load(sys.stdin)
if (state["controller_enabled"] or state["hourly_microusd"] != 0
        or state["budget_microusd"] != 0 or state["live_session"] is not None):
    sys.exit("error: hosted contract requires dispatch off, zero hourly/budget authority and no live session")'
  local url token
  url=$(stack_output ApiUrl)
  token=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["token"])' "$STAGING_CLIENT")
  (cd "$REPO_ROOT" && CONTRACT_BASE_URL="$url" CONTRACT_CLIENT_TOKEN="$token" \
    uv run pytest contract --target url -q --tb=short)
}

state() { gremlin_assume; controller '{"gremlin_operator": "state"}'; }

close_capacity() {
  gremlin_assume
  controller '{"gremlin_operator":"close_capacity"}' | python3 -c 'import json, sys
report = json.load(sys.stdin)
print(json.dumps(report))
if not report or report.get("closed") is not True:
    sys.exit("error: capacity closure is not confirmed; inspect live state and retry after settlement")'
}

close() {
  set_params ControllerEnabled=false HourlyMicrousd=0 BudgetMicrousd=0 DailyJobs=0 SweepEnabled=true
  deploy
  close_capacity
}

alarms() {
  [[ "$GREMLIN_ENV" == prod ]] || die "production alarms only"
  gremlin_assume
  aws cloudwatch describe-alarms --alarm-names "$GREMLIN_STACK-api-failures" "$GREMLIN_STACK-controller-health" \
    --query 'MetricAlarms[].{name:AlarmName,state:StateValue,reason:StateReason,actions:AlarmActions}'
  aws sns list-subscriptions-by-topic --topic-arn "$(stack_output AlertsTopicArn)" \
    --query 'Subscriptions[].{protocol:Protocol,subscription:SubscriptionArn}'
}

test_alert() {
  [[ "$GREMLIN_ENV" == prod ]] || die "production alarms only"
  gremlin_assume
  aws cloudwatch set-alarm-state --alarm-name "$GREMLIN_STACK-api-failures" \
    --state-value ALARM --state-reason 'Authorized Gremlin notification delivery drill; no API failure or GPU submission.'
  echo "Test alarm set; verify email delivery and automatic recovery to OK."
}

gpu_plan() {
  local trial=false
  if [[ "${1:-}" == --trial ]]; then
    [[ "$GREMLIN_ENV" == staging ]] || die "trial planning is staging-only"
    trial=true
  elif [[ -n "${1:-}" ]]; then
    die "usage: gpu-plan [--trial]"
  fi
  gremlin_assume
  controller "{\"gremlin_operator\":\"gpu_plan\",\"allow_trial\":$trial}"
}

release() {
  local session="${1:?session id}"
  read -rp "Release the dispatch hold of reviewed $GREMLIN_ENV session $session? Type yes: " answer
  [[ "$answer" == yes ]] || die "not released"
  gremlin_assume
  controller "{\"gremlin_operator\": \"release_dispatch_hold\", \"session_id\": \"$session\", \"authorize_recovery\": true}"
}

inspect() {
  gremlin_assume
  aws cloudformation describe-stack-resources --stack-name "$GREMLIN_STACK" \
    --query 'StackResources[].[LogicalResourceId,ResourceType,ResourceStatus]' --output text
  for fn in api controller; do
    # Configuration without environment values.
    aws lambda get-function-configuration --function-name "${GREMLIN_STACK}-$fn" \
      --query '{runtime: Runtime, arch: Architectures, memory: MemorySize, timeout: Timeout, modified: LastModified, sha256: CodeSha256}'
  done
}

# Fixed operational events and platform REPORT lines from the last N minutes (default 60).
logs() {
  gremlin_assume
  local since=$(( ($(date +%s) - ${1:-60} * 60) * 1000 ))
  for fn in api controller; do
    echo "== $fn"
    aws logs filter-log-events --log-group-name "/aws/lambda/${GREMLIN_STACK}-$fn" --start-time "$since" \
      --query 'events[].message' --output json \
      | python3 -c 'import json, re, sys
for m in json.load(sys.stdin):
    try:
        e = json.loads(m)
    except ValueError:
        print(m.rstrip()); continue
    if e.get("type") == "platform.report":
        r = e["record"]; print("REPORT", r.get("metrics"))
    elif "Healthy" in e and "_aws" in e:
        print("HEALTH", json.dumps({key: e.get(key) for key in (
            "Healthy", "reasons", "oldest_queued_seconds", "budget_held_microusd", "budget_microusd")}))
    elif "message" in e:
        message = re.sub(r"(/api/imprint/provider/[^/\s]+/)[^/\s]+", r"\1[REDACTED]", e["message"])
        print(e.get("level"), message)'
  done
}

teardown() {
  [[ "$GREMLIN_ENV" == staging ]] || die "production is never torn down by this script"
  gremlin_assume
  local bucket
  bucket=$(stack_output MediaBucketName)
  if [[ -n "$bucket" && "$bucket" != None ]]; then
    aws s3 rm "s3://$bucket" --recursive --only-show-errors
  fi
  aws cloudformation delete-stack --stack-name "$GREMLIN_STACK" --role-arn "$GREMLIN_CFN_ROLE"
  aws cloudformation wait stack-delete-complete --stack-name "$GREMLIN_STACK"
  echo "deleted $GREMLIN_STACK"
}

verify_gone() {
  gremlin_assume
  local missing=0
  aws cloudformation describe-stacks --stack-name "$GREMLIN_STACK" >/dev/null 2>&1 && { echo "stack remains"; missing=1; }
  for fn in api controller; do
    aws lambda get-function --function-name "${GREMLIN_STACK}-$fn" >/dev/null 2>&1 && { echo "function $fn remains"; missing=1; }
  done
  aws dynamodb describe-table --table-name "${GREMLIN_STACK}-state" >/dev/null 2>&1 && { echo "table remains"; missing=1; }
  aws s3api head-bucket --bucket "${GREMLIN_STACK}-media-${GREMLIN_ACCOUNT}" >/dev/null 2>&1 && { echo "bucket remains"; missing=1; }
  aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/${GREMLIN_STACK}" \
    --query 'logGroups[].logGroupName' --output text | grep . && missing=1
  [[ $missing == 0 ]] && echo "all $GREMLIN_ENV resources are gone"
}

command="${1:-}"
shift || true
case "$command" in
  params) params ;;
  client) add_client "$@" ;;
  set) set_params "$@" ;;
  deploy) deploy ;;
  smoke) smoke ;;
  contract) contract ;;
  state) state ;;
  close) close ;;
  close-capacity) close_capacity ;;
  alarms) alarms ;;
  test-alert) test_alert ;;
  gpu-plan) gpu_plan "$@" ;;
  release) release "${1:-}" ;;
  inspect) inspect ;;
  logs) logs "${1:-60}" ;;
  teardown) teardown ;;
  verify-gone) verify_gone ;;
  *) die "usage: $0 <staging|prod> <params|client ID [--delegated]|set KEY=VALUE...|deploy|smoke|contract|state|gpu-plan [--trial]|release ID|close|close-capacity|alarms|test-alert|inspect|logs [minutes]|teardown|verify-gone>" ;;
esac
