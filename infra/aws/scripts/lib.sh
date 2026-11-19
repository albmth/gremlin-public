#!/usr/bin/env bash
# Shared guard for every Gremlin AWS script. Set GREMLIN_ENV, source it, then call gremlin_assume.
# Credentials live only in this process's environment; nothing is written to disk.
set -euo pipefail

# The account ID stays out of Git: GREMLIN_ACCOUNT in the environment, or infra/aws/.state/account.
GREMLIN_ACCOUNT="${GREMLIN_ACCOUNT:-$(cat "$(dirname "${BASH_SOURCE[0]}")/../.state/account" 2>/dev/null || true)}"
[[ "$GREMLIN_ACCOUNT" =~ ^[0-9]{12}$ ]] || { echo "error: set GREMLIN_ACCOUNT or write the account ID to infra/aws/.state/account" >&2; exit 1; }
readonly GREMLIN_ACCOUNT
readonly GREMLIN_REGION=eu-north-1
readonly GREMLIN_SOURCE_PROFILE=gremlin-dev
readonly GREMLIN_DEPLOYER_ROLE="arn:aws:iam::${GREMLIN_ACCOUNT}:role/gremlin-ctl-deployer"
readonly GREMLIN_CFN_ROLE="arn:aws:iam::${GREMLIN_ACCOUNT}:role/gremlin-ctl-cfn-exec"
readonly GREMLIN_ARTIFACT_BUCKET="gremlin-ctl-artifacts-${GREMLIN_ACCOUNT}-eun1"
# Every caller names its environment; staging is disposable, prod holds the ledger of record.
case "${GREMLIN_ENV:-}" in
  staging | prod) ;;
  *) echo "error: set GREMLIN_ENV to staging or prod" >&2; exit 1 ;;
esac
readonly GREMLIN_ENV
# `control` is the generation API; `web` is the customer website (web.sh sets it).
readonly GREMLIN_STACK="gremlin-${GREMLIN_ENV}-${GREMLIN_STACK_KIND:-control}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly REPO_ROOT
readonly GREMLIN_STATE="$REPO_ROOT/infra/aws/.state"

die() { echo "error: $*" >&2; exit 1; }

gremlin_assume() {
  # Ambient credentials must never be used, even by accident.
  unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
  export AWS_REGION="$GREMLIN_REGION" AWS_DEFAULT_REGION="$GREMLIN_REGION" AWS_PAGER=""

  local who
  who=$(aws sts get-caller-identity --profile "$GREMLIN_SOURCE_PROFILE" --query Arn --output text)
  [[ "$who" == "arn:aws:iam::${GREMLIN_ACCOUNT}:user/gremlin-dev" ]] \
    || die "profile $GREMLIN_SOURCE_PROFILE resolved to '$who', expected the gremlin-dev IAM user"

  local creds
  creds=$(aws sts assume-role --profile "$GREMLIN_SOURCE_PROFILE" \
    --role-arn "$GREMLIN_DEPLOYER_ROLE" \
    --role-session-name "gremlin-agent-$(date +%s)" \
    --duration-seconds 3600 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
  read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN <<<"$creds"
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

  who=$(aws sts get-caller-identity --query Arn --output text)
  [[ "$who" == "arn:aws:sts::${GREMLIN_ACCOUNT}:assumed-role/gremlin-ctl-deployer/"* ]] \
    || die "assumed identity is '$who', expected gremlin-ctl-deployer"
  echo "identity: $who ($GREMLIN_REGION)"
}

stack_output() {
  aws cloudformation describe-stacks --stack-name "$GREMLIN_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# Invoke the controller with a JSON event and print its JSON answer.
controller() {
  local out failure result=0
  mkdir -p "$GREMLIN_STATE"
  out=$(umask 077 && mktemp "$GREMLIN_STATE/.invoke.XXXXXX")
  if ! failure=$(aws lambda invoke --function-name "${GREMLIN_STACK}-controller" \
    --cli-binary-format raw-in-base64-out --payload "$1" "$out" \
    --query FunctionError --output text); then
    rm -f "$out"
    return 1
  fi
  if [[ "$failure" != None ]]; then
    echo "error: controller Lambda failed; inspect sanitized operational logs" >&2
    result=1
  else
    python3 - "$out" <<'PY' || result=$?
import json, sys
report = json.load(open(sys.argv[1]))
print(json.dumps(report))
if isinstance(report, dict) and "error" in report:
    sys.exit(1)
PY
  fi
  rm -f "$out"
  return "$result"
}
