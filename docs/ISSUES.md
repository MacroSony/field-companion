# Known issues from live testing (Oct 23, 21:30–23:15)

Status: **recorded, not fixed unless noted below**. Code under test = branch `feat/batched-suggestions` (PR #2), served live on :8780. Condensed event log of the last session: `/data/build/tmp/review/live-test-2258-2315.log` (not in repo). Setup: Android Chrome + laptop playing `/testaudio/` clips; speaker mode 22:58–23:08, earphones mode 23:09–23:15.

## Follow-up on feat/look-ui

- E7: late initial GPS is sent to the backend; failed microphone changes retain
  the old input; a replaced phone ends its session instead of reconnecting forever.
  Covered by the isolated camera browser test; physical device validation remains.
- E9/E10: this branch builds on feat/expert's merged Observation/suggestion model
  and implements the existing Look/observation frontend contract. It does not
  resolve unrelated router/BirdNET eligibility or Realtime lifecycle issues below.

## A. Router / intent
| # | Observed (time) | Evidence | Likely cause |
|---|---|---|---|
| A1 | "Can you hear me?" / "Did you hear anything (else)?" → `get_checklist` → assistant **reads the checklist aloud** (23:05:55, 23:09:05, 23:10:04, 23:11:07). User: "keeps repeating my list". | route_done tools | Tool descriptions: get_checklist = "Read the confirmed checklist", recent_sounds = "Query timestamped BirdNET detections" — model doesn't map "hear" → BirdNET. No mic-check intent. |
| A2 | "Hey! Can you hear me?" → **`add_observation`** (23:11:00) | route_done | Router forced to pick a tool (tool_choice required) and no good fit. |
| A3 | Garbled / filler transcripts acted on: "Add add a a." → accept (23:04:32), "As for you, for me." → get_checklist + answer (23:04:34), "Uh." → recent_sounds + answer (23:10:24), "Ah." / "Oh." → answer (23:03:27, 23:03:43) | user_transcript + route_done | Background birds/noise → VAD speech → STT garbage; router has no "unintelligible → ignore" rule. |
| A4 | "Hello. Do you hear any birds?" → `speak` (22:59:02); "I think there are some Robin. Did you hear them?" → `speak` (23:10:16) | route_done | Answer phase has tool_choice none → answers **without BirdNET data** (likely hallucinated). |
| A5 | "Hey, can you hear me?" → `ignore` (22:58:43) | route_done | Addressing the assistant not treated as addressed. |
| A6 | "Did you actually hear them?" → `accept_suggestions` (fails) (23:10:33) | route_done ok=false | Pending-suggestion context biases router toward accept. |
| A7 | "Remove all of them. Those are wrong." → remove_observation fails (23:09:18) | ok=false | No clear-all/remove-many capability. |
| A8 | Earlier (21:48): route response.create got no response at all → 45 s stall → backlog replayed | turn_failed TimeoutError | Mitigated: interrupt_response off, 8 s timeout + retry, merge, stale-skip. Not seen again. |

## B. BirdNET / suggestions
| # | Observed | Evidence | Likely cause |
|---|---|---|---|
| B1 | **"Human non-vocal" suggested as a species and spoken** (23:03:59) | suggestion_prompt species ["Human non-vocal"] | BirdNET non-bird labels (Human vocal/non-vocal/whistle, Dog, Engine, Siren, Gun, Power tools, Fireworks, Noise, Environmental) not filtered. |
| B2 | Out-of-range species detected at 0.7–0.99 and shown/spoken: Moustached Turca (Chile), Golden-bellied Gerygone (Australia), Yellowhammer, Baillon's Crake, Wood Sandpiper; Turca was **spoken** as a suggestion (22:59:25) | bird_detection, suggestion_prompt | Range filter "rare lane" lets any species ≥0.7 through (added to allow Red-necked Grebe). Speaker→mic chain makes BirdNET overconfident. Checked: BirdNET geo list @0.001 (308 spp) and iNat Ontario all-time (537 spp) both include Red-necked Grebe and exclude all five. |
| B3 | Song Sparrow clip → several species ("too sensitive") | 22:59–23:00 | B2 + low display threshold (every window ≥0.25 shown in Heard strip). |
| B4 | Speaker mode: each spoken sentence blinds BirdNET ~8–10 s | bird_window_skipped | By design (half-duplex + 3 s windows). Earphones avoid it. |
| B5 | Red-necked Grebe (user's clip) not detected originally | — | Fixed by own range list; **re-test pending**. |

## C. Audio / devices
| # | Observed | Status |
|---|---|---|
| C1 | Phone speaker → mic → self-barge-in + talking to itself loop (22:37) | Fixed: speaker mode = half-duplex default, phone-reported playback end, echo transcript filter. Verified 22:58–23:08 (0 barge-ins). |
| C2 | Bluetooth earbuds: mic in call mode kills bird sounds completely | Open. Mic picker added (built-in mic + BT output) — **untested on phone**. USB-C mic / native app as alternatives. |
| C3 | After End session → restart, no assistant audio | Fixed (playback clock reset). |
| C4 | STT quality under background birdsong: "Add both for me" → "Elbows for me.", "Add them for me" → "Add a menu for me." | Open; router still often right. |
| C5 | Higgs STT accumulates transcripts when create_response=false | Fixed: input_audio_buffer.clear per turn (consequence: router sees text, not audio). |

## D. Process
- Frontend deployed (live reload) before backend restart → confusing mixed state for the tester. Restart backend together with frontend changes, and announce.
- Complexity grew fast (suggestions, echo guard, playback reports, mic picker…). Feature freeze until the issues above are triaged.

## E. Found by code review (Oct 23 night, read-only reviewers; not reproduced live unless noted)
- E1 Conversation history in the Realtime session is never pruned (transcripts, NOTIFY, function outputs) → cost/latency growth; may contribute to repeated checklist readouts.
- E2 One-action-per-turn drops compound intents ("Yeah, add it. Did you hear anything else?" → only accept, 23:05:40, proven); merging queued utterances makes it worse.
- E3 Response completion isn't correlated by response id: a late `response.done` (e.g. from a cancelled/timed-out response) can satisfy a newer waiter. Reconnect doesn't reset `user_speaking`/`awaiting_transcript`.
- E4 Allowed-species list fails open: if both sources fail, filtering is disabled and cached for the day.
- E5 Earphones + display audio=1: Realtime is guarded but BirdNET windows aren't masked (masking requires speaker mode).
- E6 Playback "active" report without a later "inactive" (e.g. AudioContext suspended) keeps the mic gated indefinitely.
- E7 Web: late GPS fix never sent to backend (backend stays at Toronto default); two phones on session `demo` evict each other forever (auto-reconnect after replacement); mic switch stops the old track before the new one succeeds; >30 s queued audio can overlap after the clock-reset guard.
- E8 Stale frontend vs backend: no version handshake (SW is network-first, but an open page keeps old JS).
- E9 Protocol/docs drift: PROTOCOL.md documents Look/observation messages that only exist in PR #1; `candidates` has no producer; echo-filter and tail values in docs don't match code.
- E10 PR #1 (observation model) and PR #2 (suggestions/echo) both rewrote router tools, finish_route and the checklist model → high semantic merge difficulty. Concepts to unify: Detection = evidence, Suggestion = pending proposal referencing evidence, Observation = organism record; checklist derived from confirmed observations.

## Review recommendations (ranked; full reviews in subagent reports of Oct 23)
1. Router wording (A1–A6): replacement texts drafted — low risk.
2. BirdNET eligibility: one allowlist = BirdNET geo @0.001 ∪ iNat Ontario all-time (place_id 6883); drop the ≥0.7 rare lane and all non-bird labels; Heard strip shows only ≥2 windows or ≥0.7; fail closed/last-good list — low–medium risk.
3. Small web bugs E7 — low risk.
4. Bound conversation history; stop merging unrelated intents — medium.
5. Tools 11 → 6 (add_observations, set_count, remove_observations incl. all, resolve_suggestions, speak, ignore) — low–medium.
6. Settings/debug drawer (mic picker, capture line, model id, text input, speaker/earphones, headset button); clearer labels for Quiet vs Speaker/Earphones vs Pause vs Tap — low.
7. Decide Look scope before merging PR #1/#2; if in scope: PR #1 model first, port PR #2 deliberately.
8. Later: retire transcript-echo heuristic after physical validation; split server.py into modules (1–2 days, not before demo).
