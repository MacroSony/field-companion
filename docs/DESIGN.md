# [Name TBD] — Hands-free field companion for birders & naturalists

**One-liner:** Merlin tells you what's singing. We let you log it — and anything else you see — without taking your eyes off the binoculars.

## 1. Problem & user
- Birders/photographers have both hands busy (binoculars, camera). Logging on eBird Mobile means looking down and tapping through lists.
- Existing tools: Merlin and BirdNET identify by sound or photo; Robin (UMich) adds voice control for accessibility. **None of them does conversational, hands-free checklist logging** that combines sound ID, photo ID and your own observations.
- Target: a walking birder with earphones, a phone in the pocket and maybe a camera.

## 2. Core user flows (demo order, ~3 min)
1. **Proactive sound ID**: the phone listens in the background. BirdNET hears a Northern Cardinal → the assistant cuts in: "I hear a Northern Cardinal, add it?" → "Yes, two, a male and a female" → the checklist updates on screen.
2. **Voice logging**: "Three mallards on the pond" → "Actually four." Mixed Chinese/English is fine.
3. **Describe-to-ID**: "Gray back, orange breast, walking on the lawn" → 3 candidate cards with photos (from local recent sightings) → "the first one" → logged.
4. **Photo ID (any taxon)**: snap a photo of a flower or insect → BioCLIP identifies it → logged, so it works beyond birds.
5. **Camera sync (stretch)**: import camera photos → match EXIF timestamps to voice notes ("those shots were the Red-tailed Hawk") → species keywords written to the photos and attached to the checklist.
6. **Export**: eBird Record Format CSV (+ iNaturalist CSV) and a map/summary.

## 3. Architecture
```
Phone (mobile web / PWA)              Backend (Python, laptop → cloud later)               External
  mic PCM  ───────── WebSocket ─────▶ relay ──▶ Higgs Realtime (voice, turn-taking, tools)  Boson API
  playback ◀──────────────────────── relay ◀──
  GPS, photos, UI ◀─ state/events ──  tools: add/remove/get checklist, nearby_species,      iNaturalist API
                                         identify_by_description, photo_id
                                       BirdNET (forked mic audio, 3 s windows) → proactive suggestions
                                       BioCLIP 2 (photo ID, all taxa)
```
- The backend holds the Boson key, forwards audio both ways and executes tools. The **checklist state lives on the backend**; the UI reflects tool results, never the model's claims.
- BirdNET runs on the same mic stream. A new species above the confidence threshold → inject `[NOTIFY] BirdNET heard X (conf)` → the assistant speaks up (tested: about 0.4 s to start speaking).

### Audio gating: always listening, but only talking when addressed
Higgs Realtime **always replies** after it detects the end of a speech turn (there is no "listen silently" mode), and it closes a session after 5 minutes without speech. So we must not stream the raw mic straight to it all the time.
- **Backend receives the full mic stream** and forks it: (1) BirdNET always; (2) a local wake-word / speech gate; (3) Higgs Realtime **only while the gate is open**.
- **Gate opens** on: wake word (e.g. openWakeWord on the backend; a pre-trained phrase for the demo, custom "hey <name>" later), a tap on the screen or headset button, or a proactive BirdNET suggestion (then keep it open for ~8 s for the answer).
- On opening, send ~0.5 s of pre-roll audio so the first word isn't cut. **Gate closes** after the reply has finished and N seconds of silence.
- **Barge-in** while the assistant talks: the gate is open, server VAD detects speech, the response is cancelled, and the client flushes queued playback (it must; the server generates audio ~6–8× faster than real time).
- Reconnect on idle timeout. Checklist state lives on the backend, so a new session gets a short state summary injected.
- Earphones avoid echo. Overheard conversations stay outside the gate.

## 4. Tools exposed to Higgs Realtime
| Tool | Purpose |
|---|---|
| `add_observation(name, group, count, notes)` | resolves zh/en/scientific names via iNat autocomplete (filtered by group) |
| `remove_observation(name)`, `get_checklist()` | edit/read |
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
| Bird calls triggering VAD/barge-in | far-field noise reduction, higher VAD threshold; test early |
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
