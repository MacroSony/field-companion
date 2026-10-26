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

## Results so far (text router, higgs-realtime-1.5-preview)

| Test set | Variant | Spoke on not-addressed turns | Missed speak | Missed log |
|---|---|---|---|---|
| Mika's set (34 turns × 2) | main | 29/50 (58%) | 0/10 | 0/8 |
| Mika's set | V2 | 5/50 (10%) | 0/10 | 0/8 |
| Held-out set (108 turns × 2) | main | 41/92 (45%) | 0/73 | 0/50 |
| Held-out set | V2 | 30/83 (36%) | 0/66 | 0/47 |

The held-out set was written by a separate agent that never saw the rule
(`/data/build/tmp/addressee/heldout/`). The large gain on Mika's set was mostly
overfitting: the rule lists the same examples. On held-out turns V2 fixes
plain social chatter ("Did you bring the flask?") and costs no missed commands,
but **bird talk between people still triggers the assistant** ("What do you
reckon, a nuthatch?", "There, it did it again.", "嘘，先听一下。"): look,
ask_expert, recent_sounds and get_checklist on turns meant for a friend.
Text alone cannot separate these; friend and bystander lines (12 of the 30)
are the speaker gate's job.

Held-out V2 had 4 upstream error turns and 16 turns not run after session
failures, hence the smaller denominators. Scoring: recomputed from raw.jsonl
with remove_observation/discard_observation counted as log and describe as
speech (`/data/build/tmp/addressee/heldout_run/`).

Next idea (V3): every routing call carries `addressed: explicit | implicit`.
Implicit speech-class routes (look, ask_expert, recent_sounds, reads) become a
silent card offering that action instead of speaking; explicit ones run as now.

## Integration with the GPT / LiveKit branch

The V2 prompt and silent-card behavior above are retained as the selectable
**Ambient** mode. The existing **Scout** mode remains LiveKit's default. It uses
the wake-name/current-question gate before routing; an explicitly accepted turn
cannot become `ignore` or `show_silently` and instead asks for clarification if
necessary. Switching modes does not replace shared dialogue memory, camera or
background investigations. This is a mode choice, not a claim to have completed
the proposed V3 policy for every implicit speech-class tool.

Recording/enrollment also work with LiveKit's own input track. Speaker scores
are shadow-only in both modes: they are logged with the same turn as the final
transcript and actual route, but never block a command or authorize a speaker.
The separate raw BirdNET track is not used for voice matching. See
[LiveKit setup, consent controls and validation](LIVEKIT.md#conversation-modes-recordings-and-voice-profiles).
