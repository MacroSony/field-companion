# Field Companion backend (protocol v0.1)

```bash
source /data/build/birding-demo/.venv/bin/activate
# BOSON_API_KEY, or the existing ~/.config/boson/api_key; never copy into the repo
python -B backend/server.py --port 8791
```

The backend serves the existing `web/` at `/` and WebSockets at `/ws`. A phone
needs HTTPS for microphone capture; use one tunnel for both. This is an
unauthenticated, in-memory hackathon demo: use a single uvicorn worker and do not
expose it publicly. Server restart loses session state.

## Sessions and audio

- `/ws?role=mic&session=demo` is the default. A newer mic replaces the old mic.
- `/ws?role=display&session=demo` shares all JSON, including checklist, heard,
  transcripts, state and earcons. Displays receive assistant PCM only with
  `&audio=1`; the mic always receives it.
- Each named session owns one Realtime connection, BirdNET pipeline, checklist,
  detection history and notification deduplication. Clients receive state and
  checklist immediately, without needing hello. Mic hello sets optional GPS
  and language. Display hello does not overwrite the microphone's GPS.
- A session lives while any client remains, then for 600 seconds after the last
  disconnect. Reconnecting within that interval restores the outing.
- Input: 48 kHz mono PCM16; output: 24 kHz mono PCM16. Stateful 63-tap FIR
  downsampling preserves packet phase. BirdNET uses three-second windows every
  1.5 seconds, a 30-second ring and one worker thread per session.
- State includes `echo_guard`: true while any audio-enabled display is attached.
  In that mode microphone PCM is withheld from Realtime during estimated assistant
  playback plus 300 ms, but still enters BirdNET. The estimate uses sent PCM bytes
  / 48,000 bytes per second; it is not a playback acknowledgement from the browser.
  `tap` sends cancellation and broadcasts flush, including for queued playback.

## Route first, answer second

Server VAD explicitly uses `create_response=false`, `interrupt_response=true`.
Each completed transcription queues one routing turn; typed text uses the same
router. One turn worker serializes session phase changes, tool execution and
responses, while an independent reader resolves `session.updated` and response
futures. Routing/answer updates wait for their echoes and validate modality,
tool choice and instructions before sending `response.create`. Higgs initializes
only after the first update; its initial `session.created` is accepted for setup.

1. Route with text-only output, required tool choice, ignore/add/set/remove/speak
   and read tools (`get_checklist`, `recent_sounds`, `nearby_species`). Route audio
   and captions are discarded defensively, never buffered for later playback.
2. Ignore returns only a function output upstream, with no UI action or answer.
   Successful mutations immediately broadcast checklist + logged earcon, return
   function output, and do not create a spoken continuation.
3. Speak/read results switch the session to audio, tool choice none, no tools;
   answer PCM streams immediately, **without waiting for response.done**.
4. Proactive BirdNET notifications enter the answer phase directly. Two >=0.5
   detections in distinct windows within ten seconds are required. Detections
   >=0.25 appear in history, but are never automatically logged. Notifications
   wait for user speech, transcription, active generation and playback; user
   turns take priority over deferred notifications.
5. Quiet mode keeps logging but suppresses spoken routes unless a tap occurred
   within the last ten seconds. Tap does not permanently enable speech.

Instructions request English by default, the question's language for answers
(including Chinese), no unsupported claims of logging, and no delayed “I've
logged X” acknowledgements. These are model instructions, not a guarantee of
factual or language compliance. Routing contract violations fail closed.

## Species data and limits

Chinese input still resolves using iNaturalist zh-CN autocomplete with iconic-group filtering. Species always contain `en` and `sci`; missing
`english_common_name` triggers an English taxon lookup for `preferred_common_name`,
then falls back to the scientific name. `zh` is optional to consumers. Checklist
items and heard events include `photo` (square or medium URL; empty if unavailable)
and `lat`/`lng` from microphone hello (Toronto defaults before hello).

No database, authentication, export, photo-ID or candidate-card pipeline is
implemented. Nearby taxa use a cached 15 km / 60 day query. iNat failure does not
mutate the checklist. Realtime reconnects with checklist context; outage audio
still goes to BirdNET but is not replayed upstream.

## Verification

```bash
python -B -m unittest discover -s backend -p 'test_*.py' -v
python -B backend/server.py --host 127.0.0.1 --port 8791
python -B backend/selftest.py --url ws://127.0.0.1:8791/ws
# Stop only the test instance when finished; leave live port 8780 untouched.
```

`selftest.py` uses the exact probe TTS function through AST loading (no probe
module side effects or audio files). It streams three real-time English PCM
utterances: chatter stays silent, a sighting logs two cardinals with an earcon,
and a checklist question receives audio. An audio-enabled display must receive
the identical checklist and audio, and state must advertise echo guard. Tap
must produce flush. Each run uses a fresh session. Failures exit nonzero.

Evidence files (all under `backend/`):

- `unit-router-test.log`: 18 offline tests, including routing silence, immediate
  answer streaming, read/speak quiet gating and tap expiry, actual echo-guard
  audio withholding while BirdNET's ring advances, audio fanout, independent
  checklists, mic replacement, retention/expiry, update serialization, English
  name fallback, BirdNET threshold/metadata, manual edits, reconnect checklist reinjection and PCM phase.
- `selftest-router.log`, `server-router-test.log`: final live port-8791 run passed:
  chatter and sighting each sent zero audio; the answer sent 192,000 PCM bytes
  to each client; identical checklists and echo-guard state verified. Earcon:
  **1,237.2 ms**; first answer audio: **2,756.8 ms** (n=1 each). Server `latency`
  records start at receipt of `speech_stopped`, not physical speech end or browser
  playback. The test server was stopped afterwards; live port 8780 was untouched.
- `selftest-router-first-pass.log`, `server-router-first-pass.log`: complete
  initial passing run: zero audio on chatter/sighting; 193,920 PCM bytes to each
  client for the answer; earcon 1,245.6 ms; first answer audio 2,908.4 ms (n=1 each).
- `selftest-router-second-pass.log`, `server-router-second-pass.log`: another
  passing run (earcon 1,341.2 ms; first audio 2,723.3 ms). The final run additionally
  includes a mic-replacement expiry-race fix, covered by the lifecycle unit test.
- `*-router-initial.log`: failed setup experiment, retained as failure evidence.
  Waiting for session.created before sending the first session.update timed out;
  fixed by sending the initial update first. This is not counted as silence.

Known gaps: Higgs STT is cumulative across silent turns (observed live); routing
instructions ask for the latest turn but do not repair upstream transcripts.
Small synthetic samples do not establish natural-speech routing reliability,
language compliance, taxonomy correctness or echo cancellation on real speakers.
Echo suppression itself is covered offline; live selftest checks the flag and
fanout, not acoustic feedback. Lifecycle tests accelerate the ten-minute expiry;
no ten-minute live wait or forced live upstream-disconnect test was run. Browser
playback duration and network buffering are estimated, and BirdNET intentionally
continues to hear assistant playback. Previous historical test logs may describe
older server behavior; the router logs above are the current evidence.
