#!/usr/bin/env bash
# Deploy the AvionPulse v3 core stack with plain CloudFormation (cloudformation/avionpulse.yaml).
# Needs the AWS CLI and Python. The Classic tier is NOT deployed here — launch it from the
# Chaos page (or scripts/classic.py up) when you need it.
#
#   ./scripts/deploy-cfn.sh
#   ADMIN_PASSWORD='S0mething-Long!' BUDGET_EMAIL=you@example.com ./scripts/deploy-cfn.sh
#   DD_API_KEY=xxx DD_PY_LAYER=107 DD_EXT_LAYER=80 ./scripts/deploy-cfn.sh
set -euo pipefail
cd "$(dirname "$0")/.."

STACK_NAME="${STACK_NAME:-avionpulse}"
REGION="${AWS_REGION:-us-east-1}"
APP_ENV="${APP_ENV:-demo}"
DD_SITE="${DD_SITE:-datadoghq.com}"
DD_PY_LAYER="${DD_PY_LAYER:-107}"
DD_EXT_LAYER="${DD_EXT_LAYER:-80}"
MONTHLY_BUDGET_USD="${MONTHLY_BUDGET_USD:-10}"
PYTHON="$(command -v python3 || command -v python)"

command -v aws >/dev/null || { echo "AWS CLI not found" >&2; exit 1; }
if (( ${#STACK_NAME} > 24 )); then
  echo "STACK_NAME must be 24 characters or fewer (the Classic tier's ALB is named <stack>-classic)" >&2; exit 1
fi
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
CODE_BUCKET="${CODE_BUCKET:-${STACK_NAME}-artifacts-${ACCOUNT}-${REGION}}"

echo "==> Artifact bucket s3://$CODE_BUCKET"
if ! aws s3api head-bucket --bucket "$CODE_BUCKET" 2>/dev/null; then
  aws s3 mb "s3://$CODE_BUCKET" --region "$REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$CODE_BUCKET" --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
fi

echo "==> Packaging code"
PKG_OUT=$("$PYTHON" scripts/package.py --bucket "$CODE_BUCKET" --region "$REGION")
echo "$PKG_OUT" | sed 's/^/    /'
CODE_KEY=$(echo "$PKG_OUT" | sed -n 's/^CodeKey=//p')

SSM_NAME="/${STACK_NAME}/datadog/api-key"
ENABLE_DD=false
if [[ -n "${DD_API_KEY:-}" ]]; then
  echo "==> Storing Datadog API key in SSM ($SSM_NAME)"
  aws ssm put-parameter --region "$REGION" --name "$SSM_NAME" --type SecureString --value "$DD_API_KEY" --overwrite >/dev/null
  ENABLE_DD=true
fi

OVERRIDES=("CodeBucket=$CODE_BUCKET" "CodeKey=$CODE_KEY" "AppEnv=$APP_ENV"
  "EnableDatadog=$ENABLE_DD" "DatadogSite=$DD_SITE" "DatadogApiKeySsmName=$SSM_NAME"
  "DatadogPythonLayerVersion=$DD_PY_LAYER" "DatadogExtensionLayerVersion=$DD_EXT_LAYER"
  "MonthlyBudgetUsd=$MONTHLY_BUDGET_USD")
[[ -n "${ADMIN_PASSWORD:-}" ]] && OVERRIDES+=("AdminPassword=$ADMIN_PASSWORD")
[[ -n "${RUM_APPLICATION_ID:-}" ]] && OVERRIDES+=("RumApplicationId=$RUM_APPLICATION_ID")
[[ -n "${RUM_CLIENT_TOKEN:-}" ]] && OVERRIDES+=("RumClientToken=$RUM_CLIENT_TOKEN")
[[ -n "${BUDGET_EMAIL:-}" ]] && OVERRIDES+=("BudgetEmail=$BUDGET_EMAIL")

# The template is over CloudFormation's 51,200-byte inline limit, so it goes via the artifact bucket.
echo "==> aws cloudformation deploy ($STACK_NAME in $REGION) — first create takes ~6-10 min"
if ! aws cloudformation deploy --region "$REGION" --stack-name "$STACK_NAME" \
      --template-file cloudformation/avionpulse.yaml --capabilities CAPABILITY_IAM \
      --s3-bucket "$CODE_BUCKET" --s3-prefix "$STACK_NAME/templates" \
      --no-fail-on-empty-changeset --tags app=avionpulse suite=aviondash \
      --parameter-overrides "${OVERRIDES[@]}"; then
  echo "Deploy failed. Most recent failure events:" >&2
  aws cloudformation describe-stack-events --region "$REGION" --stack-name "$STACK_NAME" \
    --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason] | [0:5]" --output table >&2
  exit 1
fi

URL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='SiteUrl'].OutputValue" --output text)
echo
echo "AvionPulse is live: $URL"
