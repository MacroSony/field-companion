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

## Camera development

### Install and start BioCLIP on Windows

Use the dedicated CPU environment, leaving the existing voice environment alone:

```powershell
.\.venv\Scripts\python.exe -m venv .venv-vision
.\.venv-vision\Scripts\python.exe -m pip install --upgrade pip
.\.venv-vision\Scripts\python.exe -m pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv-vision\Scripts\python.exe -m pip install -r requirements-vision.txt
.\backend\start_vision.ps1
```

The first start downloads the model and species embeddings; later starts reuse
ignored `out/hf-cache/`. An existing `HF_HOME` setting overrides this location.
The service uses the operating system's temporary directory, including on Windows.
It listens only at `http://127.0.0.1:8795`; the main backend already defaults to
that address. Startup finishes only after model load and warmup.

To start hidden in the background instead, run
`.\backend\start_vision.ps1 -Background`. Logs and the launcher PID are written
to ignored `out/vision-service/`. This is a development process, not a Windows
service or a boot-time task. If the port is occupied, the script leaves that
process alone.

Check readiness with `Invoke-RestMethod http://127.0.0.1:8795/health`; expect
`loaded: true` and `model: imageomics/bioclip-2`. Test a local JPEG with:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B backend/vision_smoke.py bird.jpg --report out/vision-check.json
```

The smoke check calls the running model and saves ranked species, timing and
crop information. It does not load another model or claim field accuracy.
The pinned CPU pair follows the [official PyTorch installation commands](https://pytorch.org/get-started/previous-versions/);
BioCLIP uses the [official pybioclip package](https://imageomics.github.io/pybioclip/).

### Camera interface and browser checks

Open **Look closer** after starting the session. The camera has separate permission
and leaves microphone input running when it is closed. Real identification also
requires the BioCLIP service; see [CAMERA.md](CAMERA.md) for setup and limitations.

For credential-free browser integration, start
`.\.venv\Scripts\python.exe -X utf8 -B web/camera-test.py --serve` (port 8878),
then an isolated Chromium with the fake-media flags below and CDP port 9224.
Run `.\.venv\Scripts\python.exe -X utf8 -B web/camera-test.py` to validate the
real frame/observation routes with a fixture classifier. This does not make
Higgs, OpenAI, BirdNET or iNaturalist requests. Screenshots/results go to ignored
`out/camera-checks/`. Start each run with a fresh browser profile.

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
$profileDirectory = Join-Path (Get-Location).Path ('out\c-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
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
Keep the profile path short on Windows: nested Service Worker cache files can
exceed filesystem path limits and prevent the offline shell from installing.
The unmodified test copy puts its screenshots and JSON results in ignored
`out/local-checks/`, preserving the committed reference screenshots in `web/`.

The separate `backend/selftest.py` now includes photo-look/confirmation and optional
chorus tests. It requires a valid Boson key, network access and the prototype's
external photo/recording fixtures (not bundled for licensing). Set
`FIELD_COMPANION_TEST_DATA` or pass `--data-dir` to their directory; the photo
default is `img/cardinal.jpg`. `--image` and `--chorus-file` override individual
fixtures. Missing fixtures print a clear skip before starting a server/API call.
It is not part of the credential-free tests above. Both `--serve` and `--chorus`
remain available after integration.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B backend/selftest.py --help
# Portable, opt-in GPT check with synthetic observation data (two API calls):
.\.venv\Scripts\python.exe -X utf8 -B backend/expert_smoke.py --live
```

Use UTF-8 mode for this test on Windows because it reads the UTF-8 probe source.
