<#
.SYNOPSIS
  Remove every AvionPulse resource (stops all charges). Irreversible: the table and bucket are deleted.
#>
param(
  [string]$StackName = "avionpulse",
  [string]$Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }),
  [switch]$KeepApiKey
)
$ErrorActionPreference = "Stop"

# The Classic stack (if launched) must go first: it's separate, and deleting the
# core stack removes the CloudFormation role the Classic stack was created with.
$classic = "$StackName-classic"
aws cloudformation describe-stacks --region $Region --stack-name $classic 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
  $role = aws cloudformation describe-stacks --region $Region --stack-name $StackName `
    --query "Stacks[0].Outputs[?OutputKey=='ClassicCfnRoleArn'].OutputValue" --output text
  Write-Host "==> Deleting Classic stack $classic first (~10 min)"
  aws cloudformation delete-stack --region $Region --stack-name $classic --role-arn $role
  aws cloudformation wait stack-delete-complete --region $Region --stack-name $classic
}
aws scheduler delete-schedule --region $Region --name "$classic-expiry" 2>$null | Out-Null

$bucket = aws cloudformation describe-stacks --region $Region --stack-name $StackName `
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text
if ($bucket -and $bucket -ne "None") {
  Write-Host "==> Emptying s3://$bucket"
  aws s3 rm "s3://$bucket" --recursive --region $Region | Out-Null
}

Write-Host "==> Deleting stack $StackName (waits until done, ~5-15 min: CloudFront is slow to delete)"
aws cloudformation delete-stack --region $Region --stack-name $StackName
aws cloudformation wait stack-delete-complete --region $Region --stack-name $StackName
if ($LASTEXITCODE -ne 0) { throw "Stack delete failed — see the Events tab in the CloudFormation console" }

if (-not $KeepApiKey) {
  aws ssm delete-parameter --region $Region --name "/$StackName/datadog/api-key" 2>$null | Out-Null
}
Write-Host "Done. Nothing from AvionPulse is left running."
Write-Host "Left behind on purpose (cents/month at most): the code artifact bucket ($StackName-artifacts-*)."
Write-Host "Delete it in the S3 console if unused."
