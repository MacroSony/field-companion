# Look and expert validation — 2026-10-24

Result: the local Look/expert path is ready for combined integration testing.
This is not phone/field acceptance or a recognition-accuracy benchmark.

## Scope and versions

- Baseline: `feat/look-ui` at `f83eb97`, with the fixes described below.
- Python 3.12 on Windows; the existing CPU BioCLIP-2 service on port 8795.
- Real Higgs routing/audio generation and real `gpt-5.4-mini` requests, using
  isolated test sessions. Typed questions and public iNaturalist reference-photo
  files replaced the physical microphone and camera. PCM was collected, not played.
- BirdNET and its geographic eligibility filter were excluded from the live
  Look/expert session, to isolate the work under test. Their complex-environment
  recognition fixes remain the parallel workstream.
- Fetched remote `fix/round1` at `72a1d9e` and `integ/look-round1` at `b321f0d`.
  These branches were inspected statically and were **not merged or executed**.
  Automatic approval rejected executing the remote branch with live credentials;
  its complete runtime behavior is therefore not validated by this report.

## Problems reproduced and fixed

1. **New target did not capture.** `new_observation` created an empty focus but
   never sent `capture`. It now starts the new focus and calls the existing Look
   capture path. Confirmed records survive; the old open target and its hint do
   not carry into the new target. Invalid groups are rejected before changing state.
2. **Pronoun confirmations lost photos.** In live trials, “There are two of them.
   Add them.” and “Add it.” selected `add_observation`, creating another record
   with voice evidence. Routing now receives the current focus, status, top
   candidates and image count, with an explicit rule to confirm this observation.
   The final live trial retained both original observation IDs and photo evidence.
3. **Chinese question became English.** Higgs rewrote “它吃什么？” into an English
   tool argument. GPT then correctly answered that rewritten question in English.
   The worker now passes the original typed/STT question to the expert and clears
   it after the turn; the router's question is only the direct-tool fallback.
4. **Expert speech changed or expanded the answer.** Higgs omitted scientific
   identifiers or appended the UI hint under the general answer prompt. Expert
   answers now use a scoped readout instruction containing only the final answer.
   The setting is cleared even when generation fails. In the final live trial,
   all three audible expert answers matched the expert text after punctuation
   normalization. This remains model instruction, not a deterministic TTS guarantee.
5. **Comparison hints could invent unsupported field marks.** A trial mentioned
   the alternative species' appearance without a supplied source for that species.
   The expert prompt now applies the source restriction to hints as well, requests
   neutral views when a comparison is unsupported and avoids asking field users
   for source excerpts. The subsequent trials did not repeat that unsupported
   appearance claim. Exact-quote validation still does not prove semantic entailment.

## Final automated checks

All **76 offline tests passed**, including four added regressions covering new
target capture/retention, current-focus context, original-question preservation
through the turn worker and readout cleanup on a generation failure.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s backend -p 'test_*.py' -q
```

The camera browser regression also passed against the updated backend, using
an isolated headless Chrome with fake media and the fixture classifier:

- 390×844 phone; 1280×720 and 1920×1080 read-only displays; no runtime exceptions.
- Three-frame burst, 2× digital zoom, normalized crop and one uploaded JPEG.
- Look again preserves focus; stale uploads cancel after discard/target changes.
- Confirmed counts/checklist, failed upload recovery and display synchronization.
- Camera permission denial preserves the mic; background/close/end release video;
  late permission grants release their tracks; replaced phones do not reconnect.

See [CAMERA.md](CAMERA.md) for the browser-test setup. Windows Chrome test
profiles must use a short path, such as `out/c-12345678`, to avoid cache path limits.

## Final live sequence

One continuous isolated session completed all **11 typed turns** below, plus
three real BioCLIP image uploads. Each route matched the expected tool.
All explicit expert questions left the checklist unchanged. No backend errors
occurred. Durations are single trials through generation completion, excluding
subsequent playback waiting; they are not time-to-first-audio measurements.

| Input | Route / observed result | Duration |
| --- | --- | --- |
| What bird is that? | `look`; capture requested | 609 ms |
| Blue Jay reference photo | BioCLIP top match Blue Jay; no GPT call | 5,235 ms |
| What does it eat? | `ask_expert`; conditional Blue Jay diet with matching source quote; exact readout | 4,390 ms |
| Could it be a Northern Cardinal? | `ask_expert`; tentative Blue Jay comparison; no checklist change | 4,375 ms |
| Can you look again? | `look`; same observation ID | 453 ms |
| Same Blue Jay photo again | Second image attached to the same observation; no GPT call | 6,859 ms |
| There are two of them. Add them. | `confirm_observation`; same ID, both photos retained, count 2; silent | 578 ms |
| How many species today? | `get_checklist`; one species, no GPT call | 3,344 ms |
| Look at a different bird. | `new_observation`; new focus and capture request | 390 ms |
| Northern Cardinal reference photo | New focus identifies Northern Cardinal; previous record retained | 6,578 ms |
| 它吃什么？ | `ask_expert`; original Chinese preserved, Chinese abstention for missing diet information | 3,266 ms |
| Add it. | `confirm_observation`; original Cardinal photo/ID retained; silent | 390 ms |
| Actually, make that four Northern Cardinals. | `set_count`; total 4, no GPT call; silent | 1,938 ms |
| What habitat does it live in? (quiet mode) | `ask_expert`; source-backed habitat result, no audio or checklist change | 3,031 ms |

Final checklist: Blue Jay 2 and Northern Cardinal 4, both with `source=photo`.
Reference photographs are clear known examples, not held-out field data.
Repeated frames are correlated and their fused scores must not be read as
calibrated identification probabilities.

The existing opt-in smoke check also passed with synthetic uncertain
Downy/Hairy Woodpecker evidence and real GPT/source requests:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B backend/expert_smoke.py --live
```

- Hairy alternative: `request_more_evidence`, a practical head/bill/full-body hint,
  no definitive species replacement (2,484 ms).
- Chinese diet: conditional Downy Woodpecker answer with a matching diet excerpt
  and supplied species identifier (2,032 ms).

Local raw artifacts are ignored: `out/local-checks/look-expert-before.json`,
`look-expert-language-fix.json`, `look-expert-live-results.json` and
`out/camera-checks/results.json` plus screenshots. No credentials are recorded.

## Merge and field acceptance

1. Combine the friend's recognition/runtime fixes with this branch and rerun the
   offline and camera checks. Both branches change `Session.config` and routing
   instructions: retain the friend's fresh state/history/reset behavior **and**
   the scoped expert readout, original turn question and pronoun-confirmation rule.
   A combined focus snapshot may serve both paths; do not lose current focus status.
2. On a real phone over HTTPS, test rear-camera permission, tap crop, autofocus,
   zoom, movement/blur, weak network, reconnection and camera/mic coexistence.
3. Exercise mixed bird calls and photographed organisms, then interrupt an expert
   answer or switch targets while it is pending. Recheck old-answer suppression
   with the friend's response-correlation/reset changes.
4. Expand factual retrieval/evaluation. The current 6,000-character extracts for
   the top two candidates can omit diet or a proposed alternative. Northern
   Cardinal diet was such a case: safe abstention passed, but factual coverage is
   still incomplete. GPT receives structured rankings, not image pixels, and
   cannot independently inspect a field mark.

Python server processes must be restarted to load the fixes. The existing
long-running main server was left intact to preserve any in-memory user session;
live verification used fresh isolated Session instances of the updated code.
