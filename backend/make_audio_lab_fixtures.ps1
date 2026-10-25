param()
$ErrorActionPreference = 'Stop'
$labRepo = Split-Path -Parent $PSScriptRoot
$labOutput = Join-Path $labRepo 'out/audio-lab'
New-Item -ItemType Directory -Force -Path $labOutput | Out-Null
Add-Type -AssemblyName System.Speech
$labSpeech = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $labFormat = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
        48000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $labSpeech.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female,
        [System.Speech.Synthesis.VoiceAge]::Adult, 0,
        [Globalization.CultureInfo]::GetCultureInfo('en-US'))
    foreach ($labCase in @(
        @{name='microphone-check'; text='Can you hear me?'},
        @{name='barge'; text='Wait, how many birds are on my checklist?'})) {
        $labSpeech.SetOutputToWaveFile((Join-Path $labOutput ($labCase.name + '.wav')), $labFormat)
        $labSpeech.Speak($labCase.text)
        $labSpeech.SetOutputToNull()
    }
} finally { $labSpeech.Dispose() }
Push-Location -LiteralPath $labRepo
try {
    @'
import wave
from pathlib import Path
folder = Path('out/audio-lab')
with wave.open(str(folder / 'microphone-check.wav'), 'rb') as source:
    parameters, frames = source.getparams(), source.readframes(source.getnframes())
with wave.open(str(folder / 'browser-mic.wav'), 'wb') as target:
    target.setparams(parameters)
    target.writeframes(bytes(48000 * 2 * 10) + frames + bytes(48000 * 2 * 45))
print('Created synthetic microphone and interruption fixtures under out/audio-lab')
'@ | & '.\.venv\Scripts\python.exe' -X utf8 -B -
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create synthetic audio fixtures' }
} finally { Pop-Location }
