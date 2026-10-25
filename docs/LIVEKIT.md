# LiveKit-managed GPT voice

This optional path lets LiveKit Agents own speech playback, turn detection,
interruption and recovery. The existing camera confirmation, checklist,
BirdNET suggestions and queued expert investigations stay in the application.
The standard Higgs and direct GPT transports remain available.

## Local setup (Windows)

Use a separate environment: LiveKit's protobuf requirement conflicts with the
TensorFlow version used by BirdNET. Do not install this optional SDK into the
original `.venv`.

```powershell
py -3.12 -m venv .venv-livekit
.\.venv-livekit\Scripts\python.exe -m pip install -r requirements-livekit.txt
powershell -ExecutionPolicy Bypass -File backend/start_livekit.ps1
```

The original `.venv` must already have the BirdNET dependencies described in
[LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md). Reuse the existing local OpenAI API
configuration. The launcher starts:

- the application on `http://localhost:8805` using `.venv-livekit`;
- a private, authenticated BirdNET worker on `127.0.0.1:8811` using `.venv`.

If `.venv-vision` is installed, it also starts (or reuses) BioCLIP on
`127.0.0.1:8795` and waits for model readiness before starting the voice app.
Photo identification needs this separate service; microphone and photo upload
can work even when the classifier is stopped. See the camera setup in
[LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md). Use `-SkipVision` for a deliberate
voice-only run. A custom `VISION_URL` leaves that service under your control.

The launcher refuses to replace an existing listener. Logs and launcher PIDs are
under ignored `out/livekit/`. The worker is loopback-only and does not have a
public tunnel.

Create a LiveKit Cloud project, then save its project WebSocket URL, API key and
API secret through the local setup page. To open the private setup link:

```powershell
$setupFragment = (Get-Content out/livekit/setup-fragment.txt -Raw).Trim()
Start-Process "http://localhost:8805/setup/livekit#$setupFragment"
```

The page accepts writes only from this computer with the private setup
capability. It saves the three values to ignored `.env.livekit.local`, clears
the form and never sends project credentials to the phone. It refuses project
changes during a LiveKit call. Do not paste project credentials into chat or
commit them. For deployment, standard `LIVEKIT_URL`, `LIVEKIT_API_KEY` and
`LIVEKIT_API_SECRET` environment variables are also supported.

Open `http://localhost:8805/?transport=livekit&session=livekit-phone` and start
listening. A phone needs an HTTPS URL forwarding to port 8805. It also needs
network access to LiveKit Cloud. A Cloudflare tunnel alone does not host the
application: the local app and worker must stay running.

## Speech and tools

There are two GPT Realtime connections per active call:

1. LiveKit's OpenAI plugin receives the processed microphone track and plays
   approved speech through one native WebRTC audio track. The browser requests
   acoustic echo cancellation. LiveKit uses Silero VAD, adaptive interruption,
   and resumption after false interruptions. GPT server turn detection is off,
   so a second system cannot independently cancel or start a spoken answer.
2. The existing text-only router receives accepted transcripts and runs the
   existing tools. It receives no voice PCM and produces no audible answer.
   Authoritative tool results are passed to the LiveKit voice session for a
   spoken response. The `on_user_turn_completed` hook suppresses the voice
   model's automatic replies, preserving silent logging and ignoring chatter.

Cached acknowledgement recordings use `AgentSession.say`, on the same speech
track as GPT. Reconnection removes stale audio elements. Manual interruption
also uses the framework; it does not cancel background investigations.

Browser signaling stays on the existing application WebSocket. The server
issues a single-use, 90-second capability to the current phone, then a
five-minute room-scoped JWT that allows microphone publication only. Project
API credentials stay on the server. Disconnecting ends the agent and deletes
its room on a best-effort basis. Audio recording is off by default.

BirdNET attempts a separate unprocessed 48 kHz microphone capture. If the
browser cannot verify that separate raw input, the UI reports BirdNET as
unavailable rather than feeding it the processed voice channel. Assistant
playback and a short tail are excluded from BirdNET input.

## Validation

Run the backend regressions in the original environment, and SDK contract
tests in the isolated environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s backend -p 'test*.py' -q
.\.venv-livekit\Scripts\python.exe -m unittest discover -s backend -p test_livekit_sdk.py -v
node --test web/test_livekit_audio.cjs web/test_audio_lab.cjs
```

The real-GPT smoke test uses a synthetic speech WAV and an in-memory audio sink.
It verifies transcription, suppression of automatic replies, and playback of an
explicitly approved answer. It makes billed OpenAI API calls:

```powershell
.\.venv-livekit\Scripts\python.exe backend/livekit_sdk_smoke.py
```

Its JSON report explicitly distinguishes this from a phone echo test and from
LiveKit Cloud adaptive-inference testing. Generate its local synthetic WAV first
with `backend/make_audio_lab_fixtures.ps1`, or supply `--wav` with a mono PCM16
recording of "Can you hear me?". This is a local diagnostic, not an offline regression.
The initial real-API run passed these three checks with no SDK runtime errors;
see the [reviewed report](benchmarks/livekit_sdk_smoke.json).

`backend/livekit_app_smoke.py` additionally connects the real text-only GPT
router to the real LiveKit-managed voice session, using accepted text inputs
and in-memory checklist/camera state. It verifies silent logging, a grounded
checklist answer, the explicit camera Start handshake, and a failed-photo retry
that waits for a new confirmation. Taxonomy and the classifier failure are fixtures;
it does not test recognition. It also makes billed API calls. The initial run
passed all four checks with no SDK runtime errors; see the
[application report](benchmarks/livekit_app_smoke.json).

After saving Cloud credentials and starting the application, run
`backend/livekit_cloud_smoke.py --port 8805` in `.venv-livekit` to test the real
room transport. It uses the generated microphone-check and barge WAV fixtures,
publishes a synthetic microphone through the RTC SDK and receives real agent
audio. It creates an isolated temporary app session and closes it afterward.
This also makes billed API calls. The initial run passed signaling, spoken
question/answer and a spoken follow-up during a reply. The server log showed
adaptive initialization, an interrupted reply, and no fallback to VAD. See the
[Cloud smoke report](benchmarks/livekit_cloud_smoke.json). Its follow-up was
split into two transcripts before the checklist answer, so this is not a claim
of perfect segmentation. The test does not evaluate browser AEC or phone echo.

## Limits and rollback

Adaptive interruption requires LiveKit Cloud credentials and service access.
The SDK can fall back to VAD interruption when its adaptive service fails; check
the agent log for that message. Adaptive interruption filters conversational
backchannels; it is not itself acoustic echo cancellation. Actual speaker echo
still depends on the phone/browser's audio processing and output route.
Separate raw and processed captures also need validation on the target phone.
Local and synthetic tests cannot establish that the physical echo bug is fixed.

The router and voice model use two API connections, with corresponding latency
and usage. This is an integration of the mature media stack while retaining the
current routing behavior; it does not yet consolidate both models into one
tool-calling agent. Browser captions currently show final accepted transcripts.

To return to standard audio, use the settings link **Use standard audio**, or
remove `transport=livekit` from the URL and use a separate session ID. Existing
servers started without `--enable-livekit` do not load the optional SDK or expose
the local setup form.

References: [LiveKit adaptive interruption](https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/),
[LiveKit OpenAI Realtime plugin](https://docs.livekit.io/agents/models/realtime/openai/),
[LiveKit turn tuning](https://docs.livekit.io/agents/logic/turns/tuning/).
