param([ValidateRange(1,65535)][int]$Port = 8821)
$ErrorActionPreference = 'Stop'
$speakerRoot = Split-Path -Parent $PSScriptRoot
$speakerPython = Join-Path $speakerRoot '.venv-speaker\Scripts\python.exe'
$speakerURL = "http://127.0.0.1:$Port"
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    $speakerHealth = Invoke-RestMethod -Uri "$speakerURL/health" -TimeoutSec 3
    if (-not $speakerHealth.loaded -or $speakerHealth.model -ne 'speechbrain/spkrec-ecapa-voxceleb') {
        throw 'Speaker port belongs to a different service; no processes were changed.'
    }
    Write-Output "Speaker service ready: $speakerURL"
    return
}
if (-not (Test-Path -LiteralPath $speakerPython)) { throw 'Install requirements-speaker.txt in .venv-speaker first.' }
$speakerLogs = Join-Path $speakerRoot 'out\speaker-service'
New-Item -ItemType Directory -Force -Path $speakerLogs | Out-Null
$speakerArgs = @('-X','utf8','-u','-B','backend/speaker_service.py','--port',$Port,
    '--model-dir',('"'+(Join-Path $speakerRoot 'out\speaker-model')+'"'),
    '--speakers-dir',('"'+(Join-Path $speakerRoot 'speakers')+'"'))
$speakerProcess = Start-Process -FilePath $speakerPython -ArgumentList $speakerArgs -WorkingDirectory $speakerRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $speakerLogs 'stdout.log') -RedirectStandardError (Join-Path $speakerLogs 'stderr.log')
@{launcher_pid=$speakerProcess.Id;port=$Port} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $speakerLogs 'process.json')
$speakerDeadline = [DateTime]::UtcNow.AddMinutes(3)
do {
    try {
        $speakerHealth = Invoke-RestMethod -Uri "$speakerURL/health" -TimeoutSec 2
        if ($speakerHealth.loaded) { Write-Output "Speaker service ready: $speakerURL"; return }
    } catch { }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $speakerDeadline)
throw 'Speaker model did not become ready; see out/speaker-service/stderr.log.'
