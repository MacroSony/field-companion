# GPT Realtime vs Higgs: project regression comparison

Run on 2026-10-24, America/New_York, on `feat/gpt-realtime`. The production
provider implementation and prompts tested are from `2e3bd40`.

GPT selected the intended app action more reliably on this synthetic regression
set. Higgs selected tools faster. These results support continuing the GPT phone
experiment with the shared safeguards enabled. Physical-phone speech quality,
echo, long conversations and recognition accuracy still need their own tests.

## Results

| Measurement | Higgs Realtime 1.5 preview | GPT Realtime 2.1 |
| --- | ---: | ---: |
| Correct tool + meaningful arguments, text | 95/112 (84.8%) | 106/112 (94.6%) |
| API completion, text | 111/112 | 112/112 |
| Median tool-routing time, text | 625 ms | 813 ms |
| P95 tool-routing time, text | 1,031 ms | 1,125 ms |
| Correct tool + meaningful arguments, synthetic audio | 13/14 | 14/14 |
| Exact normalized input transcript, synthetic audio | 13/14 | 13/14 |

Text: 56 scenarios, two rounds per provider, 224 samples. Audio: 14 identical
recordings, one round per provider, 28 samples. The audio subset contains eleven
English and three Chinese scripts, spoken by local Windows voices. Both sets
passed paired prompt/history/input-hash verification. Diagnostic pilot runs are
excluded. All 285 offline regression tests passed, including 23 new benchmark,
scorer, transcript collection and report tests.

Routing time measures the route-session update and generation, excluding
connection setup, input speech, tool work and spoken playback. Every sample
starts a fresh conversation. These timings are approximate desktop measurements,
not measurements of perceived phone response time. Higgs had one text sample
that timed out after its retry; technical failures stay visible in the total.

## Where the difference appeared

- Checklist actions: GPT 24/24; Higgs 22/24. In one Higgs response, removing the
  mallards selected `all=true`. The shared removal guard rejects this argument
  because the user did not request clearing everything.
- BirdNET/sound workflows: GPT 12/12; Higgs 8/12. Higgs sometimes selected a new
  named observation instead of accepting an existing sound suggestion, losing
  the intended evidence/confirmation workflow, or emitted multiple calls.
- Task progress: GPT 16/16; Higgs 15/16. Higgs once omitted the requested first-task
  selector and would ask which task rather than reporting the first one.
- Camera selection: both 18/20. Both repeatedly treated a description of the
  current subject's red head/white wing stripes as a new observation instead of
  attaching it to the existing focus.
- Expert requests: both 20/20. These tests check scheduling/reading/cancelling/
  retrying tool choices; they do not execute or grade the separate GPT naturalist.
- Companion chatter and ambiguous consent remain uneven. Both answered the
  snacks question in the text runs; GPT ignored it in the audio run. Both selected
  acceptance on a bare “Okay” at least once in raw routing. The application filters
  many such acknowledgements before routing and keeps the shared consent guard.

On the 112 paired text trials, both selected the correct action 95 times, only
GPT did 11 times, only Higgs did zero times, and both were wrong six times. The
repeated cases are not independent real-world users; this is a regression result,
not a population-level quality estimate.

## Shared bottleneck and routing protocol

In the audio test both systems transcribed “Two mallards” as “Two mellards”. Both
routers nevertheless selected `Mallard` with count 2. The existing shared guard
would reject that write as `ungrounded_sighting` because the normalized model
species name does not occur verbatim in the transcript. Switching the conversation
provider does not remove this backend limitation. A follow-up should review
grounded name normalization while preserving the user's confirmation rules.

GPT also generated extra **text** during 21/112 text-routing samples and 2/14
audio-routing samples, despite the route-only instruction. The app suppresses
this routing text; it is not a second spoken voice. Correct-action scores above
are separate from strict no-text/no-audio routing compliance: Higgs 95/112 versus
GPT 85/112 on text. No routing audio was observed. Provider-specific routing
prompt work is a separate follow-up; no production prompts were tuned in this run.

## Method and reproducibility

Both providers received the same production instructions, tools, synthetic state,
history and latest input. Provider order alternated; case order was seeded.
Different assistant-history encodings use each API's supported schema. The model
never sees expected answers. Equivalent tools, task selectors and harmless expert
question translations are accepted by the scorer. The backend always replaces
the expert question argument with the original user turn.

Text scorer corrections are documented in the detailed report; original labels
and changed grades remain in the local graded JSON. The raw model responses and
inputs were preserved. The audio collector is tested against split/out-of-order
VAD transcripts; it gathers the complete scripted utterance before routing and
does not measure the app's interruption or turn-coalescing timing.

No checklist, camera, classifier or investigation tools were executed. No BirdNET,
BioCLIP, iNaturalist or expert-model requests were made by the benchmark. Existing
Start/Identify handling is covered by shared backend regression/smoke tests and
is not counted as model intelligence. The benchmark does not reset live phone
sessions. The default provider remains Higgs.

- [Detailed text results and failure cases](benchmarks/realtime-text-2026-10-24.md)
- [Detailed audio results and transcripts](benchmarks/realtime-audio-2026-10-24.md)
- [Commands and setup](GPT_REALTIME.md#paired-regression-benchmark)
- Cases: `backend/realtime_eval_cases.py`; runner: `backend/realtime_eval.py`.
- Raw local reports: `out/realtime-eval/text.json`, `text-graded.json`,
  `audio-final.json`; WAVs and raw reports are gitignored.
- Evaluation design reference: [OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices).
