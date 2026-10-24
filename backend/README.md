# Field Companion backend (protocol v0.2)

The optional [GPT Naturalist expert path](../docs/EXPERT.md) handles complex
comparisons and species-fact follow-ups without changing the logging fast path.

## Feedback before slow tools (2026-10-24)

Voice-routed Look/new-subject requests say "Let me take a look." / "好的，我看一下。"
before range lookup and the camera capture request. Nearby-species and description
lookups use "Let me check." / "好的，我查一下。" These short Higgs-generated recordings
are bundled in `backend/audio/` and cached locally: runtime feedback does not wait
for another model/synthesis request. Regenerate only explicitly with
`python -B backend/tool_feedback.py --generate`, using the existing Boson key.

Feedback uses the normal PCM playback queue, start/end IDs and drained reports.
Quiet, user speech, pending playback, stop and stale input suppress it. Guarding
inside the client send lock prevents cancelled feedback from leaking after flush.
Logging and fast checklist/status reads have no extra preamble; expert questions
retain their single "Investigation R1 is scheduled." confirmation. The Identify
button retains its existing on-screen capture feedback.

Validation: 127 offline tests and both isolated browser suites passed. The camera
fixture feeds a completed voice transcript through the routing worker and checks
feedback before capture with both closed and open previews. Real Higgs routing of
two fixed English/Chinese text questions also sent feedback before a held fixture
tool: first PCM at 532 / 391 ms (two samples, no physical playback). No GPT calls
or photo uploads were made in that live check. Evidence: ignored
`out/tool-feedback-checks/live-results.json` and `out/merge-checks/`.
Current build: `2026-10-24-tool-feedback`.

## Current round-two integration (2026-10-24)

`fix/round2` is integrated with the background investigation queue. Failed route
recovery and bounded history retain per-turn ownership, while cancelled speech
releases the voice worker for the next question. Microphone input stays open in
speaker mode, and matching playback-drained acknowledgements still hold ready
offers until existing speech finishes. The filler prompt makes an exception for
accepting the latest investigation explanation offer with "Okay" / "好的".
Full session reset cancels old investigations, clears their cards/offers and
creates a fresh queue with the same limits.

Validation: 120 offline tests passed with real keys/local env-file loading disabled
and outbound sockets blocked. Both isolated Chromium suites passed with fake
media and fixture recognition/expert responses, covering sequential 40-second
PCM queues, camera/focus lifecycle, nonblocking investigations, consent-based
readout and reset. Reports are in ignored `out/merge-checks/` and
`out/camera-checks/`. This merge did not rerun real model APIs or a physical phone.
Frontend/backend build: `2026-10-24-round2-investigations`.

```bash
source /data/build/birding-demo/.venv/bin/activate
# BOSON_API_KEY, or the existing ~/.config/boson/api_key; never copy into the repo
python -B backend/server.py --port 8796
```

The backend serves the existing `web/` at `/` and WebSockets at `/ws`. A phone
needs HTTPS for microphone capture; use one tunnel for both. This is an
unauthenticated, in-memory hackathon demo: use a single uvicorn worker and do not
expose it publicly. Server restart loses session state.

## Earlier look-ui / round1 integration (2026-10-24)

Both router tool sets are retained: visual/pronoun requests use Look and the
read-only expert, while sound questions, greetings and garbage follow round1's
rules. Per-turn snapshots include focus ID, top-three fused candidates,
uncertainty and hint. History pruning is serialized and deferred until the
whole turn completes, including function outputs and the answer.

The checklist remains derived from confirmed observations, including accepted
BirdNET suggestions. Clear-all and reset clear observations/focus, frames,
sound evidence, heard history and pending suggestions; reset also reconnects
with empty conversation state. Fusion priors come from the same paginated iNat
allowlist query, not a second eligibility list.

Verification: 49 server + 27 expert + 4 model-free vision unit tests passed;
all web JavaScript passed `node --check`. Both headless browser suites passed
against isolated fixtures (camera suite also checks reset clears evidence).
Real Higgs on port 8797 routed mic check → `speak`, two mallards →
`add_observation`, “What's that?” → `look` with capture, clear-all →
`remove_observation`; reset reconnected successfully. With an explicitly empty
OpenAI key, a diet follow-up routed `ask_expert` and safely abstained with
`not_configured`; no client errors. Smoke was text/quiet mode, not acoustic or
real vision-model validation. Evidence: ignored `out/integration/` logs and
`web-self-passing-results.json` in this worktree.

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
- The mic defaults to **speaker** (`earphones=false`). Realtime input continues
  during assistant playback. In speaker mode, including an audio-enabled display,
  VAD starts are candidates: a non-echo final transcript cancels the old reply.
  Earphones without a speaking display can cancel immediately at VAD start.
- Guarding starts before the first outgoing assistant PCM. Browser `playback`
  reports track actual scheduled start and final source completion, including
  flush. Active reports hold automatic notices and BirdNET protection closed until
  reported end and PCM-duration estimate plus 500 ms; old/non-reporting clients
  use estimate plus 800 ms. These deadlines no longer withhold Realtime mic input.
  Tap still cancels immediately. Spoken “stop” / “等一下” cancels without a spoken
  acknowledgement and defers automatic notices until the next user turn.
- Speaker-mode BirdNET windows overlapping assistant playback or its tail are
  marked in the audio ring, dropped even if processed after playback, and logged
  as `bird_window_skipped` / `assistant_playback`. Earphone BirdNET is unchanged.
- Always-on STT echo defense remembers the last three spoken assistant captions
  (including partial captions before cancellation). Within ten seconds of speech,
  a contiguous match of at least three normalized English words (six tokens for
  Chinese, with each Chinese character a token) covering >=70% of the input drops
  it before routing and logs `echo_dropped`; shorter inputs and stop commands pass.
  Typed text is unaffected. Genuine repeated words can also be filtered: this is
  a conservative heuristic. Browser echo cancellation is requested; noise
  suppression and automatic gain control remain off. Browser support and actual
  speaker/mic acoustic performance still require device testing; see
  [the capture constraint documentation](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackConstraints/echoCancellation).
- The phone status toggle persists Speaker/Earphones in localStorage. The hint
  describes voice interruption with a Tap fallback; first run recommends
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

No database, authentication or export is implemented. Photo evidence and
observation candidates use the v0.2 path below. Nearby taxa use a cached 15 km / 60 day query. iNat failure does not
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

Historical gap (now fixed): Higgs STT was cumulative across silent turns. The
backend now sends input_audio_buffer.clear after each completed transcription.
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

## Protocol v0.2 observation backend

`Detection` is timestamped sound/image/description evidence. `Observation` owns
that evidence, fused candidates, status, count, notes, GPS, group and hint. One
focus per session handles pronouns and follow-ups. After 120 seconds without
user/frame activity the focus clears: open observations become discarded;
confirmed observations remain confirmed. A new focus discards an old open focus.
Session reconnects replay observation history as well as the derived checklist.

Checklist rows aggregate confirmed observations by their top species; the row ID
is the first contributing observation ID. Manual total edits consolidate the
species count into that observation and discard the other contributors; deletion
discards all contributors. History/evidence remains available in observations.
Vision never confirms automatically. Direct named sightings create a confirmed
observation with a resolved description detection. Logging remains silent + earcon.

Router additions: `look(hint?, group?)`, `get_observation`, `confirm_observation`,
`discard_observation`, `new_observation(group?)`, `describe(text, name?)`, and
`ask_expert(question)`. Existing tools and route/answer config separation remain.
`Session.ask_expert` calls the optional `NaturalistExpert` provider with a snapshot;
the structured result, grounding and fallbacks are described in `docs/EXPERT.md`.
Only explicit species names supplied to describe are resolved through iNat;
arbitrary descriptive prose does not become guessed evidence.

- `look` requests three frames from the mic only and stays silent until a frame
  arrives; WS `look` and `observation_action` use the same focus model. Optional
  `group` on look selects nonbirds; default is Aves. Displays can use these controls.
- `POST /api/frame?session=...&obs=...` accepts JPEG <=8 MiB, long side <=1920,
  optional normalized `x,y,w,h`. It calls loopback `8795/classify` with the protocol
  JSON, k=5, local bird candidates (or null for nonbirds/unavailable range list).
  Service errors return 503; malformed frames/crops 422; closed/stale focus 409.
- Frames are separate detections, not duplicate observations. In-memory <=512px
  thumbnails are served from `GET /api/frame/<detection id>.jpg`. Late inference
  is rejected if the focus changed. Observation updates fan out to all clients.
- First accepted frame per capture request queues a serialized answer with the
  **latest** fused top-three, uncertainty and hint. Subsequent frames update the
  evidence without repeating speech. No sharpest-of-three wait is implemented.
- Fusion: sum image log likelihoods + 0.5 times each sound log likelihood +
  log(local iNat count+1). Missing species in a detection get p=.01; probabilities
  are clipped at 1e-6. Resolved descriptions give a soft 2x boost (not certainty).
  Softmax considers only species seen in evidence, restricted to allowed_species
  for birds; nonbirds are open set. An unavailable/empty range list fails open.
  Uncertain means top1 <.5 or margin <.2. These are not calibrated probabilities.
- Each BirdNET window attaches its top five candidates once if within 10 seconds
  of focus activity; recent pre-look windows are also attached. Sound alone does
  not keep an otherwise idle focus alive. The existing BirdNET rarity/range filter
  are retained; proactive prompts follow the batching policy above.

### Current verification (2026-10-24 UTC)

```bash
/data/build/birding-demo/.venv/bin/python -B -m unittest discover -s backend -p test_server.py -v
/data/build/birding-demo/.venv/bin/python -B backend/selftest.py --serve
```

The first command runs 26 offline backend tests (no new packages required),
including synthetic fusion arithmetic, focus expiry, sound attachment, derived
checklist edits/deletes, frame/thumbnail ASGI requests, mocked vision HTTP and 503.
The second owns **only 8792**, starts/stops its own backend, and uses live Higgs
STT/routing/speech. It probes 8795/classify and mocks the provider in-process if
unavailable; it never starts/stops the other worker's vision service.

Passing live run: session `selftest-4a5d5e93`. Chatter: zero PCM; “What is that?”:
capture n=3 and zero PCM until cardinal.jpg was posted; image observation broadcast,
then “Looks like a Northern Cardinal.” (**94,080 PCM bytes per mic/display**).
“Add it. Two of them.”: focus confirmed, checklist count=2, earcon, zero spoken
acknowledgement. Both clients received identical checklist state; tap flush passed.
Vision was **mocked** because the listener on 8795 returned HTTP 404 for /classify.
No real BioCLIP end-to-end backend result is claimed. Earlier attempts encountered
a temporary port conflict and the 503 path before the in-process fallback was added.
Evidence is stdout plus test source (`backend/test_server.py`, `backend/selftest.py`),
not new log artifacts. Port 8792 was released after the run; 8780 was untouched.

Protocol clarifications for a future docs/PROTOCOL.md patch (not edited here):
checklist IDs represent an aggregate; edits consolidate/discard contributors;
frame limits/status codes and first-frame answer timing; optional `group` on WS
look (and router look/new_observation) to select nonbird open-set classification.
Remaining limits: in-memory, unauthenticated, unbounded outing history/thumbnails,
no tracker/deduplication of repeated frames, no calibrated fusion or field-accuracy
claim. The expert remains a stub; browser camera behavior was not tested here.

## Vision service

Windows CPU setup and the `backend/start_vision.ps1` launcher are documented in
[LOCAL_DEVELOPMENT.md](../docs/LOCAL_DEVELOPMENT.md#install-and-start-bioclip-on-windows).
Use the separate `.venv-vision` and `requirements-vision.txt`. The default Hugging
Face cache is now the repository's ignored `out/hf-cache/`, with `HF_HOME` still
taking precedence. The model is explicitly BioCLIP 2 rather than a library default.
For a portable HTTP check of an already-running service, use
`python backend/vision_smoke.py path/to/photo.jpg`.

Standalone, loopback-only BioCLIP 2 process; no changes to `server.py` are needed
for running it. The main backend must call this JSON API from its own venv.
Do not expose this unauthenticated service publicly. One process/worker only.

```bash
cd /data/build/field-companion-obs
export HF_HOME=/data/build/hf-cache TMPDIR=/data/build/tmp PYTHONDONTWRITEBYTECODE=1
CLIP=/data/build/birding-demo/.venv-clip/bin/python
# Already installed in this venv; needed when provisioning a fresh environment:
$CLIP -m pip install fastapi uvicorn
$CLIP -B backend/vision_service.py --port 8795 --threads 4
```

The model loads once during startup, warms once, and stays resident. Eager CPU
execution avoids `torch.compile` startup. Inference/decode run in a threadpool
under one process-wide lock; health checks remain asynchronous. Startup must
complete before HTTP is available. `/health` returns, for example:

```json
{"loaded":true,"model":"imageomics/bioclip-2","warm_ms":798,"load_ms":7170,"taxa":867455}
```

### JSON API

`POST /classify`, `Content-Type: application/json` (no query-param/binary API):

```json
{"jpeg_b64":"<base64 JPEG bytes>","crop":[0.485,0.46,0.05,0.085],"candidates":["Cardinalis cardinalis","Cyanocitta cristata","Notagenus notaspecies"],"k":5}
```

Illustrative response (scores depend on the image and candidate set):

```json
{"candidates":[{"sci":"Cardinalis cardinalis","p":0.99},{"sci":"Cyanocitta cristata","p":0.01}],"open_top":[{"sci":"Cardinalis cardinalis","p":0.90},{"sci":"Cardinalis phoeniceus","p":0.024},{"sci":"Piranga rubra","p":0.016}],"missing":["Notagenus notaspecies"],"sharpness":120.4,"crop_used":[0.47734375,0.44444444,0.065625,0.11666667],"ms":810.2}
```

- `candidates`: optional/null/empty means open TreeOfLife species top-k. Otherwise
  exact, case-sensitive scientific binomials (`Genus species`), with whitespace
  normalized and duplicates removed. Unknown names are skipped and returned in
  `missing`. An all-unknown list returns empty `candidates`, NOT open predictions.
- **No text encoding:** pybioclip 2.1.6 stores `txt_embeddings` as `[768,867455]`;
  taxon rows in `txt_names` end with genus and epithet. The service indexes the
  corresponding embedding columns, sharing one image encoding with the open
  reference. Restricted scores softmax only over matched rows, aggregating any
  duplicate binomials (115 in the full vocabulary). They equal full TreeOfLife
  probability mass restricted and renormalized, not the probe's CustomLabels head.
  Up to 16 candidate subsets are cached in RAM; no custom-head generation/cache files.
- `open_top`: always the top three full-vocabulary rows on the **same crop**, with
  their original open probabilities, even for a restricted request. Top-k scores
  are not renormalized after truncation; request `k >= matched taxa` for fusion.
  These are model softmax scores, **not calibrated confidence**.
- `crop`: optional `[x,y,w,h]`, normalized in EXIF-oriented image coordinates.
  The ROI gains 15% padding on each side, expands to a pixel-space square, shifts
  inside the image and clips to image bounds before the standard 224×224 resize.
  `crop_used` reports the actual integer-aligned normalized ROI; no crop is
  `[0,0,1,1]`. No automatic bird detector is included.
- `sharpness`: variance of a four-neighbor grayscale Laplacian with replicated
  borders, on the inference ROI **before resizing**. Useful for comparable frames;
  different crops/resolutions can change it independently of focus quality.
- `ms`: processing plus lock wait for `/classify`; excludes HTTP transfer/JSON parsing.
- Limits: JPEG only, 8 MiB decoded bytes, 20 megapixels, up to 512 candidates,
  `1 <= k <= 512`. Invalid images/coordinates return HTTP 422. Keep requests local
  and bounded: these validation limits are not a streaming HTTP body-size firewall.

`POST /classify_batch` takes `{"frames":[<classify request>,<classify request>]}`,
1–8 frames, each with its own optional crop/candidates/k. Returns
`{"frames":[<classify response>,...],"sharpest_index":1,"ms":1620}`.
The zero-based sharpest index refers to the largest ROI Laplacian variance
(first index wins ties). Batch inference is sequential under the same lock to
bound peak memory; per-frame `ms` excludes lock wait, outer `ms` includes it.
There is no fusion or temporal independence assumption inside this service.

A client can avoid shell-sized base64 strings:

```python
import base64, requests
payload = {"jpeg_b64": base64.b64encode(open("bird.jpg", "rb").read()).decode(),
           "candidates": ["Cardinalis cardinalis", "Cyanocitta cristata"], "k": 2}
print(requests.post("http://127.0.0.1:8795/classify", json=payload, timeout=30).json())
```

### Verification and measured results

```bash
$CLIP -B backend/test_vision.py                         # four offline unit tests
$CLIP -B backend/test_vision.py --integration --port 8793
```

The integration test starts/stops only its own service, refuses an occupied port,
prints JSON predictions/timings to stdout, and writes no image/log artifacts.
It independently verifies restricted probabilities in a short-lived model process
before launching the HTTP process. It reuses the 60-species list at
`/data/build/birding-demo/vision-imgs/candidates.json` (`--candidates` can override).

Executed 2026-10-24 UTC with pybioclip 2.1.6, torch CPU, four threads:

- Four offline tests passed (crop geometry/bounds, validation, decode, dedup/limits).
- Full HTTP integration passed on **8792**; 8795 was occupied and left untouched.
  The owned test process was stopped afterward. An initial run refused occupied
  port 8795 after its subset check; it did not launch/stop a service there.
- Exact subset verification: **57/60** names found, max absolute error
  **3.43e-8** versus full TreeOfLife probabilities renormalized over the shortlist;
  open top-three also matched `TreeOfLifeClassifier.predict`. Text encoding was
  replaced with a failing stub during verification, proving no request-time use.
- Missing exact names: `Nannopterum auritum`, `Astur cooperii`,
  `Corthylio calendula`. No synonym guessing is performed.
- All seven `/data/build/birding-demo/img/*.jpg` were classified open and local60.
  Open bird top-1: barred owl → `Strix varia` (0.975), blue jay →
  `Cyanocitta cristata` (0.995), cardinal → `Cardinalis cardinalis` (0.983),
  yellow-rumped warbler → `Setophaga coronata` (0.646). Local60 correctly suggested
  jay/cardinal/warbler; barred owl is absent and was forced to an unrelated bird.
- Nonbirds worked open: ladybug → `Coccinella septempunctata` (0.332),
  monarch → `Danaus plexippus` (0.764), trillium → `Trillium grandiflorum` (0.985).
  Restricting these to birds unsurprisingly returned birds: retain `open_top`.
- Synthetic `vision-imgs/00-6.jpg`, manually estimated bird ROI
  `[0.485,0.46,0.05,0.085]`: without ROI, open top-1 `Fusarium dactylidis`
  (0.0023), local top-1 `Spizella passerina` (0.208); with padded ROI, both
  identified `Cardinalis cardinalis` (open **0.904**, local **0.998**).
  This is one privileged synthetic crop, not detector/field accuracy evidence.
- HTTP latency (9 requests/mode: 7 source images + synthetic without/with crop):
  open median **812 ms**, range **795–928 ms**; local60 median **806 ms**, range
  **795–817 ms**. Both modes include open reference scoring and one image encoding.
  Model load **7.17 s**, warm inference **798 ms**, excluding Python/import startup.
- Batch sharpest-index check passed: blurred/original cardinal variances
  **2.18 / 926.28**, index **1**, two-frame HTTP **1623 ms**. `/health` responded in
  **1.86 ms** while a three-frame batch was still running. Unknown/all-unknown
  candidates, full-vector normalization, and HTTP 422 validation checks passed.

Gaps: no main-backend wiring in these files, no detector/tracker, synonym mapping,
score calibration, queue limits, authentication, persistence, or cross-frame fusion.
A restricted list can confidently select the wrong species for out-of-list inputs.
The full embedding matrix/encoder require substantial resident RAM; multiple
workers duplicate it. Tests are smoke tests, not held-out field validation; see
`docs/vision-test.md` for the synthetic-distance and training-overlap limitations.

Final rerun after subpixel crop-boundary hardening also passed all four offline
and full integration tests on **18795**, with nonbird top-1 assertions enabled.
That owned service was stopped too. Rerun HTTP medians: open **805 ms**, local60
**801 ms** (9 each); two-frame batch **1607 ms**; concurrent health **1.16 ms**;
model load **7.10 s**, warm **789 ms**. Subset error remained **3.43e-8**.
