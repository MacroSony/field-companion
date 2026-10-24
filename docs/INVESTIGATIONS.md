# Background investigations

Expert questions run independently of the real-time voice turn worker. Higgs
acknowledges the task immediately, then continues routing questions and sightings
while the existing naturalist provider retrieves sources and analyzes evidence.
This adds task orchestration; it does not add autonomous web research or new
species data sources.

## Using it

- Ask “Could it be a Hairy Woodpecker?” or “它吃什么？” about the current focus.
  The acknowledgement names a task such as `R1`.
- Continue talking, recording observations or looking at another organism.
- Ask “How is investigation R1 going?”, “Any results?” or “第一个调查到哪一步了？”
  to get the actual recorded stage. No percentages or estimated completion times
  are generated.
- Say “Cancel investigation R1” / “取消第一个调查”, or use the task card's Cancel
  button. Running work is cancelled locally; queued work never starts.
- Say “Try R1 again” / “继续查第一个”, or use Investigate again. This creates a
  fresh task using the saved original question and evidence. It is a new attempt,
  not resumption of an unfinished model's hidden reasoning.
- Completed answers and source links remain on the Investigations cards. A voice
  notice only says that a task is ready and asks whether to explain it; findings
  are never read automatically. Say “Yes, explain it”, “Explain R1” or “讲一下
  第一个结果” to hear the saved answer. If several tasks are ready, select one.
  A requested result includes its task ID and original subject so it is not
  mistaken for the currently photographed organism.
  The read-only display shows a compact task state in its header, keeping the
  live captions visible; full task cards and controls are on the phone.

## Scheduling and state

`backend/investigations.py` owns one queue per session. It runs one investigation
at a time, with a maximum of five active tasks (one running and four waiting).
Identical active questions with identical evidence share the existing task. Each
task stores an independent snapshot of up to 12 detections and five candidates.
New questions and changes of focus do not invalidate that snapshot or its result.
Jobs never confirm species or change checklist counts. A returned capture hint
can update the live observation only when the original snapshot is still the
current, open observation and its evidence/status have not changed.

States are `queued`, `running`, `completed`, `failed`, `timed_out` and `cancelled`.
Running stages are `retrieving` and `analyzing`; these come from the actual source
and model-call boundaries. “Analyzing” does not expose the model's internal
progress. API/network failures and exhausted budgets terminate the task without
holding the voice worker. The next queued task can then start.

Completion immediately updates the screen. Multiple ready tasks are combined
into one short offer, such as “Also, investigations R1, R2 are ready. Which would
you like me to explain?” The existing serialized voice worker yields to queued
user input, ongoing user speech, pending transcription, assistant playback and
its echo tail. If a user is speaking, their next reply is answered and played
before the offer. An in-flight session update rechecks this guard before starting
the notice. Offers remain
deferred in Quiet mode, including after Tap; a user-requested status/result read
uses the normal Tap/Quiet rules. A voice interruption ends a notice without
deleting the saved answer. Notification generation has at most one automatic
retry; the completed answer remains readable if voice delivery fails.

The voice model receives a fresh compact task summary for routing. Tools:

- `ask_expert(question)`: create a task and return its acknowledgement/ID.
- `get_investigations(id?, selection?, explain=false)`: read progress and offer
  an explanation; only `explain=true` reads completed answers. Accepting a single
  offer targets the offered job even if newer tasks exist. Accepting a multiple
  offer without selecting a task asks which result to explain.
- `manage_investigation(action, id?, selection?)`: cancel or retry.

`selection` is `first`, `latest` or `all`. Explicit “first” refers to `R1`, not the
next running task. Cancelling an already completed task cannot cancel a different
active task. Without a selector, cancel targets the latest active investigation.

The phone advertises `playback_protocol: 1` in `hello`. The backend marks each
audio reply pending **before** sending its `audio_start` marker and PCM, then
sends `audio_end` after synthesis. The client acknowledges `response_id` and
`drained` only once all chunks are received and its WebAudio queue is empty.
Queued/suspended playback therefore blocks notices, and stale acknowledgements
cannot release a newer reply. Legacy clients keep the duration/playback fallback.
Audio chunks remain sequential even when more than 30 seconds are queued; the
playback clock resets only for a new AudioContext or an explicit flush. Late
chunks and completion events from a different response are ignored.

## Time limits and retention

Configuration reads environment variables, then the existing ignored `.env.local`.
No credential migration or new key is required.

| Setting | Default | Range |
| --- | --- | --- |
| `INVESTIGATION_TIMEOUT_SECONDS` | 180 seconds per running task | 1–900 |
| `INVESTIGATION_REQUEST_TIMEOUT_SECONDS` | 120 seconds per expert call | 1–600, capped by the task limit |
| `EXPERT_TIMEOUT_SECONDS` | 7 seconds | 1–15, for direct standalone provider calls/smoke tests |

Background investigations explicitly override the old seven-second provider
deadline. The existing single expert call has a 120-second default deadline,
including source retrieval (still bounded to two seconds), with a separate outer
task budget. Queue waiting does not consume a running task's budget. OpenAI SDK
automatic retries remain disabled; explicit retry creates a new task. See the
[official SDK timeout/retry documentation](https://developers.openai.com/api/reference/python)
for the client options used by the provider.

Up to 20 tasks are retained **in the current backend process**, with oldest
finished tasks removed first. Results and active work survive a WebSocket/phone
reconnection to the same session. With no clients, the existing ten-minute
session expiry eventually closes the session and cancels work. Server restart
loses this in-memory task history, as it does the existing checklist. Persistent
storage and service-restart recovery are future work.

Cancellation stops the local coroutine/request wait; this implementation does
not use OpenAI's provider-side background-response cancellation API.

## Verification

Offline tests use events/fixtures rather than sleeping for minutes or contacting
an API. They cover serialization, original-evidence isolation, progress, queue
capacity/deduplication, timeouts/failures, cancellation, late results, retention,
retries, voice responsiveness and notification guards.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s backend -p 'test_*.py' -q
```

The camera browser test also covers task cards, continued interaction, cancel,
retry, reconnection snapshots, Quiet-mode completion and read-only displays:
it uses a fake microphone and deterministic canvas-backed video MediaStreams,
with synthetic permission failures, rather than physical camera permission checks.

```powershell
# See CAMERA.md for isolated Chrome/CDP setup.
.\.venv\Scripts\python.exe -X utf8 -B web/camera-test.py --serve
.\.venv\Scripts\python.exe -X utf8 -B web/camera-test.py
```

Optional live validation uses one real GPT/source request and real Higgs routing
and audio, followed by a **simulated 61-second expert task**. Typed questions and
synthetic evidence replace the physical microphone/camera, and PCM is collected
without playback. This proves scheduling and voice integration, not minute-long
research quality or physical phone performance.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B backend/investigation_smoke.py --live
```

The report is written to ignored `out/investigation-checks/live-results.json`.
Local validation on 2026-10-24 passed 88 offline tests, the camera/investigation
browser suite and the existing full web regression (phone and both display sizes,
audio/reconnect/export/offline shell, no runtime exceptions). The live check
completed a real source-backed Chinese GPT answer and a simulated task lasting
61.0 seconds. While that task ran, five typed voice-service turns completed in
14.73 seconds total: checklist (2.875 s), progress (2.875 s), another investigation
(3.328 s), cancel (2.719 s) and a further investigation (2.922 s). These measure
through generation completion, not time to first audio. Completion was deferred
in Quiet mode and later generated audio for the original target; no checklist
entry or new target's hint was changed. Physical phone/microphone/speaker and
field-recognition acceptance remain separate.

After merging the parallel recognition/runtime branch, retain its fresh-state,
history and reset behavior along with this independent queue. A full session reset
should close/cancel the previous investigation queue before creating a new one.

## Ready offers and playback regression — 2026-10-24

The follow-up changes automatic result readouts into short, batched explanation
offers. Offline tests cover no findings in the offer, status-only reads,
single-offer acceptance, multiple-offer selection, user-speech/next-reply ordering,
matching playback acknowledgements, stale response events and the session-update
race. A real Chromium check queues 40 seconds of PCM while WebAudio is suspended
and verifies every chunk starts after the preceding chunk ends, without waiting
40 seconds in real time. The camera browser suite checks a ready card first,
then a short offer, and an explanation only after a simulated explicit request.

Real Higgs checks with fixture expert results confirmed that “好的。” routes to
the offered investigation and “先不讲。” stays silent. A separate live check used
one real source-backed GPT result, an explicit explanation request, and a
simulated 45-second investigation while normal questions continued. These checks
collect PCM without physical playback; the mobile device remains the final
audible acceptance check. Reports are in ignored `out/investigation-checks/`.
