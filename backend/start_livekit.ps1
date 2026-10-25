param(
    [ValidateRange(1,65535)][int]$Port = 8805,
    [ValidateRange(1,65535)][int]$BirdPort = 8811,
    [switch]$SkipVision
)
$ErrorActionPreference = 'Stop'
$livekitRoot = Split-Path -Parent $PSScriptRoot
$livekitPython = Join-Path $livekitRoot '.venv-livekit\Scripts\python.exe'
$livekitBirdPython = Join-Path $livekitRoot '.venv\Scripts\python.exe'
foreach ($livekitPath in @($livekitPython,$livekitBirdPython)) {
    if (-not (Test-Path -LiteralPath $livekitPath)) { throw 'Install both local environments; see docs/LIVEKIT.md.' }
}
foreach ($livekitPort in @($Port,$BirdPort)) {
    if (Get-NetTCPConnection -LocalPort $livekitPort -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $livekitPort is already in use; existing services will not be replaced."
    }
}
# Include the installed camera dependency when restoring a complete local app.
# Reuse a healthy service; never stop or replace another listener.
$livekitVisionPython = Join-Path $livekitRoot '.venv-vision\Scripts\python.exe'
$livekitDefaultVision = -not $env:VISION_URL -or $env:VISION_URL.TrimEnd('/') -eq 'http://127.0.0.1:8795'
if (-not $SkipVision -and $livekitDefaultVision) {
    if (Test-Path -LiteralPath $livekitVisionPython) {
        if (-not (Get-NetTCPConnection -LocalPort 8795 -State Listen -ErrorAction SilentlyContinue)) {
            & (Join-Path $PSScriptRoot 'start_vision.ps1') -Background
        }
        Write-Output 'Waiting for BioCLIP model readiness...'
        $livekitVisionDeadline = [DateTime]::UtcNow.AddMinutes(3)
        $livekitVisionReady = $false
        do {
            try {
                $livekitVisionHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8795/health' -TimeoutSec 2
                $livekitVisionReady = $livekitVisionHealth.loaded -and $livekitVisionHealth.model -eq 'imageomics/bioclip-2'
            } catch { $livekitVisionReady = $false }
            if (-not $livekitVisionReady) { Start-Sleep -Seconds 2 }
        } while (-not $livekitVisionReady -and [DateTime]::UtcNow -lt $livekitVisionDeadline)
        if (-not $livekitVisionReady) {
            throw 'BioCLIP is not ready. Check out/vision-service/, or use -SkipVision for a voice-only session.'
        }
        Write-Output 'Photo identification: BioCLIP ready.'
    } else {
        Write-Warning 'Photo identification is unavailable: install .venv-vision as described in docs/LOCAL_DEVELOPMENT.md.'
    }
}
$livekitLogs = Join-Path $livekitRoot 'out\livekit'
New-Item -ItemType Directory -Force -Path $livekitLogs | Out-Null
$livekitWorkerKeyFile = Join-Path $livekitLogs 'birdnet-worker.key'
if (-not (Test-Path -LiteralPath $livekitWorkerKeyFile)) {
    $livekitRandom = [byte[]]::new(32)
    $livekitGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $livekitGenerator.GetBytes($livekitRandom) } finally { $livekitGenerator.Dispose() }
    [IO.File]::WriteAllText($livekitWorkerKeyFile, [BitConverter]::ToString($livekitRandom).Replace('-',''))
}
$livekitWorker = Start-Process -FilePath $livekitBirdPython -ArgumentList @('-X','utf8','-B','backend/birdnet_worker.py','--port',$BirdPort,'--key-file',('"' + $livekitWorkerKeyFile + '"')) -WorkingDirectory $livekitRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $livekitLogs 'birdnet.stdout.log') -RedirectStandardError (Join-Path $livekitLogs 'birdnet.stderr.log')
$livekitOldWorkerURL = $env:FIELD_BIRDNET_WORKER_URL
$livekitOldWorkerFile = $env:FIELD_BIRDNET_WORKER_KEY_FILE
try {
    $env:FIELD_BIRDNET_WORKER_URL = "http://127.0.0.1:$BirdPort"
    $env:FIELD_BIRDNET_WORKER_KEY_FILE = $livekitWorkerKeyFile
    $livekitApp = Start-Process -FilePath $livekitPython -ArgumentList @('-X','utf8','-B','backend/server.py','--host','127.0.0.1','--port',$Port,'--realtime-provider','openai','--enable-livekit') -WorkingDirectory $livekitRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $livekitLogs 'app.stdout.log') -RedirectStandardError (Join-Path $livekitLogs 'app.stderr.log')
} finally {
    $env:FIELD_BIRDNET_WORKER_URL = $livekitOldWorkerURL
    $env:FIELD_BIRDNET_WORKER_KEY_FILE = $livekitOldWorkerFile
}
@{port=$Port;bird_port=$BirdPort;app_launcher_pid=$livekitApp.Id;bird_launcher_pid=$livekitWorker.Id;url="http://localhost:$Port/?transport=livekit&session=livekit-phone"} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $livekitLogs 'process.json') -Encoding utf8
Write-Output "LiveKit app: http://localhost:$Port/?transport=livekit&session=livekit-phone"
Write-Output "Local setup: http://localhost:$Port/setup/livekit (private fragment is saved under out/livekit/)"
