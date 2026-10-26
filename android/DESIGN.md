# Field Companion Android app: design draft

Status: draft (2026-10-26). Proceed only if the Field Probe real-device test passes, i.e. USB-C input and A2DP output work together without falling into call mode. Protocol notes come from reading `origin/feat/gpt-realtime` (52f182f) and still need to be re-checked against whatever branch we build on.

## Why not just wrap the web app

Android WebView, and a Trusted Web Activity even more so, uses Chromium's audio stack. That means:

- `getUserMedia` with echo cancellation can push the system into communication mode, and Bluetooth then drops to HFP/SCO. The team's README already notes this.
- WebView JavaScript is paused when the activity stops, so recording ends once the screen is off or the phone goes into a pocket.

The UI can stay web. Audio has to be native.

## Architecture

```
┌──────────── phone ─────────────┐            ┌────────── backend ──────────┐
│ AudioService (foreground, mic) │  WS /ws    │ Session(id)                 │
│  USB-C RX ─▶ AudioRecord 48k ──┼─ role=mic ─▶ ingest_audio → STT/router   │
│  A2DP ◀── AudioTrack 24k ◀─────┼── binary ──┤ phone_audio (24 kHz PCM16)  │
│  playback reports, markers     │  + JSON    │                             │
├────────────────────────────────┤            │                             │
│ WebView (existing web/ UI)     │  WS /ws    │ phone_json → all clients    │
│  checklist, cards, Look camera ┼role=display▶ text/edit/tap/look/...      │
└────────────────────────────────┘            └─────────────────────────────┘
```

Both connections join the same `session`, with `transport=websocket`. The server already distinguishes `mic` clients from `display` clients, so the native service is just another mic client and the WebView is a display client with extra controls.

## Wire protocol used by the mic client (current web/app.js behaviour)

- Connect to `wss://<host>/ws?session=<id>&role=mic`. Use `binaryType` arraybuffer.
- First message is `hello`: `{client_build, lang, playback_protocol: 1, earphones, lat?, lon?}`.
- **Uplink**: binary PCM16 little-endian, **mono, 48 kHz**. Drop audio rather than queue it when the socket backs up (the web client drops above ~192 KB buffered).
- **Downlink**: binary PCM16 little-endian, **mono, 24 kHz**, assistant speech, queued back to back.
- **Playback reports**: `{type: 'playback', active, response_id, drained}`. The server uses these for echo gating and turn-taking, so they have to be accurate.
- **Controls** (mic role): `mic {on}`, `audio_mode {earphones}`, `output_mute {muted}`, `mode {quiet}`, `tap`, `text`, `reset`, `location {...}`.
- **Display allowlist** today: `hello, text, edit, delete, tap, suggestion_action, playback, look, observation_action, investigation_action, reset`. Anything else from a display client is rejected with "Display cannot control microphone or mode".

## Changes needed

### Server (keep small; the teammate is actively editing server.py)

1. Let the app UI change mute, quiet mode and mic on/off. Either the native service relays these over its mic socket via a JS bridge, which needs no server change, or the server gets a trusted "app display" flag. **Prefer the relay.**
2. Accept a `native` client tag in `hello` for logging (`client_build: "android-x.y"`).
3. Later, split stereo: the receiver sends L = collar mic (speech to STT) and R = outward mic (to BirdNET). This needs a stereo uplink format or a second stream. **v1 sends L only.**

### Web (web/app.js)

Add an `?shell=android` mode: role=display, but with full controls and Look/camera enabled, no `getUserMedia` audio, and no assistant playback (native does that). Controls that need the mic role go through the `window.FieldNative` bridge.

### Android

- **AudioService**: grows out of `RecorderService`. It adds a WebSocket client (OkHttp, our first third-party dependency), a 24 kHz streaming AudioTrack (USAGE_MEDIA) with accurate playback/drained reporting, interruption (flushing the queue), reconnect with backoff, and optional local WAV logging (the Probe recorder) for field data.
- **MainActivity**: WebView pointing at the server URL with `?shell=android&session=…`. It handles camera and geolocation permissions in `WebChromeClient` and exposes a small JS bridge (`start`, `stop`, `mute`, `quiet`, `tap`, `status`).
- **Settings**: server URL, session id, input/output device, which channel goes to speech.
- **Probe screen** stays in the app as an audio diagnostics page.

## Out of scope for v1

- The LiveKit/WebRTC transports. A native LiveKit client would need their Android SDK. The websocket transport should still cover both the Higgs and direct GPT backends; check this on the chosen base branch.
- iOS.
- Offline use. The backend still runs on the laptop behind a tunnel.

## Milestones

- **M0**: Field Probe on the real phone (README test plan). Go/no-go for this design.
- **M1**: Native mic client and WebView shell running on the emulator against a local server, with a WAV-file "fake mic" so tests are repeatable.
- **M2**: Real phone with DJI Mic Mini 2 + Enco: talk, log a bird, interrupt, screen off.
- **M3**: First outing, with data recorded for the addressee research.

## Open questions

- Base branch: wait for `feat/gpt-realtime` to land in main, or branch from it now.
- What should the earbud gestures do: mark only, or also push-to-talk / interrupt?
- GPS: phone location through the native service (works with the screen off), or keep the browser geolocation?
