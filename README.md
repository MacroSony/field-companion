# Field Companion

**Hands-free birding: talk to log, listen for birds, and look closer—without turning your outing into a chat session.**

Built for the Boson AI × MScAC Higgs Audio Hackathon, October 23–24, 2026.
## What it does
- **Talk to log:** species, counts and corrections become a structured checklist;
  successful logging gets an earcon, not a spoken interruption.
- **Listen in the background:** server-side BirdNET proposes birds it hears;
  accept or dismiss the suggestions by voice or on screen. Nothing auto-logs.
- **“What was that?”:** retrospectively query recent BirdNET detections after a
  call ends. This replays detection history, **not recorded audio**.
- **Look mode:** open the camera, aim/zoom/select a region, then say “start” or
  tap Identify. The phone picks the sharpest of up to three frames; BioCLIP 2
  ranks candidates. Look again adds evidence; confirmation adds the observation.
- **Background naturalist investigations:** ask about the current organism;
  keep logging while GPT retrieves species context and analyzes an evidence
  snapshot. Task cards show progress, cancellation/retry and source-linked results;
  a ready notice offers an explanation rather than reading it uninvited.
- **Export:** eBird Record Format CSV (Incidental, not a complete effort checklist)
  and a generic all-taxa CSV. No direct eBird upload or iNaturalist import claim.
- **Big-screen mode:** `/?display=1&session=demo` mirrors the phone's outing;
  append `&audio=1` for assistant audio. The display requests no mic or camera.

## Architecture
```text
Phone PWA (mic / camera / GPS / checklist) + big-screen display
                        <-> same-origin WebSocket + JPEG HTTP
                   FastAPI backend (session state + tools)
                        <->
   +---------------------+---------------------------------------+
   | Higgs Realtime: STT -> route-then-speak tools -> speech    |
   | Higgs TTS: pre-generated feedback clips (bundled PCM WAVs)   |
   | BirdNET: background sound windows + suggestions             |
   | BioCLIP 2 service: image rankings (separate CPU environment) |
   | iNaturalist: names, photos, nearby taxa / geographic context |
   | GPT expert background jobs: evidence + Wikipedia extracts   |
   +-------------------------------------------------------------+
```

One mic stream feeds both paths: 48 kHz PCM for BirdNET and a downsampled
24 kHz copy for Higgs. The backend owns observations and counts; model speech
never substitutes for a successful tool result. State is in memory, not a database.

## How we use Higgs
- **`higgs-realtime-1.5-preview`** provides transcription, tool routing and spoken
  answers. Server VAD detects turns; automatic responses and automatic VAD
  interruption are disabled so the backend controls when to act and speak.
- **Route, then speak:** switch **session** configuration in two phases and wait
  for the settings echo. Routing is text-only with `tool_choice: required`;
  read/answer turns switch to audio with no tools. Per-response overrides were
  unreliable in our probes—see [router-test](docs/router-test.md).
- `ignore()` keeps chatter/fillers silent. Validated checklist writes play only
  an earcon; read-only questions and approved tasks can speak. Routing is not
  infallible, so stale/ungrounded writes are rejected or clarified.
- **Interruptions:** with earphones, speaking cancels the reply at voice onset
  ("stop" / "等一下" works). Speaker mode and audio-enabled displays are
  half-duplex: mic audio is not sent to Higgs while the assistant plays (plus a
  short tail), so tap to interrupt. Transcript echo filtering and BirdNET
  playback-window exclusion keep the assistant from hearing itself.
- English/Chinese input and code-switching are supported; STT and routing can
  still misinterpret short confirmations in noise.
- A bounded conversation window uses `conversation.item.delete` (last eight
  tracked items), plus a fresh compact state snapshot for routing.
- **Higgs TTS 3 (`higgs-tts-3`)** generated the bundled feedback clips, including
  “Let me take a look,” “Let me check,” and “Camera ready…” in English/Chinese.
  They play locally through the shared audio queue; no synthesis call per cue.

## Quick start (Python 3.12, Linux/macOS shell)

The frontend is plain HTML/CSS/JS: no Node install or build step.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# Optional but required for background bird recognition (large TensorFlow install):
.venv/bin/python -m pip install -r requirements-birdnet.txt
```

Provide a Boson key via **`~/.config/boson/api_key`** (file contains only the key)
OR `BOSON_API_KEY` in the server environment. Boson does not read `.env.local`.
Optional GPT investigations read `OPENAI_API_KEY` from the environment or ignored
root `.env.local`; without it, basic voice/checklist/vision still work.
Never put keys in browser code or commit them.

For Look mode, install/start BioCLIP in a **separate environment** (large downloads;
first start downloads model weights/embeddings). CPU wheels avoid the CUDA stack:

```bash
python3.12 -m venv .venv-vision
.venv-vision/bin/python -m pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cpu
.venv-vision/bin/python -m pip install -r requirements-vision.txt
.venv-vision/bin/python backend/vision_service.py --port 8796 --threads 4
# In another terminal, from the same repository root:
VISION_URL=http://127.0.0.1:8796 .venv/bin/python backend/server.py --host 127.0.0.1 --port 8765
```

Without vision, run just the final server command; photo identification will be
unavailable. `VISION_URL` otherwise defaults to `http://127.0.0.1:8795`.
Keep vision loopback-only. Cache defaults: ignored `out/cache/` (override
`FIELD_COMPANION_CACHE`) and `out/hf-cache/` (override `HF_HOME`).

Open `http://localhost:8765/` locally. For a phone, use an HTTPS tunnel, e.g.
`cloudflared tunnel --url http://127.0.0.1:8765`, then open its HTTPS URL.
Mic/camera require a secure context. Allow permissions, Start, and use the same
`?session=demo` on phone/display. Prefer the phone mic + wired earphones.
**No authentication:** use a temporary trusted demo tunnel, not a public deployment.
For Windows and detailed model setup: [LOCAL_DEVELOPMENT](docs/LOCAL_DEVELOPMENT.md).

## Tests and reproducibility
```bash
(cd backend && ../.venv/bin/python -m unittest discover -p 'test_*.py')
for js in web/*.js; do node --check "$js"; done
# Optional live Higgs check; external fixtures and a real key are required:
.venv/bin/python backend/selftest.py --serve --data-dir /path/to/fixtures
```

Live fixtures use `img/cardinal.jpg`, or `testmix/{cardinal,robin,bluejay}_best.wav`
for `--chorus`. Override with `FIELD_COMPANION_TEST_DATA`, `--image` or
`--chorus-file`. Missing fixtures print **SKIP**, not a successful live result.
[Submission verification](docs/SUBMISSION.md) records the fresh-clone commands,
235 passing offline tests, dependency sizes and what was **not** tested.

## Known limitations
- BirdNET runs on the **server**, not the phone; core-only installation omits it.
- Bluetooth headsets force call-mode capture on tested hardware, killing birdsong
  detail. Separate phone-mic/Bluetooth-output selection still needs device testing.
- Speaker mode is **half-duplex**: you cannot interrupt by voice while the
  assistant speaks (tap instead), and BirdNET skips windows overlapping playback.
  Full-duplex over a loudspeaker without browser echo cancellation was unreliable.
- Bird vision is restricted to a local species list when available: out-of-range
  birds can be confidently forced to a local look-alike. Nonbirds use open rankings;
  scores/fusion are uncalibrated, not field-accuracy guarantees.
- Compound requests are only partly handled; short/noisy speech can misroute.
- GPT expert needs an OpenAI key; retrieval/citation checks do not guarantee facts.
- Tested mainly on one Android/Chrome setup; no broad iOS/device/field validation.
- No persistent storage/authentication; restart loses outings. Export before stopping.
- Licensed `testaudio`/photo fixtures are not included; live probes can skip.

## Repo and further reading
`backend/`: FastAPI, routing/state, expert queue, vision service and unit tests.
`web/`: phone PWA, display, audio worklet, camera and CSV export.
`docs/`: [DESIGN](docs/DESIGN.md), [PROTOCOL](docs/PROTOCOL.md),
[INVESTIGATIONS](docs/INVESTIGATIONS.md), [CAMERA](docs/CAMERA.md),
[EXPERT](docs/EXPERT.md), [ISSUES](docs/ISSUES.md),
[Look/expert tests](docs/LOOK_EXPERT_TESTING.md), [vision tests](docs/vision-test.md).
`probes/`: Higgs/router/ignore/fan-out/vision experiments. Older design and test
notes describe their dated revisions, not necessarily today's behavior.

## Team
- Zihan Wang (@MacroSony) — TODO
- Tianlei (@TianleiP) — TODO
