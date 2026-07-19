# BusinessSearchAgent — environment setup (Windows / PowerShell)
#
#   .\setup.ps1              create .venv and verify
#   .\setup.ps1 -Offline     skip the live network check
#   .\setup.ps1 -Recreate    delete an existing .venv first
#
# There are no runtime dependencies to install; the venv exists to pin the
# interpreter and isolate the project from system Python.

param(
    [switch]$Offline,
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "BusinessSearchAgent - environment setup" -ForegroundColor Cyan
Write-Host ""

# --- locate a suitable interpreter (>= 3.11) --------------------------------
$python = $null
foreach ($candidate in @("py -3.14", "py -3.13", "py -3.12", "py -3.11", "python")) {
    $parts = $candidate.Split(" ")
    try {
        $ver = & $parts[0] $parts[1..$parts.Length] --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $ver -match "Python (\d+)\.(\d+)") {
            if ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11)) {
                $python = $candidate
                Write-Host "  interpreter : $ver  ($candidate)"
                break
            }
        }
    } catch { }
}

if (-not $python) {
    Write-Host "  ERROR: no Python >= 3.11 found." -ForegroundColor Red
    Write-Host "  Install from https://www.python.org/downloads/"
    exit 1
}

# --- create the virtual environment -----------------------------------------
if ($Recreate -and (Test-Path ".venv")) {
    Write-Host "  removing existing .venv ..."
    Remove-Item -Recurse -Force .venv
}

if (Test-Path ".venv") {
    Write-Host "  .venv       : already exists (use -Recreate to rebuild)"
} else {
    Write-Host "  creating .venv ..."
    $parts = $python.Split(" ")
    & $parts[0] $parts[1..$parts.Length] -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: venv creation failed" -ForegroundColor Red; exit 1 }
    Write-Host "  .venv       : created"
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# --- no dependencies, but keep pip current ----------------------------------
Write-Host "  upgrading pip (quiet) ..."
& $venvPython -m pip install --quiet --upgrade pip 2>&1 | Out-Null
Write-Host "  dependencies: none required (standard library only)"

# --- .env is optional --------------------------------------------------------
# No credentials are required for the core pipeline. Create .env by hand only
# if you need optional settings (e.g. ANTHROPIC_API_KEY for enrich_web.py).
if (Test-Path ".env") {
    Write-Host "  .env        : found, will be loaded (never committed)"
} else {
    Write-Host "  .env        : none (fine - no credentials required)"
}

# --- verify -------------------------------------------------------------------
Write-Host ""
$verifyArgs = @("verify.py")
if ($Offline) { $verifyArgs += "--offline" }
& $venvPython @verifyArgs
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "Activate the environment with:" -ForegroundColor Green
    Write-Host "    .\.venv\Scripts\Activate.ps1"
    Write-Host ""
    Write-Host "Then try:"
    Write-Host "    python agent.py Concord MA"
} else {
    Write-Host "Verification failed - see above." -ForegroundColor Red
}
exit $code
