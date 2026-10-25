# Realtime comparison — synthetic-audio

Started: 2026-10-25T02:11:52.764845+00:00. Finished: 2026-10-25T02:15:02.129505+00:00.

14 scenarios × 1 rounds × 2 providers. Pairing verified: True.

Same production prompts, tool definitions, state, conversation history and input. Fresh connection for each sample; alternating provider order; no executed tools or field recordings.

An action is correct only when the tool and meaningful arguments match an allowed outcome. Strict routing additionally requires no text/audio before the call. Extra routing text is suppressed by the app, so it is reported separately from choosing the right action.

| Provider / model | Correct action | Strict routing | API completion | Median route | P95 route | Non-allow guard audits |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| higgs / `higgs-realtime-1.5-preview` | 13/14 (92.9%) | 13/14 (92.9%) | 14/14 (100.0%) | 0.50s | 0.64s | 1 |
| openai / `gpt-realtime-2.1` | 14/14 (100.0%) | 12/14 (85.7%) | 14/14 (100.0%) | 0.76s | 1.02s | 1 |

Latency includes the route session update and generation, excluding connection setup, speech input, tool execution and spoken playback. Retried route attempts are included; retry connection setup is excluded. Fresh sessions do not measure warm-session phone latency.

## Correct actions by category

| Category | higgs | openai |
| --- | ---: | ---: |
| camera | 5/5 | 5/5 |
| checklist | 3/3 | 3/3 |
| conversation | 2/2 | 2/2 |
| expert | 1/1 | 1/1 |
| progress | 1/1 | 1/1 |
| silence | 0/1 | 1/1 |
| sounds | 1/1 | 1/1 |

Paired action outcomes: both_correct=13; openai_only=1; higgs_only=0; both_incorrect=0.

## Synthetic microphone transcription

Identical WAV audio from local Windows voices, streamed at real-time speed. No microphone, noise or speaker echo. The model receives the audio and its actual STT transcript; it does not receive the known script as a user message. Split VAD transcripts are collected in audio order before routing the full fixture. This does not measure production turn-coalescing or interruption timing.

| Provider | Exact normalized transcript | Token errors |
| --- | ---: | ---: |
| higgs | 13/14 | 1/103 (1.0%) |
| openai | 13/14 | 1/103 (1.0%) |

Punctuation/case are ignored; Chinese characters and English words are tokens. Digits versus spelled-out numbers still count as transcription differences. Use action accuracy, not this small synthetic STT score alone, for product decisions.

| Case / round | Script | higgs | openai |
| --- | --- | --- | --- |
| chatter-snacks / 1 | Did you bring the snacks? | Did you bring the snacks? | Did you bring the snacks? |
| correct-total / 1 | Actually four mallards in total. | Actually, four mallards in total. | Actually four mallards in total. |
| expert-compare / 1 | Could it be a Cooper's Hawk instead? | Could it be a Cooper's hawk instead? | Could it be a Cooper's hawk instead? |
| greeting-zh / 1 | 你好，能听到我说话吗？ | 你好，能听到我说话吗？ | 你好，能听到我说话吗？ |
| log-two / 1 | Two mallards. | Two Mellards. | Two mellards. |
| log-zh / 1 | 我看到两只绿头鸭，帮我记下来。 | 我看到两只绿头鸭，帮我记下来。 | 我看到两只绿头鸭，帮我记下来。 |
| mic-check / 1 | Can you hear me? | Can you hear me? | Can you hear me? |
| new-after-eagle / 1 | I saw another animal, could you help me check it out? | I saw another animal. Could you help me check it out? | I saw another animal. Could you help me check it out? |
| photo-progress / 1 | Check out the identification process. | Check out the identification process. | Check out the identification process. |
| plant-after-eagle / 1 | I see a plant. Can you identify it? | I see a plant. Can you identify it? | I see a plant. Can you identify it? |
| plant-zh / 1 | 我发现一株不认识的植物，帮我看看。 | 我发现一株不认识的植物，帮我看看。 | 我发现一株不认识的植物，帮我看看。 |
| recent-bird / 1 | What bird was singing just now? | What bird was singing just now? | What bird was singing just now? |
| retake-current / 1 | Take another photo of the same bird. | Take another photo of the same bird. | Take another photo of the same bird. |
| unknown-animal / 1 | What kind of animal is that? | What kind of animal is that? | What kind of animal is that? |

## Incorrect actions / technical failures

- **higgs, round 1, chatter-snacks**: Did you bring the snacks? → `[{"tool": "speak", "arguments": {"reason": "General question about snacks, not a birding or sound question."}}]`

## Routing text/audio and protective checks

- higgs: 0 responses emitted routing text/audio; 0 samples attempted an explicitly forbidden action; 1 triggered non-allow decisions in the existing shared guard audit; 0 completed responses violated the single-call/schema requirement; 0 retries.
- openai: 2 responses emitted routing text/audio; 0 samples attempted an explicitly forbidden action; 1 triggered non-allow decisions in the existing shared guard audit; 0 completed responses violated the single-call/schema requirement; 0 retries.

Correct model actions that would still encounter a non-allow shared guard decision:

- higgs, log-two, round 1: input `Two Mellards.` → `add_observation`; guard `[{"decision": "clarify", "reason": "ungrounded_sighting"}]`.
- openai, log-two, round 1: input `Two mellards.` → `add_observation`; guard `[{"decision": "clarify", "reason": "ungrounded_sighting"}]`.

The guard is audited only; it does not rescue model scores or prove an end-to-end outcome. Some raw filler cases are filtered before reaching the router in the running app.

## Limits

These are hand-authored regression cases, with repeated identical intents; the trials are not independent real-world users and no population-level significance is claimed. Only the current provider configurations are compared. This run does not assess spoken answer quality, echo cancellation, long-dialogue memory, species recognition accuracy, or successful execution of investigations. BirdNET, BioCLIP, iNaturalist and GPT expert reasoning were not invoked. No production prompts were tuned using these results.
