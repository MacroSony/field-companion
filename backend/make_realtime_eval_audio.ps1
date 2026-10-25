param([string]$OutputDirectory = 'out/realtime-eval/audio')
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRepo
$taskOutput = [IO.Path]::GetFullPath((Join-Path $taskRepo $OutputDirectory))
New-Item -ItemType Directory -Path $taskOutput -Force | Out-Null
$taskJson = & '.\.venv\Scripts\python.exe' -X utf8 -B -c 'import json,sys; sys.path.insert(0,"backend"); from realtime_eval_cases import CASES; print(json.dumps([{ "id":c.id,"text":c.text } for c in CASES if c.audio],ensure_ascii=True))'
if ($LASTEXITCODE -ne 0) { throw 'Cannot read synthetic speech scripts.' }
$taskCases = $taskJson | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$taskSpeech = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $taskFormat = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
        48000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    foreach ($taskCase in $taskCases) {
        $taskCulture = if ($taskCase.text -match '[\u3400-\u9fff]') { 'zh-CN' } else { 'en-US' }
        $taskSpeech.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female,
            [System.Speech.Synthesis.VoiceAge]::Adult, 0, [Globalization.CultureInfo]::GetCultureInfo($taskCulture))
        $taskSpeech.SetOutputToWaveFile((Join-Path $taskOutput ($taskCase.id + '.wav')), $taskFormat)
        $taskSpeech.Speak($taskCase.text)
        $taskSpeech.SetOutputToNull()
        Write-Output ($taskCase.id + ': ' + $taskCulture + ', ' + $taskSpeech.Voice.Name)
    }
} finally { $taskSpeech.Dispose() }
