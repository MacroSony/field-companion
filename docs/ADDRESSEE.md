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
3. **Speaker verification — shadow mode implemented.** Optional local CPU
   ECAPA (`speechbrain/spkrec-ecapa-voxceleb`) scores every delimited audio turn
   against enrolled profiles, logs cosine scores, and joins opt-in recorder rows.
   **NEVER blocks or changes routing/echo guard; routing does not await scoring.**
   Phone Settings offers a named 20 s ring-buffer enrollment; only that explicit
   read-aloud window suppresses commands (`reason: enrollment`). Multiple profiles
   append normalized chunk embeddings locally. See [backend setup/API/privacy](../backend/README.md#speaker-verification--shadow-mode-2026-10-24).
   Evidence: `/data/build/tmp/addressee/spk/REPORT.md`; ≤1.5 s/noisy/distant turns
   are unreliable. No thresholds or hard speaker gate are implemented.
4. **Tiered response** (kind C): **implemented (V2, measured on text router)**.
   The router picks one of: speak (clearly addressed), `show_silently` on screen
   (possibly addressed, e.g. a bird question with no addressee), ignore. A false
   activation then costs a card, not a sentence. Logging keeps its earcon.
   Silent cards use the original server transcript, expire after about 30 s,
   and have Answer/dismiss controls on phone and display. Answer sends the note
   id; the server queues its original question prefixed `[TAP]` through the
   existing text router, with the ordinary tap allowance in Quiet mode.
   No checklist write, earcon, feedback clip or answer request accompanies a card.
   V2 and the tool schema match the measured literals verbatim; the override is
   last, after dynamic routing context, matching the harness.
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

### V2 text-router result and implementation

`/data/build/tmp/addressee/router/REPORT.md`: appending V2 reduced false speech
on not-addressed chatter from **58% to 10%**, with **no missed commands**.
The frozen literals are `prompts.json` keys `V2` and `show_silently`; the harness
appended the override after the state snapshot and all dynamic context.
This does not establish real audio/field reliability; those evaluations remain.

Offline regressions cover literal prompt/schema parity, routing silence,
original-transcript fan-out, Answer `[TAP]` re-routing, bounded/expired/reset
notes, and recorder outcome `routed` / routed_tool `show_silently` with no audio.
The extended offline web mock/self-test covers phone/display cards, Answer,
dismiss/expiry, literal transcript rendering, and absence of new audio sources.
