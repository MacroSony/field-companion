param(
    [ValidateRange(1, 65535)][int]$Port = 8795,
    [ValidateRange(1, 64)][int]$Threads = 4,
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
$visionRepoDirectory = Split-Path -Parent $PSScriptRoot
$visionPythonPath = Join-Path $visionRepoDirectory '.venv-vision\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $visionPythonPath)) {
    throw 'Create .venv-vision and install requirements-vision.txt first; see docs/LOCAL_DEVELOPMENT.md.'
}
$visionListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($visionListener) {
    throw "Port $Port is already in use. Check http://127.0.0.1:$Port/health before starting another service."
}
$visionArguments = @('-X', 'utf8', '-B', 'backend/vision_service.py', '--port', $Port, '--threads', $Threads)
if ($Background) {
    $visionLogDirectory = Join-Path $visionRepoDirectory 'out\vision-service'
    New-Item -ItemType Directory -Force -Path $visionLogDirectory | Out-Null
    $visionProcess = Start-Process -FilePath $visionPythonPath -ArgumentList $visionArguments -WorkingDirectory $visionRepoDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $visionLogDirectory 'stdout.log') -RedirectStandardError (Join-Path $visionLogDirectory 'stderr.log')
    @{ launcher_pid = $visionProcess.Id; port = $Port; threads = $Threads } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $visionLogDirectory 'process.json')
    Write-Output "BioCLIP starting (launcher $($visionProcess.Id)). Health: http://127.0.0.1:$Port/health"
    Write-Output "Startup/model downloads: $visionLogDirectory"
} else {
    Push-Location -LiteralPath $visionRepoDirectory
    try {
        & $visionPythonPath @visionArguments
        if ($LASTEXITCODE -ne 0) { throw 'BioCLIP service exited with an error.' }
    } finally { Pop-Location }
}
