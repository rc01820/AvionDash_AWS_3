#!/usr/bin/env bash
# Remove every AvionPulse resource (stops all charges). Irreversible: the table and bucket are deleted.
set -euo pipefail
STACK_NAME="${STACK_NAME:-avionpulse}"
REGION="${AWS_REGION:-us-east-1}"

# The Classic stack (if launched) must go first: it's separate, and deleting the
# core stack removes the CloudFormation role the Classic stack was created with.
CLASSIC="${STACK_NAME}-classic"
if aws cloudformation describe-stacks --region "$REGION" --stack-name "$CLASSIC" >/dev/null 2>&1; then
  ROLE=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='ClassicCfnRoleArn'].OutputValue" --output text)
  echo "==> Deleting Classic stack $CLASSIC first (~10 min)"
  aws cloudformation delete-stack --region "$REGION" --stack-name "$CLASSIC" --role-arn "$ROLE"
  aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$CLASSIC"
fi
aws scheduler delete-schedule --region "$REGION" --name "${CLASSIC}-expiry" >/dev/null 2>&1 || true

BUCKET=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text || true)
if [[ -n "$BUCKET" && "$BUCKET" != "None" ]]; then
  echo "==> Emptying s3://$BUCKET"
  aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" >/dev/null
fi

echo "==> Deleting stack $STACK_NAME (waits until done, ~5-15 min: CloudFront is slow to delete)"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"

if [[ "${KEEP_API_KEY:-}" != "1" ]]; then
  aws ssm delete-parameter --region "$REGION" --name "/${STACK_NAME}/datadog/api-key" 2>/dev/null || true
fi
echo "Done. Nothing from AvionPulse is left running."
echo "Left behind on purpose (cents/month at most): the code artifact bucket (${STACK_NAME}-artifacts-*)."
echo "Delete it in the S3 console if unused."
