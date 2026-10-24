# Field Companion (placeholder name)

Eyes up. Ears open.

Plain HTML/CSS/JS, no build step. Serve this directory at `/` with the protocol WebSocket at `/ws`.

## Phone and projector

- Phone: `/?session=demo` (the session defaults to `demo`). Tap **Start listening**, allow the microphone, and use earphones. Phones require HTTPS; localhost also works.
- Projector: `/?display=1&session=demo`. Connects automatically with `role=display`, never requests the microphone or GPS, and is silent by default. Designed for 1280×720 and 1920×1080 landscape.
- Projector audio: add `&audio=1`, then click **Start display audio** to unlock WebAudio. The backend enables its half-duplex echo guard for this connection: phone speech is gated while the assistant speaks and for 300 ms afterward; the phone tap button still cancels speech.
- Session and role parameters are forwarded to the WebSocket, including when using the optional `?ws=wss%3A%2F%2Fother-host%2Fws` override. Hello is always first and uses `lang: "en"`.
- English common names are primary, scientific names italicized below. `zh` fields are ignored. Photos appear on observations, heard detections, candidates, and map popups. Missing or failed photos fall back to a local source icon.
- The phone's **Observation map** is collapsed initially. The projector map is always open. Observation coordinates determine markers and bounds; the phone additionally shows its available GPS position as a blue dot. Displays do not invent a phone location.

## Microphones and headset buttons

Open the header **Settings** gear for Speaker/Earphones, **Quiet (only speaks when you tap)**, microphone and capture diagnostics, model/build IDs, headset controls, **Type instead**, CSV export, and **Reset session**. Heard nearby and the map start collapsed on phones. The main controls are Start/End, **Tap to talk** (the next thing you say is addressed to the assistant; tap again to stop speech), and **Pause mic** (stops mic capture/transmission).

After **Start listening** grants permission, use **Microphone** in Settings to choose the phone, USB-C, or headset input. The device ID and label are saved locally. Switching acquires and connects the new input before stopping the old track, preserving the old input on failure; a missing saved or disconnected input falls back to the system default. The small debug line shows actual track settings and AudioContext rate ("not reported" means the browser omitted a setting). Speaker/Earphones controls conversation behavior, not hardware routing.

**Headset-button push-to-talk is opt-in and defaults off.** Only enabling it during a session (or starting with it checked) plays a looping, zero-valued WAV through a separate audio element to keep Media Session active. Disabling it stops the loop and releases Media Session. Supported play/pause/next/previous headset actions act like **Tap to talk**, cancel speech, and show **Headset button → talk**. End releases this media session. This is tap-to-talk, not hold-to-record; unsupported browsers still have the on-screen button.

On Android, compare built-in/USB-C capture with Bluetooth capture while earbuds play speech. Check the debug settings, bird sound pickup, playback quality, physical buttons, unplug/reconnect, and lock-screen behavior. Chrome/Android may ignore constraints, change Bluetooth profiles, or suppress silent media sessions; selecting an input does not guarantee high-quality Bluetooth playback. Keep the page foregrounded for initial tests.

## Protocol mock

From the repository root:

```sh
/data/build/birding-demo/.venv/bin/python web/mock-server.py --port 8877 --host 127.0.0.1
# Phone: http://localhost:8877/?session=demo
# Display: http://localhost:8877/?display=1&session=demo
```

Choose an unused port; do not use the live backend port. Static assets and `/ws` share the chosen port. The mock fetches six default taxon photos from iNaturalist at startup (bounded timeouts, source-icon fallback offline). It sends English example observations with coordinates, detections, captions, candidates, earcons, a 24 kHz tone, and a flush. The tone is not speech.

Mock checklist state is shared per session, persists through reconnects, and clears on `reset` or when the mock process restarts. The mock accepts mic `location` updates and supports suggestion chips. Add `--offline` to skip remote photos for deterministic headless runs. Edit/delete/text changes fan out to all clients in that session. A new mic replaces the previous mic. Displays receive binary audio only with `audio=1`; sending microphone frames from a display is rejected. The mock simulates echo gating but has no BirdNET, speech recognition, identification, or persistent storage. Typed input always adds a mallard, regardless of wording.

## Implementation notes

- Start unlocks a shared AudioContext and requests unprocessed 48 kHz mono input. An AudioWorklet captures PCM16 in 20 ms chunks. A stateful linear JS resampler handles other negotiated rates; it is not a production band-limited resampler.
- Hello includes `client_build` (`2026-10-24-integ1`); an optional differing `server_build` in state shows a reload banner. Absence is supported. Service-worker cache is bumped with this build. Deploy frontend and backend together.
- GPS never delays hello. Late fixes and subsequent movement over 200 m send `location`. A replaced mic (backend reason `Replaced by newer microphone`, or a replacement state/error) stops audio/capture and auto-reconnect; the user must tap the takeover notice to reconnect.
- Reset in Settings and the display header confirms **Clear checklist, suggestions, heard history and conversation?**, sends `reset`, and immediately clears local checklist, suggestions, heard history, candidates, and captions. Server reset clears authoritative session/conversation state.
- Incoming 24 kHz PCM is queued through WebAudio. The >30 s clock guard flushes existing scheduled sources before rescheduling. Flush stops speech and earcons. Disconnected edits are disabled; reconnect uses backoff. Stale mic frames are dropped instead of buffered.
- Screen Wake Lock is best effort. Pause disables mic tracks/transmission. End releases tracks, socket, audio, and wake lock.
- Backend checklist messages are the only source of observation state; counts are not updated optimistically. New/changed cards animate (respects reduced motion).
- eBird export uses 19 columns, no header, exact `group: "Aves"`, and the honest `Incidental` protocol with no invented survey effort. Generic CSV contains English/scientific names and all taxa, preferring item coordinates where available.
- Leaflet 1.9.4 JS, CSS, images, and license are vendored in `vendor/leaflet/`, from `https://unpkg.com/leaflet@1.9.4/`. Only map tiles need an external map service (OpenStreetMap, attributed in the map). Photos also need network access.
- The versioned service worker caches the app shell and Leaflet assets. Offline reload does not restore backend checklist data. No recordings/checklists are stored locally.

## Round 1 verification (2026-10-23)

Headless Chromium against the offline mock at **127.0.0.1:8889**, using fake mic and injected GPS:
- Phone 390×844: no horizontal overflow, collapsed Heard/map, hello-first build ID, late GPS plus >200 m updates (sub-threshold movement suppressed), headset off by default and loop starts/stops only on opt-in.
- Forced mic acquisition failure retained unchanged capture settings; successful switch also checked. Fake track reported 44.1 kHz while AudioContext was 48 kHz (browser-negotiated settings, not a failed capture).
- Reset confirmation/immediate clear and empty state after reconnect; ordinary reconnect; >30 s queued audio stops existing sources; optional build mismatch banner.
- Actual second mock mic replacement stopped reconnects, including after `online`; explicit reconnect worked. Replacement error message also checked. No runtime exceptions in the final regression run.
- Display header reset and silent automatic connection checked at 1280×720.
- `node --check web/app.js`, `node --check web/sw.js`, Python AST parse of mock passed.

Current screenshots: `screenshot.png` (390×844 phone, drawer closed) and `screenshot-drawer.png` (drawer open). Remote photos skipped via `--offline`.

## Earlier verification evidence (not rerun in round 1)

`self-test-results.json` records an actual headless Chromium CDP run against mock port **8877** (not the live backend):

- Phone: 390×844, fake microphone/GPS, hello-first English framing, session routing, 48 kHz/1920-byte mic frames, flush, reconnect, edit/delete/text, quiet/tap/pause, both CSV exports, GPS marker, and offline shell reload.
- Display: 1280×720 screenshot; four loaded species photos and map markers, both caption roles, session hint, shared-session updates, no mic requests or binary uplink, and silent-by-default WebAudio behavior.
- 1920×1080 layout checked for page overflow and larger captions. Opt-in display audio tested with a user gesture, including flush and zero mic requests.
- No runtime exceptions in either mode. Syntax checks: `node --check` for every JS file (including Leaflet), Python AST parsing for the mock and test.

Screenshots: `screenshot.png` (phone) and `screenshot-display.png` (1280×720 projector).

To rerun: start a **fresh** mock on 8877 and Chrome with `--headless=new --no-sandbox --use-fake-device-for-media-stream --use-fake-ui-for-media-stream --remote-debugging-port=9223`, then run `/data/build/birding-demo/.venv/bin/python web/self-test.py`. The test denies real downloads and examines CSV Blobs directly. Its sessions should be fresh for deterministic initial counts.

## Camera and observation follow-up

The phone now has **Look closer**: separate camera permission, rear-facing preview,
1–4× digital zoom, tap-to-select crop, three-frame sharpness selection and JPEG
upload. It renders the current Observation, top-three candidates, recent evidence,
expert hints, Look again, New subject, confirmation/count and discard. The display
mirrors the observation without requesting media. See [CAMERA.md](../docs/CAMERA.md)
for the protocol, real vision-service requirement and isolated browser test.

Late GPS is sent to the backend through `location` updates. Failed microphone changes
retain the previous stream, and a replaced phone stops instead of evicting the new
mic through reconnect. Capture diagnostics are available in **Settings**; Look stays on the main screen.

## Integration verification (2026-10-24)

The drawer and primary-screen Look UI are combined. Build is
`2026-10-24-integ1`, with a new shell cache including `camera.js`. Reset also
cancels camera uploads and clears mirrored observation evidence. The camera
headless fixture now uses `watchPosition` and the explicit takeover button.
Both browser suites passed with fake media and no real vision backend; evidence
is retained in ignored `out/integration/` (camera screenshots in
`out/camera-checks/`). Historical screenshots/results above are not overwritten.
The legacy self-test's first fresh-mock run passed; subsequent exploratory
reruns hit stale-session/DevTools timing failures, not counted as passes.

## Known gaps

Physical Android/iOS camera and the real BioCLIP → GPT → Higgs loop still need hardware testing. Mobile backgrounding, device audio routes, ignored capture constraints, and autoplay after interruptions also need hardware checks; keep the app foregrounded and use wired earphones. Map tiles and photos require internet; imagery may fail independently of the session. Captions show the latest utterance for each speaker, not an archived conversation. Long checklists/timelines/captions scroll within display panels. Offline checklist persistence, full survey-effort collection and eBird taxonomy validation remain open.
