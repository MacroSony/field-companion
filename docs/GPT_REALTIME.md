# GPT Realtime experiment

Branch: `feat/gpt-realtime`, based on `feat/look-ui` at `69ca3c9`.

This experiment makes the conversation provider selectable. The default backend
still uses Higgs. OpenAI uses `gpt-realtime-2.1`, input transcription with
`gpt-transcribe`, and the `marin` voice. Credentials stay on the backend; the phone
receives only the selected provider/model and the normal application messages.

## Run

The OpenAI path reuses the project's existing `OPENAI_API_KEY` from the process
environment or ignored `.env.local`. It does not create or copy a key. An absent
key fails startup, without falling back to another provider.

From the repository root in PowerShell:

```powershell
.\backend\start_realtime.ps1 -Provider openai -Port 8800 -Background
```

Open <http://localhost:8800/?session=openai-experiment>. The settings panel shows
the actual connected model. The launcher binds only to loopback and leaves an
occupied port alone. Its process metadata and logs are under
`out/realtime-openai-8800/`. Phone access requires a separate HTTPS tunnel, as in
the existing Higgs setup. Restarting a backend loses its in-memory checklist.

The direct command is:

```powershell
.\.venv\Scripts\python.exe -X utf8 -B backend/server.py --host 127.0.0.1 --port 8800 --realtime-provider openai
```

To use Higgs, run the existing backend command without the new option, or pass
`--realtime-provider higgs`. Distinct ports let both providers run concurrently.
Each backend has its own session/checklist state; using the same session name
on separate ports does not merge their data.

Optional non-secret settings are read from the process environment or
`.env.local`:

| Setting | Default |
| --- | --- |
| `REALTIME_PROVIDER` | `higgs` |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` |
| `OPENAI_REALTIME_TRANSCRIPTION_MODEL` | `gpt-transcribe` |
| `OPENAI_REALTIME_VOICE` | `marin` |

The CLI provider option takes precedence. `--realtime-model` optionally overrides
the OpenAI model. No setting changes the fixed provider API hosts.

## What this changes

- Realtime speech, input transcription and tool routing use the chosen provider.
- OpenAI's initial default `session.created` is not considered ready. The backend
  waits for a matching `session.updated`, including the expected 24 kHz PCM formats.
- Each route/answer update uses the GA `type: realtime` schema. Routing remains
  text-only with a required single tool; answers use audio with tools disabled.
  Low reasoning effort and a 1,024-token response cap bound the experiment.
- Existing playback ownership, cancellation, speaker/earphone modes, camera
  confirmation, task status, GPT expert investigations and checklist tools remain
  shared. The OpenAI inactive-cancellation error also releases pending cancellation.

## Current boundaries

- Camera recognition still uses BioCLIP. Photos are **not** sent to GPT Realtime
  in this first integration; direct visual conversation is a separate experiment.
- Birdsong classification still uses BirdNET. Switching the voice provider does
  not change either specialist model's recognition accuracy.
- Short camera/task acknowledgements still use the existing cached Higgs TTS
  recordings, so their voice can differ from the OpenAI conversation voice.
  Playback still uses the same non-overlapping queue, with no extra TTS requests.
- The existing eight-item conversation pruning and semantic safety checks remain
  in place. This is a provider integration, not a claim of improved field accuracy
  or a complete solution to phone-speaker echo.

## Checks

Offline tests (no live provider calls):

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -p 'test_*.py' -q
```

The opt-in live smoke uses the real configured Realtime provider, synthetic text
or a supplied synthetic 48 kHz mono PCM16 WAV, fixture taxonomy, and a simulated
in-progress photo task. It checks speech, logging, camera confirmation and task
status. It does not play or save response audio, load BirdNET/BioCLIP, or establish
physical-phone accuracy. It makes billable Realtime calls.

```powershell
$env:REALTIME_PROVIDER = 'openai'
.\.venv\Scripts\python.exe -X utf8 -B backend/realtime_smoke.py --report out/gpt-realtime/smoke.json
```

Pass `--audio-input path-to-synthetic.wav` to additionally test actual streamed
audio input and transcription. The report labels fixtures and real services.

### Initial validation (2026-10-24)

- 262 offline tests passed, including 14 provider configuration and handshake tests.
- A real `gpt-realtime-2.1` session accepted the audio/transcription/tool configuration.
- The live synthetic-input smoke passed: microphone transcription and spoken reply,
  two-mallard checklist logging, camera opening for a new animal, waiting for Start
  before capture, and querying the in-progress identification without reopening it.
- The running HTTP/browser WebSocket service reported the expected OpenAI model
  and `2026-10-24-realtime-provider` build.

Reports are ignored under `out/gpt-realtime/`. These checks do not establish
comparative model quality, physical-phone echo handling or real photo recognition.

### Paired regression benchmark

See [the measured comparison](REALTIME_COMPARISON.md) for the initial paired text
and synthetic-audio results, failure examples and remaining shared limitations.

The opt-in benchmark in `backend/realtime_eval.py` compares both configured
providers using 56 frozen synthetic scenarios. It tests tool selection and
meaningful arguments separately from obeying the text-free routing protocol.
It does not execute checklist writes, investigations or recognition services,
and does not connect to or reset the running phone sessions.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B backend/realtime_eval.py --rounds 2 --report out/realtime-eval/text.json
.\.venv\Scripts\python.exe -X utf8 -B backend/realtime_eval_report.py out/realtime-eval/text.json --output out/realtime-eval/text.md
```

Inputs cover named sightings, total-count corrections, pronoun confirmation,
new organisms versus old photo evidence, nonbird categories, progress versus
restarts, task IDs, accepting/rejecting result offers, BirdNET confirmation,
mixed English/Chinese, and companion chatter. Each sample starts a fresh
conversation. Expected answers never enter model prompts. Both sides receive
identical semantic history; their assistant-message encodings differ according
to their API schemas (`text` for Higgs, `output_text` for OpenAI).

For identical synthetic microphone input, generate local Windows speech first:

```powershell
.\backend\make_realtime_eval_audio.ps1
.\.venv\Scripts\python.exe -X utf8 -B backend/realtime_eval.py --rounds 1 --audio-dir out/realtime-eval/audio --report out/realtime-eval/audio.json
.\.venv\Scripts\python.exe -X utf8 -B backend/realtime_eval_report.py out/realtime-eval/audio.json --output out/realtime-eval/audio.md
```

These are real, billable API calls using the existing project keys. Run the text
and audio benchmarks sequentially. `--cases log-two photo-progress` limits a run;
`--providers higgs` or `--providers openai` supports diagnosis. Raw reports and
WAVs are ignored under `out/`; only synthetic scripts and reviewed findings
should be committed. The report includes API failures, retries, latency, raw
calls, guard audits and paired prompt/input hashes. A passing routing score
does not establish spoken-answer quality or field accuracy. Physical-phone
tests remain necessary for echo, interruptions, birdsong and camera recognition.
Start/Identify confirmation is handled by shared deterministic backend code and
the existing smoke test; it is not counted as a model-intelligence comparison.

## Official protocol references

- [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Realtime client events](https://developers.openai.com/api/reference/resources/realtime/client-events)
- [GPT-Realtime-2.1](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)
