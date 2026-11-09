# Install Ollama with BOTH the app binaries and the model weights on D:.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_ollama.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_ollama.ps1 -Installer "<path-to>\OllamaSetup.exe"
#
# Docs: https://docs.ollama.com/windows
#   /DIR=...          -> app binaries location
#   OLLAMA_MODELS     -> model weights location (user-scope env var)

param(
    [string]$Installer = "",
    [string]$AppDir = "",
    [string]$ModelsDir = ""
)

$ErrorActionPreference = "Stop"
# Derive the cache root from the script location; never hardcode a local drive.
# Override with -CacheRoot or the OA_CACHE_ROOT environment variable.
$CacheRoot = if ($env:OA_CACHE_ROOT) {
    $env:OA_CACHE_ROOT
} else {
    Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) ".cache"
}
if (-not $AppDir) { $AppDir = Join-Path $CacheRoot "ollama\app" }
if (-not $ModelsDir) { $ModelsDir = Join-Path $CacheRoot "ollama\models" }

Write-Host "=== Ollama non-system-drive install ==="
Write-Host "App binaries : $AppDir"
Write-Host "Model weights: $ModelsDir"
Write-Host ""

# --- 1. Preflight: Ollama reads env vars at process start -----------------
$running = Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue
if ($running) {
    Write-Warning "Ollama is already running. It will IGNORE OLLAMA_MODELS until restarted."
    Write-Host "Quit the tray icon first, then re-run this script."
    exit 1
}

# --- 2. Locate the installer ---------------------------------------------
if (-not $Installer) {
    $candidates = @(
        (Join-Path $env:USERPROFILE "Downloads\OllamaSetup.exe"),
        (Join-Path $env:USERPROFILE "Downloads\OllamaSetup (1).exe"),
        (Join-Path $CacheRoot "ollama\OllamaSetup.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $Installer = $c; break }
    }
}
if (-not $Installer -or -not (Test-Path $Installer)) {
    Write-Host "OllamaSetup.exe not found. Do this first:"
    Write-Host "  1) Download from https://ollama.com/download/windows"
    Write-Host "  2) Re-run with:  -Installer `"<full path to OllamaSetup.exe>`""
    exit 1
}
Write-Host "Installer: $Installer"

# --- 3. Prepare D: dirs and enforce the model path ------------------------
New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
New-Item -ItemType Directory -Path $ModelsDir -Force | Out-Null
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $ModelsDir, "User")
Set-Item -Path "Env:OLLAMA_MODELS" -Value $ModelsDir
Write-Host "[OK] OLLAMA_MODELS set to $ModelsDir (User scope, persists after reboot)"

# --- 4. Install the app with /DIR -----------------------------------------
Write-Host ""
Write-Host "Launching installer with /DIR (wait for the GUI to finish)..."
$dirArg = '/DIR="' + $AppDir + '"'
Start-Process -FilePath $Installer -ArgumentList $dirArg -Wait

# --- 5. Verification checklist -------------------------------------------
Write-Host ""
Write-Host "=== Next steps ==="
Write-Host "  1. Open a NEW terminal (env vars apply to new processes only)"
Write-Host "  2. ollama --version"
Write-Host "  3. echo `$env:OLLAMA_MODELS     # must print $ModelsDir"
Write-Host "  4. ollama pull qwen2.5:7b-instruct-q4_K_M   # ~4.7 GB, lands on D:"
Write-Host ("  5. cd " + (Split-Path -Parent $PSScriptRoot) + "; .venv\Scripts\python.exe main.py --check")
Write-Host ""
Write-Host "Verify the pull landed on the cache drive:  Get-ChildItem '$ModelsDir\blobs' | Measure-Object Length -Sum"
Write-Host 'If %USERPROFILE%\.ollama\models grows instead, the tray app was not restarted.'
Write-Host ""
Write-Host "Note: %LOCALAPPDATA%\Ollama (logs + downloaded updates) and %TEMP%\ollama*"
Write-Host "      still live on C: and are small. There is no supported env var to move them."
