# Camera and visual follow-up

The phone uses the v0.2 observation routes with a two-step camera confirmation
handshake. No model or API credential is placed in the browser.

## Use

1. Start listening, then open **Look closer → Open camera**. The microphone and
   camera have separate permission requests. A denied camera does not stop audio.
2. Choose the taxon group. Use the 1–4× digital zoom and optionally tap the subject
   to select a region. **Full frame** clears the selection.
3. Asking “What is that?” opens/reuses the preview without taking a photo. Once
   video is usable, the assistant says **“Camera ready. Say start to take a photo.”**
   (Chinese: “相机已就绪。说开始就拍照。”). Manually opening the camera uses the same
   flow. Aim first, then say **“start” / “开始”**, or tap **Identify this subject**.
   Only then does the browser take up to three frames ~400 ms apart, select the
   sharpest within the selected region, and upload one JPEG. The long side is at
   most 1920 pixels. Crop coordinates refer to the digitally zoomed JPEG.
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

The short confirmation is handled directly from final STT or typed text, without
another model routing decision. It only applies to a ready, current camera request;
an early “start” is not saved for later, and “yes” / “go ahead” do not authorize a
photo. “Cancel” / “取消” cancels the request while leaving the preview open. A new
identification request is required after cancellation or a successful photo.
Identify, Look again and New subject buttons count as explicit confirmation.
Closing, backgrounding, changing/closing focus, resetting or replacing/disconnecting
the microphone invalidates the old confirmation. The frame endpoint requires the
matching single-use request ID. Old clients cannot upload without this handshake.

On “start” / “开始” or Identify, the assistant says **“Start taking photo.”** /
“开始拍照。” before requesting the frames. The shutter no longer waits for a cold
BirdNET range/model load. Once a valid JPEG upload arrives, it queues **“Photo ready.
Starting identification.”** / “照片已拍好，开始识别。” while range lookup and recognition
run. The result waits for this progress clip to finish. This cue refers to image
identification; a GPT investigation is separately scheduled only when required.

Ready prompts use bundled Higgs recordings through the shared PCM queue, with no
runtime synthesis call. They wait for ongoing speech/playback; quiet and interruption
rules apply. Other voice/text input is classified before any task confirmation.
Ignored chatter and unmatched Yes/Okay stay silent. Accepted identification tasks
receive their look/check cue after routing, before the tool; normal chat answers
directly. Early/repeated Start reports the actual camera phase, never a generic
input receipt, and does not authorize another photo.
Investigations and other questions can continue while
the camera waits for confirmation.

The display receives the same observation card and evidence, without requesting
a camera or microphone. Its observation panel shares the right column with the
map, detections and captions.

Photo confirmation executes directly on the phone connection, independently of
the chat worker and Higgs recovery. Asking about an in-flight photo gives a
capture/analysis status instead of an answer from old evidence, and ordinary
identification questions do not silently restart it. Explicit retake commands
still prepare another photo. Failed analysis requests a retry. Readouts carry the
photo request/revision so an older result cannot be announced as a newer photo.
Ambiguous speech cannot confirm an observation or cancel its pending photo through
a mistaken checklist write; it asks for clarification. Manual confirmation still
requires a finished identification.

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

The browser test covers preview-only opening, ready prompts, a separate voice
confirmation with closed/open cameras, stale confirmation after backgrounding,
camera denial/mic isolation, 3-frame/one-upload capture,
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
