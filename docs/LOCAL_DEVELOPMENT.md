# Local development

Run these commands from the repository root in PowerShell. Use Python 3.12;
the setup command's `python` must refer to that interpreter. Check
`python --version` first; Python 3.7 cannot run the existing async tests.
The frontend is plain HTML/CSS/JavaScript and has no package-install or build step.

## Install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On the current development machine, `.venv` already uses Python 3.12. Call its
interpreter directly, so activation and the system Python version do not matter.
The virtual environment is ignored by Git.

To enable local BirdNET inference as well:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-birdnet.txt
```

The Windows setup uses TensorFlow 2.19.1 for the TFLite interpreter expected by
`birdnetlib`. The current live microphone path passes PCM buffers directly to
BirdNET; file-format conversion is a separate concern.

## Run the frontend with simulated data

```powershell
.\.venv\Scripts\python.exe -B web/mock-server.py --host 127.0.0.1 --port 8877
```

- Phone layout: <http://localhost:8877/?session=demo>
- Shared projector layout: <http://localhost:8877/?display=1&session=demo>

The mock needs no Boson key and does not recognize speech or birds. It supplies
example observations, candidate cards, detections and audio so UI work can
continue independently. Stop it with Ctrl+C in its terminal.

## Configure the Boson key and run the backend

The backend reads `BOSON_API_KEY` from the process environment, or a file at
`~/.config/boson/api_key`. On Windows this is
`%USERPROFILE%\.config\boson\api_key`.

Put only the complete key in that file, with no quotes or variable assignment.
Keep the file outside the repository. An abbreviated key ending in an ellipsis
will not work. Boson credentials do not use `.env` files. The optional GPT expert
reads `OPENAI_API_KEY` and `EXPERT_*` settings from the ignored `.env.local`;
see [the expert integration](EXPERT.md).

```powershell
.\.venv\Scripts\python.exe -B backend/server.py --host 127.0.0.1 --port 8791
```

Open <http://localhost:8791/>. The same process serves static assets and `/ws`.
Localhost supports microphone access; a phone accessing another computer needs
HTTPS. The server's session state is in memory.

## Existing tests

Offline backend regressions do not need API credentials:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s backend -p 'test_*.py' -v
node --check web/app.js
node --check web/audio-worklet.js
node --check web/sw.js
```

The existing browser test expects the mock on port 8877 and an isolated Chromium
instance with a debugging endpoint on port 9223. Use a separate browser profile
and fake media devices. For example, in a second PowerShell terminal:

```powershell
New-Item -ItemType Directory -Force out\local-checks | Out-Null
$profileDirectory = Join-Path (Get-Location).Path ('out\local-checks\chrome-profile-' + [guid]::NewGuid().ToString('N'))
$chromeArguments = @(
    '--headless=new',
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=9223',
    ('--user-data-dir="' + $profileDirectory + '"'),
    '--no-first-run',
    '--no-default-browser-check',
    'about:blank'
)
$testBrowser = Start-Process -FilePath "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" -ArgumentList $chromeArguments -WindowStyle Hidden -PassThru
try {
    $browserReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            Invoke-WebRequest 'http://127.0.0.1:9223/json/version' -UseBasicParsing -TimeoutSec 1 | Out-Null
            $browserReady = $true
            break
        } catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $browserReady) { throw 'Test browser did not become ready on port 9223.' }
    Copy-Item web\self-test.py out\local-checks\self-test.py
    .\.venv\Scripts\python.exe -B out/local-checks/self-test.py
    if ($LASTEXITCODE -ne 0) { throw 'Browser tests failed.' }
} finally {
    if (-not $testBrowser.HasExited) { Stop-Process -Id $testBrowser.Id }
}
```

Start with a fresh mock process and browser profile for each full browser run.
The unmodified test copy puts its screenshots and JSON results in ignored
`out/local-checks/`, preserving the committed reference screenshots in `web/`.

The separate `backend/selftest.py` now includes photo-look/confirmation and optional
chorus tests. It requires a valid Boson key, network access and the prototype's
photo/recording fixtures, which currently use `/data/build/birding-demo` paths.
Those fixture paths need adapting before running that live test on Windows.
It is not part of the credential-free tests above. Both `--serve` and `--chorus`
remain available after integration.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B backend/selftest.py --help
# Portable, opt-in GPT check with synthetic observation data (two API calls):
.\.venv\Scripts\python.exe -X utf8 -B backend/expert_smoke.py --live
```

Use UTF-8 mode for this test on Windows because it reads the UTF-8 probe source.
