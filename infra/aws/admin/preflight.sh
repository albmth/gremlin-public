#!/usr/bin/env bash
# Read-only inventory, run by an account admin BEFORE applying bootstrap.yaml.
# Usage: AWS_PROFILE=<admin profile> infra/aws/admin/preflight.sh
# Refuses to run as root or as the agent user. The account ID comes from GREMLIN_ACCOUNT or
# infra/aws/.state/account, never from Git.
set -euo pipefail
export AWS_PAGER=""
ACCOUNT="${GREMLIN_ACCOUNT:-$(cat "$(dirname "$0")/../.state/account" 2>/dev/null || true)}"
[[ "$ACCOUNT" =~ ^[0-9]{12}$ ]] || { echo "set GREMLIN_ACCOUNT or infra/aws/.state/account" >&2; exit 1; }
readonly ACCOUNT REGION=eu-north-1
who=$(aws sts get-caller-identity --query Arn --output text)
[[ "$who" != *":root" && "$who" != *"user/gremlin-dev" ]] \
  || { echo "run as an admin identity, not $who" >&2; exit 1; }
acct=$(aws sts get-caller-identity --query Account --output text)
[[ "$acct" == "$ACCOUNT" ]] || { echo "wrong account $acct" >&2; exit 1; }
echo "identity: $who"

# An absent name is the expected answer, so a probe reports the first non-blank line and never aborts.
probe() { { "$@" 2>&1 || true; } | grep -m1 . || echo "(no output)"; }

echo "== name collisions (each should print NoSuchEntity, NotFound or 404)"
probe aws iam get-user --user-name gremlin-dev --query 'User.Arn' --output text
for r in gremlin-ctl-deployer gremlin-ctl-cfn-exec; do probe aws iam get-role --role-name "$r" --query 'Role.Arn' --output text; done
probe aws iam get-policy --policy-arn "arn:aws:iam::${acct}:policy/gremlin-ctl-workload-boundary" --query 'Policy.Arn' --output text
probe aws s3api head-bucket --bucket "gremlin-ctl-artifacts-${acct}-eun1"

echo "== existing gremlin stacks in $REGION"
aws cloudformation list-stacks --region "$REGION" \
  --query 'StackSummaries[?starts_with(StackName,`gremlin-`) && StackStatus!=`DELETE_COMPLETE`].[StackName,StackStatus]' --output text

echo "== region opted in"
probe aws account get-region-opt-status --region-name "$REGION" --query RegionOptStatus --output text

echo "== Lambda account limits in $REGION (concurrency is per region)"
aws lambda get-account-settings --region "$REGION" --query 'AccountLimit.[ConcurrentExecutions,UnreservedConcurrentExecutions]' --output text

echo "== existing account-wide budgets"
aws budgets describe-budgets --account-id "$acct" --query 'Budgets[].[BudgetName,BudgetLimit.Amount,TimeUnit]' --output text

echo "== CloudTrail"
aws cloudtrail describe-trails --region "$REGION" --query 'trailList[].[Name,IsMultiRegionTrail]' --output text
