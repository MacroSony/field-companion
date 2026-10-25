# Realtime comparison — text

Started: 2026-10-25T01:57:54.296918+00:00. Finished: 2026-10-25T02:06:51.123596+00:00.

56 scenarios × 2 rounds × 2 providers. Pairing verified: True.

Same production prompts, tool definitions, state, conversation history and input. Fresh connection for each sample; alternating provider order; no executed tools or field recordings.

An action is correct only when the tool and meaningful arguments match an allowed outcome. Strict routing additionally requires no text/audio before the call. Extra routing text is suppressed by the app, so it is reported separately from choosing the right action.

| Provider / model | Correct action | Strict routing | API completion | Median route | P95 route | Non-allow guard audits |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| higgs / `higgs-realtime-1.5-preview` | 95/112 (84.8%) | 95/112 (84.8%) | 111/112 (99.1%) | 0.62s | 1.03s | 11 |
| openai / `gpt-realtime-2.1` | 106/112 (94.6%) | 85/112 (75.9%) | 112/112 (100.0%) | 0.81s | 1.12s | 5 |

Latency includes the route session update and generation, excluding connection setup, speech input, tool execution and spoken playback. Retried route attempts are included; retry connection setup is excluded. Fresh sessions do not measure warm-session phone latency.

## Correct actions by category

| Category | higgs | openai |
| --- | ---: | ---: |
| camera | 18/20 | 18/20 |
| checklist | 22/24 | 24/24 |
| conversation | 4/4 | 4/4 |
| expert | 20/20 | 20/20 |
| progress | 15/16 | 16/16 |
| safety | 2/6 | 4/6 |
| silence | 6/10 | 8/10 |
| sounds | 8/12 | 12/12 |

Paired action outcomes: both_correct=95; openai_only=11; higgs_only=0; both_incorrect=6.

## Incorrect actions / technical failures

- **higgs, round 1, describe-mark**: It has a red head and white stripes on its wings. → `[{"tool": "new_observation", "arguments": {"group": "Aves"}}]`
- **openai, round 1, describe-mark**: It has a red head and white stripes on its wings. → `[{"tool": "new_observation", "arguments": {"group": "Aves"}}]`
- **higgs, round 1, remove-one**: Remove the mallards from my checklist. → `[{"tool": "remove_observation", "arguments": {"name": "Mallard", "all": true}}]`
- **higgs, round 1, filler-ordinal**: First. → `[{"tool": "get_task_status", "arguments": {"id": "photo-eagle", "selection": "first", "scope": "auto"}}]`
- **higgs, round 1, suggest-subset**: Just the cardinal. → `[{"tool": "add_observation", "arguments": {"name": "Northern Cardinal", "group": "Aves", "count": 1, "notes": "Heard in the area"}}]`
- **higgs, round 1, suggest-yes**: Yes, add them all. → `[{"tool": "add_observations", "arguments": {"items": [{"name": "Northern Cardinal", "group": "Aves", "count": 1, "notes": ""}, {"name": "American Robin", "group": "Aves", "count": 1, "notes": ""}]}}, {"tool": "accept_suggestions", "arguments": {"species": ["Northern Cardinal", "American Robin"], "counts": {"Northern Cardinal": 1, "American Robin": 1}}}]`
- **openai, round 1, chatter-snacks**: Did you bring the snacks? → `[{"tool": "speak", "arguments": {"reason": "General conversation addressed to a companion. Respond conversationally; this isn't a birding or logging action."}}]`
- **higgs, round 1, chatter-snacks**: Did you bring the snacks? → `[{"tool": "speak", "arguments": {"reason": "mic check / general question about snacks"}}]`
- **higgs, round 1, negative-log**: Don't add that bird to my checklist. → `[{"tool": "discard_observation", "arguments": {}}]`
- **openai, round 1, suggest-okay**: Okay. → `[{"tool": "accept_suggestions", "arguments": {}}]`
- **higgs, round 1, suggest-okay**: Okay. → `[{"tool": "accept_suggestions", "arguments": {"species": ["Northern Cardinal", "American Robin"], "counts": {"Northern Cardinal": 1, "American Robin": 1}}}]`
- **higgs, round 2, suggest-yes**: Yes, add them all. → `[{"tool": "add_observations", "arguments": {"items": [{"name": "Northern Cardinal", "group": "Aves", "count": 1, "notes": "Heard"}, {"name": "American Robin", "group": "Aves", "count": 1, "notes": "Heard"}]}}]`
- **higgs, round 2, filler-ordinal**: First. → `[{"tool": "get_task_status", "arguments": {"id": "photo-eagle", "selection": "first", "scope": "auto"}}]`
- **higgs, round 2, suggest-okay**: Okay. → `[{"tool": "accept_suggestions", "arguments": {"species": ["Northern Cardinal", "American Robin"], "counts": {"Northern Cardinal": 1, "American Robin": 1}}}]`
- **higgs, round 2, research-progress**: How is the first investigation going? → `[{"tool": "get_task_status", "arguments": {"scope": "investigations"}}]`
- **higgs, round 2, log-many**: Two mallards and three American robins. → `[]` (technical failure: TimeoutError)
- **openai, round 2, negative-log**: Don't add that bird to my checklist. → `[{"tool": "discard_observation", "arguments": {}}]`
- **higgs, round 2, negative-log**: Don't add that bird to my checklist. → `[{"tool": "discard_observation", "arguments": {}}]`
- **higgs, round 2, describe-mark**: It has a red head and white stripes on its wings. → `[{"tool": "new_observation", "arguments": {"group": "Aves"}}]`
- **openai, round 2, describe-mark**: It has a red head and white stripes on its wings. → `[{"tool": "new_observation", "arguments": {"group": "Aves"}}]`
- **higgs, round 2, suggest-subset**: Just the cardinal. → `[{"tool": "add_observation", "arguments": {"name": "Northern Cardinal", "group": "Aves", "count": 1, "notes": ""}}]`
- **openai, round 2, chatter-snacks**: Did you bring the snacks? → `[{"tool": "speak", "arguments": {"reason": "General chat addressed to me: the user asked about snacks. Respond conversationally without using tools that read checklist or sounds."}}]`
- **higgs, round 2, chatter-snacks**: Did you bring the snacks? → `[{"tool": "speak", "arguments": {"reason": "User is asking a general question about snacks; respond with a friendly greeting."}}]`

## Routing text/audio and protective checks

- higgs: 0 responses emitted routing text/audio; 4 samples attempted an explicitly forbidden action; 11 triggered non-allow decisions in the existing shared guard audit; 1 completed responses violated the single-call/schema requirement; 1 retries.
- openai: 21 responses emitted routing text/audio; 2 samples attempted an explicitly forbidden action; 5 triggered non-allow decisions in the existing shared guard audit; 0 completed responses violated the single-call/schema requirement; 0 retries.

The guard is audited only; it does not rescue model scores or prove an end-to-end outcome. Some raw filler cases are filtered before reaching the router in the running app.

## Limits

These are hand-authored regression cases, with repeated identical intents; the trials are not independent real-world users and no population-level significance is claimed. Only the current provider configurations are compared. This run does not assess spoken answer quality, echo cancellation, long-dialogue memory, species recognition accuracy, or successful execution of investigations. BirdNET, BioCLIP, iNaturalist and GPT expert reasoning were not invoked. No production prompts were tuned using these results.

Scoring audit: Selector-equivalence fixes accept scope=all without redundant selection=all, accept scope=all when only one photo task exists, reject contradictory task IDs, and accept explicit R1 with an equivalent selector. Expert question paraphrases/translations are not penalized because the backend replaces that argument with the original user turn. Original labels and any changed grades are retained. No model inputs or responses changed.
