#!/usr/bin/env bash
# The owner stores a Gremlin secret as an SSM SecureString, typed or piped on standard input.
# It never appears in a command line, the shell history, CloudFormation or this repository.
# Usage: AWS_PROFILE=<admin profile> infra/aws/admin/put-secret.sh /gremlin/<staging|prod>/<name>
# Names: runpod-key (control API); web/gremlin-token and web/replicate-token (the website, docs/web.md).
set -euo pipefail
export AWS_PAGER=""
readonly REGION=eu-north-1
ACCOUNT="${GREMLIN_ACCOUNT:-$(cat "$(dirname "$0")/../.state/account" 2>/dev/null || true)}"
[[ "$ACCOUNT" =~ ^[0-9]{12}$ ]] || { echo "set GREMLIN_ACCOUNT or infra/aws/.state/account" >&2; exit 1; }
name="${1:?parameter name}"
[[ "$name" =~ ^/gremlin/(staging|prod)/(runpod-key|web/gremlin-token|web/replicate-token)$ ]] \
  || { echo "unknown parameter name; see the usage comment" >&2; exit 1; }
who=$(aws sts get-caller-identity --query Arn --output text)
[[ "$who" == *":${ACCOUNT}:"* && "$who" != *"user/gremlin-dev" && "$who" != *":root" ]] \
  || { echo "run as the owner's admin identity in the Gremlin account" >&2; exit 1; }
if [[ -t 0 ]]; then
  read -rsp "Value for $name: " key; echo
else
  read -r key
fi
[[ -n "$key" ]] || { echo "empty value" >&2; exit 1; }
# The CLI cannot read --cli-input-json from /dev/stdin, so the request body goes through a
# private temporary file that is removed on exit.
umask 077
state_dir="$(cd "$(dirname "$0")/.." && pwd)/.state"
mkdir -p "$state_dir"
body=$(mktemp "$state_dir/.ssm-input.XXXXXX")
trap 'rm -f "$body"' EXIT
KEY="$key" NAME="$name" python3 -c 'import json, os; print(json.dumps({"Name": os.environ["NAME"], "Value": os.environ["KEY"], "Type": "SecureString", "Overwrite": True, "Tier": "Standard"}))' >"$body"
unset key
aws ssm put-parameter --region "$REGION" --cli-input-json "file://$body" --query Version --output text
rm -f "$body"
aws ssm add-tags-to-resource --region "$REGION" --resource-type Parameter --resource-id "$name" --tags Key=project,Value=gremlin
echo "stored $name (value not shown)"
