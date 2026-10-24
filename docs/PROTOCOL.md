# Phone ↔ backend protocol (v0.2)

One WebSocket per phone: `wss://<host>/ws`. The backend also serves the web app (`web/`) at `/` on the same port, so one HTTPS tunnel covers both.

## Roles and sessions (v0.1)
- `wss://<host>/ws?role=mic&session=<id>` (default role `mic`, default session `demo`): the phone. It sends mic audio and commands and receives everything below.
- `wss://<host>/ws?role=display&session=<id>`: a big-screen view (laptop/projector, URL `/?display=1`). It receives the same JSON messages (checklist, heard, transcripts, candidates, state). It receives assistant audio only with `&audio=1` (so the audience can hear). It sends no mic audio, but may send `text`, `edit`, `delete`.
- One backend session = one Higgs Realtime session + one BirdNET pipeline + one checklist, shared by all devices in it. Exactly one mic device per session; a newer mic replaces the older one.
- **Echo guard**: if any display has audio on, the backend runs half-duplex. While the assistant is speaking, plus 300 ms after, mic audio is not forwarded to Realtime (BirdNET still gets it). Barge-in is lost in this mode; the phone tap button still cancels speech.

## Phone → backend
- **Binary frames**: microphone audio, PCM16 little-endian, mono, **48 kHz**, about 20–40 ms per frame. Browser mic processing is off (echoCancellation/noiseSuppression/autoGainControl = false).
- **JSON text frames**:
  - `{"type":"hello","lat":43.66,"lng":-79.39,"lang":"zh"}`: sent first. lat/lng are optional.
  - `{"type":"mic","on":true|false}`: mic paused or resumed.
  - `{"type":"mode","quiet":true|false}`: quiet mode. Logging still works, but the assistant only speaks after a tap.
  - `{"type":"tap"}`: push-to-talk / "talk to me now". The next utterance is treated as addressed.
  - `{"type":"text","text":"..."}`: typed input (debug fallback).
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
  - `{"type":"state","listening":true,"speaking":false,"model":"higgs-realtime-1.5-preview","connected":true}`: status for the UI indicator.
  - `{"type":"error","message":"..."}`

## Notes
- Audio direction asymmetry is intentional: 48 kHz in (BirdNET needs it), 24 kHz out (Higgs output).
- All checklist state lives on the backend; reconnecting phones receive `state` + `checklist` right after `hello`.

## v0.2: Observations and Look mode (see docs/PLAN.md)
### Backend → clients
- `{"type":"observation","id":"ob3","focus":true,"status":"open"|"confirmed"|"discarded","count":null|2,"notes":"","t_start":"<iso>","lat":..,"lng":..,
   "detections":[{"id":"d7","kind":"sound"|"image"|"description","t":"<iso>","thumb":"/api/frame/d7.jpg"?,"text":"..."?,"candidates":[{"sci":"..","en":"..","p":0.62}]}],
   "fused":[{"sci":"..","en":"..","photo":"https://..","p":0.71}],"uncertain":true|false,"hint":"Check the bill length"?}`
  Sent on every change. `checklist` messages continue as before; they are derived from confirmed observations.
- `{"type":"capture","obs":"ob3","n":3,"hint":"Zoom on the head","zoom":2.0?}`: the phone should switch to Look mode (if not already), grab n frames over ~1 s, and POST them.

### Phone → backend
- `POST /api/frame?session=<id>&obs=<id>[&x=..&y=..&w=..&h=..]`, body = JPEG (≤ 1920 px long side). Optional crop is normalized 0..1 (from a tap on the bird). One request per frame. Response: `{"detection": {...}}`. The backend then broadcasts the updated `observation`.
- `{"type":"look"}`: the user opened Look mode / pressed the shutter without speaking. Equivalent to "what's that?" (opens a new focus if none is open).
- `{"type":"observation_action","id":"ob3","action":"confirm"|"discard"|"focus","count":2?}`: UI buttons.

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
