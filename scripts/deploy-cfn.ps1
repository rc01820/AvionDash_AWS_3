<#
.SYNOPSIS
  Deploy the AvionPulse v3 core stack with plain CloudFormation (cloudformation/avionpulse.yaml). Needs the
  AWS CLI and Python. The site deployer custom resource uploads the web tier, so there's no S3 sync step.
  The Classic tier is NOT deployed here — launch it from the Chaos page (or scripts/classic.py up).

.EXAMPLE
  ./scripts/deploy-cfn.ps1
  ./scripts/deploy-cfn.ps1 -AdminPassword 'S0mething-Long!' -BudgetEmail you@example.com
  ./scripts/deploy-cfn.ps1 -DatadogApiKey $env:DD_API_KEY -DatadogPythonLayerVersion 107 -DatadogExtensionLayerVersion 80
#>
param(
  [string]$StackName = "avionpulse",
  [string]$Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }),
  [string]$CodeBucket = "",
  [string]$AppEnv = "demo",
  [string]$AdminPassword = "",
  [string]$DatadogApiKey = $env:DD_API_KEY,
  [string]$DatadogSite = $(if ($env:DD_SITE) { $env:DD_SITE } else { "datadoghq.com" }),
  [int]$DatadogPythonLayerVersion = 107,
  [int]$DatadogExtensionLayerVersion = 80,
  [string]$RumApplicationId = "",
  [string]$RumClientToken = "",
  [string]$BudgetEmail = "",
  [int]$MonthlyBudgetUsd = 10
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw "AWS CLI not found on PATH" }
if ($StackName.Length -gt 24) { throw "StackName must be 24 characters or fewer (the Classic tier's ALB is named <stack>-classic)" }

$account = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) { throw "AWS credentials not configured (run: aws configure  or  aws sso login)" }
if (-not $CodeBucket) { $CodeBucket = "$StackName-artifacts-$account-$Region" }

Write-Host "==> Artifact bucket s3://$CodeBucket"
aws s3api head-bucket --bucket $CodeBucket 2>$null
if ($LASTEXITCODE -ne 0) {
  aws s3 mb "s3://$CodeBucket" --region $Region | Out-Null
  aws s3api put-public-access-block --bucket $CodeBucket --public-access-block-configuration `
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" | Out-Null
}

Write-Host "==> Packaging code"
$pkg = python scripts/package.py --bucket $CodeBucket --region $Region
if ($LASTEXITCODE -ne 0) { throw "packaging failed" }
$pkg | ForEach-Object { Write-Host "    $_" }
$codeKey = ($pkg | Select-String '^CodeKey=(.+)$').Matches[0].Groups[1].Value

$ssmName = "/$StackName/datadog/api-key"
$enableDd = "false"
if ($DatadogApiKey) {
  Write-Host "==> Storing Datadog API key in SSM ($ssmName)"
  aws ssm put-parameter --region $Region --name $ssmName --type SecureString --value $DatadogApiKey --overwrite | Out-Null
  $enableDd = "true"
}

$overrides = @(
  "CodeBucket=$CodeBucket", "CodeKey=$codeKey", "AppEnv=$AppEnv",
  "EnableDatadog=$enableDd", "DatadogSite=$DatadogSite", "DatadogApiKeySsmName=$ssmName",
  "DatadogPythonLayerVersion=$DatadogPythonLayerVersion", "DatadogExtensionLayerVersion=$DatadogExtensionLayerVersion",
  "MonthlyBudgetUsd=$MonthlyBudgetUsd"
)
if ($AdminPassword)    { $overrides += "AdminPassword=$AdminPassword" }
if ($RumApplicationId) { $overrides += "RumApplicationId=$RumApplicationId" }
if ($RumClientToken)   { $overrides += "RumClientToken=$RumClientToken" }
if ($BudgetEmail)      { $overrides += "BudgetEmail=$BudgetEmail" }

# The template is over CloudFormation's 51,200-byte inline limit, so it goes via the artifact bucket.
Write-Host "==> aws cloudformation deploy ($StackName in $Region) — first create takes ~6-10 min"
aws cloudformation deploy `
  --region $Region `
  --stack-name $StackName `
  --template-file cloudformation/avionpulse.yaml `
  --capabilities CAPABILITY_IAM `
  --s3-bucket $CodeBucket `
  --s3-prefix "$StackName/templates" `
  --no-fail-on-empty-changeset `
  --tags app=avionpulse suite=aviondash `
  --parameter-overrides $overrides
if ($LASTEXITCODE -ne 0) {
  Write-Host "Deploy failed. Most recent failure events:" -ForegroundColor Red
  aws cloudformation describe-stack-events --region $Region --stack-name $StackName `
    --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason] | [0:5]" --output table
  exit 1
}

$url = aws cloudformation describe-stacks --region $Region --stack-name $StackName `
  --query "Stacks[0].Outputs[?OutputKey=='SiteUrl'].OutputValue" --output text
Write-Host ""
Write-Host "AvionPulse is live: $url"
Write-Host "Login: admin / $(if ($AdminPassword) { '<your AdminPassword>' } else { 'aviondash123' })"
