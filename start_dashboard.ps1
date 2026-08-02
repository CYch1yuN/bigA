# start_dashboard.ps1 - Start the BigA quant workbench Dashboard (Windows / PowerShell)
#
# - Loads credentials from .env with a strict whitelist:
#     ASHARE_DASHBOARD_USERNAME / ASHARE_DASHBOARD_PASSWORD_HASH / ASHARE_DASHBOARD_SESSION_SECRET
# - Rejects duplicate keys, empty values and any plaintext ASHARE_DASHBOARD_PASSWORD key.
# - Verifies .env, the venv and the frontend dist build; exits with clear guidance if missing.
# - Never prints any secret value.
# - Note: on first start the backend persists the password hash to
#   state/dashboard/auth.json, which then takes precedence over .env.
#   To rotate the password later, use the UI "change password" flow.

[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [string]$EnvFile = ''   # optional override (used by tests); defaults to <root>/.env
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path   # repo root (this script lives at repo root)
if ([string]::IsNullOrWhiteSpace($EnvFile)) { $EnvFile = Join-Path $root '.env' }
$py = Join-Path $root '.venv\Scripts\python.exe'
$distIndex = Join-Path $root 'dashboard\frontend\dist\index.html'
$appDir = Join-Path $root 'dashboard\backend'

function Fail([string]$msg) {
    Write-Host "[FAIL] $msg" -ForegroundColor Red
    exit 1
}

# 1) .env must exist
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Fail "missing $EnvFile - run 'pip install -e .[workbench]' and configure credentials first (see README)"
}

# 2) strict .env parse: dict keyed by name; reject duplicates; require the 3
#    whitelist keys present and non-empty; reject plaintext PASSWORD key.
$allow = @(
    'ASHARE_DASHBOARD_USERNAME',
    'ASHARE_DASHBOARD_PASSWORD_HASH',
    'ASHARE_DASHBOARD_SESSION_SECRET'
)
$creds = @{}
$seen = @{}
Get-Content -LiteralPath $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        $k = $Matches[1]
        $v = $Matches[2]
        if ($seen.ContainsKey($k)) {
            Fail "duplicate key in .env: $k"
        }
        $seen[$k] = $true
        if ($k -eq 'ASHARE_DASHBOARD_PASSWORD') {
            Fail "plaintext key ASHARE_DASHBOARD_PASSWORD is not allowed in .env - keep the password in your password manager"
        }
        if ($k -in $allow) {
            $creds[$k] = $v
        }
    }
}
foreach ($key in $allow) {
    if (-not $creds.ContainsKey($key) -or [string]::IsNullOrEmpty($creds[$key])) {
        Fail "missing or empty required key in .env: $key"
    }
}
# load into process environment (values never echoed)
foreach ($key in $allow) {
    Set-Item -Path "env:$key" -Value $creds[$key]
}

# 3) venv must exist
if (-not (Test-Path -LiteralPath $py)) {
    Fail "missing venv at $py - run 'pip install -e .[workbench]'"
}

# 4) frontend build must exist (never start API-only silently)
if (-not (Test-Path -LiteralPath $distIndex)) {
    Fail "frontend not built (missing dashboard\frontend\dist\index.html) - cd dashboard\frontend && npm ci && npm run build"
}

Write-Host "[OK] preflight passed: .env / venv / frontend dist ready (credentials not shown)" -ForegroundColor Green
if ($CheckOnly) { exit 0 }

Write-Host "[OK] starting Dashboard -> http://127.0.0.1:8765 (loopback only)"
& $py -m uvicorn app.main:create_app --factory --app-dir $appDir --host 127.0.0.1 --port 8765
