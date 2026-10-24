# Experiment E (not deployed)

`TEMPLATE_ANSWERS=1` enables deterministic English answers only for
`recent_sounds` and `get_checklist`. Unset/default retains Realtime answers.
`TEMPLATE_TTS_VOICE` optionally overrides the REST voice (default: `default`).
No dependency changes. Authentication reuses `server.api_key()`.

REST uses a per-session pooled HTTP connection, PCM16 mono 24 kHz,
three seconds to first body data / between chunks, twenty seconds total.
Complete samples enter the shared Realtime playback method. Quiet, stale
questions, tap cancellation, VAD barge-in, flush, echo timing and client
speaking state use the existing session controls. Completed answer text is
inserted as an assistant conversation item and participates in history pruning.
Errors and empty/truncated streams fall back to Realtime; a partial stream is
flushed first (the answer may repeat). Cancellation does not trigger fallback.

Offline tests: `cd backend && python -m unittest discover -v`.
Real-upstream benchmark: `python probes/overnight_e.py` (owns port 8797 only;
synthetic local data, real Higgs routing/answers). It clears conversation items
before each question to avoid previous upstream failures polluting routing;
this is a controlled latency experiment, not a production-history test.
Voice samples: `python probes/overnight_e_voices.py`.
Summary: `python probes/overnight_e_summary.py`.
Artifacts: `/data/build/tmp/overnight/E/`.

Remaining: human voice comparison and phone/microphone barge-in testing;
concurrent multi-user load; interrupted-answer history semantics (only completed
answers are stored); repeated speech after partial synthesis failure; English-only
templates despite session language; real detector/checklist fixtures beyond the
controlled two-species benchmark. A blocking HTTP reader can live for its socket
timeout after cancellation, but cannot send audio after the session gates close.
