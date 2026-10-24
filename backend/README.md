# Field Companion v0 backend

From the repository root:

```bash
source /data/build/birding-demo/.venv/bin/activate
# BOSON_API_KEY, or ~/.config/boson/api_key (never copied into this repo)
python backend/server.py --port 8765
```

Open `http://localhost:8765/`. `web/` is served with `StaticFiles(html=True)`;
if the directory is absent at startup a placeholder is served instead. An existing
but unfinished `web/` without `index.html` returns 404. Use a single HTTPS tunnel
for the page and `/ws` when testing a real phone (microphone requires HTTPS).
`--host` defaults to `0.0.0.0`. Do not expose this unauthenticated demo publicly.

The provided venv includes FastAPI, uvicorn, websockets (the `additional_headers`
API), requests, numpy, scipy, birdnetlib and the compatible TFLite shim. No model
files or credentials are vendored. The first BirdNET window lazily loads models.

## Implemented

- `docs/PROTOCOL.md` message names and PCM rates: 48 kHz mono PCM16 in,
  24 kHz mono PCM16 out; hello/state/checklist, text, mic, quiet mode, tap,
  edit/delete, captions, flush and local earcons.
- One Higgs session per phone; model pinned in URL **and** session, server VAD,
  Higgs STT 3.1, bounded audio queue and automatic upstream reconnection with
  checklist context. Audio during an upstream outage still feeds BirdNET but is
  not replayed to Higgs after reconnect.
- Stateful 63-tap anti-alias FIR and phase-correct 2:1 downsampling, a 30-second
  48 kHz ring, and a dedicated single-thread BirdNET executor per connection.
  Three-second windows every 1.5 seconds; busy workers skip rather than backlog.
- iNaturalist zh-CN autocomplete filtered by iconic group, count/notes merging,
  count correction, deletion, recent detection history, and cached nearby taxa
  from a 15 km radius over the past 60 days. GPS defaults to Toronto.
- Notifications need two >=0.5 detections in distinct windows within 10 seconds,
  never auto-log, and queue while a response/user speech/playback is active.
  Quiet mode defers proactive speech; tap allows the next addressed utterance.
- Mutations return tool output and checklist + logged earcon without
  `response.create`; ignore returns nothing; read tools get a follow-up response.

**Silent logging is enforced:** Higgs sometimes emits speech *before* a tool call,
so outgoing audio/captions are buffered until `response.done`. Tool-turn preambles
are discarded; non-tool responses are forwarded as the original PCM delta frames.
This adds approximately one response-generation interval to spoken-answer latency.
Playback duration is tracked independently of generation completion, so barge-in
flushes even audio already queued on the phone. The expected Higgs
`response_not_active` on cancelling completed generation is harmless and suppressed.
Higgs rejects `role: system` conversation items; context is injected as explicitly
non-addressed user text, with behavior controlled by session instructions.

## State and limits

The protocol has no phone identity/session token. For this v0, **all connected
phones share one in-memory outing**; changes broadcast to every phone. Phone
reconnections get the current checklist, but server restart loses it. BirdNET
history/notification deduplication are per connection. Use a single uvicorn worker.
There is no database, authentication, export, photo ID or dedicated candidate-card
identification pipeline. `nearby_species` provides names/photos to the model but
this server does not fabricate candidate cards. iNat failure leaves logging
unmodified and reports an error; BirdNET can fall back to English display names.
BirdNET detections down to 0.25 are displayed/history, but only >=0.5 detections
count toward proactive notification. They are unconfirmed, not checklist entries.

## Tests and evidence

```bash
python -B -m unittest discover -s backend -p 'test_*.py' -v
# With the server running:
python -B backend/selftest.py
# If 8765 is occupied:
python -B backend/server.py --port 8766
python -B backend/selftest.py --url ws://127.0.0.1:8766/ws
```

The self-test uses the exact `tts()` definition in `probes/realtime_probe.py`,
loaded via AST without its module-level `probes/out` directory side effect.
It mixes Chinese TTS at 8 seconds into the first 20 seconds of cardinal audio,
upsamples TTS 24→48 kHz, streams real-time 40 ms packets, prints all received JSON
and counts PCM bytes. It then exercises manual edit/delete and a retrospective
text question (retrying once if Higgs returns no answer). It never plays audio.

Evidence is kept under `backend/`:

- `unit-test.log`: 10 offline tests covering dispatch, silence on logging/ignore,
  quiet/tap audio gating, odd-packet decimation phase, barge-in, notification
  threshold/dedup/checklist guard, manual edits, and mocked upstream reconnect
  with model settings and checklist reinjection.
- `selftest.log`, `server-selftest.log`: final live run passed. 2,112,000 input
  bytes; **556,800 output PCM bytes**, **12 heard messages**, no phone error messages.
  Chinese STT logged `Cardinalis cardinalis`, count **2**, notes `在灌木丛里`,
  with an earcon and no spoken logging preamble. The proactive prompt was Chinese;
  barge-in produced `flush`. Manual count **3**, deletion, and `recent_sounds`
  with a Chinese spoken answer all worked. The backend root returned HTTP **200**.
- `selftest-previous.log`, `server-selftest-previous.log`: prior live run, including
  successful Chinese transcription and count=2 logging, 12 heard messages,
  proactive speech and flush, successful manual edit to 3 then deletion. That run
  hit a transient Higgs `upstream_error` on the final retrospective text question.

Port 8765 was already occupied by an unrelated Python process, so live tests used
8766 and left the existing listener untouched. A direct live iNat nearby query returned 60 bird taxa.
No claim of live forced-disconnect reconnection testing: reconnection is covered
by the offline fake-upstream test. Test server processes are stopped on completion.
