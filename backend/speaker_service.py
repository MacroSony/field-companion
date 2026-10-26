#!/usr/bin/env python3
"""Loopback-only CPU ECAPA service. Separate venv/process; one resident model.

Standard-library threaded HTTP keeps /health responsive during locked inference.
No identity decisions or thresholds: scores are diagnostic cosine similarities.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL = "speechbrain/spkrec-ecapa-voxceleb"
MAX_BYTES = 16000 * 2 * 60


def profile_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("profile must be 1–64 letters, digits, underscores or hyphens")
    return value


def unit(vector):
    vector = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(vector)
    if not np.isfinite(vector).all() or norm < 1e-12:
        raise ValueError("invalid voice embedding")
    return vector / norm


class SpeakerEngine:
    def __init__(self, directory, model_dir=None, threads=4, embed=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        if embed is None:
            os.environ.setdefault("HF_HOME", str(ROOT / "out" / "hf-cache"))
            import torch
            from speechbrain.inference.speaker import EncoderClassifier
            from speechbrain.utils.fetching import LocalStrategy
            torch.set_num_threads(threads)
            model = EncoderClassifier.from_hparams(source=MODEL,
                savedir=str(model_dir or ROOT / "out" / "speaker-model"), run_opts={"device": "cpu"},
                local_strategy=LocalStrategy.COPY)
            model.eval()

            def embed(samples):
                with torch.inference_mode():
                    return model.encode_batch(torch.from_numpy(samples).unsqueeze(0)).reshape(-1).cpu().numpy()
        self.embed = embed
        self.profiles = {}
        for path in self.directory.glob("*.json"):
            profile_name(path.stem)
            row = json.loads(path.read_text())
            unit(row["sum"])  # Fail startup clearly rather than use a corrupt profile.
            os.chmod(path, 0o600)
            self.profiles[path.stem] = row

    def process(self, action, pcm, profile=None):
        started = time.perf_counter()
        if len(pcm) % 2 or len(pcm) > MAX_BYTES:
            raise ValueError("expected at most 60 s of little-endian PCM16 mono at 16000 Hz")
        duration = len(pcm) / 32000
        if action == "enroll":
            profile = profile_name(profile)
        if duration < .3:
            return {"ok": False, "error": "audio_too_short", "minimum_duration_s": .3,
                    "scores": {}, "duration_s": duration, "ms": 0.0}
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        with self.lock:
            if action == "enroll":
                # Mean of unit embeddings from <=5 s chunks, then normalize again.
                vectors = [unit(self.embed(samples[i:i + 80000]))
                           for i in range(0, len(samples), 80000) if len(samples[i:i + 80000]) >= 4800]
                old = self.profiles.get(profile)
                total = np.sum(vectors, axis=0) + (np.asarray(old["sum"]) if old else 0)
                row = {"sum": total.tolist(), "embedding": unit(total).tolist(),
                       "chunks": len(vectors) + (old["chunks"] if old else 0)}
                fd, name = tempfile.mkstemp(dir=self.directory, prefix=".enroll-")
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(row, stream)
                    os.replace(name, self.directory / (profile + ".json"))
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
                self.profiles[profile] = row
                result = {"profile": profile, "chunks": row["chunks"]}
            else:
                vector = unit(self.embed(samples))
                result = {"scores": {name: float(np.clip(np.dot(vector, row["embedding"]), -1, 1))
                                     for name, row in self.profiles.items()}}
        return {"ok": True, **result, "duration_s": duration,
                "ms": round((time.perf_counter() - started) * 1000, 2)}

    def reset(self, profile):
        profile = profile_name(profile)
        with self.lock:
            (self.directory / (profile + ".json")).unlink(missing_ok=True)
            self.profiles.pop(profile, None)
        return {"ok": True, "profile": profile}


def handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log raw audio or request bodies.

        def reply(self, status, data):
            body = json.dumps(data, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                return self.reply(404, {"error": "not_found"})
            self.reply(200, {"loaded": True, "model": MODEL})

        def do_POST(self):
            self.connection.settimeout(5)
            path = urlsplit(self.path)
            if path.path not in ("/enroll", "/score", "/reset"):
                return self.reply(404, {"error": "not_found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > MAX_BYTES * 4 // 3 + 1024:
                    return self.reply(413, {"error": "audio_too_large"})
                body = self.rfile.read(size)
                profile = parse_qs(path.query).get("profile", [None])[0]
                if self.headers.get("Content-Type", "").split(";")[0] == "application/json":
                    data = json.loads(body)
                    profile = data.get("profile", profile)
                    pcm = base64.b64decode(data.get("pcm16", ""), validate=True)
                else:
                    pcm = body
                result = (engine.reset(profile) if path.path == "/reset" else
                          engine.process(path.path[1:], pcm, profile))
                self.reply(200, result)
            except (ValueError, TypeError, AttributeError) as exc:
                self.reply(422, {"error": str(exc)})
            except Exception:
                self.reply(500, {"error": "speaker_processing_failed"})

        do_DELETE = do_POST  # DELETE /reset?profile=owner (or JSON body).
    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8796)
    parser.add_argument("--speakers-dir", type=Path, default=ROOT / "speakers")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    engine = SpeakerEngine(args.speakers_dir, args.model_dir, args.threads)
    http = ThreadingHTTPServer(("127.0.0.1", args.port), handler(engine))
    print(f"Speaker model ready: http://127.0.0.1:{http.server_port}", flush=True)
    http.serve_forever()
