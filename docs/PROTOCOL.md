# Phone ↔ backend protocol (v0)

One WebSocket per phone: `wss://<host>/ws`. The backend also serves the web app (`web/`) at `/` on the same port, so one HTTPS tunnel covers both.

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
  - `{"type":"checklist","items":[{"id":"o1","sci":"Cardinalis cardinalis","en":"Northern Cardinal","zh":"北美红雀","group":"Aves","count":2,"notes":"","first_seen":"2026-10-24T10:02:11-04:00","source":"voice"|"birdnet"|"photo"}]}`: full checklist state, sent after every change. The UI renders only this, never the model's words.
  - `{"type":"heard","sci":"...","en":"...","zh":"...","conf":0.87,"t":"<iso>","new":true}`: a BirdNET detection. `new` means first time in this session above threshold.
  - `{"type":"transcript","role":"user"|"assistant","text":"...","final":true|false}`: captions.
  - `{"type":"candidates","items":[{"sci":"...","en":"...","zh":"...","photo":"https://...","why":"..."}]}`: identification candidates to show as cards (describe-to-ID / photo ID).
  - `{"type":"state","listening":true,"speaking":false,"model":"higgs-realtime-1.5-preview","connected":true}`: status for the UI indicator.
  - `{"type":"error","message":"..."}`

## Notes
- Audio direction asymmetry is intentional: 48 kHz in (BirdNET needs it), 24 kHz out (Higgs output).
- All checklist state lives on the backend; reconnecting phones receive `state` + `checklist` right after `hello`.
