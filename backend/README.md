# Field Companion backend (protocol v0.1)

```bash
source /data/build/birding-demo/.venv/bin/activate
# BOSON_API_KEY, or the existing ~/.config/boson/api_key; never copy into the repo
python -B backend/server.py --port 8796
```

The backend serves the existing `web/` at `/` and WebSockets at `/ws`. A phone
needs HTTPS for microphone capture; use one tunnel for both. This is an
unauthenticated, in-memory hackathon demo: use a single uvicorn worker and do not
expose it publicly. Server restart loses session state.

## Round 1 fixes (2026-10-23)

- Sound questions use `recent_sounds(seconds=15)`; checklist/count questions use
  `get_checklist`. Greetings/mic checks use `speak`; filler/garbled fragments use
  `ignore`. Only explicit approval accepts pending suggestions. Explicit
  all/everything/clear requests can use `remove_observation(all=true)`.
- BirdNET uses one scientific-name allowlist: geo SpeciesList at **0.001** union
  iNat verifiable Aves species, all time, paginated at 500/page. Ontario bbox
  (41.7–56.9 N, 95.2–74.3 W) uses place 6883; elsewhere uses a 100 km radius.
  Non-bird labels and non-binomial names are always dropped; no rarity bypass.
  Both sources must succeed to replace the last good list. Failure reuses the
  regional memory/disk cache (`/data/build/tmp/allowlist-<lat>_<lng>.json`, rounded
  to one decimal), or suppresses all detections and logs; retries after 60 s.
- All eligible >=0.25 detections remain in `recent_sounds` history. Heard events
  and suggestions require two distinct >=0.5 windows within 10 s or one >=0.7
  hit. `unusual` is always false.
- Higgs item deletion was probed successfully. Item-created/added IDs are tracked;
  each completed turn retains only the last **8 items**, including function
  outputs and NOTIFY. Fresh route instructions carry checklist species/counts,
  pending suggestions and the last three distinct heard species. The active
  strategy and pruning counts are logged. Reconnect clears VAD/transcript flags;
  response completion must match the awaited response ID.
- `reset` from mic **or display** flushes speech, clears outing/queued state and
  reconnects Higgs with an empty checklist. Mic `location` supports late GPS.

Verification: **39 offline tests**, plus ten real-Higgs text routing cases on
**127.0.0.1:8796** (quiet mode suppressed answer audio; pending suggestions were
seeded in the test process). All ten selected the expected tools; explicit
remove-all emptied two checklist species. Display reset established a fresh
Realtime connection. No port-8780 changes or acoustic/STT retest.
Evidence (outside the repository): `/data/build/tmp/round1-unit.log`,
`round1-delete-probe.log`, `round1-live.log`, `round1-live-results.json`,
`round1-allowlist-live.log`. Live allowlist: 517 species, including Red-necked
Grebe, excluding the five reviewed out-of-range species and Human non-vocal.
Historical verification sections below describe earlier revisions.

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
- The mic defaults to **speaker** (`earphones=false`): safe half-duplex without
  needing a display. `audio_mode` (or hello's boolean `earphones`) selects
  earphones/full-duplex; any display with `audio=1` still forces echo guarding.
- Guarding starts before the first outgoing assistant PCM. Browser `playback`
  reports track actual scheduled start and final source completion, including
  flush. Active reports hold the gate closed. Input resumes after the later of
  reported end and PCM-duration estimate plus 500 ms; old/non-reporting clients
  use estimate plus 800 ms. The latest audio-output deadline wins. Guarded VAD
  starts are ignored, not treated as barge-in; Tap still cancels and flushes.
- Speaker-mode BirdNET windows overlapping assistant playback or its tail are
  marked in the audio ring, dropped even if processed after playback, and logged
  as `bird_window_skipped` / `assistant_playback`. Earphone BirdNET is unchanged.
- Always-on STT echo defense remembers the last three spoken assistant captions
  (including partial captions before cancellation). Within ten seconds of speech,
  normalized word overlap >=0.6 or difflib ratio >=0.75 drops the input before
  routing and logs `echo_dropped`; one-word input requires an exact word match.
  Typed text is unaffected. Genuine repeated words can also be filtered: this is
  a conservative heuristic, not AEC. Browser AEC stays off for BirdNET.
- The phone status toggle persists Speaker/Earphones in localStorage. The hint
  distinguishes Tap interruption from voice interruption; first run recommends
  earphones. See `docs/PROTOCOL.md` for wire details.

## Route first, answer second

Server VAD explicitly uses `create_response=false`, `interrupt_response=false`.
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
   detections in distinct windows within ten seconds OR a single >=0.7 hit qualify. Detections
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
python -B backend/server.py --host 127.0.0.1 --port 8793
python -B backend/selftest.py --url ws://127.0.0.1:8793/ws
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
no ten-minute live wait or forced live upstream-disconnect test was run. Previous historical test logs describe older server behavior; in particular the
old guard did not cover speaker-only phones or suppress BirdNET windows. See the
speaker-echo verification below for current behavior.

## Batched proactive suggestions (feat/batched-suggestions)

The two-window / ten-second / >=0.5 rule (or one >=0.7 hit) populates an ordered
session suggestion buffer, retaining best confidence and first/last heard times.
A 3.5-second debounce produces one batch; prompt starts are at least 25 seconds
apart (measured again at first output audio). New species accumulate during the
limit. User turns take priority, and active generation, user speech, pending
transcription and estimated playback block proactive speech. Quiet mode keeps
chips only, even after a tap. At most three names are supplied for speech, in
confidence order, with an “N more on screen” instruction for larger batches.
Unprompted species stale for >60 seconds are removed; prompted suggestions expire
120 seconds after dispatch. Suggested/dismissed species stay deduplicated for the
session. Detection timestamps still update the UI while confirmation is pending.

`accept_suggestions(species?, counts?)` and `dismiss_suggestions(species?)` use
omitted/empty species for all pending, or select an unambiguous named subset.
The router gets the current list in each route session's instructions, so yes,
yes both, just the cardinal and no have explicit context. Accept uses cached
BirdNET taxon metadata, without another network lookup. `add_observations(items)`
resolves and validates the entire multi-species report before mutation. These
write actions return a single earcon and no answer generation. Existing
`add_observation` is unchanged for single-species callers. The web Heard area
shows highlighted Add? chips with independent accept/dismiss buttons; displays
can use them too. See `docs/PROTOCOL.md` for the wire format.

### Current verification (2026-10-23, port 8793 only)

```bash
/data/build/birding-demo/.venv/bin/python -B -m unittest discover -s backend -p 'test_*.py' -v
node --check web/app.js
# With a test server running on 8793:
/data/build/birding-demo/.venv/bin/python -B backend/selftest.py --url ws://127.0.0.1:8793/ws --chorus
```

All **26 unit tests passed**; `node --check web/app.js` and `git diff --check`
also passed. Evidence is reproducible in `backend/test_server.py` and
`backend/selftest.py`; live output was captured by the execution command, not
written as extra repo files.
Chorus session `chorus-afc07fc5` passed real BirdNET + Higgs STT/router on 8793:
- Mixed testmix cardinal/robin/bluejay recordings in memory (gains .5/1/.05);
  the five-species fixture did not exist. Two species qualified.
- Exactly one spoken prompt: “BirdNET just heard a Northern Cardinal and an
  American Robin. Would you like me to add either or both to your checklist?”
- Probe `tts("Yes, add them all.")` transcribed “Yes. Add them all.”; one
  `accept_suggestions` call added both checklist species with exactly one logged
  earcon and no spoken acknowledgement.
- Buffer-to-dispatch **3,640 ms**; stream-start-to-first-audio **10,059.6 ms**
  (includes time needed for two BirdNET windows); speech-stopped-to-earcon
  **897.5 ms**. One sample, not a latency distribution. Test server was stopped.

Earlier live attempts failed honestly: equal-gain audio qualified only a cardinal;
then a second mix put cardinal's second hit just beyond the 3-second boundary,
so only robin was spoken (although accept-all did log both). The final 3.5-second
debounce includes that boundary and passed. Audio mixing can also create false
positive detections (one unusual treecreeper hit was seen but never suggested).
No detection thresholds were relaxed. Spoken max-three/extra wording is model
instructed and covered at injection level offline, not live with >3 species.
Natural speech subset/no routing, physical browser playback/chip appearance and
real-speaker echo behavior remain unverified live; unit tests cover the action
semantics, guards, rate limit, expiry, deduplication and atomic multi-add.

## Speaker self-echo verification (2026-10-23, port 8794 only)

`backend/test_server.py` now has **31 passing offline tests**, including speaker
and earphone policy, display override, playback end/estimate/fallback timing,
flush tail retention, guarded VAD ignoring vs earphone barge-in, overlapping
BirdNET window rejection, and echo fragment vs genuine question filtering.
`node --check web/app.js`, `node --check web/sw.js`, and `git diff --check` pass.

A temporary test server on **8794** generated one real Higgs spoken answer per
mode. Its outgoing 24 kHz PCM was recorded **in memory**, resampled to 48 kHz and
streamed back as real-time mic input, with playback start/end reports and trailing
silence. Evidence was printed by the execution command (no extra audio/log files):

- `echo-speaker-c86321`: 376,320 PCM bytes; exactly one `route_done` (`speak`),
  zero `barge_in`, five BirdNET windows skipped for assistant playback; no loop.
- `echo-earphones-7f026f`: 336,000 PCM bytes; guard off, exactly one route,
  zero barge-in, zero playback-related BirdNET skips. Actual STT of the replay
  matched the assistant answer and logged `echo_dropped`; no second route/audio.

The test instance was stopped; port **8780 was not restarted or killed**. Backend
changes need a later deployment/restart to affect that existing Python process;
web assets take effect on reload (service-worker shell v5). These live tests use
PCM loopback after recording the answer, not a physical phone speaker/room or
browser WebAudio scheduling. Simultaneous guarded speech-start handling is tested
offline. Mobile playback timing, UI appearance, and physical acoustic conditions
remain unverified. A client reporting start but never end stays safely muted
until it reports completion, disconnects, or switches to unguarded earphone mode.
