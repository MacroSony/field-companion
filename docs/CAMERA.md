# Camera and visual follow-up

The phone uses the existing v0.2 `capture`, `observation`, `/api/frame` and
`observation_action` contracts. No model or API credential is placed in the browser.

## Use

1. Start listening, then open **Look closer → Open camera**. The microphone and
   camera have separate permission requests. A denied camera does not stop audio.
2. Choose the taxon group. Use the 1–4× digital zoom and optionally tap the subject
   to select a region. **Full frame** clears the selection.
3. Tap **Identify this subject**, or ask “What is that?” while listening. The
   server opens/reuses the focus and requests a capture. The browser takes up to
   three frames ~400 ms apart, selects the sharpest within the selected region,
   and uploads one JPEG. The long side is at most 1920 pixels. Crop coordinates
   refer to the already digitally zoomed JPEG, matching the preview.

Voice requests first play a short "Let me take a look." / "好的，我看一下。" before
the slow lookup and capture request, with the camera either closed or open.
This uses bundled Higgs audio through the normal playback queue; it adds no
runtime synthesis request. Quiet and interruption rules still apply. Button
requests retain the visible photo-capture progress without an extra spoken line.
4. The **Current observation** card shows up to three ranked candidates, the last
   six evidence items and any expert hint. Rankings are uncalibrated scores.
   **Look again** adds evidence to the same focus. **New subject** closes an open,
   unconfirmed focus and requests a fresh one; confirmed records are retained.
5. Set the count and **Add to checklist**, or **Discard**. Only server messages
   update the checklist; recognition alone does not confirm an observation.

An open camera stays in preview mode, but no continuous video is uploaded. Closing
it, ending the session or backgrounding the page stops its tracks and cancels
pending uploads. Late permission results are also stopped. Reopen explicitly
after returning to the foreground. A changed/closed focus or lost socket cancels
pending capture. Uploads time out after 35 seconds and are not retried silently.
An unavailable vision service leaves existing checklist records intact.

The display receives the same observation card and evidence, without requesting
a camera or microphone. Its observation panel shares the right column with the
map, detections and captions.

## Run and validate

The real backend also needs a running `backend/vision_service.py` service at its
configured `VISION_URL` (default `http://127.0.0.1:8795`). This UI does not install
the BioCLIP environment or model weights. A phone requires HTTPS and a reachable
backend; serve the web app, socket and image routes from the same origin.
Browser permission requirements are described in
[MDN's getUserMedia documentation](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia).

`web/camera-test.py --serve` starts a **local test fixture** on 8878 using the real
Observation model, socket controls, frame validation, thumbnail and checklist
routes, with a fixed classifier and no BirdNET/Higgs/OpenAI requests. Run the test
without `--serve` against an isolated Chromium on CDP port 9224, with
`--use-fake-device-for-media-stream --use-fake-ui-for-media-stream` and a fresh
profile. Pass `--port`/`--cdp` to change the ports. Reports and screenshots go to
ignored `out/camera-checks/`.
Keep Windows browser profiles short, for example `out/c-12345678`, because deeply
nested Service Worker cache files can exceed filesystem path limits.

The browser test covers camera denial/mic isolation, 3-frame/one-upload capture,
digital zoom and normalized crop, same-focus follow-up, cancellation on a changed
focus, explicit count confirmation, service failure, track release, late camera
permission, display-only observation rendering and runtime errors. It also covers
late GPS delivery, failed microphone switching and replacement/reconnect behavior.

These checks use fake media and a fixture classifier. Real Android camera behavior,
permission/autoplay recovery, rear camera choice, field sharpness selection and
the complete real BioCLIP → expert → Higgs spoken flow still need hardware tests.

## Local verification on feat/look-ui

- All 72 existing offline backend tests passed; no backend behavior was changed.
- Camera integration passed in isolated Chromium at 390×844, with JPEG 960×540
  at 2× zoom, a normalized selected region, three encodes/one upload, same-focus
  follow-up, changed-focus cancellation, explicit count=3 confirmation, 503
  fallback, background/End/late-permission track release and replacement handling.
- Read-only display observation layouts passed at 1280×720 and 1920×1080, without
  camera/microphone requests, page overflow or runtime errors.
- The original mock-backed browser regression passed: microphone/playback,
  suggestions, reconnect, edits, exports, offline shell, shared maps/photos and
  silent/audio-enabled displays. Its screenshots/JSON remain in ignored output.
- JS syntax checks, Python AST parsing and `git diff --check` passed.

Those camera browser checks used a fixture classifier. A subsequent real-model
installation is recorded below; neither run used a physical phone camera.

## Real BioCLIP installation — 2026-10-24

Installed `.venv-vision` with Python 3.12, torch 2.8.0+cpu, torchvision 0.23.0+cpu,
pybioclip 2.1.6, open-clip-torch 3.3.0 and huggingface-hub 2.3.0. Both the voice
and vision environments pass `pip check`; all 72 backend tests still pass.
Model weights and species embeddings are cached in ignored `out/hf-cache/`.

The real service on 127.0.0.1:8795 reports `loaded: true`, model
`imageomics/bioclip-2` and 867,455 taxa. Four publicly sourced iNaturalist default
photos were checked, with provenance/attribution retained in ignored output:

| Photo label | Real open-classifier top candidate | HTTP duration |
| --- | --- | --- |
| Common Milkweed | Asclepias syriaca | 3,279 ms |
| Northern Cardinal | Cardinalis cardinalis | 2,253 ms |
| Blue Jay | Cyanocitta cristata | 1,900 ms |
| Monarch | Danaus plexippus | 2,150 ms |

These are four clear reference photographs, possibly overlapping training data;
they are an installation smoke check, not a held-out accuracy benchmark.
Results are in `out/vision-setup/real-model-results.json`; sources are in
`out/vision-setup/photos/sources.json`.

A separate `vision-install-*` session on the real app backend (127.0.0.1:8791)
also passed for Blue Jay and Monarch: `look` → JPEG POST → real BioCLIP →
Observation/top candidates → Higgs PCM reply → explicit confirmation. Image POSTs
returned HTTP 200 in 3,875 ms and 3,437 ms respectively; both top candidates
matched the photo labels. The session confirmed two species and generated
147,840 PCM bytes, consumed by the test without playback. It used file uploads,
not a microphone or physical camera. Report: `out/vision-setup/backend-live-results.json`.

The vision service stays running; use `backend/start_vision.ps1 -Background` to
start it after a future shutdown. The script refuses an occupied port rather
than loading a second model. Physical phone camera, audio routing, autofocus
and in-field identification remain separate acceptance checks.
