# Redirect all heavyweight dev caches from C: to D:.
# Run once:  powershell -ExecutionPolicy Bypass -File scripts\set_disk_env.ps1
# Requires a NEW shell afterwards for the variables to take effect.

$ErrorActionPreference = "Stop"
$CacheRoot = "D:\Projects\.cache"

$dirs = @(
    "$CacheRoot\ollama\models",
    "$CacheRoot\pip",
    "$CacheRoot\hf\hub",
    "$CacheRoot\torch",
    "$CacheRoot\uv"
)
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

$vars = [ordered]@{
    # Ollama model weights (a 7B Q4_K_M model is ~4.5 GB each)
    "OLLAMA_MODELS"  = "$CacheRoot\ollama\models"
    # pip wheel/download cache
    "PIP_CACHE_DIR"  = "$CacheRoot\pip"
    # HuggingFace model download (local embedding / reranker)
    "HF_HOME"        = "$CacheRoot\hf"
    "HF_HUB_CACHE"   = "$CacheRoot\hf\hub"
    # torch pretrained checkpoints
    "TORCH_HOME"     = "$CacheRoot\torch"
}

foreach ($k in $vars.Keys) {
    [Environment]::SetEnvironmentVariable($k, $vars[$k], "User")
    Set-Item -Path "Env:$k" -Value $vars[$k]
    Write-Host ("  {0,-14} -> {1}" -f $k, $vars[$k])
}

Write-Host ""
Write-Host "Done. Open a NEW terminal for these to apply globally."
Write-Host "Ollama: after installing, 'ollama pull qwen2.5:7b-instruct-q4_K_M' will land on D:"
