# Build plan (Fri night → Sat noon freeze)

## Data model (teammate's proposal: Detection / Observation / Checklist)
| Object | What it is | Created by |
|---|---|---|
| **Detection** | One piece of evidence: a BirdNET window, a BioCLIP frame, or a user description. `{id, kind: sound|image|description, t, candidates:[{sci, en, p}], media?}` | BirdNET worker, `/api/frame`, the router |
| **Observation** | One real organism the user is looking at, plus its evidence. `{id, status: open|confirmed|discarded, detections:[...], fused:[{sci, en, photo, p}], count?, notes, t_start, lat, lng}` | "what's that?" / a proactive sound prompt / a direct sighting |
| **Checklist** | The outing: confirmed observations aggregated by species → eBird CSV. Derived from Observations, not edited separately (manual edits change the Observation). | confirm |

- **Focus**: the backend keeps one `focus` observation. Pronouns and follow-ups ("look again", "what does it eat", "add it", "two of them") refer to the focus. "What about this one?" / a new "what's that?" opens a new focus. An open focus expires after ~2 min idle (kept in history as discarded/unconfirmed).
- Direct sightings ("two mallards") create a confirmed Observation immediately: no camera, just a description detection.
- BirdNET detections within ±10 s of an open focus are attached as sound evidence (attached, not merged blindly: the fusion step decides).

## Fusion (deterministic, explainable)
score(species) = Σ log p_image(frames) + w_s · log p_sound + log prior(local iNat counts) + description match.
- The candidate set is restricted to the local list (iNat 25 km/120 d ∪ BirdNET season list, the same list as the BirdNET range fix).
- Uncertain if top-1 < 0.5 or margin < 0.2 → ask to look again. Calibration is imperfect (docs/vision-test.md): show top-3 and let the user confirm. Never auto-log from vision alone.

## Tools (router)
`look()` (request camera frames for the focus), `describe(text)`, `get_observation()`, `confirm_observation(count, notes)`, `discard_observation()`, `new_observation()`, `ask_expert(question)`, plus the existing add_observation / set_count / remove / get_checklist / recent_sounds / nearby_species / ignore.

## Protocol v0.2 additions (draft)
- Backend → phone `{"type":"capture","obs":"<id>","n":3,"hint":"zoom on the tail"}`: the phone grabs n frames (pick the sharpest, or send all) and POSTs them.
- Phone → backend `POST /api/frame?session=<id>&obs=<id>` with a JPEG body; optional `x,y,w,h` crop (normalized) from a tap on the bird. Returns the detection JSON.
- Backend → all `{"type":"observation", ...Observation, "focus":true|false}` on every change (the display shows the evidence timeline + top-3 bars moving).
- Phone camera UI: `getUserMedia({video:{facingMode:"environment"}})`, digital/optical zoom via `track.applyConstraints({advanced:[{zoom}]})` (Android Chrome), tap-to-crop, "smart glasses" feel = camera preview always on in Look mode.

## Expert path ("GPT Naturalist Reasoning Layer")
- `ask_expert(question)` → a stronger LLM with the focus observation's evidence + candidate species facts (iNat taxon summary / Wikipedia extract) as grounding → returns ≤3 short sentences. Higgs says "let me check" first (latency 2–5 s).
- Its best use is the **discriminating follow-up**: given the top-2 candidates, say what to look at ("Downy vs Hairy: check the bill length relative to the head"). This becomes the `hint` for the next `capture`.
- Also covers questions Higgs gets wrong (it described Blue Jay as having a blue-green crown).
- Open: which model/key (GPT? Claude? Boson's understanding-v3.5 with thinking if hackathon.boson.ai comes up), and whether non-Boson LLMs are OK with the organizers.

## Split
| Who | Owns |
|---|---|
| **Teammate** | Look mode in the phone UI: camera preview, zoom, tap-to-crop, capture → POST frames, Observation card (evidence timeline, top-3 bars), display-mode observation panel. Expert-path prompt design (he proposed it). |
| **Zihan** | Integration and demo owner: end-to-end testing on the Android phone + projector, demo script and recorded backup, pitch slides. Final say on router prompt/tool behavior. |
| **Mika (+ subagents)** | Backend: Observation/Detection model + focus + fusion, `/api/frame` + BioCLIP service (local-list text embeddings precomputed and cached; ~0.7 s/frame CPU), router tools, `ask_expert` plumbing, tests. Keeps PROTOCOL.md current. |

## Timeline
- **Tonight**: Observation model + `/api/frame` + BioCLIP service (Mika); camera UI v0 against a mock (teammate); demo script draft (Zihan).
- **Sat 09:00–12:00**: fusion + look-again loop end-to-end, expert path, display observation panel, field test outside, record backup video.
- **Sat 12:00**: feature freeze → rehearsal, slides.
