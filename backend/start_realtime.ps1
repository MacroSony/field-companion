param(
    [ValidateSet('openai', 'higgs')][string]$Provider = 'openai',
    [ValidateRange(1, 65535)][int]$Port = 8800,
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
$realtimeRepoDirectory = Split-Path -Parent $PSScriptRoot
$realtimePythonPath = Join-Path $realtimeRepoDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $realtimePythonPath)) {
    throw 'Install the existing backend requirements in .venv first; see docs/LOCAL_DEVELOPMENT.md.'
}
$realtimeListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($realtimeListener) {
    throw "Port $Port is already in use; this launcher will not replace that process."
}
$realtimeArguments = @('-X', 'utf8', '-B', 'backend/server.py', '--host', '127.0.0.1', '--port', $Port,
                       '--realtime-provider', $Provider)
if ($Background) {
    $realtimeLogDirectory = Join-Path $realtimeRepoDirectory "out\realtime-$Provider-$Port"
    New-Item -ItemType Directory -Force -Path $realtimeLogDirectory | Out-Null
    $realtimeProcess = Start-Process -FilePath $realtimePythonPath -ArgumentList $realtimeArguments -WorkingDirectory $realtimeRepoDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $realtimeLogDirectory 'stdout.log') -RedirectStandardError (Join-Path $realtimeLogDirectory 'stderr.log')
    @{ launcher_pid = $realtimeProcess.Id; port = $Port; provider = $Provider;
       url = "http://localhost:$Port/?session=$Provider-experiment" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $realtimeLogDirectory 'process.json') -Encoding utf8
    Write-Output "Starting $Provider Realtime on http://localhost:$Port/?session=$Provider-experiment"
    Write-Output "Logs: $realtimeLogDirectory"
} else {
    Push-Location -LiteralPath $realtimeRepoDirectory
    try {
        & $realtimePythonPath @realtimeArguments
        if ($LASTEXITCODE -ne 0) { throw 'Realtime backend exited with an error.' }
    } finally { Pop-Location }
}
