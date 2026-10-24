# [Name TBD] — Hands-free field companion for birders & naturalists

**One-liner:** Merlin tells you what's singing. We let you log it — and anything else you see — without taking your eyes off the binoculars.

## 1. Problem & user
- Birders/photographers have both hands busy (binoculars, camera). Logging on eBird Mobile means looking down and tapping through lists.
- Existing tools: Merlin and BirdNET identify by sound or photo; Robin (UMich) adds voice control for accessibility. **None of them does conversational, hands-free checklist logging** that combines sound ID, photo ID and your own observations.
- Target: a walking birder with earphones, a phone in the pocket and maybe a camera.

## 2. Core user flows (demo order, ~3 min)
1. **Just talk**: "Two mallards on the pond" → a soft *ding*, the checklist updates. No wake word, no reply. "Actually four" → *ding*. Mixed Chinese/English is fine.
2. **Proactive sound ID**: BirdNET keeps listening. A new species (Northern Cardinal) → the assistant asks briefly: "Cardinal singing, add it?" → "Yes, two" → *ding*.
3. **Ask about the past**: "What was that just now?" → the assistant checks BirdNET detections from the last ~15 s and answers. No need to wait for the bird to sing again.
4. **Ignores chatter**: talking to a friend ("did you bring sunscreen?") → it stays silent.
5. **Describe-to-ID**: "Gray back, orange breast, walking on the lawn" → 3 candidate cards with photos (from local recent sightings) → "the first one" → *ding*.
6. **Photo ID (any taxon)**: snap a flower or insect → BioCLIP identifies it → logged, so it works beyond birds.
7. **Camera sync (stretch)**: import camera photos → match EXIF timestamps to voice notes ("those shots were the Red-tailed Hawk") → species keywords written to the photos and attached to the checklist.
8. **Export**: eBird Record Format CSV (+ iNaturalist CSV) and a map/summary.

## 3. Architecture
```
Phone (mobile web / PWA)                Backend (Python, laptop -> cloud later)                External
  mic PCM 48 kHz ──── WebSocket ─────▶  fan-out ─┬─▶ 24 kHz ─▶ Higgs Realtime 1.5 (voice, tools)  Boson API
  playback / earcons ◀──────────────────────────┘◀─
                                                 └─▶ 30 s ring buffer (48 kHz)
                                                       └─ every 1.5 s, last 3 s ─▶ BirdNET ─▶ timestamped detections
  GPS, photos, UI ◀── state/events ────  tools: add/remove/get checklist, ignore, recent_sounds,       iNaturalist API
                                           nearby_species, identify_by_description, photo_id
                                         BioCLIP 2 (photo ID, all taxa)
```
- The backend holds the Boson key and executes all tools. The **checklist state lives on the backend**; the UI reflects tool results, never the model's claims.
- **One mic, two consumers.** The phone captures at **48 kHz** (bird songs reach 8-12 kHz+; BirdNET analyzes up to 15 kHz). The backend downsamples a copy to 24 kHz for Realtime and keeps the original in a ring buffer for BirdNET. BirdNET on CPU: ~40 ms per 3 s window.
- Browser mic processing must be **off** (echoCancellation / noiseSuppression / autoGainControl = false), or noise suppression removes bird sounds → use earphones so the assistant's voice isn't picked up.

### Open mic by default: speak little, act a lot
Birders rarely talk to themselves in the field. Speech is usually a sighting, a question, or chat with a companion (whose callouts are worth logging anyway). Wake words would put friction on the most frequent action, so:
- **Everything goes to Realtime** (server VAD). The model gets an **`ignore()` tool**: if the speech is not for the assistant (chatter, phone call, noise), it calls only `ignore()`; the backend sends no `response.create`, so it stays silent.
- **Logging = earcon, not speech.** After `add_observation` succeeds, the backend plays a *ding* and does not ask the model to speak. The model speaks only when something is ambiguous ("which duck?"). Quiet matters: talking scares birds.
- **Sound ID is retrospective.** `recent_sounds(seconds)` queries BirdNET detections from the ring buffer, so "what was that?" works after the bird stops. Proactive suggestions fire only for new species above a threshold, with settings for all / rare only / off. A proactive question keeps the floor ~8 s for the answer.
- **Barge-in**: server VAD detects speech while the assistant talks → cancel the response, and the client flushes queued audio (the server generates audio ~6-8x faster than real time).
- **Group mode (optional)**: a wake word (openWakeWord) or tap-to-talk for noisy, chatty groups.
- Sessions close after ~5 min without speech: reconnect automatically and inject a short checklist summary.
- **Model**: `higgs-realtime-1.5-preview` (listed in /v1/models since Oct 23 evening; set it in both the URL and `session.model`, because the latter overrides the URL). Earlier tests ran on unversioned `higgs-realtime`; re-validating on 1.5.

### UI decisions (Oct 23)
- **English common names + Latin (scientific) names** everywhere; no Chinese in the UI. Voice input can still be any language.
- Candidate and species photos: iNaturalist default taxon photos.
- **Map** of observations (GPS per record): nice-to-have.
- Demo phone: Android + Chrome.
- **Multi-device**: the phone is the mic (+ earphones); a laptop at `/?display=1` mirrors the session (checklist, heard strip, captions, map) on the projector, optionally playing the assistant's voice for the audience (half-duplex echo guard).

## 4. Tools exposed to Higgs Realtime
| Tool | Purpose |
|---|---|
| `add_observation(name, group, count, notes)` | resolves zh/en/scientific names via iNat autocomplete (filtered by group) |
| `remove_observation(name)`, `get_checklist()` | edit/read |
| `ignore()` | speech not addressed to the assistant → stay silent |
| `recent_sounds(seconds)` | BirdNET detections from the last N seconds (ring buffer) |
| `nearby_species(group)` | iNat species counts near GPS (birds, insects, plants, …) |
| `identify_by_description(description, group)` | returns top-3 candidates + photo URLs; user confirms |
| `photo_id(photo_id)` | BioCLIP 2 top-3 |
| `tag_photos(time_range, species)` (stretch) | EXIF time match + XMP keywords |

## 5. What we already verified (Oct 23, see FEASIBILITY.md)
- Realtime: ~0.3–0.5 s text→audio, ~1.0–1.2 s speech-end→audio, barge-in ~0.3 s, multi-step/parallel tool calls correct, mixed zh/en logging correct.
- iNat name resolution (Chinese common names OK), nearby species without a key.
- BirdNET: cardinal 0.97, robin 0.79 on CPU (<1.5 s per 2 min of audio).
- BioCLIP 2: 7/7 correct on birds, insect, butterfly, plant (CPU ~0.3 s/image; Wikimedia images, likely optimistic).
- **Weak spot:** description→species by the model alone was 5/8 → design uses top-3 + user confirmation.

### Why not just ChatGPT / Doubao voice mode?
- General voice assistants can't **keep listening to the environment** and run a dedicated bird-sound classifier. They also hallucinate IDs: Higgs guessed 0/3 bird songs, and general models have the same failure.
- They keep no **structured checklist state** (species, counts, time, GPS, effort) and can't export eBird/iNat data. The value is in the domain tools + state, not in the chat.
- **Proactive** interjections ("I hear a cardinal, add it?") and offline-friendly local models are product decisions general assistants don't make.
- Honest risk: Merlin/eBird adding voice logging is a bigger threat than ChatGPT. Our answer is speed of iteration, multi-taxa, and camera/photo sync.

## 6. Risks & mitigations
| Risk | Mitigation |
|---|---|
| Bluetooth headset mic is narrowband + noise-suppressed → bird sounds lost | demo with wired earphones or the phone mic; pitch dual-mic (voice vs ambient) as future work |
| Browser stops the mic when the phone locks | Wake Lock (screen on) for the demo; native app later |
| HTTPS required for mic/GPS on phones | cloudflared/ngrok tunnel to the backend |
| Bird calls / chatter triggering replies | `ignore()` tool + earcons; VAD threshold; group mode with wake word (testing now) |
| 1.5-preview not yet in docs; latency differs from unversioned | re-run tool/latency/barge-in tests on 1.5; fall back to `higgs-realtime` if worse |
| Model says "added" without calling the tool | UI shows backend state only; instruction + check |
| Noisy venue for live demo | play bird audio from a speaker near the mic; have a recorded backup video |

## 7. Split & timeline (Fri 21:00 → Sat 15:00)
- **A (backend):** relay + Realtime session + tools + BirdNET fork + BioCLIP endpoint.
- **B (frontend):** mobile web UI (checklist, candidate cards, photo capture, map), mic/playback worklets (start from the Higgs Realtime tutorial), export.
- Fri night: phone ↔ backend ↔ Realtime voice loop working end-to-end; checklist tools.
- Sat morning: BirdNET proactive flow, describe-to-ID cards, photo ID.
- Sat 12:00 feature freeze → rehearsal, backup video, slides. (Camera EXIF sync only if ahead.)

## 8. Open questions
- Organizers: may we use non-Boson models (BirdNET, BioCLIP) as tools? Is there an event-specific Realtime or understanding-v3.5 key/endpoint?
- Team: iOS or Android for the demo phone? Any mobile dev experience (affects PWA vs native)?
