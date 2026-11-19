#!/usr/bin/env bash
# The public website stack, every step through gremlin-dev -> gremlin-ctl-deployer.
# Usage: infra/aws/scripts/web.sh <staging|prod> <command>
#   params | set KEY=VALUE... | deploy | smoke | state | replenish | remove-result <id> | logs [minutes]
# docs/web.md is the runbook.
set -euo pipefail
GREMLIN_ENV="${1:-}"
shift || true
export GREMLIN_ENV GREMLIN_STACK_KIND=web
source "$(dirname "$0")/lib.sh"

readonly PARAMS="$GREMLIN_STATE/${GREMLIN_ENV}-web-params.json"
readonly SETTABLE="PublicOrigin GremlinBaseUrl TotalGenerations AddressHourlyDrafts AddressDailyVideos RetentionHours CaptionModel CaptionDailyMicrousd CaptionMonthlyMicrousd LogRetentionDays AlertEmail"
# Before the first deployment of a stack without a custom domain, its own URL is unknown.
readonly PENDING_ORIGIN="https://pending.invalid"

params() {
  umask 077
  mkdir -p "$GREMLIN_STATE"
  python3 - "$PARAMS" <<'PY'
import json, os, sys
path = sys.argv[1]
params = {"PublicOrigin": "", "GremlinBaseUrl": ""}
if os.path.exists(path):
    params |= json.load(open(path))
json.dump(params, open(path, "w"), indent=1)
print(json.dumps(params))
PY
  echo "wrote $PARAMS"
}

set_params() {
  [[ -f "$PARAMS" ]] || die "run params first"
  python3 - "$PARAMS" "$SETTABLE" "$@" <<'PY'
import json, sys
path, settable, *pairs = sys.argv[1:]
allowed = set(settable.split())
params = json.load(open(path))
for pair in pairs:
    key, _, value = pair.partition("=")
    if key not in allowed:
        sys.exit(f"error: {key} is not a settable parameter")
    params[key] = value
json.dump(params, open(path, "w"), indent=1)
print(json.dumps(params))
PY
}

worker() {
  local out failure result=0
  mkdir -p "$GREMLIN_STATE"
  out=$(umask 077 && mktemp "$GREMLIN_STATE/.invoke.XXXXXX")
  if ! failure=$(aws lambda invoke --function-name "${GREMLIN_STACK}-worker" \
    --cli-binary-format raw-in-base64-out --payload "$1" "$out" \
    --query FunctionError --output text); then
    rm -f "$out"
    return 1
  fi
  if [[ "$failure" != None ]]; then
    echo "error: worker Lambda failed; inspect the logs" >&2
    result=1
  else
    python3 -c 'import json, sys; r = json.load(open(sys.argv[1])); print(json.dumps(r)); sys.exit(isinstance(r, dict) and "error" in r)' "$out" || result=$?
  fi
  rm -f "$out"
  return "$result"
}

sam_deploy() {
  local build="$1" origin="$2" commit="$3"
  local overrides="$GREMLIN_STATE/${GREMLIN_ENV}-web-deploy-params.yaml"  # JSON content
  (umask 077 && python3 -c 'import json, sys; p = {k: v for k, v in json.load(open(sys.argv[1])).items() if k in sys.argv[6].split()}; p |= {"Environment": sys.argv[2], "BuildCommit": sys.argv[3], "PublicOrigin": sys.argv[4]}; json.dump(p, open(sys.argv[5], "w"))' \
    "$PARAMS" "$GREMLIN_ENV" "$commit" "$origin" "$overrides" "$SETTABLE")
  (cd "$REPO_ROOT" && sam deploy -t "$build/template.yaml" \
    --stack-name "$GREMLIN_STACK" --region "$GREMLIN_REGION" \
    --s3-bucket "$GREMLIN_ARTIFACT_BUCKET" --s3-prefix "$GREMLIN_STACK" \
    --role-arn "$GREMLIN_CFN_ROLE" --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --tags project=gremlin env="$GREMLIN_ENV" \
    --parameter-overrides "file://$overrides" \
    --no-fail-on-empty-changeset --no-confirm-changeset)
}

deploy() {
  [[ -f "$PARAMS" ]] || die "run params first"
  git -C "$REPO_ROOT" diff --quiet HEAD -- web infra/aws/web \
    || die "commit web/ and infra/aws/web/ before deploying"
  # The application refuses to start without it; fail here instead of at the first request.
  python3 -c 'import json, sys; json.load(open(sys.argv[1])).get("GremlinBaseUrl") or sys.exit("error: set GremlinBaseUrl first (docs/web.md)")' "$PARAMS"
  local commit build="$REPO_ROOT/artifacts/sam-build-web-$GREMLIN_ENV" origin
  commit=$(git -C "$REPO_ROOT" rev-parse HEAD)
  "$REPO_ROOT/web/devtools/prepare_showcase.sh"
  (cd "$REPO_ROOT" && sam validate --lint -t infra/aws/web/template.yaml)
  (cd "$REPO_ROOT" && sam build -t infra/aws/web/template.yaml --build-dir "$build")
  gremlin_assume
  origin=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["PublicOrigin"])' "$PARAMS")
  sam_deploy "$build" "${origin:-$PENDING_ORIGIN}" "$commit"
  if [[ -z "$origin" ]]; then
    # No custom domain: the site's origin is its Function URL, known only now.
    origin=$(stack_output SiteUrl)
    origin="${origin%/}"
    set_params "PublicOrigin=$origin" >/dev/null
    sam_deploy "$build" "$origin" "$commit"
  fi
  echo "SiteUrl: $(stack_output SiteUrl)"
}

smoke() {
  gremlin_assume
  local url
  # The public origin: the custom domain when one is set, otherwise the Function URL.
  url="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["PublicOrigin"])' "$PARAMS")/"
  curl -fsS "${url}health"; echo
  curl -fsS -o /dev/null -w "home %{http_code}\n" "$url"
  worker '{"web_operator": "state"}'
}

state() { gremlin_assume; worker '{"web_operator": "state"}'; }

# The shared allowance starts again; uploads and budgets stay.
replenish() { gremlin_assume; worker '{"web_operator": "replenish"}'; }

# Takes one result down from the gallery and from storage at once.
remove_result() {
  [[ "${1:-}" =~ ^[0-9a-f]{32}$ ]] || die "usage: remove-result <32-hex result id>"
  gremlin_assume
  worker "{\"web_operator\": \"remove_result\", \"gid\": \"$1\"}"
}

logs() {
  gremlin_assume
  local since=$(( ($(date +%s) - ${1:-60} * 60) * 1000 ))
  for fn in api worker; do
    echo "== $fn"
    aws logs filter-log-events --log-group-name "/aws/lambda/${GREMLIN_STACK}-$fn" --start-time "$since" \
      --query 'events[].message' --output text | tr '\t' '\n'
  done
}

command="${1:-}"
shift || true
case "$command" in
  params) params ;;
  set) set_params "$@" ;;
  deploy) deploy ;;
  smoke) smoke ;;
  state) state ;;
  replenish) replenish ;;
  remove-result) remove_result "$@" ;;
  logs) logs "${1:-60}" ;;
  *) die "usage: $0 <staging|prod> <params|set KEY=VALUE...|deploy|smoke|state|replenish|remove-result <id>|logs [minutes]>" ;;
esac
