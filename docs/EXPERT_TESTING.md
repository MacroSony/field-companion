# Expert integration validation — 2026-10-23

This report records local validation on `feat/expert`. It does not establish
naturalist accuracy or a real-camera end-to-end result.

## Offline regressions

Run from the repository root with Python 3.12:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s backend -p 'test_*.py' -v
```

All **56 tests passed** locally, including 26 new expert tests and the existing
30 backend/vision tests. Expert
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
| Could it be a Hairy Woodpecker? | `ask_expert` | Explained uncertainty and asked for a side view; returned audio | 6,797 ms |
| 它吃什么？ | `ask_expert` | Conditional Chinese diet answer for Downy Woodpecker, backed by its source excerpt; returned audio | 3,360 ms |
| How many species today? | `get_checklist` | Answered zero species without the expert | 2,891 ms |

All three cases returned audio; none added a checklist observation. Durations are
single synthetic typed-input trials through completion of response generation,
including routing, GPT when selected and Higgs audio. They are not time to first
audio or microphone-to-playback latency. The earlier 429 cooldown/fallback checks
also passed before activation; the final run exercised successful live GPT results.

## Remaining acceptance checks

1. Expand repeatable evaluation of supported facts, conflicting image/sound evidence, Downy/Hairy and
   Cooper's/Sharp-shinned alternatives, Chinese replies and requests for more evidence.
2. Test the real phone camera → BioCLIP → expert → Higgs loop together with Look UI.
3. Confirm uncertain IDs remain tentative and ordinary logging retains its latency.
