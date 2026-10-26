"""Local diagnostic JSONL and opt-in input-turn WAVs; no network operations."""
import asyncio
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import uuid
import wave

import numpy as np

LOG = logging.getLogger("field-companion")
_APPEND_LOCK = threading.Lock()


def wall_time():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def redact(value):
    """Defense in depth for diagnostics, not a reason to log request headers."""
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if (k == "speaker_scores" and isinstance(v, dict)
                    and all(isinstance(score, (int, float)) for score in v.values())):
                # These keys are profile names, not credential field names (e.g.
                # a profile named "monkey"). Only validated numeric scores qualify.
                result[k] = {name: redact(score) for name, score in v.items()}
            else:
                result[k] = ("[redacted]" if re.search(
                    r"key|token|secret|password|authorization|headers|cookie", str(k), re.I)
                    else redact(v))
        return result
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        for name in ("BOSON_API_KEY", "OPENAI_API_KEY"):
            secret = os.environ.get(name)
            if secret:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(authorization\s*[:=]\s*)[^\r\n]+", r"\1[redacted]", value)
        value = re.sub(r"(?i)bearer\s+[^\s\"',}]+|\bsk-[\w-]+", "[redacted]", value)
        return re.sub(r"(?i)((?:[\w-]*(?:api[_-]?key|token|secret|password)|authorization)"
                      r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", r"\1[redacted]", value)
    return value


def append_jsonl(path, record):
    """Append immediately (no userspace buffering); a disk failure cannot stop speech."""
    try:
        line = json.dumps(redact(record), ensure_ascii=False, allow_nan=False) + "\n"
        with _APPEND_LOCK:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(line)
        return True
    except (OSError, ValueError, TypeError):
        LOG.warning("Local diagnostic JSONL write failed")
        return False


def extract_turn_audio(ring, position, start, stop, *, sample_rate=48000, padding=.3):
    """Copy a padded interval from a float mic ring using absolute sample indices.

    Returns (mono float samples, bounds). Bounds expose unavailable/overwritten
    padding or speech; never substitute zeroes or wrap into unrelated old audio.
    This is also the reusable extraction point for future speaker embeddings.
    """
    pad = round(padding * sample_rate)
    requested_start, requested_end = start - pad, stop + pad
    first = min(position, max(0, position - len(ring), requested_start))
    end = min(position, max(first, requested_end))
    samples = ring[np.arange(first, end) % len(ring)].copy()
    return samples, {"start_sample": first, "end_sample": end,
                     "speech_start_sample": start, "speech_end_sample": stop,
                     "requested_start_sample": requested_start,
                     "requested_end_sample": requested_end,
                     "clipped": first != requested_start or end != requested_end}


def _dbfs(samples, peak=False):
    if not len(samples):
        return None
    value = float(np.max(np.abs(samples))) if peak else float(np.sqrt(np.mean(samples.astype(float) ** 2)))
    return round(20 * math.log10(max(value, 1e-6)), 2)


PRE_PAD_S = 1.0   # speech_started receipt lags the real onset (VAD + network)
POST_PAD_S = .3   # speech_stopped already includes the VAD silence hangover


def frame_dbfs(samples, sample_rate, frame_s=.02):
    """20 ms frame RMS levels in dBFS (empty array if too short)."""
    n = int(frame_s * sample_rate)
    if n <= 0 or len(samples) < n:
        return np.empty(0)
    frames = samples[: len(samples) // n * n].reshape(-1, n)
    return 20 * np.log10(np.maximum(np.sqrt(np.mean(frames ** 2, axis=1)), 1e-6))


class TurnRecorder:
    """One final JSONL row per VAD occurrence, even with no transcript or route.

    Save audio as soon as POST_PAD_S of post-padding arrives (500 ms wall timeout).
    Metadata waits for route completion, at most 60 s, or flush on disconnect,
    reset/close. An optional audio callback supports shadow scores without WAVs;
    only the JSONL row waits for its bounded background completion.
    Unique occurrence IDs, not provider IDs, own queued route results.
    """
    def __init__(self, directory, session_id, extract, sample_rate=48000, on_audio=None):
        self.directory = Path(directory).expanduser() if directory is not None else None
        self.on_audio = on_audio
        self.session_id = session_id
        self.extract = extract
        self.sample_rate = sample_rate
        self.pending = {}
        self.position = 0

    def start(self, item_id, position, at, mode):
        self.position = position
        turn_id = uuid.uuid4().hex
        row = {"turn_id": turn_id, "item_id": item_id, "connection": self.session_id,
               "wall_time": wall_time(), "vad_started_monotonic": at,
               "alignment": "server_vad_receipt_mic_position", "audio_mode": mode,
               "transcript": "", "outcome": "no_route", "reason": "awaiting_transcript",
               "routed_tool": None, "routed_arguments": None, "assistant_audio": False,
               "wav_filename": None, "duration_s": None,
               "speech_rms_dbfs": None, "peak_dbfs": None, "noise_floor_dbfs": None}
        state = {"row": row, "start": position, "stop": None, "audio_done": False,
                 "directory": self.directory,
                 "settled": False, "audio_timer": None, "chunks": [],
                 "cursor": position - round(PRE_PAD_S * self.sample_rate), "clipped": False,
                 "final_received": False}
        self.pending[turn_id] = state
        self._collect(state, position)
        state["expiry"] = asyncio.get_running_loop().call_later(60, self.finish, turn_id, "timeout")
        return turn_id

    def find(self, item_id, *, final=False):
        """Recorder-only fallback after the safety VAD provenance has expired."""
        return next((turn_id for turn_id, state in self.pending.items()
                     if state["row"]["item_id"] == item_id and not state["final_received"]
                     and (final or state["stop"] is None)), None)

    def input_final(self, turn_id, transcript, position, at):
        state = self.pending.get(turn_id)
        if state:
            state["final_received"] = True
            state["row"]["transcript"] = transcript
            if state["stop"] is None:
                self.stop(turn_id, position, at)
                state["row"]["missing_speech_stop"] = True

    def update(self, turn_id, **fields):
        if turn_id in self.pending:
            self.pending[turn_id]["row"].update(fields)

    def stop(self, turn_id, position, at):
        state = self.pending.get(turn_id)
        if not state or state["stop"] is not None:
            return
        self.position = position
        state["stop"] = max(state["start"], position)
        state["expiry"].cancel()
        state["expiry"] = asyncio.get_running_loop().call_later(60, self.finish, turn_id, "timeout")
        state["row"]["vad_stopped_monotonic"] = at
        state["audio_timer"] = asyncio.get_running_loop().call_later(.5, self.capture, turn_id)

    def _collect(self, state, position):
        """Copy new mic-ring slices before rollover, including during long speech."""
        end = position if state["stop"] is None else min(position, state["stop"] + round(POST_PAD_S * self.sample_rate))
        if end <= state["cursor"]:
            return
        samples, bounds = self.extract(state["cursor"], end, padding=0)
        state["clipped"] |= bounds["clipped"]
        if len(samples):
            state["chunks"].append((bounds["start_sample"], samples))
        state["cursor"] = end

    def poll_audio(self, position):
        self.position = position
        for turn_id, state in list(self.pending.items()):
            if state["audio_done"]:
                continue
            self._collect(state, position)
            if state["stop"] is not None and position >= state["stop"] + round(POST_PAD_S * self.sample_rate):
                self.capture(turn_id)

    def capture(self, turn_id):
        state = self.pending.get(turn_id)
        if not state or state["audio_done"] or state["stop"] is None:
            return
        if state["audio_timer"]:
            state["audio_timer"].cancel()
        row = state["row"]
        self._collect(state, self.position)
        chunks = state["chunks"]
        samples = np.concatenate([part for _, part in chunks]) if chunks else np.empty(0, dtype=np.float32)
        indices = np.concatenate([np.arange(start, start + len(part)) for start, part in chunks]) if chunks else np.empty(0)
        pre, post = round(PRE_PAD_S * self.sample_rate), round(POST_PAD_S * self.sample_rate)
        row.update(start_sample=int(indices[0]) if len(indices) else state["start"],
                   end_sample=int(indices[-1]) + 1 if len(indices) else state["start"],
                   speech_start_sample=state["start"], speech_end_sample=state["stop"],
                   requested_start_sample=state["start"] - pre, requested_end_sample=state["stop"] + post,
                   clipped=state["clipped"] or state["cursor"] < state["stop"] + post)
        row["duration_s"] = len(samples) / self.sample_rate
        row["speech_duration_s"] = (state["stop"] - state["start"]) / self.sample_rate
        speech = (indices >= state["start"]) & (indices < state["stop"])
        row["speech_rms_dbfs"] = _dbfs(samples[speech])
        row["peak_dbfs"] = _dbfs(samples[speech], peak=True)
        # VAD markers are late at the start, so the padding may hold speech onset:
        # use frame-level percentiles over the whole clip for robust level features.
        levels = frame_dbfs(samples, self.sample_rate)
        row["noise_floor_dbfs"] = round(float(np.percentile(levels, 10)), 2) if len(levels) else None
        row["speech_p90_dbfs"] = round(float(np.percentile(levels, 90)), 2) if len(levels) else None
        if self.on_audio:
            state["speaker_pending"] = True
            self.on_audio(turn_id, samples)
        if state['directory'] is not None:
            self._save_wav(row, samples, directory=state['directory'])
        state["audio_done"] = True
        state["chunks"].clear()
        self._write_if_ready(turn_id)

    def _save_wav(self, row, samples, *, directory=None):
        directory = directory if directory is not None else self.directory
        filename = row["turn_id"] + ".wav"
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream, wave.open(stream, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
                wav.writeframes(np.clip(np.rint(samples * 32768), -32768, 32767).astype("<i2").tobytes())
            row["wav_filename"] = filename
        except OSError:
            row["recording_error"] = "wav_write_failed"
            LOG.warning("Local turn WAV write failed")

    def speaker_done(self, turn_id, **fields):
        state = self.pending.get(turn_id)
        if state:
            state["row"].update(fields)
            state["speaker_pending"] = False
            self._write_if_ready(turn_id)

    def settle(self, turn_id, **fields):
        state = self.pending.get(turn_id)
        if state:
            state["row"].update(fields)
            state["settled"] = True
            self._write_if_ready(turn_id)

    def _write_if_ready(self, turn_id):
        state = self.pending.get(turn_id)
        if state and state["audio_done"] and state["settled"] and not state.get("speaker_pending"):
            if state['directory'] is not None:
                append_jsonl(state['directory'] / "turns.jsonl", state["row"])
            state["expiry"].cancel()
            self.pending.pop(turn_id)

    def finish(self, turn_id, reason, position=None):
        state = self.pending.get(turn_id)
        if not state:
            return
        if state["stop"] is None:
            state["stop"] = max(state["start"], position if position is not None else self.position)
            state["row"]["missing_speech_stop"] = True
        if state["row"]["outcome"] == "no_route":
            state["row"]["reason"] = reason
        state["settled"] = True
        self.capture(turn_id)
        self._write_if_ready(turn_id)

    def flush(self, reason, position):
        for turn_id in list(self.pending):
            self.finish(turn_id, reason, position)
