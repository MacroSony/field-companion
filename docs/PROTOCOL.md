# Phone ↔ backend protocol (v0.2)

One WebSocket per phone: `wss://<host>/ws`. The backend also serves the web app (`web/`) at `/` on the same port, so one HTTPS tunnel covers both.

## Roles and sessions (v0.1)
- `wss://<host>/ws?role=mic&session=<id>` (default role `mic`, default session `demo`): the phone. It sends mic audio and commands and receives everything below.
- `wss://<host>/ws?role=display&session=<id>`: a big-screen view (laptop/projector, URL `/?display=1`). It receives the same JSON messages (checklist, heard, transcripts, candidates, state). It receives assistant audio only with `&audio=1` (so the audience can hear). It sends no mic audio, but may send `text`, `edit`, `delete`.
- One backend session = one Higgs Realtime session + one BirdNET pipeline + one checklist, shared by all devices in it. Exactly one mic device per session; a newer mic replaces the older one.
- **Echo guard**: the microphone defaults to speaker mode (`earphones=false`), which is safe half-duplex. Guarding is enabled when the mic uses its speaker **or** any display has `audio=1`. It begins before the first outgoing assistant PCM. Mic audio is withheld from Realtime until the later of the backend duration estimate and reported playback end, plus **500 ms**. A reported active playback keeps the gate closed until its end; clients without end reports use the estimate plus **800 ms**. With multiple audio outputs the latest deadline wins. Guarded VAD speech-start events do not cancel playback; **Tap still interrupts**.
- In speaker mode, three-second BirdNET windows containing any assistant-playback/tail samples are dropped and logged (`bird_window_skipped`, reason `assistant_playback`). Earphone-mode BirdNET is unchanged, including with an audio-enabled display.
- Independently of output mode, recent assistant speech is compared to incoming STT before routing: up to three spoken transcripts, within ten seconds of assistant speech. A contiguous match of at least three normalized alphanumeric words covering >=70% of the input drops an echo (`echo_dropped`). Inputs shorter than three words pass. Typed text is not filtered. This is a heuristic defense, not acoustic echo cancellation.

## Phone → backend
- **Binary frames**: microphone audio, PCM16 little-endian, mono, **48 kHz**, about 20–40 ms per frame. Browser mic processing is off (echoCancellation/noiseSuppression/autoGainControl = false).
- **JSON text frames**:
  - `{"type":"hello","lat":43.66,"lng":-79.39,"lang":"zh","earphones":false}`: sent first. GPS, language and boolean `earphones` are optional; each new mic defaults to speaker mode. The mic may repeat hello with its current language/output mode when an initially pending GPS fix arrives; subsequent observations use the updated location.
  - `{"type":"audio_mode","earphones":true|false}`: mic only; change output mode. `false` = speaker / half-duplex, `true` = earphones / voice interruption (unless an audio-enabled display forces guarding). The UI persists the selection locally and sends it on connect and change.
  - `{"type":"playback","active":true|false}`: audio-output client reports when scheduled assistant audio actually starts and when its last queued source finishes, including after flush. End times are measured at backend receipt, not supplied by the client. Audio-enabled displays may report too. Reports never shorten the backend estimate.
  - `{"type":"mic","on":true|false}`: mic paused or resumed.
  - `{"type":"mode","quiet":true|false}`: quiet mode. Logging still works, but the assistant only speaks after a tap.
  - `{"type":"tap"}`: push-to-talk / "talk to me now". The next utterance is treated as addressed.
  - `{"type":"text","text":"..."}`: typed input (debug fallback).
  - `{"type":"investigation_action","id":"R1","action":"cancel"|"retry"}`:
    manage a background investigation; retry uses its original question/evidence.
  - `{"type":"edit","id":"<obs id>","count":3}` / `{"type":"delete","id":"<obs id>"}`: manual checklist edits from the UI.

## Backend → phone
- **Binary frames**: assistant speech, PCM16 little-endian, mono, **24 kHz**. The phone plays them in order through a queue.
- **JSON text frames**:
  - `{"type":"flush"}`: barge-in or cancelled response. Stop playback now and drop the queued audio.
  - `{"type":"earcon","kind":"logged"|"heard"|"error"}`: play a short local sound (generated on the phone, no file).
  - `{"type":"checklist","items":[{"id":"o1","sci":"Cardinalis cardinalis","en":"Northern Cardinal","zh":"北美红雀",  (zh optional; UI is English-first)"group":"Aves","count":2,"notes":"","first_seen":"2026-10-24T10:02:11-04:00","source":"voice"|"birdnet"|"photo"}]}`: full checklist state, sent after every change. The UI renders only this, never the model's words.
  - `{"type":"heard","sci":"...","en":"...","zh":"...","conf":0.87,"t":"<iso>","new":true}`: a BirdNET detection. `new` means first time in this session above threshold.
  - `{"type":"transcript","role":"user"|"assistant","text":"...","final":true|false}`: captions.
  - `{"type":"candidates","items":[{"sci":"...","en":"...","zh":"...","photo":"https://...","why":"..."}]}`: identification candidates to show as cards (describe-to-ID / photo ID).
  - `{"type":"state","listening":true,"speaking":false,"model":"higgs-realtime-1.5-preview","connected":true,"echo_guard":true,"session":"demo"}`: status for the UI indicator. `echo_guard` describes the selected policy, not whether input is currently being dropped.
  - `{"type":"error","message":"..."}`
  - `{"type":"investigations","items":[{"id":"R1","question":"...","observation_id":"ob3","species":"Blue Jay","status":"running","stage":"analyzing","queue_position":0,"announced":false,"result":null}],"capacity":5,"timeout_seconds":180}`:
    full investigation state on connect/hello and task changes. Completed results
    contain the existing expert decision, final answer and cited source metadata.
    See [INVESTIGATIONS.md](INVESTIGATIONS.md) for all states and retention rules.

## Notes
- Audio direction asymmetry is intentional: 48 kHz in (BirdNET needs it), 24 kHz out (Higgs output).
- All checklist state lives on the backend; reconnecting phones receive `state` + `checklist` right after `hello`.

## v0.2: Observations and Look mode (see docs/PLAN.md)
### Backend → clients
- `{"type":"observation","id":"ob3","focus":true,"status":"open"|"confirmed"|"discarded","count":null|2,"notes":"","t_start":"<iso>","lat":..,"lng":..,
   "detections":[{"id":"d7","kind":"sound"|"image"|"description","t":"<iso>","thumb":"/api/frame/d7.jpg"?,"text":"..."?,"candidates":[{"sci":"..","en":"..","p":0.62}]}],
   "fused":[{"sci":"..","en":"..","photo":"https://..","p":0.71}],"uncertain":true|false,"hint":"Check the bill length"?}`
  Sent on every change. `checklist` messages continue as before; they are derived from confirmed observations.
- `{"type":"capture","obs":"ob3","n":3,"hint":"Zoom on the head","zoom":2.0?}`: the phone should switch to Look mode (if not already) and grab n frames over ~1 s. The current UI caps n at 3, selects the sharpest within the chosen region and POSTs one JPEG. Zoom is user-controlled digital zoom in this version; the optional server zoom field is not applied.

### Phone → backend
- `POST /api/frame?session=<id>&obs=<id>[&x=..&y=..&w=..&h=..]`, body = JPEG (≤ 1920 px long side). Optional crop is normalized 0..1 (from a tap on the bird). One request per frame. Response: `{"detection": {...}}`. The backend then broadcasts the updated `observation`.
- `{"type":"look"}`: the user opened Look mode / pressed the shutter without speaking. Equivalent to "what's that?" (opens a new focus if none is open).
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
