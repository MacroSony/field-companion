# Audio fan-out prototype

Status: **two complete 66 s live runs passed**, following two aborted attempts.
The last run tests the final source, including corrected nominal-window alignment.
No commits or pushes. Only this report and `probes/fanout_probe.py` were written;
`websockets` was installed into the existing BirdNET venv. Unrelated files were left alone.

## Setup

Used `/data/build/birding-demo/.venv`, adding only `websockets`.
One synthetic 48 kHz mono microphone stream: 40 s cardinal + 20 s robin
at reduced gain, then 6 s silence; TTS overlays at 8, 30, 50 s.
Fork 40 ms frames to Higgs Realtime (24 kHz) and a 30 s BirdNET ring.
Analyze the last 3 s every 1.5 s on a dedicated worker thread.
All test evidence is appended here; audio and credentials are not saved.

## Results — final-source run

| Measurement | Result |
| --- | --- |
| Input | 66 s; 1,650 × 40 ms frames; no clipped samples |
| BirdNET | 43 windows, 3–66 s inclusive, every 1.5 s; zero skipped |
| BirdNET compute | mean **37.92 ms**, median **34.93 ms**, p95 **56.57 ms**, max **59.91 ms** |
| Warm-up (excluded above) | 49.02 ms after model initialization |
| Event-loop timer lag | median 0.65 ms, p95 1.79 ms, max **2.89 ms**; zero stalls >50 ms |
| Audio-send lateness vs frame deadline | median 1.60 ms, p95 2.80 ms, max 6.90 ms; queue max 1 frame |
| VAD | 3 starts / 3 stops; **0 bird-only triggers** in final run |
| Speech-stop event → first audio | 561.0 / 545.6 / 505.7 ms (median **545.6 ms**) |
| Approximate TTS active end → first audio | 1,391 / 1,272 / 1,198 ms |
| Proactive notifications | **3/3 succeeded**, no duplicates; injection 0.2–0.3 ms after detection |
| Detection → first proactive audio | Cardinal **671.9 ms**, Sparrow **357.6 ms**, Robin **410.2 ms** |
| Species-count question | **Correct relative to tools:** 3 species heard, 1 confirmed checklist species (2 cardinals) |

Output audio was received and counted, **not played**. First-audio latency is network
arrival, not speaker onset. Timer lag samples a 20 ms watchdog and is not a CPU profile.
BirdNET model loading is outside the streaming measurements. Input downsampling uses a
stateful anti-alias FIR (~0.65 ms group delay). The 30 s ring wraps during the test;
1.5 s boundaries use exact sample positions, even when a 40 ms frame overshoots by 20 ms.

### Timeline (final run; wall seconds from streaming start)

| Time | Event |
| --- | --- |
| 3.037 | Cardinal detected, confidence 0.6501; first proactive audio 3.709 |
| 8.268–11.749 | First TTS detected by VAD; transcript correct; add_observation at 12.930 stores Northern Cardinal ×2 |
| 27.054 | House Sparrow detected, confidence 0.7035; notification audio 27.412 |
| 30.311–32.469 | “刚才那是什么在叫？”; transcript correct |
| 33.367 | recent_sounds(10) returns Cardinal + Sparrow; answer audio 34.222; final Chinese answer names both |
| 40.000 | Background switches to robin recording |
| 49.558 | First Robin result above 0.25: confidence 0.2804 |
| 50.311–52.465 | “How many species do we have?”; transcript correct |
| 53.490 | get_checklist + recent_sounds(60); heard set Cardinal/Sparrow/Robin; checklist Cardinal ×2 |
| 53.917 | First audio of substantive count answer (1.452 s after VAD stop; earlier audio was tool preamble) |
| 55.392 | Answer complete: 1 confirmed species, 3 distinct heard species; all names match tool output |
| 55.558 | First Robin confidence ≥0.5: 0.8441; proactive audio 55.968 |
| 66.063 | All frames sent and all 43 windows analyzed |

The 30 s question's substantive answer begins **1.753 s** after VAD stop, rather than
the 546 ms pre-tool preamble. Cardinal ×2 is a fake checklist entry, not a field count.

### Detection delay / accuracy caveats

- Relative to **recording-segment start**, Cardinal first result is **3.037 s**;
  Robin first ≥0.25 is **9.558 s**, first ≥0.5 is **15.558 s** after its 40 s start.
  These are not precise song-onset delays: species-specific acoustic onsets were not
  annotated. Windows are 3 s long with 1.5 s hops; availability includes inference time.
- The earlier complete run detected a low-confidence Robin at 43.559 s (3.559 s after
  segment start). TTS was regenerated between runs and the final run corrects the
  20 ms window-boundary offset. This was **not a controlled accuracy comparison**.
- BirdNET also reports **House Sparrow**, including 0.7035 confidence, in the cardinal
  recording. Could be a co-occurring bird or misclassification; not independently
  verified. Thus “3 heard” correctly reflects tool data, **not verified truth that the
  two named recordings contain three species**. Detection-derived counts need qualification.

### Failures and fixes still needed

- First aborted attempt waited 30 s for `session.updated`; service delivered only
  `session.created`. Fixed by accepting either acknowledgement; final returned settings
  confirm server_vad threshold 0.5, silence 600 ms, model 1.5-preview. Initial-session
  events may show defaults, so production code should verify applied configuration.
- Second aborted attempt hit a logger argument-name collision at first response;
  fixed and covered by both complete runs.
- Earlier complete run: **1 bird-only VAD trigger** at 42.111 s, transcript “Oh.”,
  resulting in an unsolicited checklist response. Final run had zero. Across the two
  complete runs: **1/132 s** of input. Classification is relative to known synthetic
  utterance times; no independent speech audit of source recordings was performed.
- Earlier run's Sparrow proactive response failed with `server_error/upstream_error`;
  **2/3 notifications succeeded** there. Final run **3/3**. Add bounded retry/backoff;
  currently first-detection deduplication prevents retrying a failed announcement.
- Busy-response and active-speech notification queueing passed **offline mock checks**;
  live detections happened while idle, so live queued delivery was not exercised.
  Production needs robust arbitration with server-created VAD responses and cancellation.
- Raise/tune VAD threshold or add speech gating to prevent bird-only turns; evaluate
  confidence persistence before notifying or counting species. Replies were not always
  in the user's language (first Chinese observation got an English acknowledgement).
- This is file-backed synthetic mic input, no actual device capture, playback, echo
  cancellation, or hardware end-to-end latency test. No reconnect handling. BirdNET
  worker overload logs/skips a window instead of blocking capture; not reached here.

### Reproduction / checks

```sh
cd /data/build/field-companion
TMPDIR=/data/build/tmp /data/build/birding-demo/.venv/bin/pip install websockets
PYTHONDONTWRITEBYTECODE=1 /data/build/birding-demo/.venv/bin/python -B probes/fanout_probe.py
```

Python 3.12; birdnetlib 0.18.1, ai-edge-litert 2.3.0 (existing tflite_runtime shim),
numpy 2.1.3, scipy 1.18.1, websockets 17.3, requests 2.34.2. Geographic filter:
43.66, −79.39; date 2026-05-15; min_conf 0.25. `NUMBA_DISABLE_JIT=1` avoids lazy JIT
compilation during streaming. TTS generated in memory each run; no audio artifacts.
The helper reads credentials internally; no credentials were logged or saved.

Also ran in-memory AST syntax validation and mock checks for response/speech queueing,
one-shot flush/reservation, recent_sounds time filtering, fake checklist species count,
and exact ring-window indexing across wrap. All passed (`offline_checks` below).
Raw incremental evidence follows, including failed attempts; **the last Live evidence
section is the final-source run**. The single `Test Bird` notification is a mock event,
not an audio detection.

## Live evidence — 2026-10-23T20:45:08

- `{"event": "tts_ready", "start": 8, "end": 11.24, "text": "两只北美红雀在灌木丛里", "active_end": 10.991}`
- `{"event": "tts_ready", "start": 30, "end": 31.72, "text": "刚才那是什么在叫？", "active_end": 31.519}`
- `{"event": "tts_ready", "start": 50, "end": 51.96, "text": "how many species do we have?", "active_end": 51.662}`
- `{"event": "mixed_stream", "duration_s": 66, "background_gain": 0.25, "tts_gain": 0.85, "clipped_samples": 0, "background_segments": [{"species": "Northern Cardinal", "start": 0, "end": 40}, {"species": "American Robin", "start": 40, "end": 60}]}`
- `{"event": "birdnet_warmup", "compute_ms": 51.68}`
- `{"event": "run_failed", "error": "TimeoutError: "}`
- `{"event": "summary", "wall_s": 0, "stream_s": 0.0, "failure": "TimeoutError: ", "bird_windows": 0, "bird_compute_ms": {}, "event_loop_lag_ms": {}, "audio_send_lag_ms": {}, "event_loop_stalls_over_50ms": 0, "vad_starts": 0, "bird_only_vad_candidates": 0, "unique_birdnet_species": [], "checklist": [], "notifications": [], "vad_stop_to_first_audio_ms": {}, "skipped_windows": 0, "pending_notifications": 0}`

## Live evidence — 2026-10-23T20:46:06

- `{"event": "tts_ready", "start": 8, "end": 11.04, "text": "两只北美红雀在灌木丛里", "active_end": 10.774}`
- `{"event": "tts_ready", "start": 30, "end": 31.88, "text": "刚才那是什么在叫？", "active_end": 31.644}`
- `{"event": "tts_ready", "start": 50, "end": 51.96, "text": "how many species do we have?", "active_end": 51.658}`
- `{"event": "mixed_stream", "duration_s": 66, "background_gain": 0.25, "tts_gain": 0.85, "clipped_samples": 0, "background_segments": [{"species": "Northern Cardinal", "start": 0, "end": 40}, {"species": "American Robin", "start": 40, "end": 60}]}`
- `{"event": "birdnet_warmup", "compute_ms": 52.44}`
- `{"event": "session_ready", "acknowledgement": "session.created", "model": "higgs-realtime-1.5-preview", "turn_detection": {"type": "server_vad", "create_response": true, "interrupt_response": true, "min_speech_duration": 0.125, "prefix_padding_ms": 300, "silence_duration_ms": 600, "threshold": 0.5}, "note": "session.created reflects defaults; requested session.update may not be acknowledged"}`
- `{"event": "bird_window", "wall_s": 3.0403, "stream_s": 3.0, "window_start_s": 0.0, "window_end_s": 3.0, "compute_ms": 35.52, "executor_total_ms": 35.81, "count": 1}`
- `{"event": "bird_detection", "wall_s": 3.0405, "stream_s": 3.0, "time": 3.0, "window_start": 0.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.6501, "compute_ms": 35.52, "available_wall_s": 3.0405}`
- `{"event": "notify_queued", "wall_s": 3.049, "stream_s": 3.0, "species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.049, "window_end_s": 3.0, "busy": false, "speaking": false}`
- `{"event": "notify_injected", "wall_s": 3.0493, "stream_s": 3.0, "species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.049, "window_end_s": 3.0, "injected_wall_s": 3.0493, "detection_to_inject_ms": 0.3}`
- `{"event": "run_failed", "error": "TypeError: Probe.log() got multiple values for argument 'kind'"}`
- `{"event": "summary", "wall_s": 3.1041, "stream_s": 3.04, "failure": "TypeError: Probe.log() got multiple values for argument 'kind'", "bird_windows": 1, "bird_compute_ms": {"n": 1, "median": 35.52, "p95": 35.52, "max": 35.52}, "event_loop_lag_ms": {"n": 148, "median": 0.67, "p95": 1.83, "max": 3.43}, "audio_send_lag_ms": {"n": 76, "median": 1.7, "p95": 3.23, "max": 10.2}, "event_loop_stalls_over_50ms": 0, "vad_starts": 0, "bird_only_vad_candidates": 0, "unique_birdnet_species": ["Northern Cardinal"], "checklist": [], "notifications": [{"species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.049, "window_end_s": 3.0, "injected_wall_s": 3.0493}], "vad_stop_to_first_audio_ms": {}, "skipped_windows": 0, "pending_notifications": 0}`

## Live evidence — 2026-10-23T20:46:21

- `{"event": "tts_ready", "start": 8, "end": 11.0, "text": "两只北美红雀在灌木丛里", "active_end": 10.987}`
- `{"event": "tts_ready", "start": 30, "end": 31.92, "text": "刚才那是什么在叫？", "active_end": 31.665}`
- `{"event": "tts_ready", "start": 50, "end": 52.0, "text": "how many species do we have?", "active_end": 51.74}`
- `{"event": "mixed_stream", "duration_s": 66, "background_gain": 0.25, "tts_gain": 0.85, "clipped_samples": 0, "background_segments": [{"species": "Northern Cardinal", "start": 0, "end": 40}, {"species": "American Robin", "start": 40, "end": 60}]}`
- `{"event": "birdnet_warmup", "compute_ms": 51.35}`
- `{"event": "session_ready", "acknowledgement": "session.created", "model": "higgs-realtime-1.5-preview", "turn_detection": {"type": "server_vad", "create_response": true, "interrupt_response": true, "min_speech_duration": 0.125, "prefix_padding_ms": 300, "silence_duration_ms": 600, "threshold": 0.5}, "note": "session.created reflects defaults; requested session.update may not be acknowledged"}`
- `{"event": "bird_window", "wall_s": 3.0405, "stream_s": 3.0, "window_start_s": 0.0, "window_end_s": 3.0, "compute_ms": 34.81, "executor_total_ms": 35.09, "count": 1}`
- `{"event": "bird_detection", "wall_s": 3.0407, "stream_s": 3.0, "time": 3.0, "window_start": 0.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.6501, "compute_ms": 34.81, "available_wall_s": 3.0407}`
- `{"event": "notify_queued", "wall_s": 3.0408, "stream_s": 3.0, "species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.0407, "window_end_s": 3.0, "busy": false, "speaking": false}`
- `{"event": "notify_injected", "wall_s": 3.041, "stream_s": 3.0, "species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.0407, "window_end_s": 3.0, "injected_wall_s": 3.041, "detection_to_inject_ms": 0.3}`
- `{"event": "response_created", "wall_s": 3.067, "stream_s": 3.04, "response_id": "de74_resp_2", "kind": "notify"}`
- `{"event": "first_audio", "wall_s": 3.4271, "stream_s": 3.4, "response_id": "de74_resp_2", "kind": "notify", "detection_to_first_audio_ms": 386.4}`
- `{"event": "response_done", "wall_s": 3.8821, "stream_s": 3.88, "response_id": "de74_resp_2", "kind": "notify", "status": "completed", "text": "Northern Cardinal, heard.", "audio_bytes": 119040, "status_details": null}`
- `{"event": "bird_window", "wall_s": 4.5601, "stream_s": 4.52, "window_start_s": 1.5, "window_end_s": 4.5, "compute_ms": 34.78, "executor_total_ms": 35.12, "count": 1}`
- `{"event": "bird_detection", "wall_s": 4.5602, "stream_s": 4.52, "time": 4.5, "window_start": 1.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8721, "compute_ms": 34.78, "available_wall_s": 4.5602}`
- `{"event": "bird_window", "wall_s": 6.0382, "stream_s": 6.0, "window_start_s": 3.0, "window_end_s": 6.0, "compute_ms": 34.69, "executor_total_ms": 34.99, "count": 2}`
- `{"event": "bird_detection", "wall_s": 6.0383, "stream_s": 6.0, "time": 6.0, "window_start": 3.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.814, "compute_ms": 34.69, "available_wall_s": 6.0383}`
- `{"event": "bird_detection", "wall_s": 6.0384, "stream_s": 6.0, "time": 6.0, "window_start": 3.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3052, "compute_ms": 34.69, "available_wall_s": 6.0384}`
- `{"event": "bird_window", "wall_s": 7.5577, "stream_s": 7.52, "window_start_s": 4.5, "window_end_s": 7.5, "compute_ms": 34.59, "executor_total_ms": 34.83, "count": 2}`
- `{"event": "bird_detection", "wall_s": 7.5579, "stream_s": 7.52, "time": 7.5, "window_start": 4.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8422, "compute_ms": 34.59, "available_wall_s": 7.5579}`
- `{"event": "bird_detection", "wall_s": 7.558, "stream_s": 7.52, "time": 7.5, "window_start": 4.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4274, "compute_ms": 34.59, "available_wall_s": 7.558}`
- `{"event": "vad", "wall_s": 8.3154, "stream_s": 8.28, "type": "input_audio_buffer.speech_started", "audio_time_s": 7.956, "bird_only_candidate": false}`
- `{"event": "bird_window", "wall_s": 9.0381, "stream_s": 9.0, "window_start_s": 6.0, "window_end_s": 9.0, "compute_ms": 35.02, "executor_total_ms": 35.34, "count": 0}`
- `{"event": "bird_window", "wall_s": 10.5583, "stream_s": 10.52, "window_start_s": 7.5, "window_end_s": 10.5, "compute_ms": 35.42, "executor_total_ms": 35.64, "count": 0}`
- `{"event": "vad", "wall_s": 11.5087, "stream_s": 11.48, "type": "input_audio_buffer.speech_stopped", "audio_time_s": 12.056, "bird_only_candidate": false}`
- `{"event": "response_created", "wall_s": 11.522, "stream_s": 11.48, "response_id": "de74_resp_4", "kind": "vad"}`
- `{"event": "user_transcript", "wall_s": 11.8167, "stream_s": 11.8, "text": "两只北美红雀在灌木丛里。", "item_id": "f2a42508"}`
- `{"event": "bird_window", "wall_s": 12.0376, "stream_s": 12.0, "window_start_s": 9.0, "window_end_s": 12.0, "compute_ms": 34.86, "executor_total_ms": 35.1, "count": 1}`
- `{"event": "bird_detection", "wall_s": 12.0378, "stream_s": 12.0, "time": 12.0, "window_start": 9.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4233, "compute_ms": 34.86, "available_wall_s": 12.0378}`
- `{"event": "first_audio", "wall_s": 12.041, "stream_s": 12.04, "response_id": "de74_resp_4", "kind": "vad", "vad_stop_to_first_audio_ms": 532.3, "tts_active_end_to_first_audio_ms": 1054.0}`
- `{"event": "response_done", "wall_s": 12.5425, "stream_s": 12.52, "response_id": "de74_resp_4", "kind": "vad", "status": "completed", "text": "Adding two Northern Cardinals to the checklist.", "audio_bytes": 145920, "status_details": null}`
- `{"event": "tool_call", "wall_s": 12.5427, "stream_s": 12.52, "name": "add_observation", "arguments": {"species": "Northern Cardinal", "count": 2}, "result": {"ok": true, "checklist": [{"species": "Northern Cardinal", "count": 2}]}}`
- `{"event": "response_created", "wall_s": 12.5829, "stream_s": 12.56, "response_id": "de74_resp_7", "kind": "vad"}`
- `{"event": "first_audio", "wall_s": 12.8766, "stream_s": 12.84, "response_id": "de74_resp_7", "kind": "vad"}`
- `{"event": "response_done", "wall_s": 13.3881, "stream_s": 13.36, "response_id": "de74_resp_7", "kind": "vad", "status": "completed", "text": "Two Northern Cardinals confirmed in the checklist.", "audio_bytes": 149760, "status_details": null}`
- `{"event": "bird_window", "wall_s": 13.5574, "stream_s": 13.52, "window_start_s": 10.5, "window_end_s": 13.5, "compute_ms": 34.94, "executor_total_ms": 35.24, "count": 1}`
- `{"event": "bird_detection", "wall_s": 13.5575, "stream_s": 13.52, "time": 13.5, "window_start": 10.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.7957, "compute_ms": 34.94, "available_wall_s": 13.5575}`
- `{"event": "bird_window", "wall_s": 15.0386, "stream_s": 15.0, "window_start_s": 12.0, "window_end_s": 15.0, "compute_ms": 35.32, "executor_total_ms": 35.56, "count": 1}`
- `{"event": "bird_detection", "wall_s": 15.0387, "stream_s": 15.0, "time": 15.0, "window_start": 12.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8503, "compute_ms": 35.32, "available_wall_s": 15.0387}`
- `{"event": "bird_window", "wall_s": 16.5569, "stream_s": 16.52, "window_start_s": 13.5, "window_end_s": 16.5, "compute_ms": 34.14, "executor_total_ms": 34.36, "count": 1}`
- `{"event": "bird_detection", "wall_s": 16.557, "stream_s": 16.52, "time": 16.5, "window_start": 13.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.9145, "compute_ms": 34.14, "available_wall_s": 16.557}`
- `{"event": "bird_window", "wall_s": 18.0381, "stream_s": 18.0, "window_start_s": 15.0, "window_end_s": 18.0, "compute_ms": 35.24, "executor_total_ms": 35.42, "count": 0}`
- `{"event": "bird_window", "wall_s": 19.5578, "stream_s": 19.52, "window_start_s": 16.5, "window_end_s": 19.5, "compute_ms": 34.69, "executor_total_ms": 34.9, "count": 0}`
- `{"event": "bird_window", "wall_s": 21.0378, "stream_s": 21.0, "window_start_s": 18.0, "window_end_s": 21.0, "compute_ms": 34.37, "executor_total_ms": 34.6, "count": 1}`
- `{"event": "bird_detection", "wall_s": 21.0379, "stream_s": 21.0, "time": 21.0, "window_start": 18.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3378, "compute_ms": 34.37, "available_wall_s": 21.0379}`
- `{"event": "bird_window", "wall_s": 22.5574, "stream_s": 22.52, "window_start_s": 19.5, "window_end_s": 22.5, "compute_ms": 34.77, "executor_total_ms": 34.99, "count": 1}`
- `{"event": "bird_detection", "wall_s": 22.5576, "stream_s": 22.52, "time": 22.5, "window_start": 19.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2823, "compute_ms": 34.77, "available_wall_s": 22.5576}`
- `{"event": "bird_window", "wall_s": 24.0374, "stream_s": 24.0, "window_start_s": 21.0, "window_end_s": 24.0, "compute_ms": 34.07, "executor_total_ms": 34.31, "count": 1}`
- `{"event": "bird_detection", "wall_s": 24.0376, "stream_s": 24.0, "time": 24.0, "window_start": 21.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3359, "compute_ms": 34.07, "available_wall_s": 24.0375}`
- `{"event": "bird_window", "wall_s": 25.5582, "stream_s": 25.52, "window_start_s": 22.5, "window_end_s": 25.5, "compute_ms": 34.12, "executor_total_ms": 34.35, "count": 1}`
- `{"event": "bird_detection", "wall_s": 25.5583, "stream_s": 25.52, "time": 25.5, "window_start": 22.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3446, "compute_ms": 34.12, "available_wall_s": 25.5583}`
- `{"event": "bird_window", "wall_s": 27.0383, "stream_s": 27.0, "window_start_s": 24.0, "window_end_s": 27.0, "compute_ms": 34.72, "executor_total_ms": 34.96, "count": 2}`
- `{"event": "bird_detection", "wall_s": 27.0385, "stream_s": 27.0, "time": 27.0, "window_start": 24.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.7035, "compute_ms": 34.72, "available_wall_s": 27.0385}`
- `{"event": "notify_queued", "wall_s": 27.0386, "stream_s": 27.0, "species": "House Sparrow", "confidence": 0.7035, "detected_wall_s": 27.0386, "window_end_s": 27.0, "busy": false, "speaking": false}`
- `{"event": "bird_detection", "wall_s": 27.0386, "stream_s": 27.0, "time": 27.0, "window_start": 24.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.3082, "compute_ms": 34.72, "available_wall_s": 27.0386}`
- `{"event": "notify_injected", "wall_s": 27.0389, "stream_s": 27.0, "species": "House Sparrow", "confidence": 0.7035, "detected_wall_s": 27.0386, "window_end_s": 27.0, "injected_wall_s": 27.0389, "detection_to_inject_ms": 0.3}`
- `{"event": "response_created", "wall_s": 27.0668, "stream_s": 27.04, "response_id": "de74_resp_9", "kind": "notify"}`
- `{"event": "realtime_error", "wall_s": 28.4454, "stream_s": 28.44, "error": {"type": "server_error", "code": "upstream_error", "message": "The response failed because an upstream service call did not complete.", "param": null, "event_id": "ee96b158-5f78-42e8-9c0f-1d670535c7f3"}}`
- `{"event": "response_done", "wall_s": 28.4456, "stream_s": 28.44, "response_id": "de74_resp_9", "kind": "notify", "status": "failed", "text": "", "audio_bytes": 0, "status_details": {"type": "failed", "error": {"code": "upstream_error", "message": "The response failed because an upstream service call did not complete."}}}`
- `{"event": "bird_window", "wall_s": 28.5578, "stream_s": 28.52, "window_start_s": 25.5, "window_end_s": 28.5, "compute_ms": 34.95, "executor_total_ms": 35.24, "count": 2}`
- `{"event": "bird_detection", "wall_s": 28.558, "stream_s": 28.52, "time": 28.5, "window_start": 25.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8329, "compute_ms": 34.95, "available_wall_s": 28.558}`
- `{"event": "bird_detection", "wall_s": 28.5581, "stream_s": 28.52, "time": 28.5, "window_start": 25.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4754, "compute_ms": 34.95, "available_wall_s": 28.5581}`
- `{"event": "bird_window", "wall_s": 30.0697, "stream_s": 30.04, "window_start_s": 27.0, "window_end_s": 30.0, "compute_ms": 64.72, "executor_total_ms": 64.95, "count": 2}`
- `{"event": "bird_detection", "wall_s": 30.0699, "stream_s": 30.04, "time": 30.0, "window_start": 27.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8534, "compute_ms": 64.72, "available_wall_s": 30.0699}`
- `{"event": "bird_detection", "wall_s": 30.07, "stream_s": 30.04, "time": 30.0, "window_start": 27.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.6188, "compute_ms": 64.72, "available_wall_s": 30.07}`
- `{"event": "vad", "wall_s": 30.3081, "stream_s": 30.28, "type": "input_audio_buffer.speech_started", "audio_time_s": 29.94, "bird_only_candidate": false}`
- `{"event": "bird_window", "wall_s": 31.5587, "stream_s": 31.52, "window_start_s": 28.5, "window_end_s": 31.5, "compute_ms": 35.79, "executor_total_ms": 36.03, "count": 1}`
- `{"event": "bird_detection", "wall_s": 31.5589, "stream_s": 31.52, "time": 31.5, "window_start": 28.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4672, "compute_ms": 35.79, "available_wall_s": 31.5589}`
- `{"event": "vad", "wall_s": 32.3902, "stream_s": 32.36, "type": "input_audio_buffer.speech_stopped", "audio_time_s": 32.951, "bird_only_candidate": false}`
- `{"event": "response_created", "wall_s": 32.3991, "stream_s": 32.36, "response_id": "de74_resp_10", "kind": "vad"}`
- `{"event": "user_transcript", "wall_s": 32.6596, "stream_s": 32.64, "text": "刚才那是什么在叫？", "item_id": "7a974675"}`
- `{"event": "bird_window", "wall_s": 33.039, "stream_s": 33.0, "window_start_s": 30.0, "window_end_s": 33.0, "compute_ms": 34.8, "executor_total_ms": 35.1, "count": 1}`
- `{"event": "bird_detection", "wall_s": 33.0392, "stream_s": 33.0, "time": 33.0, "window_start": 30.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2545, "compute_ms": 34.8, "available_wall_s": 33.0392}`
- `{"event": "first_audio", "wall_s": 34.2641, "stream_s": 34.24, "response_id": "de74_resp_10", "kind": "vad", "vad_stop_to_first_audio_ms": 1873.9, "tts_active_end_to_first_audio_ms": 2599.1}`
- `{"event": "bird_window", "wall_s": 34.5586, "stream_s": 34.52, "window_start_s": 31.5, "window_end_s": 34.5, "compute_ms": 35.04, "executor_total_ms": 35.33, "count": 0}`
- `{"event": "response_done", "wall_s": 34.6289, "stream_s": 34.6, "response_id": "de74_resp_10", "kind": "vad", "status": "completed", "text": "Let me check the recent sounds.", "audio_bytes": 101760, "status_details": null}`
- `{"event": "tool_call", "wall_s": 34.6291, "stream_s": 34.6, "name": "recent_sounds", "arguments": {"seconds": 10}, "result": {"stream_time": 34.6, "seconds": 10.0, "detections": [{"time": 25.5, "window_start": 22.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3446, "compute_ms": 34.12, "available_wall_s": 25.5583}, {"time": 27.0, "window_start": 24.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.7035, "compute_ms": 34.72, "available_wall_s": 27.0385}, {"time": 27.0, "window_start": 24.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.3082, "compute_ms": 34.72, "available_wall_s": 27.0386}, {"time": 28.5, "window_start": 25.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8329, "compute_ms": 34.95, "available_wall_s": 28.558}, {"time": 28.5, "window_start": 25.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4754, "compute_ms": 34.95, "available_wall_s": 28.5581}, {"time": 30.0, "window_start": 27.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8534, "compute_ms": 64.72, "available_wall_s": 30.0699}, {"time": 30.0, "window_start": 27.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.6188, "compute_ms": 64.72, "available_wall_s": 30.07}, {"time": 31.5, "window_start": 28.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4672, "compute_ms": 35.79, "available_wall_s": 31.5589}, {"time": 33.0, "window_start": 30.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2545, "compute_ms": 34.8, "available_wall_s": 33.0392}], "species": ["House Sparrow", "Northern Cardinal"], "source": "BirdNET, unconfirmed"}}`
- `{"event": "response_created", "wall_s": 34.6592, "stream_s": 34.64, "response_id": "de74_resp_13", "kind": "vad"}`
- `{"event": "first_audio", "wall_s": 35.5849, "stream_s": 35.56, "response_id": "de74_resp_13", "kind": "vad"}`
- `{"event": "bird_window", "wall_s": 36.0392, "stream_s": 36.0, "window_start_s": 33.0, "window_end_s": 36.0, "compute_ms": 34.91, "executor_total_ms": 35.2, "count": 1}`
- `{"event": "bird_detection", "wall_s": 36.0394, "stream_s": 36.0, "time": 36.0, "window_start": 33.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2675, "compute_ms": 34.91, "available_wall_s": 36.0394}`
- `{"event": "response_done", "wall_s": 36.1641, "stream_s": 36.16, "response_id": "de74_resp_13", "kind": "vad", "status": "completed", "text": "That was a House Sparrow, Passer domesticus, mixed with a Northern Cardinal.", "audio_bytes": 255360, "status_details": null}`
- `{"event": "bird_window", "wall_s": 37.5584, "stream_s": 37.52, "window_start_s": 34.5, "window_end_s": 37.5, "compute_ms": 34.67, "executor_total_ms": 34.97, "count": 0}`
- `{"event": "bird_window", "wall_s": 39.038, "stream_s": 39.0, "window_start_s": 36.0, "window_end_s": 39.0, "compute_ms": 34.44, "executor_total_ms": 34.69, "count": 0}`
- `{"event": "bird_window", "wall_s": 40.5773, "stream_s": 40.56, "window_start_s": 37.5, "window_end_s": 40.5, "compute_ms": 53.4, "executor_total_ms": 53.68, "count": 0}`
- `{"event": "bird_window", "wall_s": 42.0362, "stream_s": 42.0, "window_start_s": 39.0, "window_end_s": 42.0, "compute_ms": 34.54, "executor_total_ms": 34.75, "count": 1}`
- `{"event": "bird_detection", "wall_s": 42.0363, "stream_s": 42.0, "time": 42.0, "window_start": 39.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.2886, "compute_ms": 34.54, "available_wall_s": 42.0363}`
- `{"event": "vad", "wall_s": 42.1105, "stream_s": 42.08, "type": "input_audio_buffer.speech_started", "audio_time_s": 41.747, "bird_only_candidate": true}`
- `{"event": "vad", "wall_s": 42.7442, "stream_s": 42.72, "type": "input_audio_buffer.speech_stopped", "audio_time_s": 43.287, "bird_only_candidate": false}`
- `{"event": "response_created", "wall_s": 42.7531, "stream_s": 42.72, "response_id": "de74_resp_15", "kind": "vad"}`
- `{"event": "user_transcript", "wall_s": 42.9199, "stream_s": 42.88, "text": "Oh.", "item_id": "03d76b2b"}`
- `{"event": "first_audio", "wall_s": 43.3014, "stream_s": 43.28, "response_id": "de74_resp_15", "kind": "vad", "vad_stop_to_first_audio_ms": 557.2}`
- `{"event": "bird_window", "wall_s": 43.5589, "stream_s": 43.52, "window_start_s": 40.5, "window_end_s": 43.5, "compute_ms": 35.44, "executor_total_ms": 35.7, "count": 1}`
- `{"event": "bird_detection", "wall_s": 43.5591, "stream_s": 43.52, "time": 43.5, "window_start": 40.5, "species": "American Robin", "scientific_name": "Turdus migratorius", "confidence": 0.3796, "compute_ms": 35.44, "available_wall_s": 43.5591}`
- `{"event": "response_done", "wall_s": 43.8792, "stream_s": 43.84, "response_id": "de74_resp_15", "kind": "vad", "status": "completed", "text": "I'll pull up the current checklist to see what we've got so far.", "audio_bytes": 167040, "status_details": null}`
- `{"event": "tool_call", "wall_s": 43.8794, "stream_s": 43.84, "name": "get_checklist", "arguments": {}, "result": {"checklist": [{"species": "Northern Cardinal", "count": 2}], "species_count": 1}}`
- `{"event": "response_created", "wall_s": 43.9072, "stream_s": 43.88, "response_id": "de74_resp_18", "kind": "vad"}`
- `{"event": "first_audio", "wall_s": 44.2324, "stream_s": 44.2, "response_id": "de74_resp_18", "kind": "vad"}`
- `{"event": "response_done", "wall_s": 45.0197, "stream_s": 45.0, "response_id": "de74_resp_18", "kind": "vad", "status": "completed", "text": "We have one confirmed species so far: the Northern Cardinal, with two individuals.", "audio_bytes": 303360, "status_details": null}`
- `{"event": "bird_window", "wall_s": 45.0365, "stream_s": 45.0, "window_start_s": 42.0, "window_end_s": 45.0, "compute_ms": 34.35, "executor_total_ms": 34.58, "count": 0}`
- `{"event": "bird_window", "wall_s": 46.5579, "stream_s": 46.52, "window_start_s": 43.5, "window_end_s": 46.5, "compute_ms": 34.71, "executor_total_ms": 34.9, "count": 0}`
- `{"event": "bird_window", "wall_s": 48.0388, "stream_s": 48.0, "window_start_s": 45.0, "window_end_s": 48.0, "compute_ms": 34.83, "executor_total_ms": 35.08, "count": 0}`
- `{"event": "bird_window", "wall_s": 49.5808, "stream_s": 49.56, "window_start_s": 46.5, "window_end_s": 49.5, "compute_ms": 57.69, "executor_total_ms": 57.89, "count": 0}`
- `{"event": "vad", "wall_s": 50.3044, "stream_s": 50.28, "type": "input_audio_buffer.speech_started", "audio_time_s": 49.971, "bird_only_candidate": false}`
- `{"event": "bird_window", "wall_s": 51.0621, "stream_s": 51.04, "window_start_s": 48.0, "window_end_s": 51.0, "compute_ms": 58.2, "executor_total_ms": 58.48, "count": 0}`
- `{"event": "vad", "wall_s": 52.3858, "stream_s": 52.36, "type": "input_audio_buffer.speech_stopped", "audio_time_s": 52.951, "bird_only_candidate": false}`
- `{"event": "response_created", "wall_s": 52.3947, "stream_s": 52.36, "response_id": "de74_resp_20", "kind": "vad"}`
- `{"event": "bird_window", "wall_s": 52.5597, "stream_s": 52.52, "window_start_s": 49.5, "window_end_s": 52.5, "compute_ms": 36.24, "executor_total_ms": 36.47, "count": 0}`
- `{"event": "user_transcript", "wall_s": 52.6283, "stream_s": 52.6, "text": "How many species do we have?", "item_id": "2b3f30d7"}`
- `{"event": "first_audio", "wall_s": 52.8843, "stream_s": 52.88, "response_id": "de74_resp_20", "kind": "vad", "vad_stop_to_first_audio_ms": 498.5, "tts_active_end_to_first_audio_ms": 1144.3}`
- `{"event": "response_done", "wall_s": 53.3008, "stream_s": 53.28, "response_id": "de74_resp_20", "kind": "vad", "status": "completed", "text": "Let me check the recent sounds.", "audio_bytes": 97920, "status_details": null}`
- `{"event": "tool_call", "wall_s": 53.3009, "stream_s": 53.28, "name": "recent_sounds", "arguments": {"seconds": 60}, "result": {"stream_time": 53.28, "seconds": 60.0, "detections": [{"time": 3.0, "window_start": 0.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.6501, "compute_ms": 34.81, "available_wall_s": 3.0407}, {"time": 4.5, "window_start": 1.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8721, "compute_ms": 34.78, "available_wall_s": 4.5602}, {"time": 6.0, "window_start": 3.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.814, "compute_ms": 34.69, "available_wall_s": 6.0383}, {"time": 6.0, "window_start": 3.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3052, "compute_ms": 34.69, "available_wall_s": 6.0384}, {"time": 7.5, "window_start": 4.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8422, "compute_ms": 34.59, "available_wall_s": 7.5579}, {"time": 7.5, "window_start": 4.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4274, "compute_ms": 34.59, "available_wall_s": 7.558}, {"time": 12.0, "window_start": 9.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4233, "compute_ms": 34.86, "available_wall_s": 12.0378}, {"time": 13.5, "window_start": 10.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.7957, "compute_ms": 34.94, "available_wall_s": 13.5575}, {"time": 15.0, "window_start": 12.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8503, "compute_ms": 35.32, "available_wall_s": 15.0387}, {"time": 16.5, "window_start": 13.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.9145, "compute_ms": 34.14, "available_wall_s": 16.557}, {"time": 21.0, "window_start": 18.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3378, "compute_ms": 34.37, "available_wall_s": 21.0379}, {"time": 22.5, "window_start": 19.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2823, "compute_ms": 34.77, "available_wall_s": 22.5576}, {"time": 24.0, "window_start": 21.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3359, "compute_ms": 34.07, "available_wall_s": 24.0375}, {"time": 25.5, "window_start": 22.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3446, "compute_ms": 34.12, "available_wall_s": 25.5583}, {"time": 27.0, "window_start": 24.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.7035, "compute_ms": 34.72, "available_wall_s": 27.0385}, {"time": 27.0, "window_start": 24.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.3082, "compute_ms": 34.72, "available_wall_s": 27.0386}, {"time": 28.5, "window_start": 25.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8329, "compute_ms": 34.95, "available_wall_s": 28.558}, {"time": 28.5, "window_start": 25.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4754, "compute_ms": 34.95, "available_wall_s": 28.5581}, {"time": 30.0, "window_start": 27.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8534, "compute_ms": 64.72, "available_wall_s": 30.0699}, {"time": 30.0, "window_start": 27.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.6188, "compute_ms": 64.72, "available_wall_s": 30.07}, {"time": 31.5, "window_start": 28.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4672, "compute_ms": 35.79, "available_wall_s": 31.5589}, {"time": 33.0, "window_start": 30.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2545, "compute_ms": 34.8, "available_wall_s": 33.0392}, {"time": 36.0, "window_start": 33.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2675, "compute_ms": 34.91, "available_wall_s": 36.0394}, {"time": 42.0, "window_start": 39.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.2886, "compute_ms": 34.54, "available_wall_s": 42.0363}, {"time": 43.5, "window_start": 40.5, "species": "American Robin", "scientific_name": "Turdus migratorius", "confidence": 0.3796, "compute_ms": 35.44, "available_wall_s": 43.5591}], "species": ["American Robin", "House Sparrow", "Northern Cardinal"], "source": "BirdNET, unconfirmed"}}`
- `{"event": "response_created", "wall_s": 53.3294, "stream_s": 53.32, "response_id": "de74_resp_23", "kind": "vad"}`
- `{"event": "bird_window", "wall_s": 54.0376, "stream_s": 54.0, "window_start_s": 51.0, "window_end_s": 54.0, "compute_ms": 35.04, "executor_total_ms": 35.34, "count": 0}`
- `{"event": "first_audio", "wall_s": 54.3046, "stream_s": 54.28, "response_id": "de74_resp_23", "kind": "vad"}`
- `{"event": "response_done", "wall_s": 55.4625, "stream_s": 55.44, "response_id": "de74_resp_23", "kind": "vad", "status": "completed", "text": "We've got three species heard in the last 60 seconds, American Robin, House Sparrow, and Northern Cardinal, but only the Northern Cardinal is confirmed on the checklist, with two of them.", "audio_bytes": 593280, "status_details": null}`
- `{"event": "bird_window", "wall_s": 55.5571, "stream_s": 55.52, "window_start_s": 52.5, "window_end_s": 55.5, "compute_ms": 35.12, "executor_total_ms": 35.39, "count": 1}`
- `{"event": "bird_detection", "wall_s": 55.5573, "stream_s": 55.52, "time": 55.5, "window_start": 52.5, "species": "American Robin", "scientific_name": "Turdus migratorius", "confidence": 0.8145, "compute_ms": 35.12, "available_wall_s": 55.5573}`
- `{"event": "notify_queued", "wall_s": 55.5574, "stream_s": 55.52, "species": "American Robin", "confidence": 0.8145, "detected_wall_s": 55.5574, "window_end_s": 55.5, "busy": false, "speaking": false}`
- `{"event": "notify_injected", "wall_s": 55.5576, "stream_s": 55.52, "species": "American Robin", "confidence": 0.8145, "detected_wall_s": 55.5574, "window_end_s": 55.5, "injected_wall_s": 55.5576, "detection_to_inject_ms": 0.2}`
- `{"event": "response_created", "wall_s": 55.5927, "stream_s": 55.56, "response_id": "de74_resp_25", "kind": "notify"}`
- `{"event": "first_audio", "wall_s": 55.9649, "stream_s": 55.96, "response_id": "de74_resp_25", "kind": "notify", "detection_to_first_audio_ms": 407.5}`
- `{"event": "response_done", "wall_s": 56.3123, "stream_s": 56.28, "response_id": "de74_resp_25", "kind": "notify", "status": "completed", "text": "American Robin, heard.", "audio_bytes": 96000, "status_details": null}`
- `{"event": "bird_window", "wall_s": 57.0376, "stream_s": 57.0, "window_start_s": 54.0, "window_end_s": 57.0, "compute_ms": 34.38, "executor_total_ms": 34.59, "count": 1}`
- `{"event": "bird_detection", "wall_s": 57.0377, "stream_s": 57.0, "time": 57.0, "window_start": 54.0, "species": "American Robin", "scientific_name": "Turdus migratorius", "confidence": 0.6299, "compute_ms": 34.38, "available_wall_s": 57.0377}`
- `{"event": "bird_window", "wall_s": 58.5568, "stream_s": 58.52, "window_start_s": 55.5, "window_end_s": 58.5, "compute_ms": 34.28, "executor_total_ms": 34.58, "count": 0}`
- `{"event": "bird_window", "wall_s": 60.038, "stream_s": 60.0, "window_start_s": 57.0, "window_end_s": 60.0, "compute_ms": 34.7, "executor_total_ms": 34.99, "count": 0}`
- `{"event": "bird_window", "wall_s": 61.5574, "stream_s": 61.52, "window_start_s": 58.5, "window_end_s": 61.5, "compute_ms": 34.6, "executor_total_ms": 34.81, "count": 0}`
- `{"event": "bird_window", "wall_s": 63.0382, "stream_s": 63.0, "window_start_s": 60.0, "window_end_s": 63.0, "compute_ms": 34.59, "executor_total_ms": 34.76, "count": 0}`
- `{"event": "bird_window", "wall_s": 64.5585, "stream_s": 64.52, "window_start_s": 61.5, "window_end_s": 64.5, "compute_ms": 34.59, "executor_total_ms": 34.8, "count": 0}`
- `{"event": "bird_window", "wall_s": 66.0383, "stream_s": 66.0, "window_start_s": 63.0, "window_end_s": 66.0, "compute_ms": 34.77, "executor_total_ms": 35.06, "count": 0}`
- `{"event": "stream_complete", "wall_s": 66.0384, "stream_s": 66.0, "frames": 1650, "max_audio_queue": 1}`
- `{"event": "summary", "wall_s": 67.0704, "stream_s": 66.0, "failure": null, "bird_windows": 43, "bird_compute_ms": {"n": 43, "median": 34.78, "p95": 57.26, "max": 64.72}, "event_loop_lag_ms": {"n": 3245, "median": 0.64, "p95": 1.8, "max": 4.0}, "audio_send_lag_ms": {"n": 1650, "median": 1.5, "p95": 2.8, "max": 8.5}, "event_loop_stalls_over_50ms": 0, "vad_starts": 4, "bird_only_vad_candidates": 1, "unique_birdnet_species": ["American Robin", "House Sparrow", "Northern Cardinal"], "checklist": [{"species": "Northern Cardinal", "count": 2}], "notifications": [{"species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.0407, "window_end_s": 3.0, "injected_wall_s": 3.041, "first_audio_wall_s": 3.4271}, {"species": "House Sparrow", "confidence": 0.7035, "detected_wall_s": 27.0386, "window_end_s": 27.0, "injected_wall_s": 27.0389}, {"species": "American Robin", "confidence": 0.8145, "detected_wall_s": 55.5574, "window_end_s": 55.5, "injected_wall_s": 55.5576, "first_audio_wall_s": 55.9649}], "vad_stop_to_first_audio_ms": {"n": 4, "median": 544.75, "p95": 1676.39, "max": 1873.9}, "skipped_windows": 0, "pending_notifications": 0}`
- `{"event": "notify_injected", "wall_s": 0.0, "stream_s": 0.0, "species": "Test Bird", "confidence": 0.75, "detected_wall_s": 0, "window_end_s": 3, "injected_wall_s": 0.0, "detection_to_inject_ms": 0.0}`
- `{"event": "offline_checks", "result": "PASS", "checks": ["AST syntax", "busy response queues notify", "speech queues notify", "idle flush sends item+response", "reservation prevents double-inject", "recent_sounds excludes old/future", "fake checklist species count", "nominal window indexing across ring wrap"]}`

## Live evidence — 2026-10-23T20:48:16

- `{"event": "tts_ready", "start": 8, "end": 11.16, "text": "两只北美红雀在灌木丛里", "active_end": 10.919}`
- `{"event": "tts_ready", "start": 30, "end": 32.0, "text": "刚才那是什么在叫？", "active_end": 31.743}`
- `{"event": "tts_ready", "start": 50, "end": 52.04, "text": "how many species do we have?", "active_end": 51.772}`
- `{"event": "mixed_stream", "duration_s": 66, "background_gain": 0.25, "tts_gain": 0.85, "clipped_samples": 0, "background_segments": [{"species": "Northern Cardinal", "start": 0, "end": 40}, {"species": "American Robin", "start": 40, "end": 60}]}`
- `{"event": "birdnet_warmup", "compute_ms": 49.02}`
- `{"event": "session_ready", "acknowledgement": "session.created", "model": "higgs-realtime-1.5-preview", "turn_detection": {"type": "server_vad", "create_response": true, "interrupt_response": true, "min_speech_duration": 0.125, "prefix_padding_ms": 300, "silence_duration_ms": 600, "threshold": 0.5}, "note": "session.created accepted; server does not reliably emit session.updated; inspect returned settings"}`
- `{"event": "bird_window", "wall_s": 3.0372, "stream_s": 3.0, "window_start_s": 0.0, "window_end_s": 3.0, "compute_ms": 33.54, "executor_total_ms": 33.75, "count": 1}`
- `{"event": "bird_detection", "wall_s": 3.0374, "stream_s": 3.0, "time": 3.0, "window_start": 0.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.6501, "compute_ms": 33.54, "available_wall_s": 3.0374}`
- `{"event": "notify_queued", "wall_s": 3.0374, "stream_s": 3.0, "species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.0374, "window_end_s": 3.0, "busy": false, "speaking": false}`
- `{"event": "notify_injected", "wall_s": 3.0376, "stream_s": 3.0, "species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.0374, "window_end_s": 3.0, "injected_wall_s": 3.0376, "detection_to_inject_ms": 0.2}`
- `{"event": "response_created", "wall_s": 3.0664, "stream_s": 3.04, "response_id": "f0f0_resp_2", "kind": "notify"}`
- `{"event": "first_audio", "wall_s": 3.7093, "stream_s": 3.68, "response_id": "f0f0_resp_2", "kind": "notify", "detection_to_first_audio_ms": 671.9}`
- `{"event": "response_done", "wall_s": 3.8351, "stream_s": 3.8, "response_id": "f0f0_resp_2", "kind": "notify", "status": "completed", "text": "Heard a Northern Cardinal.", "audio_bytes": 86400, "status_details": null}`
- `{"event": "bird_window", "wall_s": 4.559, "stream_s": 4.52, "window_start_s": 1.5, "window_end_s": 4.5, "compute_ms": 34.89, "executor_total_ms": 35.75, "count": 1}`
- `{"event": "bird_detection", "wall_s": 4.5591, "stream_s": 4.52, "time": 4.5, "window_start": 1.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8733, "compute_ms": 34.89, "available_wall_s": 4.5591}`
- `{"event": "bird_window", "wall_s": 6.0376, "stream_s": 6.0, "window_start_s": 3.0, "window_end_s": 6.0, "compute_ms": 33.74, "executor_total_ms": 33.98, "count": 2}`
- `{"event": "bird_detection", "wall_s": 6.0377, "stream_s": 6.0, "time": 6.0, "window_start": 3.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.814, "compute_ms": 33.74, "available_wall_s": 6.0377}`
- `{"event": "bird_detection", "wall_s": 6.0378, "stream_s": 6.0, "time": 6.0, "window_start": 3.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3052, "compute_ms": 33.74, "available_wall_s": 6.0378}`
- `{"event": "bird_window", "wall_s": 7.5567, "stream_s": 7.52, "window_start_s": 4.5, "window_end_s": 7.5, "compute_ms": 33.66, "executor_total_ms": 33.89, "count": 2}`
- `{"event": "bird_detection", "wall_s": 7.5569, "stream_s": 7.52, "time": 7.5, "window_start": 4.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.866, "compute_ms": 33.66, "available_wall_s": 7.5569}`
- `{"event": "bird_detection", "wall_s": 7.5569, "stream_s": 7.52, "time": 7.5, "window_start": 4.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4787, "compute_ms": 33.66, "available_wall_s": 7.5569}`
- `{"event": "vad", "wall_s": 8.2682, "stream_s": 8.24, "type": "input_audio_buffer.speech_started", "audio_time_s": 7.924, "bird_only_candidate": false}`
- `{"event": "bird_window", "wall_s": 9.038, "stream_s": 9.0, "window_start_s": 6.0, "window_end_s": 9.0, "compute_ms": 34.96, "executor_total_ms": 35.2, "count": 0}`
- `{"event": "bird_window", "wall_s": 10.5578, "stream_s": 10.52, "window_start_s": 7.5, "window_end_s": 10.5, "compute_ms": 34.71, "executor_total_ms": 34.97, "count": 1}`
- `{"event": "bird_detection", "wall_s": 10.5579, "stream_s": 10.52, "time": 10.5, "window_start": 7.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3905, "compute_ms": 34.71, "available_wall_s": 10.5579}`
- `{"event": "vad", "wall_s": 11.749, "stream_s": 11.72, "type": "input_audio_buffer.speech_stopped", "audio_time_s": 12.28, "bird_only_candidate": false}`
- `{"event": "response_created", "wall_s": 11.7541, "stream_s": 11.72, "response_id": "f0f0_resp_4", "kind": "vad"}`
- `{"event": "bird_window", "wall_s": 12.0392, "stream_s": 12.0, "window_start_s": 9.0, "window_end_s": 12.0, "compute_ms": 35.47, "executor_total_ms": 35.8, "count": 1}`
- `{"event": "bird_detection", "wall_s": 12.0394, "stream_s": 12.0, "time": 12.0, "window_start": 9.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4697, "compute_ms": 35.47, "available_wall_s": 12.0394}`
- `{"event": "user_transcript", "wall_s": 12.0423, "stream_s": 12.04, "text": "两只北美红雀在灌木丛里。", "item_id": "3132097d"}`
- `{"event": "first_audio", "wall_s": 12.31, "stream_s": 12.28, "response_id": "f0f0_resp_4", "kind": "vad", "vad_stop_to_first_audio_ms": 561.0, "tts_active_end_to_first_audio_ms": 1391.0}`
- `{"event": "response_done", "wall_s": 12.9301, "stream_s": 12.92, "response_id": "f0f0_resp_4", "kind": "vad", "status": "completed", "text": "Adding those two Northern Cardinals to the checklist.", "audio_bytes": 159360, "status_details": null}`
- `{"event": "tool_call", "wall_s": 12.9302, "stream_s": 12.92, "name": "add_observation", "arguments": {"species": "Northern Cardinal", "count": 2}, "result": {"ok": true, "checklist": [{"species": "Northern Cardinal", "count": 2}]}}`
- `{"event": "response_created", "wall_s": 12.9541, "stream_s": 12.92, "response_id": "f0f0_resp_7", "kind": "vad"}`
- `{"event": "first_audio", "wall_s": 13.2668, "stream_s": 13.24, "response_id": "f0f0_resp_7", "kind": "vad"}`
- `{"event": "bird_window", "wall_s": 13.573, "stream_s": 13.56, "window_start_s": 10.5, "window_end_s": 13.5, "compute_ms": 50.52, "executor_total_ms": 50.85, "count": 1}`
- `{"event": "bird_detection", "wall_s": 13.5732, "stream_s": 13.56, "time": 13.5, "window_start": 10.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.7792, "compute_ms": 50.52, "available_wall_s": 13.5731}`
- `{"event": "response_done", "wall_s": 13.9446, "stream_s": 13.92, "response_id": "f0f0_resp_7", "kind": "vad", "status": "completed", "text": "Done, two Northern Cardinals are now on the checklist.", "audio_bytes": 197760, "status_details": null}`
- `{"event": "bird_window", "wall_s": 15.0388, "stream_s": 15.0, "window_start_s": 12.0, "window_end_s": 15.0, "compute_ms": 35.07, "executor_total_ms": 35.38, "count": 1}`
- `{"event": "bird_detection", "wall_s": 15.0389, "stream_s": 15.0, "time": 15.0, "window_start": 12.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8503, "compute_ms": 35.07, "available_wall_s": 15.0389}`
- `{"event": "bird_window", "wall_s": 16.5577, "stream_s": 16.52, "window_start_s": 13.5, "window_end_s": 16.5, "compute_ms": 34.69, "executor_total_ms": 34.95, "count": 1}`
- `{"event": "bird_detection", "wall_s": 16.5579, "stream_s": 16.52, "time": 16.5, "window_start": 13.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.9102, "compute_ms": 34.69, "available_wall_s": 16.5579}`
- `{"event": "bird_window", "wall_s": 18.0534, "stream_s": 18.04, "window_start_s": 15.0, "window_end_s": 18.0, "compute_ms": 50.27, "executor_total_ms": 50.54, "count": 0}`
- `{"event": "bird_window", "wall_s": 19.557, "stream_s": 19.52, "window_start_s": 16.5, "window_end_s": 19.5, "compute_ms": 34.39, "executor_total_ms": 34.64, "count": 0}`
- `{"event": "bird_window", "wall_s": 21.0374, "stream_s": 21.0, "window_start_s": 18.0, "window_end_s": 21.0, "compute_ms": 34.53, "executor_total_ms": 34.8, "count": 1}`
- `{"event": "bird_detection", "wall_s": 21.0376, "stream_s": 21.0, "time": 21.0, "window_start": 18.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3378, "compute_ms": 34.53, "available_wall_s": 21.0376}`
- `{"event": "bird_window", "wall_s": 22.5592, "stream_s": 22.52, "window_start_s": 19.5, "window_end_s": 22.5, "compute_ms": 34.96, "executor_total_ms": 35.23, "count": 0}`
- `{"event": "bird_window", "wall_s": 24.0371, "stream_s": 24.0, "window_start_s": 21.0, "window_end_s": 24.0, "compute_ms": 34.26, "executor_total_ms": 34.51, "count": 1}`
- `{"event": "bird_detection", "wall_s": 24.0372, "stream_s": 24.0, "time": 24.0, "window_start": 21.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3359, "compute_ms": 34.26, "available_wall_s": 24.0372}`
- `{"event": "bird_window", "wall_s": 25.5589, "stream_s": 25.52, "window_start_s": 22.5, "window_end_s": 25.5, "compute_ms": 34.93, "executor_total_ms": 35.19, "count": 1}`
- `{"event": "bird_detection", "wall_s": 25.559, "stream_s": 25.52, "time": 25.5, "window_start": 22.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3196, "compute_ms": 34.93, "available_wall_s": 25.559}`
- `{"event": "bird_window", "wall_s": 27.0537, "stream_s": 27.04, "window_start_s": 24.0, "window_end_s": 27.0, "compute_ms": 50.53, "executor_total_ms": 50.8, "count": 2}`
- `{"event": "bird_detection", "wall_s": 27.0539, "stream_s": 27.04, "time": 27.0, "window_start": 24.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.7035, "compute_ms": 50.53, "available_wall_s": 27.0539}`
- `{"event": "notify_queued", "wall_s": 27.054, "stream_s": 27.04, "species": "House Sparrow", "confidence": 0.7035, "detected_wall_s": 27.054, "window_end_s": 27.0, "busy": false, "speaking": false}`
- `{"event": "bird_detection", "wall_s": 27.054, "stream_s": 27.04, "time": 27.0, "window_start": 24.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.3082, "compute_ms": 50.53, "available_wall_s": 27.054}`
- `{"event": "notify_injected", "wall_s": 27.0543, "stream_s": 27.04, "species": "House Sparrow", "confidence": 0.7035, "detected_wall_s": 27.054, "window_end_s": 27.0, "injected_wall_s": 27.0543, "detection_to_inject_ms": 0.3}`
- `{"event": "response_created", "wall_s": 27.0867, "stream_s": 27.08, "response_id": "f0f0_resp_9", "kind": "notify"}`
- `{"event": "first_audio", "wall_s": 27.4116, "stream_s": 27.4, "response_id": "f0f0_resp_9", "kind": "notify", "detection_to_first_audio_ms": 357.6}`
- `{"event": "response_done", "wall_s": 27.6765, "stream_s": 27.64, "response_id": "f0f0_resp_9", "kind": "notify", "status": "completed", "text": "Heard a House Sparrow.", "audio_bytes": 71040, "status_details": null}`
- `{"event": "bird_window", "wall_s": 28.559, "stream_s": 28.52, "window_start_s": 25.5, "window_end_s": 28.5, "compute_ms": 35.81, "executor_total_ms": 36.08, "count": 2}`
- `{"event": "bird_detection", "wall_s": 28.5592, "stream_s": 28.52, "time": 28.5, "window_start": 25.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8129, "compute_ms": 35.81, "available_wall_s": 28.5592}`
- `{"event": "bird_detection", "wall_s": 28.5593, "stream_s": 28.52, "time": 28.5, "window_start": 25.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4617, "compute_ms": 35.81, "available_wall_s": 28.5593}`
- `{"event": "bird_window", "wall_s": 30.038, "stream_s": 30.0, "window_start_s": 27.0, "window_end_s": 30.0, "compute_ms": 35.37, "executor_total_ms": 35.61, "count": 2}`
- `{"event": "bird_detection", "wall_s": 30.0382, "stream_s": 30.0, "time": 30.0, "window_start": 27.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8534, "compute_ms": 35.37, "available_wall_s": 30.0382}`
- `{"event": "bird_detection", "wall_s": 30.0383, "stream_s": 30.0, "time": 30.0, "window_start": 27.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.6188, "compute_ms": 35.37, "available_wall_s": 30.0383}`
- `{"event": "vad", "wall_s": 30.3113, "stream_s": 30.28, "type": "input_audio_buffer.speech_started", "audio_time_s": 29.94, "bird_only_candidate": false}`
- `{"event": "bird_window", "wall_s": 31.5576, "stream_s": 31.52, "window_start_s": 28.5, "window_end_s": 31.5, "compute_ms": 35.0, "executor_total_ms": 35.29, "count": 1}`
- `{"event": "bird_detection", "wall_s": 31.5578, "stream_s": 31.52, "time": 31.5, "window_start": 28.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4773, "compute_ms": 35.0, "available_wall_s": 31.5578}`
- `{"event": "vad", "wall_s": 32.4694, "stream_s": 32.44, "type": "input_audio_buffer.speech_stopped", "audio_time_s": 33.015, "bird_only_candidate": false}`
- `{"event": "response_created", "wall_s": 32.4776, "stream_s": 32.44, "response_id": "f0f0_resp_11", "kind": "vad"}`
- `{"event": "user_transcript", "wall_s": 32.7393, "stream_s": 32.72, "text": "刚才那是什么在叫？", "item_id": "4f350720"}`
- `{"event": "first_audio", "wall_s": 33.015, "stream_s": 33.0, "response_id": "f0f0_resp_11", "kind": "vad", "vad_stop_to_first_audio_ms": 545.6, "tts_active_end_to_first_audio_ms": 1272.0}`
- `{"event": "bird_window", "wall_s": 33.0377, "stream_s": 33.0, "window_start_s": 30.0, "window_end_s": 33.0, "compute_ms": 35.13, "executor_total_ms": 35.39, "count": 0}`
- `{"event": "response_done", "wall_s": 33.3665, "stream_s": 33.36, "response_id": "f0f0_resp_11", "kind": "vad", "status": "completed", "text": "Let me check the recent sounds.", "audio_bytes": 94080, "status_details": null}`
- `{"event": "tool_call", "wall_s": 33.3666, "stream_s": 33.36, "name": "recent_sounds", "arguments": {"seconds": 10}, "result": {"stream_time": 33.36, "seconds": 10.0, "detections": [{"time": 24.0, "window_start": 21.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3359, "compute_ms": 34.26, "available_wall_s": 24.0372}, {"time": 25.5, "window_start": 22.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3196, "compute_ms": 34.93, "available_wall_s": 25.559}, {"time": 27.0, "window_start": 24.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.7035, "compute_ms": 50.53, "available_wall_s": 27.0539}, {"time": 27.0, "window_start": 24.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.3082, "compute_ms": 50.53, "available_wall_s": 27.054}, {"time": 28.5, "window_start": 25.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8129, "compute_ms": 35.81, "available_wall_s": 28.5592}, {"time": 28.5, "window_start": 25.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4617, "compute_ms": 35.81, "available_wall_s": 28.5593}, {"time": 30.0, "window_start": 27.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8534, "compute_ms": 35.37, "available_wall_s": 30.0382}, {"time": 30.0, "window_start": 27.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.6188, "compute_ms": 35.37, "available_wall_s": 30.0383}, {"time": 31.5, "window_start": 28.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4773, "compute_ms": 35.0, "available_wall_s": 31.5578}], "species": ["House Sparrow", "Northern Cardinal"], "source": "BirdNET, unconfirmed"}}`
- `{"event": "response_created", "wall_s": 33.3944, "stream_s": 33.36, "response_id": "f0f0_resp_14", "kind": "vad"}`
- `{"event": "first_audio", "wall_s": 34.2224, "stream_s": 34.2, "response_id": "f0f0_resp_14", "kind": "vad"}`
- `{"event": "bird_window", "wall_s": 34.5582, "stream_s": 34.52, "window_start_s": 31.5, "window_end_s": 34.5, "compute_ms": 34.93, "executor_total_ms": 35.25, "count": 0}`
- `{"event": "response_done", "wall_s": 34.8693, "stream_s": 34.84, "response_id": "f0f0_resp_14", "kind": "vad", "status": "completed", "text": "刚才那阵叫声是北美红雀和家麻雀。", "audio_bytes": 193920, "status_details": null}`
- `{"event": "bird_window", "wall_s": 36.0385, "stream_s": 36.0, "window_start_s": 33.0, "window_end_s": 36.0, "compute_ms": 34.78, "executor_total_ms": 35.03, "count": 1}`
- `{"event": "bird_detection", "wall_s": 36.0387, "stream_s": 36.0, "time": 36.0, "window_start": 33.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2675, "compute_ms": 34.78, "available_wall_s": 36.0387}`
- `{"event": "bird_window", "wall_s": 37.5575, "stream_s": 37.52, "window_start_s": 34.5, "window_end_s": 37.5, "compute_ms": 34.55, "executor_total_ms": 34.86, "count": 0}`
- `{"event": "bird_window", "wall_s": 39.0383, "stream_s": 39.0, "window_start_s": 36.0, "window_end_s": 39.0, "compute_ms": 34.77, "executor_total_ms": 35.02, "count": 0}`
- `{"event": "bird_window", "wall_s": 40.5832, "stream_s": 40.56, "window_start_s": 37.5, "window_end_s": 40.5, "compute_ms": 59.39, "executor_total_ms": 59.72, "count": 1}`
- `{"event": "bird_detection", "wall_s": 40.5834, "stream_s": 40.56, "time": 40.5, "window_start": 37.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.3061, "compute_ms": 59.39, "available_wall_s": 40.5834}`
- `{"event": "bird_window", "wall_s": 42.0386, "stream_s": 42.0, "window_start_s": 39.0, "window_end_s": 42.0, "compute_ms": 35.49, "executor_total_ms": 35.69, "count": 1}`
- `{"event": "bird_detection", "wall_s": 42.0387, "stream_s": 42.0, "time": 42.0, "window_start": 39.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.2886, "compute_ms": 35.49, "available_wall_s": 42.0387}`
- `{"event": "bird_window", "wall_s": 43.5743, "stream_s": 43.56, "window_start_s": 40.5, "window_end_s": 43.5, "compute_ms": 50.31, "executor_total_ms": 50.55, "count": 0}`
- `{"event": "bird_window", "wall_s": 45.0384, "stream_s": 45.0, "window_start_s": 42.0, "window_end_s": 45.0, "compute_ms": 34.54, "executor_total_ms": 34.75, "count": 0}`
- `{"event": "bird_window", "wall_s": 46.5569, "stream_s": 46.52, "window_start_s": 43.5, "window_end_s": 46.5, "compute_ms": 34.42, "executor_total_ms": 34.66, "count": 0}`
- `{"event": "bird_window", "wall_s": 48.0594, "stream_s": 48.04, "window_start_s": 45.0, "window_end_s": 48.0, "compute_ms": 57.24, "executor_total_ms": 57.47, "count": 0}`
- `{"event": "bird_window", "wall_s": 49.5581, "stream_s": 49.52, "window_start_s": 46.5, "window_end_s": 49.5, "compute_ms": 34.64, "executor_total_ms": 34.89, "count": 1}`
- `{"event": "bird_detection", "wall_s": 49.5583, "stream_s": 49.52, "time": 49.5, "window_start": 46.5, "species": "American Robin", "scientific_name": "Turdus migratorius", "confidence": 0.2804, "compute_ms": 34.64, "available_wall_s": 49.5583}`
- `{"event": "vad", "wall_s": 50.3108, "stream_s": 50.28, "type": "input_audio_buffer.speech_started", "audio_time_s": 49.971, "bird_only_candidate": false}`
- `{"event": "bird_window", "wall_s": 51.0386, "stream_s": 51.0, "window_start_s": 48.0, "window_end_s": 51.0, "compute_ms": 35.59, "executor_total_ms": 35.8, "count": 0}`
- `{"event": "vad", "wall_s": 52.4647, "stream_s": 52.44, "type": "input_audio_buffer.speech_stopped", "audio_time_s": 53.015, "bird_only_candidate": false}`
- `{"event": "response_created", "wall_s": 52.4726, "stream_s": 52.44, "response_id": "f0f0_resp_16", "kind": "vad"}`
- `{"event": "bird_window", "wall_s": 52.5584, "stream_s": 52.52, "window_start_s": 49.5, "window_end_s": 52.5, "compute_ms": 35.13, "executor_total_ms": 35.39, "count": 0}`
- `{"event": "user_transcript", "wall_s": 52.7197, "stream_s": 52.68, "text": "How many species do we have?", "item_id": "029f0835"}`
- `{"event": "first_audio", "wall_s": 52.9705, "stream_s": 52.96, "response_id": "f0f0_resp_16", "kind": "vad", "vad_stop_to_first_audio_ms": 505.7, "tts_active_end_to_first_audio_ms": 1198.4}`
- `{"event": "response_done", "wall_s": 53.4897, "stream_s": 53.48, "response_id": "f0f0_resp_16", "kind": "vad", "status": "completed", "text": "Pulling up the checklist and recent sounds now.", "audio_bytes": 149760, "status_details": null}`
- `{"event": "tool_call", "wall_s": 53.4899, "stream_s": 53.48, "name": "get_checklist", "arguments": {}, "result": {"checklist": [{"species": "Northern Cardinal", "count": 2}], "species_count": 1}}`
- `{"event": "tool_call", "wall_s": 53.4901, "stream_s": 53.48, "name": "recent_sounds", "arguments": {"seconds": 60}, "result": {"stream_time": 53.48, "seconds": 60.0, "detections": [{"time": 3.0, "window_start": 0.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.6501, "compute_ms": 33.54, "available_wall_s": 3.0374}, {"time": 4.5, "window_start": 1.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8733, "compute_ms": 34.89, "available_wall_s": 4.5591}, {"time": 6.0, "window_start": 3.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.814, "compute_ms": 33.74, "available_wall_s": 6.0377}, {"time": 6.0, "window_start": 3.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3052, "compute_ms": 33.74, "available_wall_s": 6.0378}, {"time": 7.5, "window_start": 4.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.866, "compute_ms": 33.66, "available_wall_s": 7.5569}, {"time": 7.5, "window_start": 4.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4787, "compute_ms": 33.66, "available_wall_s": 7.5569}, {"time": 10.5, "window_start": 7.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3905, "compute_ms": 34.71, "available_wall_s": 10.5579}, {"time": 12.0, "window_start": 9.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4697, "compute_ms": 35.47, "available_wall_s": 12.0394}, {"time": 13.5, "window_start": 10.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.7792, "compute_ms": 50.52, "available_wall_s": 13.5731}, {"time": 15.0, "window_start": 12.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8503, "compute_ms": 35.07, "available_wall_s": 15.0389}, {"time": 16.5, "window_start": 13.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.9102, "compute_ms": 34.69, "available_wall_s": 16.5579}, {"time": 21.0, "window_start": 18.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3378, "compute_ms": 34.53, "available_wall_s": 21.0376}, {"time": 24.0, "window_start": 21.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3359, "compute_ms": 34.26, "available_wall_s": 24.0372}, {"time": 25.5, "window_start": 22.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.3196, "compute_ms": 34.93, "available_wall_s": 25.559}, {"time": 27.0, "window_start": 24.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.7035, "compute_ms": 50.53, "available_wall_s": 27.0539}, {"time": 27.0, "window_start": 24.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.3082, "compute_ms": 50.53, "available_wall_s": 27.054}, {"time": 28.5, "window_start": 25.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8129, "compute_ms": 35.81, "available_wall_s": 28.5592}, {"time": 28.5, "window_start": 25.5, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.4617, "compute_ms": 35.81, "available_wall_s": 28.5593}, {"time": 30.0, "window_start": 27.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.8534, "compute_ms": 35.37, "available_wall_s": 30.0382}, {"time": 30.0, "window_start": 27.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.6188, "compute_ms": 35.37, "available_wall_s": 30.0383}, {"time": 31.5, "window_start": 28.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.4773, "compute_ms": 35.0, "available_wall_s": 31.5578}, {"time": 36.0, "window_start": 33.0, "species": "House Sparrow", "scientific_name": "Passer domesticus", "confidence": 0.2675, "compute_ms": 34.78, "available_wall_s": 36.0387}, {"time": 40.5, "window_start": 37.5, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.3061, "compute_ms": 59.39, "available_wall_s": 40.5834}, {"time": 42.0, "window_start": 39.0, "species": "Northern Cardinal", "scientific_name": "Cardinalis cardinalis", "confidence": 0.2886, "compute_ms": 35.49, "available_wall_s": 42.0387}, {"time": 49.5, "window_start": 46.5, "species": "American Robin", "scientific_name": "Turdus migratorius", "confidence": 0.2804, "compute_ms": 34.64, "available_wall_s": 49.5583}], "species": ["American Robin", "House Sparrow", "Northern Cardinal"], "source": "BirdNET, unconfirmed"}}`
- `{"event": "response_created", "wall_s": 53.5178, "stream_s": 53.48, "response_id": "f0f0_resp_21", "kind": "vad"}`
- `{"event": "first_audio", "wall_s": 53.9169, "stream_s": 53.88, "response_id": "f0f0_resp_21", "kind": "vad"}`
- `{"event": "bird_window", "wall_s": 54.0386, "stream_s": 54.0, "window_start_s": 51.0, "window_end_s": 54.0, "compute_ms": 35.04, "executor_total_ms": 35.35, "count": 0}`
- `{"event": "response_done", "wall_s": 55.3924, "stream_s": 55.36, "response_id": "f0f0_resp_21", "kind": "vad", "status": "completed", "text": "We have one confirmed species on the checklist, the Northern Cardinal. From the last 60 seconds of sounds, we've heard three distinct species: the Northern Cardinal, the House Sparrow, and the American Robin.", "audio_bytes": 700800, "status_details": null}`
- `{"event": "bird_window", "wall_s": 55.5579, "stream_s": 55.52, "window_start_s": 52.5, "window_end_s": 55.5, "compute_ms": 34.79, "executor_total_ms": 35.08, "count": 1}`
- `{"event": "bird_detection", "wall_s": 55.5581, "stream_s": 55.52, "time": 55.5, "window_start": 52.5, "species": "American Robin", "scientific_name": "Turdus migratorius", "confidence": 0.8441, "compute_ms": 34.79, "available_wall_s": 55.5581}`
- `{"event": "notify_queued", "wall_s": 55.5581, "stream_s": 55.52, "species": "American Robin", "confidence": 0.8441, "detected_wall_s": 55.5581, "window_end_s": 55.5, "busy": false, "speaking": false}`
- `{"event": "notify_injected", "wall_s": 55.5584, "stream_s": 55.52, "species": "American Robin", "confidence": 0.8441, "detected_wall_s": 55.5581, "window_end_s": 55.5, "injected_wall_s": 55.5584, "detection_to_inject_ms": 0.3}`
- `{"event": "response_created", "wall_s": 55.5893, "stream_s": 55.56, "response_id": "f0f0_resp_23", "kind": "notify"}`
- `{"event": "first_audio", "wall_s": 55.9683, "stream_s": 55.96, "response_id": "f0f0_resp_23", "kind": "notify", "detection_to_first_audio_ms": 410.2}`
- `{"event": "response_done", "wall_s": 56.2875, "stream_s": 56.28, "response_id": "f0f0_resp_23", "kind": "notify", "status": "completed", "text": "Heard an American Robin.", "audio_bytes": 90240, "status_details": null}`
- `{"event": "bird_window", "wall_s": 57.0372, "stream_s": 57.0, "window_start_s": 54.0, "window_end_s": 57.0, "compute_ms": 34.46, "executor_total_ms": 34.78, "count": 1}`
- `{"event": "bird_detection", "wall_s": 57.0374, "stream_s": 57.0, "time": 57.0, "window_start": 54.0, "species": "American Robin", "scientific_name": "Turdus migratorius", "confidence": 0.6299, "compute_ms": 34.46, "available_wall_s": 57.0374}`
- `{"event": "bird_window", "wall_s": 58.5573, "stream_s": 58.52, "window_start_s": 55.5, "window_end_s": 58.5, "compute_ms": 34.61, "executor_total_ms": 34.8, "count": 0}`
- `{"event": "bird_window", "wall_s": 60.0366, "stream_s": 60.0, "window_start_s": 57.0, "window_end_s": 60.0, "compute_ms": 34.83, "executor_total_ms": 35.08, "count": 0}`
- `{"event": "bird_window", "wall_s": 61.5577, "stream_s": 61.52, "window_start_s": 58.5, "window_end_s": 61.5, "compute_ms": 34.83, "executor_total_ms": 35.07, "count": 0}`
- `{"event": "bird_window", "wall_s": 63.0369, "stream_s": 63.0, "window_start_s": 60.0, "window_end_s": 63.0, "compute_ms": 34.69, "executor_total_ms": 34.93, "count": 0}`
- `{"event": "bird_window", "wall_s": 64.5581, "stream_s": 64.52, "window_start_s": 61.5, "window_end_s": 64.5, "compute_ms": 35.3, "executor_total_ms": 35.54, "count": 0}`
- `{"event": "bird_window", "wall_s": 66.0631, "stream_s": 66.0, "window_start_s": 63.0, "window_end_s": 66.0, "compute_ms": 59.91, "executor_total_ms": 60.22, "count": 0}`
- `{"event": "stream_complete", "wall_s": 66.0633, "stream_s": 66.0, "frames": 1650, "max_audio_queue": 1}`
- `{"event": "summary", "wall_s": 67.0904, "stream_s": 66.0, "failure": null, "bird_windows": 43, "bird_compute_ms": {"n": 43, "median": 34.93, "p95": 56.57, "max": 59.91}, "event_loop_lag_ms": {"n": 3244, "median": 0.65, "p95": 1.79, "max": 2.89}, "audio_send_lag_ms": {"n": 1650, "median": 1.6, "p95": 2.8, "max": 6.9}, "event_loop_stalls_over_50ms": 0, "vad_starts": 3, "bird_only_vad_candidates": 0, "unique_birdnet_species": ["American Robin", "House Sparrow", "Northern Cardinal"], "checklist": [{"species": "Northern Cardinal", "count": 2}], "notifications": [{"species": "Northern Cardinal", "confidence": 0.6501, "detected_wall_s": 3.0374, "window_end_s": 3.0, "injected_wall_s": 3.0376, "first_audio_wall_s": 3.7093}, {"species": "House Sparrow", "confidence": 0.7035, "detected_wall_s": 27.054, "window_end_s": 27.0, "injected_wall_s": 27.0543, "first_audio_wall_s": 27.4116}, {"species": "American Robin", "confidence": 0.8441, "detected_wall_s": 55.5581, "window_end_s": 55.5, "injected_wall_s": 55.5584, "first_audio_wall_s": 55.9683}], "vad_stop_to_first_audio_ms": {"n": 3, "median": 545.6, "p95": 559.46, "max": 561.0}, "skipped_windows": 0, "pending_notifications": 0}`
