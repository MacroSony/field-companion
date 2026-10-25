# Speaker echo and WebRTC audio experiment

This opt-in experiment uses browser WebRTC audio processing for GPT Realtime
voice and keeps the existing backend in charge of tools, checklist changes,
camera confirmations and background investigations. The normal WebSocket audio
path and its speaker/tap mode remain available. This is a testable first step,
not a claim that speaker echo has been eliminated on physical phones.

## Start a separate test server

Use the existing local OpenAI key and installed project dependencies:

```powershell
.\backend\start_realtime.ps1 -Provider openai -Port 8802 -WebRTC -Background
```

Open `http://localhost:8802/?transport=webrtc&session=audio-lab`. A phone needs
an HTTPS endpoint forwarding to this port. A temporary tunnel still depends on
this computer and backend remaining on. Use a different session from other
open phones. Normal audio is available by removing `transport=webrtc` and using
a different session name. Do not change transport inside an active session.

The experimental mode starts with voice interruption enabled. Settings lets
you switch to **WebRTC · tap to interrupt** if the phone still hears itself.
Microphone selection in the experiment uses the device selected before Start;
end the session before changing devices.

## What changed

- Speech travels directly between the browser and OpenAI over WebRTC. A
  backend-only sideband WebSocket owns configuration and tool execution. SDP
  setup requires a short-lived, single-use capability issued only to the
  current microphone connection; the API key never enters the browser.
- Remote voice and cached task cues share the app's output gain. Remote voice
  is never also forwarded as PCM through the app socket. Output mute applies
  to both paths.
- Experimental speech input requests browser echo cancellation, noise
  suppression and automatic gain control. Sustained VAD for 180 ms can cancel
  a speaking response before transcription completes; the application does
  not cancel a response for a short VAD burst alone. WebRTC playout can also
  report an earlier buffer clear; that is recorded separately and is not
  proof the 180 ms application hold controlled audible interruption. This
  duration is a starting point, not measured phone performance.
- Cancellation clears the provider's WebRTC output buffer and flushes local
  audio. The provider owns WebRTC playout truncation. A late event from a
  cancelled response cannot resume it. Investigations continue running.
- Cancellation before `response.created` is matched to its ordered server
  acknowledgement. A missing acknowledgement or failed control/media
  connection ends the experimental call explicitly and requires a new Start.
- A second physical-input request attempts 48 kHz environmental capture with
  processing disabled, while the voice track retains AEC. It is not a clone
  of the processed speech track. If separate capture cannot be verified,
  BirdNET listening is visibly unavailable in this mode; voice still works.
  If voice AEC itself cannot be verified, the experiment stops.
- Browser settings are evidence of requested/reported processing, not proof
  of a raw hardware microphone or successful acoustic echo cancellation.
  BirdNET still masks assistant-playback windows and their existing tail.
  This version does not classify birdsong throughout assistant speech.

The normal speaker mode is still the reliable fallback. The experimental
mode deliberately avoids rejecting speech just because its words resemble
the assistant's: real users can repeat those words. Acoustic validation must
establish whether this is safe on a given device.

## Local diagnostic recording

After Start, open Settings → **Audio experiments and diagnostics** →
**Record audio diagnostic**. Reproduce the issue, stop recording, then save.
Recording is opt-in, held in browser memory and bounded to the latest 60 s
of audio and 5,000 events. The exported HTML is a self-contained offline player
with WAV and timeline JSON downloads. It contains no external scripts or
network requests. Ending the session stops capture; Clear removes the memory
copy. Refreshing the page discards unsaved diagnostics.

The stereo file uses one AudioContext clock:

| Channel | Contents |
| --- | --- |
| Left | Captured voice microphone; browser/system processing may already be applied |
| Right | App playback reference after app gain/mute, before hardware speaker volume |

The right channel is a reference, not a recording of what the physical speaker
actually emitted. Browser event receipt times share the local audio clock;
server monotonic timestamps are a separate clock. Worklet chunks are 20 ms.

The export includes spoken captions, capture settings without device IDs,
VAD/output-buffer events, playback flushes and a whitelist of backend status
events. SDP, keys, setup tokens, coordinates and tool arguments are excluded.
The recording feature does not upload or persist the diagnostic file. Normal
voice still goes to the chosen provider, and the app still uses its normal
backend logs. Treat a saved diagnostic as a private voice recording.

## Physical-phone validation still required

Use the phone's speaker at ordinary volume, not headphones, for the echo test.
Record short runs so the failure stays inside the saved 60-second window:

1. Ask a question, stay silent through the answer, and repeat several times.
   Any user transcript or interruption during silence is a failure to inspect.
2. While it speaks, ask a different ordinary question. Try short phrases and
   words similar to its own answer; no special “stop” command should be needed.
3. Queue an investigation and interrupt a foreground answer. Confirm the
   investigation still completes and its notification waits for a free turn.
4. Repeat with low/normal/high speaker volume, phone near/far from the face,
   and an open camera. Save device/browser version and the failing recording.
5. Play the same bird recording with the assistant idle in normal versus
   experimental mode. Compare detected species/confidence, and check the
   environmental-capture status. Do not interpret fake-input tests as BirdNET
   accuracy evidence.

Initial engineering targets: zero false interruptions in 10 minutes of
assistant-only playback, at least 95% successful real interruptions across a
documented phrase set, and interruption within 500 ms at normal volume.
These are targets, not achieved results. If a browser still produces echo,
keep tap mode and use the aligned recording to decide between capture/output
routing changes, a native WebRTC audio module or another verified DSP path.

## Automated checks

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s backend -p 'test*.py' -q
node --test web/test_audio_lab.cjs
```

Backend tests cover session ownership, bounded signaling, secret redaction,
duplicate-audio prevention, voice/environment separation, interruption,
cancel-before-created recovery and preserving investigations. Frontend tests
cover bounded stereo recording, safe offline export, cancelled starts, voice
gating and fail-closed AEC checks.

The optional `backend/webrtc_smoke_server.py` is **loopback-only test code**.
It replaces taxonomy, BirdNET and research with fixtures while testing real
OpenAI media, transcription, routing and tools. Never tunnel/deploy it. The
Playwright smoke test in `backend/webrtc_browser_smoke.cjs` needs local Chrome,
Playwright, synthetic 48 kHz `browser-mic.wav` (10 s silence, “Can you hear me?”,
45 s silence) and `barge.wav` (“Wait, how many birds are on my checklist?”) under
`out/audio-lab/`. `FIELD_PLAYWRIGHT_MODULE` can point to Playwright's directory.
It runs real, billable API calls and writes ignored reports/diagnostics there.
Its clean synthetic microphone cannot validate physical acoustic echo.

The initial real-API/Chrome run passed seven checks: microphone transcription
and reply, a natural spoken interruption, checklist tool execution, queued
research, an immediate progress follow-up while cancelling a response, both
diagnostic audio channels, and the offline replay export. See the
[reviewed smoke report](benchmarks/webrtc_audio_smoke.json). Taxonomy and
research were fixtures and BirdNET was disabled; neither field recognition
accuracy nor physical speaker echo was tested by that run.

On Windows, generate the speech locally, start the private fixture server in
one terminal, and run Chrome automation in another:

```powershell
.\backend\make_audio_lab_fixtures.ps1
.\.venv\Scripts\python.exe -X utf8 -B backend/webrtc_smoke_server.py --port 8803
# In a second terminal, after the fixture server is listening:
node backend/webrtc_browser_smoke.cjs
```

## Protocol references

- [OpenAI WebRTC setup](https://developers.openai.com/api/docs/guides/voice-webrtc?voice-api=realtime)
- [Server-side Realtime control](https://developers.openai.com/api/docs/guides/voice-server-controls?voice-api=realtime)
- [Interruption and truncation](https://developers.openai.com/api/docs/guides/realtime-conversations#interruption-and-truncation)
- [Browser echoCancellation constraints](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackConstraints/echoCancellation)
