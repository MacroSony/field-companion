# GPT Naturalist expert path

The expert is an on-demand, read-only layer. Higgs still handles speech,
transcription and routing; BioCLIP, BirdNET and iNaturalist retain their current
jobs. Ordinary sightings, count changes and checklist queries do not call GPT.

## Entry points

- Higgs calls `ask_expert(question)` for candidate comparisons, alternative-ID
  questions and species facts about the focus (diet, habitat, behaviour).
- An uncertain visual response calls the expert once for the current observation,
  after a short capture-burst delay. A confident visual response uses the existing
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
The Higgs answer prompt asks it to speak the finished answer verbatim.

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
- The expert cannot replace `fused`, select a different confirmed species, count
  organisms or log observations. Alternative-ID conflicts require more evidence
  or clarification. Confirmation still uses the current fusion top candidate.
- Results are discarded when focus, evidence/status or user input changes during
  a request. A late-frame result can fall back to the newest basic vision answer;
  a new user question suppresses the previous answer entirely.
- The total expert deadline defaults to seven seconds, including source lookup
  (at most two seconds). No automatic OpenAI request retry. HTTP 429 starts a
  bounded cooldown (Retry-After or 15 seconds). Existing quiet-mode rules apply.
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
