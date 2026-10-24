# Addressee gating: stop answering chatter and bystanders

Branch `research/addressee`, from main `39addd7` (demo build `6d74c77` + README).

## Problem (field report, 2026-10-24)

In live use the assistant answers speech that was not meant for it, far more
often than our synthetic test suggested (0/6 chatter false-acts on clean,
single-speaker TTS). Bystander voices also trigger responses. We could not
record a clean "assistant stays quiet" example for the demo.

Server logs from the demo day were only in the tmux pane and are lost, so
there are no field transcripts to analyse. Collecting data comes first.

## Three kinds of false activation

| Kind | Example | Speaker ID helps? |
|---|---|---|
| A. Bystander | Another team talking nearby | Yes |
| B. Own echo | Speaker-mode TTS picked up by the mic | Yes; may allow full duplex |
| C. Owner to a friend | "Did you bring the snacks?" | No; same voice |

## Plan

Gates run in order after `input_audio_transcription.completed`, in the
existing filter chain (echo filter → filler filter → **new gates** → router).
Gated turns are deleted from the Realtime conversation with
`conversation.item.delete`, as the existing filters already do.

1. **Data first.** Opt-in session recorder: per-turn mic audio (WAV),
   transcript, gate features and route decision, written as JSONL plus WAV
   files under a local directory. Off by default; bystander speech is
   recorded only when the owner turns it on.
2. **Level gate.** Near-field owner speech is louder and less reverberant than
   bystanders. Compare each turn's speech level with the owner's running
   level. Cheap first filter. Must not reject the owner speaking softly.
3. **Speaker verification.** Enrol the owner (10–20 s), compute an embedding
   per turn on CPU (ECAPA-TDNN or similar) and skip turns that do not match.
   Threshold errs toward accepting the owner: a missed sighting is worse than
   a stray answer. Support several enrolled companions.
4. **Tiered response** (kind C). The router picks one of: speak (clearly
   addressed), show silently on screen (possibly addressed, e.g. a bird
   question with no addressee), ignore. A false activation then costs a card,
   not a sentence. Logging keeps its earcon.
5. **Context window.** After the assistant asks a question ("add it?"), a
   short reply within a few seconds is addressed to it. Otherwise the default
   for ambiguous speech is not to speak.
6. Optional: a second text classifier ("is this addressed to the assistant?")
   only on turns the router wants to speak.

## Evaluation

- **Speaker gate (offline):** real human speech from public corpora, short
  utterances (0.8–3 s), outdoor noise and far-field simulation. Report false
  accept (bystander passes) and false reject (owner blocked) per threshold,
  and CPU latency.
- **Addressee (router):** scripted multi-turn dialogues labelled
  to-assistant / owner-to-friend / bystander / echo, replayed through the real
  router sequentially (no concurrent Higgs sessions).
- **Field set:** recordings from the session recorder, labelled by hand. This
  is the number that matters; synthetic results are an upper bound.

Metrics: false speak rate on not-addressed turns, missed command rate on
addressed turns (sightings and questions), added latency.
