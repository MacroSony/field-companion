# 野伴 · Field Companion (v0)

Plain HTML/CSS/JS. No install or build step, no framework/CDN. The real backend should serve this directory at `/` with its WebSocket at `/ws`.

## Run the protocol mock

From the repository root:

```sh
/data/build/birding-demo/.venv/bin/python web/mock-server.py
# Open http://localhost:8766
```

If that port is occupied (it was during this build):

```sh
/data/build/birding-demo/.venv/bin/python web/mock-server.py --port 8767
# Open http://localhost:8767
```

Both static assets and `/ws` are on the same port. Use an HTTPS tunnel to that port on a physical phone; plain LAN HTTP cannot access the mic. An optional `?ws=wss%3A%2F%2Fother-host%2Fws` override supports a separate WebSocket server. HTTPS pages need secure `wss` endpoints.

Tap **Start** and grant mic access. The mock supplies four observations (including one plant), detections, captions, three candidate illustrations, earcons, a 24 kHz tone and a flush. `+`, `−`, delete/swipe, quiet, mic pause, tap and typed input exercise protocol messages. Typed input increments the mock mallard regardless of wording; this is deliberately not real identification. Mock state resets on reconnect; the real backend owns state and must resend its checklist after hello.

## Implementation

- Start gesture unlocks a shared AudioContext; requests 48 kHz unprocessed mono input. AudioWorklet captures PCM16 in 20 ms native-rate chunks. If the context negotiates another rate, a stateful **linear JS resampler** converts to 48 kHz (not a band-limited production resampler).
- Incoming little-endian mono PCM16 is placed in 24 kHz AudioBuffers and scheduled consecutively on that same context. Flush stops all active/scheduled speech sources and local earcons immediately.
- No optimistic checklist mutations: only server `checklist` messages render observations. Socket backoff reconnects automatically, and disconnected edits are disabled. Stale mic frames are dropped rather than buffered on a slow connection.
- Screen Wake Lock is requested while running and reacquired when visible, where supported. Pause disables the mic track and transmission; End stops tracks, releases wake lock and closes audio/socket.
- eBird export: 19 columns, no header, only exact `group: "Aves"`; English common names, genus/species, local session date/time and GPS if available. All-taxa CSV includes headers and Chinese names. Location/state/country are editable in the export dialog. **Incidental remains the protocol even after End**: this v0 does not collect complete survey effort, movement or all-species reporting, so it must not invent a Stationary/Traveling checklist. Duration/distance/area are blank; all observations reported is `N`.
- A service worker caches the app shell after the first successful visit. Offline UI and current-page CSV export work; identification/audio assistance requires the server. Reloading offline does **not** restore the backend checklist. No audio recordings or checklist data are stored locally.

## Verification and evidence

- `node --check web/app.js`, `web/audio-worklet.js`, `web/sw.js`: passed.
- Python compile checks for mock and smoke test: passed.
- `self-test-results.json`: actual Chromium 149 CDP smoke-test results using a fake mic and granted fake GPS, against the mock on 8767. Covers hello-first framing, 48 kHz mic capture, scheduled-source flush, checklist edit/delete, typed input, quiet/tap, mic pause transmission stop, reconnect, CSV column/row counts, GPS export, offline shell reload and absence of runtime exceptions.
- `screenshot.png`: actual 390×844 viewport against that mock.
- Linear-resampler unit exercise: 1 second at 44.1/32/96 kHz produced 47,999 / 47,999 / 48,000 samples. Tested 440 Hz continuity error below 17 PCM units. No physical input-device fallback was tested.

`self-test.py` can rerun the browser checks using `websockets`; it expects the mock on 8767 and an existing Chrome CDP instance on 9223 launched with fake-media flags. It denies actual file downloads and checks generated CSV Blobs directly.

## Known limitations / phone caveats

- Not tested on physical iOS Safari or Android hardware; the browser test uses synthetic mic audio. Start must be a user gesture. Safari may suspend/interfere with audio after interruption, route changes or backgrounding; tap **Tap to talk** to resume, or End/Start if capture stops. Wake Lock is best effort and cannot keep background microphone access alive. Keep the page foregrounded/screen on. Use wired earphones to prevent feedback.
- Browsers/devices can ignore requested hardware rate and mic-processing constraints. Bluetooth microphones may be narrowband; resampling cannot recover missing bird frequencies.
- GPS never blocks hello. If permission resolves after the socket opens, coordinates are used for export but cannot be sent retroactively under this protocol.
- eBird files may need species/name review before import; no effort/track editor or validation against eBird taxonomy. Generic export is not an iNaturalist-specific importer.
- SVG manifest icon is a simple v0 asset; older iOS install icons may need dedicated PNGs. No camera upload/map, persistent offline checklist, background recording or real speech/BirdNET inference in the mock. Candidate photos supplied by a backend may require network access; failed images get a local placeholder.
