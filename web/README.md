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

After **Start listening** grants permission, use **Microphone** near Speaker/Earphones to choose the phone, USB-C, or headset input. The device ID and label are saved locally. Switching stops the old input and reconnects to the same capture worklet/session; a missing saved or disconnected input falls back to the system default. The small debug line shows actual track settings and AudioContext rate ("not reported" means the browser omitted a setting). Speaker/Earphones controls conversation behavior, not hardware routing.

Start also plays a looping, zero-valued WAV through a separate audio element to keep Media Session active. Supported play/pause/next/previous headset actions act like **Tap to talk**, cancel speech, and show **Headset button → talk**. End releases this media session. This is tap-to-talk, not hold-to-record; unsupported browsers still have the on-screen button.

On Android, compare built-in/USB-C capture with Bluetooth capture while earbuds play speech. Check the debug settings, bird sound pickup, playback quality, physical buttons, unplug/reconnect, and lock-screen behavior. Chrome/Android may ignore constraints, change Bluetooth profiles, or suppress silent media sessions; selecting an input does not guarantee high-quality Bluetooth playback. Keep the page foregrounded for initial tests.

## Protocol mock

From the repository root:

```sh
/data/build/birding-demo/.venv/bin/python web/mock-server.py --port 8877 --host 127.0.0.1
# Phone: http://localhost:8877/?session=demo
# Display: http://localhost:8877/?display=1&session=demo
```

Choose an unused port; do not use the live backend port. Static assets and `/ws` share the chosen port. The mock fetches six default taxon photos from iNaturalist at startup (bounded timeouts, source-icon fallback offline). It sends English example observations with coordinates, detections, captions, candidates, earcons, a 24 kHz tone, and a flush. The tone is not speech.

Mock checklist state is shared per session, persists through reconnects, and resets when the mock process restarts. Edit/delete/text changes fan out to all clients in that session. A new mic replaces the previous mic. Displays receive binary audio only with `audio=1`; sending microphone frames from a display is rejected. The mock simulates echo gating but has no BirdNET, speech recognition, identification, or persistent storage. Typed input always adds a mallard, regardless of wording.

## Implementation notes

- Start unlocks a shared AudioContext and requests unprocessed 48 kHz mono input. An AudioWorklet captures PCM16 in 20 ms chunks. A stateful linear JS resampler handles other negotiated rates; it is not a production band-limited resampler.
- Incoming 24 kHz PCM is queued through WebAudio. Flush stops speech and earcons. Disconnected edits are disabled; reconnect uses backoff. Stale mic frames are dropped instead of buffered.
- Screen Wake Lock is best effort. Pause disables mic tracks/transmission. End releases tracks, socket, audio, and wake lock.
- Backend checklist messages are the only source of observation state; counts are not updated optimistically. New/changed cards animate (respects reduced motion).
- eBird export uses 19 columns, no header, exact `group: "Aves"`, and the honest `Incidental` protocol with no invented survey effort. Generic CSV contains English/scientific names and all taxa, preferring item coordinates where available.
- Leaflet 1.9.4 JS, CSS, images, and license are vendored in `vendor/leaflet/`, from `https://unpkg.com/leaflet@1.9.4/`. Only map tiles need an external map service (OpenStreetMap, attributed in the map). Photos also need network access.
- The versioned service worker caches the app shell and Leaflet assets. Offline reload does not restore backend checklist data. No recordings/checklists are stored locally.

## Verification evidence

`self-test-results.json` records an actual headless Chromium CDP run against mock port **8877** (not the live backend):

- Phone: 390×844, fake microphone/GPS, hello-first English framing, session routing, 48 kHz/1920-byte mic frames, flush, reconnect, edit/delete/text, quiet/tap/pause, both CSV exports, GPS marker, and offline shell reload.
- Display: 1280×720 screenshot; four loaded species photos and map markers, both caption roles, session hint, shared-session updates, no mic requests or binary uplink, and silent-by-default WebAudio behavior.
- 1920×1080 layout checked for page overflow and larger captions. Opt-in display audio tested with a user gesture, including flush and zero mic requests.
- No runtime exceptions in either mode. Syntax checks: `node --check` for every JS file (including Leaflet), Python AST parsing for the mock and test.

Screenshots: `screenshot.png` (phone) and `screenshot-display.png` (1280×720 projector).

To rerun: start a **fresh** mock on 8877 and Chrome with `--headless=new --no-sandbox --use-fake-device-for-media-stream --use-fake-ui-for-media-stream --remote-debugging-port=9223`, then run `/data/build/birding-demo/.venv/bin/python web/self-test.py`. The test denies real downloads and examines CSV Blobs directly. Its sessions should be fresh for deterministic initial counts.

## Known gaps

No physical Android/iOS or live-model/backend verification in this pass. Mobile backgrounding, device audio routes, ignored capture constraints, and autoplay after interruptions still need hardware testing; keep the app foregrounded and use wired earphones. GPS does not block hello, and late coordinates are used locally rather than retroactively changing the protocol hello. Map tiles and photos require internet; imagery may fail independently of the session. Captions show the latest utterance for each speaker, not an archived conversation. Long checklists/timelines/captions scroll within display panels. No camera upload, offline checklist persistence, full survey-effort collection, or eBird taxonomy validation.
