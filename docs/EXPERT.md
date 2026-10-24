# GPT Naturalist expert path

The expert is an on-demand, read-only layer. Higgs still handles speech,
transcription and routing; BioCLIP, BirdNET and iNaturalist retain their current
jobs. Ordinary sightings, count changes and checklist queries do not call GPT.

## Entry points

- Higgs calls `ask_expert(question)` to schedule a background investigation for
  candidate comparisons, alternative-ID questions and species facts about the
  focus (diet, habitat, behaviour). The tool returns an acknowledgement/task ID
  immediately; the voice worker continues handling subsequent turns.
  The turn worker supplies the original typed/STT question, so a router rewrite
  cannot silently translate it or add a species hypothesis.
- An uncertain visual response schedules an investigation for the current
  observation. A confident visual response uses the existing
  BioCLIP answer. There is no GPT request per uploaded frame.
- Missing focus prompts the user to look first. Disabled/unavailable GPT preserves
  the basic visual path; explicit expert questions get a brief unavailable answer.

## Provider contract

`backend/expert.py` receives the question and a copy of the focus observation.
Input includes up to 12 recent detections, top candidates, score rankings, time,
location, user descriptions and up to two species-knowledge extracts. It excludes
local thumbnail URLs: this version sends no raw images or audio to OpenAI.

The OpenAI Responses API returns a strict structured `Decision`:

```json
{
  "action": "request_more_evidence",
  "basis": "insufficient",
  "answer": "Could you get a closer view of the head?",
  "hint": "Zoom on the head",
  "evidence_ids": ["d7"],
  "source_ids": [],
  "source_quotes": []
}
```

`action` is `answer`, `request_more_evidence`, `clarify` or `abstain`. `basis` is
`observation`, `species_knowledge`, `mixed` or `insufficient`. The backend rejects
unknown evidence/source IDs, missing citations for a factual basis and empty capture hints.
Every cited source also needs a short supporting `source_quotes` entry whose
`quote` appears in that source's extract (whitespace normalized). This checks
quote provenance; semantic entailment and field accuracy still require evaluation.
The request schema restricts evidence and source IDs to this snapshot's actual
IDs, so the observation ID cannot be substituted for a detection ID. Empty
contexts only allow empty reference arrays. Responses must match the question's
English/Chinese language; wrong-language responses are rejected.
Results carry an observation ID, snapshot revision and cited source metadata.
Expert speech uses a scoped readout prompt containing only the finished answer;
the normal answer prompt is restored after generation, including on failure.
The model is instructed to read verbatim, which remains a live-evaluated behavior.

For more evidence, the backend updates the existing observation's `hint` and
broadcasts the existing `observation` message. The user's next “look again” reuses
that focus and hint through the existing `capture` message. There is no automatic
capture loop or new frontend protocol message.

## Grounding and state rules

- Wikipedia lookups use exact scientific titles and follow redirects. They reject
  missing/disambiguation pages and cache successes for 24 hours, failures for five
  minutes (at most 128 entries per provider). Retrieved text is untrusted data.
- Species facts require a supplied source. If the extract lacks the relevant fact,
  the prompt requires abstention. An uncertain identity requires conditional facts.
- Species names stay in the supplied English/scientific form, including in Chinese
  prose. Chinese factual answers without a supplied species identifier are rejected;
  translated names are not accepted as substitutes for those identifiers.
- BioCLIP/fusion values are uncalibrated rankings. Multiple frames are correlated;
  a nearby BirdNET call need not belong to the photographed organism. Model prompts
  explicitly distinguish these cases and prohibit invented visible field marks.
- Fusion rank alone does not establish visual certainty. The latest image must
  support the fused winner with raw score >=0.5 and margin >=0.2; otherwise the
  observation remains uncertain and follows the expert/evidence-request path.
  Strong local abundance priors or repeated weak frames cannot remove this gate.
- The expert cannot replace `fused`, select a different confirmed species, count
  organisms or log observations. Alternative-ID conflicts require more evidence
  or clarification. Confirmation still uses the current fusion top candidate.
- Background results remain attached to their original evidence, even after
  another question or focus change. A capture hint updates the live observation
  only if it still matches that snapshot. Completion speech yields to the user
  and waits in Quiet mode; the result remains on its task card.
- Background calls default to 120 seconds, with a 180-second outer task budget.
  Direct standalone provider calls still default to seven seconds. Source lookup
  remains bounded to two seconds. No automatic OpenAI request retry. HTTP 429
  starts a bounded cooldown (Retry-After or 15 seconds).
- Logs contain only safe status, exception type, latency and model metadata.

## Configuration and verification

Install `requirements.txt`. The provider reads process environment first, then
the ignored repository `.env.local`; it never changes the Boson environment.

| Variable | Default |
| --- | --- |
| `OPENAI_API_KEY` | Securely configured local credential; required for GPT |
| `EXPERT_ENABLED` | `1`; set `0` to keep the expert disabled |
| `EXPERT_MODEL` | `gpt-5.4-mini` |
| `EXPERT_TIMEOUT_SECONDS` | `7`, bounded to 1–15 seconds |

The model is configurable. The default supports Responses and structured output,
but account access, field accuracy and end-to-end latency require live evaluation.
See [the model reference](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
and [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
The source adapter uses [MediaWiki Query](https://www.mediawiki.org/wiki/API:Query)
and [TextExtracts](https://www.mediawiki.org/wiki/Extension:TextExtracts).

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s backend -p 'test_*.py' -v
# Opt-in: two synthetic expert questions, with real API/source requests.
.\.venv\Scripts\python.exe -X utf8 -B backend/expert_smoke.py --live
```

Offline tests exercise the real SDK against a local mock HTTP transport, plus
focus isolation, stale results, hints, quiet mode and the core-path fallback.
They establish the integration contract, not naturalist accuracy. Live model
evaluation and an actual phone → camera → BioCLIP → GPT → Higgs field test remain
separate acceptance checks.
See [LOOK_EXPERT_TESTING.md](LOOK_EXPERT_TESTING.md) for the latest Look/expert
validation, reproduced handoff bugs, fixes and remaining combined-field checks.
See [INVESTIGATIONS.md](INVESTIGATIONS.md) for the newer background queue,
progress/cancel/retry controls, time limits, session retention and live test.
