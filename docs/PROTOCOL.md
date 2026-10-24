# Phone ↔ backend protocol (v0.2)

One WebSocket per phone: `wss://<host>/ws`. The backend also serves the web app (`web/`) at `/` on the same port, so one HTTPS tunnel covers both.

## Roles and sessions (v0.1)
- `wss://<host>/ws?role=mic&session=<id>` (default role `mic`, default session `demo`): the phone. It sends mic audio and commands and receives everything below.
- `wss://<host>/ws?role=display&session=<id>`: a big-screen view (laptop/projector, URL `/?display=1`). It receives the same JSON messages (checklist, heard, transcripts, candidates, state). It receives assistant audio only with `&audio=1` (so the audience can hear). It sends no mic audio, but may send `text`, `edit`, `delete`.
- One backend session = one Higgs Realtime session + one BirdNET pipeline + one checklist, shared by all devices in it. Exactly one mic device per session; a newer mic replaces the older one.
- **Echo guard**: speaker mode (`earphones=false`) and audio-enabled displays use transcript-confirmed interruption. Microphone audio continues to Realtime during playback. VAD alone is a candidate; a non-echo transcript cancels generation, flushes queued playback and routes the new question. Earphones without an audio-enabled display cancel at VAD start. Spoken “stop” / “等一下” cancels without a spoken acknowledgement, keeps investigations running, and defers automatic notices until the next user turn. Tap cancels immediately. Playback estimates, actual playback reports and their 500 ms tail (800 ms for legacy clients) still protect BirdNET and automatic notifications; they do not close Realtime input.
- In speaker mode, three-second BirdNET windows containing any assistant-playback/tail samples are dropped and logged (`bird_window_skipped`, reason `assistant_playback`). Earphone-mode BirdNET is unchanged, including with an audio-enabled display.
- Independently of output mode, recent assistant speech is compared to incoming STT before routing: up to three spoken transcripts, within ten seconds of assistant speech. A contiguous match of at least three normalized English words, or six tokens for Chinese (individual Chinese characters are tokens), covering >=70% of the input drops an echo (`echo_dropped`). Shorter inputs and explicit stop commands pass. Typed text is not filtered. This heuristic supplements browser acoustic echo cancellation.

## Phone → backend
- **Binary frames**: microphone audio, PCM16 little-endian, mono, **48 kHz**, about 20–40 ms per frame. The browser requests echoCancellation=true, noiseSuppression=false and autoGainControl=false. Actual processing depends on browser/device support.
- **JSON text frames**:
  - `{"type":"hello","lat":43.66,"lng":-79.39,"lang":"zh","earphones":false}`: sent first. GPS, language and boolean `earphones` are optional; each new mic defaults to speaker mode. The mic may repeat hello with its current language/output mode when an initially pending GPS fix arrives; subsequent observations use the updated location.
  - `{"type":"audio_mode","earphones":true|false}`: mic only; change output policy. `false` = speaker / transcript-confirmed interruption; `true` = earphones / voice-onset interruption unless an audio-enabled display requires transcript confirmation. The UI persists the selection locally and sends it on connect and change.
  - `{"type":"playback","active":true|false}`: audio-output client reports when scheduled assistant audio actually starts and when its last queued source finishes, including after flush. End times are measured at backend receipt, not supplied by the client. Audio-enabled displays may report too. Reports never shorten the backend estimate.
  - `{"type":"mic","on":true|false}`: mic paused or resumed.
  - `{"type":"mode","quiet":true|false}`: quiet mode. Logging still works, but the assistant only speaks after a tap.
  - `{"type":"tap"}`: push-to-talk / "talk to me now". The next utterance is treated as addressed.
  - `{"type":"text","text":"..."}`: typed input (debug fallback).
  - `{"type":"investigation_action","id":"R1","action":"cancel"|"retry"}`:
    manage a background investigation; retry uses its original question/evidence.
  - `{"type":"location","lat":43.66,"lng":-79.39}`: mic only; update GPS after a late fix. Both finite, in-range coordinates are required; subsequent detections and range filtering use the new position.
  - `{"type":"reset"}`: mic or display; start a new outing. Cancel running/queued investigations, clear completed results and explanation offers, checklist, pending suggestions, heard history, notification/focus queues and queued turns/audio. Cancel speech and broadcast `flush`, empty `checklist`, empty `suggestions`, empty `investigations` and `state`. The backend reconnects Realtime to start a fresh conversation; clients stay connected. GPS, language, investigation limits and audio/quiet settings are retained.
  - `{"type":"edit","id":"<obs id>","count":3}` / `{"type":"delete","id":"<obs id>"}`: manual checklist edits from the UI.

## Backend → phone
- **Binary frames**: assistant speech, PCM16 little-endian, mono, **24 kHz**. The phone plays them in order through a queue.
- **JSON text frames**:
  - `{"type":"flush"}`: barge-in or cancelled response. Stop playback now and drop the queued audio.
  - `{"type":"earcon","kind":"logged"|"heard"|"error"}`: play a short local sound (generated on the phone, no file).
  - `{"type":"checklist","items":[{"id":"o1","sci":"Cardinalis cardinalis","en":"Northern Cardinal","zh":"北美红雀",  (zh optional; UI is English-first)"group":"Aves","count":2,"notes":"","first_seen":"2026-10-24T10:02:11-04:00","source":"voice"|"birdnet"|"photo"}]}`: full checklist state, sent after every change. The UI renders only this, never the model's words.
  - `{"type":"heard","sci":"...","en":"...","zh":"...","conf":0.87,"t":"<iso>","new":true}`: an eligible BirdNET detection: two distinct windows >=0.5 within ten seconds, or one >=0.7 hit. `new` means the first qualifying event for this species in the outing; `unusual` is false/absent. Lower eligible hits >=0.25 remain queryable through `recent_sounds`, but are not sent as heard events. Reset clears backend heard history; clients should also clear their local outing display when resetting.
  - `{"type":"transcript","role":"user"|"assistant","text":"...","final":true|false}`: captions.
  - `{"type":"candidates","items":[{"sci":"...","en":"...","zh":"...","photo":"https://...","why":"..."}]}`: identification candidates to show as cards (describe-to-ID / photo ID).
  - `{"type":"state","listening":true,"speaking":false,"model":"higgs-realtime-1.5-preview","connected":true,"echo_guard":true,"session":"demo"}`: status for the UI indicator. `echo_guard` describes transcript confirmation and BirdNET playback protection, not whether Realtime microphone input is being dropped.
  - `{"type":"error","message":"..."}`
  - `{"type":"investigations","items":[{"id":"R1","question":"...","observation_id":"ob3","species":"Blue Jay","status":"running","stage":"analyzing","queue_position":0,"announced":false,"result":null}],"capacity":5,"timeout_seconds":180}`:
    full investigation state on connect/hello and task changes. Completed results
    contain the existing expert decision, final answer and cited source metadata.
    See [INVESTIGATIONS.md](INVESTIGATIONS.md) for all states and retention rules.

## Notes
- Accepted final voice transcripts and typed inputs receive a local cached spoken
  receipt before routing or waiting for the turn worker. Echoes, empty/filler-only
  speech and stop commands do not get a receipt; quiet rules still apply. Camera
  confirmation uses its specific “Start taking photo.” cue instead. A receipt
  means the input was heard, not that a tool succeeded or a sighting was logged.
- After a confirmed, valid JPEG upload the backend queues “Photo ready. Starting
  identification.” while inference proceeds. Progress waits for ongoing playback
  and conversation; the visual result waits until this cue drains. Cancelling or
  changing the camera request discards its obsolete queued progress.
- Audio direction asymmetry is intentional: 48 kHz in (BirdNET needs it), 24 kHz out (Higgs output).
- All checklist state lives on the backend; reconnecting phones receive `state` + `checklist` right after `hello`.

## v0.2: Observations and Look mode (see docs/PLAN.md)
### Backend → clients
- `{"type":"observation","id":"ob3","focus":true,"status":"open"|"confirmed"|"discarded","count":null|2,"notes":"","t_start":"<iso>","lat":..,"lng":..,
   "detections":[{"id":"d7","kind":"sound"|"image"|"description","t":"<iso>","thumb":"/api/frame/d7.jpg"?,"text":"..."?,"candidates":[{"sci":"..","en":"..","p":0.62}]}],
   "fused":[{"sci":"..","en":"..","photo":"https://..","p":0.71}],"uncertain":true|false,"hint":"Check the bill length"?}`
  Sent on every change. `checklist` messages continue as before; they are derived from confirmed observations.
- `{"type":"camera_prepare","obs":"ob3","request_id":"<id>","hint":"Zoom on the head","open_id":null}`: open/reuse the preview only. Do not encode or upload a frame. Echo `camera_ready` with this request ID only after usable video is available. An optional `open_id` ties the preparation to a manually opened preview; ignore/cancel it if that preview was already closed.
- `{"type":"camera_cancelled","request_id":"<id>"}`: cancel matching preparation, capture or upload; the preview may remain open.
- `{"type":"capture","obs":"ob3","request_id":"<id>","n":3,"hint":"Zoom on the head","zoom":2.0?}`: sent only after ready-video handshake and explicit user confirmation. Require the same prepared request ID and a live preview. Grab at most 3 frames over ~1 s, select the sharpest within the chosen region and POST one JPEG. Zoom is user-controlled digital zoom; optional server zoom is not applied.

### Phone → backend
- `{"type":"camera_ready","open_id":"<preview id>","group":"Aves"}`: current mic only; a manually opened preview is usable. The backend creates a preparation request tied to this preview ID, without authorization to take a photo.
- `{"type":"camera_ready","request_id":"<id>"}`: current mic only; acknowledge matching preparation. The backend queues “Camera ready. Say start to take a photo.”, waiting for ongoing conversation/playback to finish. It then waits for final STT or typed “start” / “开始”. An early confirmation is not saved; unrelated “yes” / “go ahead” remains available for other consent flows.
- `{"type":"camera_closed","request_id":"<id>","open_id":"<preview id>"}`: current mic only; either matching ID invalidates the request and any pending capture.
- `POST /api/frame?session=<id>&obs=<id>&capture=<request id>[&x=..&y=..&w=..&h=..]`, body = JPEG (≤ 1920 px long side). Optional crop is normalized 0..1. Only the current confirmed capture accepts one upload; absent, stale, unconfirmed or consumed IDs return 409 before inference. Response: `{"detection": {...}}`; the backend broadcasts the updated `observation`. Recognition failure requires a fresh confirmation or another Identify request.
- `{"type":"look","confirmed":false,"group":"Aves"}`: prepare preview and wait for confirmation, equivalent to voice "what's that?". Identify/Look again/New subject buttons send `confirmed:true`; that click authorizes capture once the matching preview-ready handshake completes. A new open focus is created if needed.
- `{"type":"observation_action","id":"ob3","action":"confirm"|"discard"|"focus","count":2?}`: UI buttons.

The phone uploads only on a requested capture. A frame's crop is normalized to the
uploaded, digitally zoomed JPEG. Closing/backgrounding the camera, ending a session,
losing the socket or changing/closing the focus cancels outstanding client work.
After replacement by another mic, the old phone stops rather than auto-reconnecting
and evicting the replacement. See [CAMERA.md](CAMERA.md) for UI behavior and tests.

## Batched BirdNET suggestions
- `{"type":"suggestions","items":[{"sci":"Cardinalis cardinalis","en":"Northern Cardinal","photo":"https://…","conf":0.87,"t":"<iso>"}]}`:
  full pending confirmation state, broadcast on change and sent on connection/hello.
  `conf` is the best confidence; `t` is the last detection time. Empty clears the UI.
- `{"type":"suggestion_action","sci":"Cardinalis cardinalis","action":"accept"|"dismiss"}`:
  mic or display may accept/dismiss a chip without model routing. Accept adds one
  individual with source `birdnet`; either action gives one logged earcon, no speech.
- Qualifying species accumulate for ~3.5 s. One proactive prompt per 25 s, at most
  three names (confidence order), with extras on screen. Quiet mode is UI-only
  even after tap. Unannounced detections older than 60 s are dropped; prompted
  suggestions expire 120 s after prompt dispatch, without re-suggesting this session.
  Voice “yes/both/all”, a named subset, and “no” route to silent accept/dismiss;
  counts and multi-species observation reports are supported in one routed action.

## Expert integration
- Accepted suggestions create confirmed `Observation` records with sound evidence;
  direct single/multiple sightings create confirmed records with description evidence.
  All checklist counts and manual edits remain derived from those observations.
- Accepting background suggestions preserves an existing open camera focus and its
  expert evidence. Without an open focus, the most recently accepted organism can
  become the focus for follow-up questions. Batch confirmation stays silent with
  one earcon. Expert results never confirm or change checklist counts.
