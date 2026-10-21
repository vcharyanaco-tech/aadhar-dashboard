# Deploy only the aadhar-backup Worker.
#
# The Cloudflare credential is intentionally not stored in this repository.
# It lives in the sibling, gitignored file ../.cloudflare.local:
#
#   CLOUDFLARE_API_TOKEN=...
#   CLOUDFLARE_ACCOUNT_ID=606780bc6c3ecd48289472c73d27a982
#
# Usage:
#   .\deploy.ps1             # deploy
#   .\deploy.ps1 -DryRun     # bundle without uploading

[CmdletBinding()]
param(
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$credFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.cloudflare.local'
$expectedAccount = '606780bc6c3ecd48289472c73d27a982'

if (-not (Test-Path $credFile)) {
  throw "Missing $credFile. Create it with the Aadhaar Cloudflare API token and account id."
}

$cfg = @{}
Get-Content $credFile | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith('#')) {
    $eq = $line.IndexOf('=')
    if ($eq -gt 0) {
      $name = $line.Substring(0, $eq).Trim()
      $value = $line.Substring($eq + 1).Trim().Trim('"').Trim("'")
      $cfg[$name] = $value
    }
  }
}

$token = $cfg['CLOUDFLARE_API_TOKEN']
$account = $cfg['CLOUDFLARE_ACCOUNT_ID']
if (-not $token) { throw 'CLOUDFLARE_API_TOKEN is missing from .cloudflare.local.' }
if ($account -ne $expectedAccount) {
  throw "Refusing to deploy: .cloudflare.local points at '$account', not the Aadhaar backup account."
}

$configAccount = (Select-String -Path (Join-Path $PSScriptRoot 'wrangler.toml') `
    -Pattern '^\s*account_id\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if ($configAccount -ne $expectedAccount) {
  throw "Refusing to deploy: wrangler.toml account_id is '$configAccount'."
}

$wrangler = Get-Command wrangler -ErrorAction SilentlyContinue
if (-not $wrangler) { throw 'wrangler is not installed or not on PATH.' }

$oldToken = $env:CLOUDFLARE_API_TOKEN
$oldAccount = $env:CLOUDFLARE_ACCOUNT_ID
try {
  $env:CLOUDFLARE_API_TOKEN = $token
  $env:CLOUDFLARE_ACCOUNT_ID = $account
  Push-Location $PSScriptRoot
  try {
    if ($DryRun) {
      & $wrangler.Source deploy --dry-run
    } else {
      & $wrangler.Source deploy
    }
    if ($LASTEXITCODE -ne 0) { throw "wrangler failed with exit code $LASTEXITCODE" }
  } finally {
    Pop-Location
  }
} finally {
  if ($null -eq $oldToken) { Remove-Item Env:CLOUDFLARE_API_TOKEN -ErrorAction SilentlyContinue }
  else { $env:CLOUDFLARE_API_TOKEN = $oldToken }
  if ($null -eq $oldAccount) { Remove-Item Env:CLOUDFLARE_ACCOUNT_ID -ErrorAction SilentlyContinue }
  else { $env:CLOUDFLARE_ACCOUNT_ID = $oldAccount }
}
