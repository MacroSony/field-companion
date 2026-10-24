# Expert integration validation — 2026-10-23

This report records local validation on `feat/expert`. It does not establish
naturalist accuracy or a real-camera end-to-end result.

## Offline regressions

Run from the repository root with Python 3.12:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s backend -p 'test_*.py' -v
```

All **72 tests passed** after merging the observation-backend and batched-suggestions
branches, including the original expert/backend/vision regressions and new cross-feature checks. Expert
tests use the installed OpenAI SDK with a mock HTTP transport, exercising strict
structured output, timeout/cancellation, provider failures, 429 cooldown, grounding
references, exact supporting quotes, per-request reference enums, English/Chinese
language and species-name binding, bounded context, source caching, stale results, focus isolation, hint
reuse, quiet mode and the ordinary-path fallback. No OpenAI request is made by
these tests, even when a local key exists.

## Live checks

- A real Wikipedia scientific-title lookup succeeded for `Dryobates pubescens`.
  The returned extract contained 1,209 characters. This verifies source transport,
  not that it contains every fact a user might request.
- The initial HTTP 429 restriction was resolved after API-tier activation. Real
  `gpt-5.4-mini` requests now complete successfully with the saved credential.
- Live testing exposed translated species-name mistakes, an unsupported diet
  generalization and confusion between an observation ID and a detection ID.
  The integration now retains supplied species identifiers, requires matching
  source excerpts, constrains reference IDs in the request schema and validates
  response language. These checks do not establish semantic accuracy in general.
- The final real Higgs → real GPT → Higgs-audio session used synthetic, unconfirmed
  Downy/Hairy Woodpecker evidence. It did not use a microphone, camera or audio
  playback. Both expert results passed schema, reference, language and quote checks.

| Typed question | Selected Higgs tool | Result | Turn duration |
| --- | --- | --- | --- |
| Could it be a Hairy Woodpecker? | `ask_expert` | Explained uncertainty; returned audio | 6,125 ms |
| 它吃什么？ | `ask_expert` | Conditional Chinese diet answer for Downy Woodpecker, backed by its source excerpt; returned audio | 4,000 ms |
| How many species today? | `get_checklist` | Answered zero species without the expert | 2,797 ms |
| Yes, add them all. | `accept_suggestions` | Confirmed two synthetic BirdNET suggestions with one earcon, no speech and the original camera focus preserved | 719 ms |

The first three cases returned audio and added no checklist observation. The final
confirmation created two sound-evidence observations; a subsequent count edit also
preserved the open focus. No extra GPT call was made for confirmation or editing.
Durations are
single synthetic typed-input trials through completion of response generation,
including routing, GPT when selected and Higgs audio. They are not time to first
audio or microphone-to-playback latency. The earlier 429 cooldown/fallback checks
also passed before activation; the final run exercised successful live GPT results.

## Upstream merge and browser validation

- Integrated `origin/feat/observation-backend` at `f8ca74d`, `origin/main` at
  `952d174`, and `origin/feat/batched-suggestions` at `c9b4a74` into `feat/expert`.
- Batch acceptance and multi-species reports write confirmed `Observation` records;
  the checklist remains derived. Sound suggestions retain sound evidence and do
  not discard an open camera observation. Speaker echo events do not invalidate
  expert results; genuine earphone-mode barge-in does.
- The full `web/self-test.py` passed in an isolated headless Chrome with fake mic/GPS:
  390×844 phone, 1280×720 and 1920×1080 displays, shared checklist/map updates,
  exports, reconnect, pause, offline shell, audio unlock and zero runtime exceptions.
- New browser checks passed for suggestion-chip accept/dismiss messages, persisted
  Speaker/Earphones changes, actual playback start/end reports and stop/start
  playback. The restarted context scheduled 20 PCM buffers within 0.775 seconds
  of its own clock, rather than reusing the old AudioContext timeline.
- Browser suggestion clicks were checked against mock transport; backend acceptance
  and focus/count effects were covered offline and by the real Higgs confirmation.
  BirdNET suggestions and visual focus in the live check were synthetic fixtures,
  not field detections. No physical camera or speaker feedback test was run.

## Remaining acceptance checks

1. Expand repeatable evaluation of supported facts, conflicting image/sound evidence, Downy/Hairy and
   Cooper's/Sharp-shinned alternatives, Chinese replies and requests for more evidence.
2. Test the real phone camera → BioCLIP → expert → Higgs loop together with Look UI.
3. Confirm uncertain IDs remain tentative and ordinary logging retains its latency.
