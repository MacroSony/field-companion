#!/usr/bin/env python3
"""Local CPU BioCLIP 2 JSON service; run in the pybioclip venv, one worker."""
import argparse
import base64
import binascii
from contextlib import asynccontextmanager
from functools import lru_cache
import io
import math
import os
from pathlib import Path
import threading
import time
import warnings
from typing import Literal
from vision_policy import GROUPS, assess_visual, iconic_group

# Keep downloaded models in the ignored workspace cache on every platform.
# An explicitly configured Hugging Face cache still takes precedence.
os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parents[1] / "out" / "hf-cache"))

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

MAX_JPEG = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000


class FrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jpeg_b64: str = Field(min_length=4, max_length=4 * ((MAX_JPEG + 2) // 3))
    crop: tuple[float, float, float, float] | None = None
    candidates: list[str] | None = Field(default=None, max_length=512)
    target_group: Literal['Aves', 'Insecta', 'Plantae', 'Mammalia', 'Fungi', 'Reptilia',
                          'Amphibia', 'Arachnida', 'Mollusca', 'Actinopterygii'] | None = None
    k: int = Field(default=5, ge=1, le=512)

    @field_validator("crop")
    @classmethod
    def valid_crop(cls, value):
        if value is not None:
            x, y, w, h = value
            if (not all(math.isfinite(v) for v in value) or x < 0 or y < 0
                    or w <= 0 or h <= 0 or x + w > 1 + 1e-9 or y + h > 1 + 1e-9):
                raise ValueError("crop must be normalized [x,y,w,h] inside the image")
        return value

    @field_validator("candidates")
    @classmethod
    def valid_candidates(cls, value):
        if value is not None:
            if any(not name.strip() or len(name) > 200 for name in value):
                raise ValueError("candidate names must be nonempty, at most 200 characters")
            return list(dict.fromkeys(" ".join(name.split()) for name in value))
        return value


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frames: list[FrameRequest] = Field(min_length=1, max_length=8)


def prepare_image(request):
    """EXIF-oriented coordinates; pad each side 15%, square in pixel space."""
    try:
        raw = base64.b64decode(request.jpeg_b64, validate=True)
        if len(raw) > MAX_JPEG:
            raise ValueError("JPEG exceeds 8 MiB")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != "JPEG":
                    raise ValueError("jpeg_b64 must contain a JPEG, not another image format")
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError("image exceeds 20 megapixels")
                image = ImageOps.exif_transpose(source).convert("RGB")
    except (ValueError, binascii.Error, OSError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(422, f"Invalid JPEG: {exc}") from exc
    width, height = image.size
    crop_used = [0.0, 0.0, 1.0, 1.0]
    if request.crop is not None:
        x, y, w, h = request.crop
        cx, cy = (x + w / 2) * width, (y + h / 2) * height
        side = max(w * width, h * height) * 1.30
        # Shift the square inside the image where possible; clip on short axes.
        cw, ch = min(side, width), min(side, height)
        left = max(0, min(cx - cw / 2, width - cw))
        top = max(0, min(cy - ch / 2, height - ch))
        x0 = max(0, min(width - 1, math.floor(left)))
        y0 = max(0, min(height - 1, math.floor(top)))
        box = (x0, y0, min(width, max(x0 + 1, math.ceil(left + cw))),
               min(height, max(y0 + 1, math.ceil(top + ch))))
        image = image.crop(box)
        crop_used = [box[0] / width, box[1] / height,
                     (box[2] - box[0]) / width, (box[3] - box[1]) / height]
    # Measure before the encoder's 224x224 resize, on the actual inference ROI.
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    padded = np.pad(gray, 1, mode="edge")
    lap = (padded[:-2, 1:-1] + padded[2:, 1:-1] + padded[1:-1, :-2]
           + padded[1:-1, 2:] - 4 * gray)
    return image, crop_used, float(lap.var())


class VisionEngine:
    def __init__(self, threads=4):
        import torch
        from bioclip import TreeOfLifeClassifier
        self.torch = torch
        self.lock = threading.Lock()
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        started = time.perf_counter()
        # The library unconditionally compiles. Use eager CPU, as in the probe.
        compile_original = torch.compile
        try:
            torch.compile = lambda model, *args, **kwargs: model
            self.tree = TreeOfLifeClassifier(model_str="hf-hub:imageomics/bioclip-2", device="cpu")
        finally:
            torch.compile = compile_original
        self.tree.model.eval()
        # pybioclip 2.1.6: txt_embeddings is [embedding_dim, taxa], NOT [taxa, dim].
        # txt_names[i] = [taxonomy, common_name]; taxonomy ends in genus, epithet.
        self.names = [" ".join(row[0][-2:]) for row in self.tree.txt_names]
        self.groups = [iconic_group(row[0]) for row in self.tree.txt_names]
        self.group_indices = {group: torch.tensor([i for i,g in enumerate(self.groups) if g == group],
                              dtype=torch.long) for group in GROUPS}
        self.name_indices = {}
        for index, name in enumerate(self.names):
            self.name_indices.setdefault(name, []).append(index)
        if self.tree.txt_embeddings.shape[1] != len(self.names):
            raise RuntimeError("Unsupported pybioclip embedding layout")
        self.load_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        with torch.inference_mode():
            features = self.tree.create_image_features([Image.new("RGB", (224, 224))])
            self.tree.create_probabilities(features, self.tree.txt_embeddings)
        self.warm_ms = (time.perf_counter() - started) * 1000
        print(f"BioCLIP 2 ready: {len(self.names)} taxa; load={self.load_ms:.0f}ms "
              f"warm={self.warm_ms:.0f}ms", flush=True)

    @lru_cache(maxsize=16)
    def subset(self, requested):
        names, missing, indices, owners = [], [], [], []
        for name in requested:
            rows = self.name_indices.get(name)
            if rows is None:
                missing.append(name)
            else:
                owners.extend([len(names)] * len(rows))
                indices.extend(rows)
                names.append(name)
        # No tokenizer / encode_text / CustomLabelsClassifier call anywhere.
        embeddings = self.tree.txt_embeddings.index_select(
            1, self.torch.tensor(indices, dtype=self.torch.long))
        return names, missing, embeddings, self.torch.tensor(owners, dtype=self.torch.long)

    def top(self, probs, names, k):
        values, indices = probs.topk(min(k, len(names)))
        return [{"sci": names[i], "p": float(p)}
                for i, p in zip(indices.tolist(), values.tolist())]

    def evidence_top(self, probs, indices, k, denominator=1.0):
        values, positions = probs.topk(min(k, len(probs)))
        original_indices = indices[positions].tolist()
        return [{'sci': self.names[i], 'p': float(p)/denominator, 'global_p': float(p),
                 'group': self.groups[i]} for i,p in zip(original_indices, values.tolist())]

    def _classify(self, request):
        started = time.perf_counter()
        image, crop_used, sharpness = prepare_image(request)
        with self.torch.inference_mode():
            features = self.tree.create_image_features([image])
            open_probs = self.tree.create_probabilities(features, self.tree.txt_embeddings)[0]
            all_indices = self.torch.arange(len(self.names))
            open_top = self.evidence_top(open_probs, all_indices, 3)
            missing = []
            indices = self.group_indices[request.target_group] if request.target_group else all_indices
            group_mass = float(open_probs[indices].sum())
            if not request.candidates:
                scores = open_probs[indices]
                candidate_mass = float(scores.sum())
                predictions = (self.evidence_top(scores, indices, request.k,
                    candidate_mass if request.target_group else 1.0) if candidate_mass > 0 else [])
            else:
                # Reuse the full image probabilities, preserving duplicate-label mass.
                names, rows, owners = [], [], []
                for name in request.candidates:
                    matched = [i for i in self.name_indices.get(name, ())
                               if not request.target_group or self.groups[i] == request.target_group]
                    if not matched:
                        missing.append(name)
                        continue
                    owners.extend([len(names)]*len(matched)); rows.extend(matched); names.append(name)
                predictions = []
                candidate_mass = 0.0
                if names:
                    probs = self.torch.zeros(len(names),dtype=open_probs.dtype)
                    probs.scatter_add_(0,self.torch.tensor(owners,dtype=self.torch.long),open_probs[rows])
                    candidate_mass = float(probs.sum())
                    if candidate_mass > 0:
                        for row in self.top(probs,names,request.k):
                            raw = row['p']; row.update(p=raw/candidate_mass,global_p=raw,
                                group=request.target_group or self.groups[self.name_indices[row['sci']][0]])
                            predictions.append(row)
        scope = {'target_group':request.target_group,'group_mass':group_mass,
                 'candidate_mass':candidate_mass,'category_conflict':bool(request.target_group
                     and open_top[0]['group'] in GROUPS and open_top[0]['group'] != request.target_group)}
        result = {"candidates": predictions, "open_top": open_top, "missing": missing, 'scope':scope,
                "sharpness": sharpness, "crop_used": crop_used,
                "ms": round((time.perf_counter() - started) * 1000, 2)}
        result['assessment'] = assess_visual(result,request.target_group)
        return result

    def classify(self, request):
        started = time.perf_counter()
        with self.lock:
            result = self._classify(request)
        result["ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    def classify_batch(self, request):
        started = time.perf_counter()
        # Whole batch is serialized; sequential encoding bounds peak CPU memory.
        with self.lock:
            results = [self._classify(frame) for frame in request.frames]
        return {"frames": results,
                "sharpest_index": max(range(len(results)), key=lambda i: results[i]["sharpness"]),
                "ms": round((time.perf_counter() - started) * 1000, 2)}


@asynccontextmanager
async def lifespan(app):
    app.state.engine = await run_in_threadpool(
        VisionEngine, int(os.environ.get("VISION_THREADS", "4")))
    yield
    del app.state.engine


app = FastAPI(title="BioCLIP 2 vision", lifespan=lifespan)


@app.get("/health")
async def health():
    engine = getattr(app.state, "engine", None)
    return {"loaded": engine is not None, "model": "imageomics/bioclip-2",
            'capabilities': ['target_group_scope', 'global_visual_scores'],
            "warm_ms": engine.warm_ms if engine else None,
            "load_ms": engine.load_ms if engine else None,
            "taxa": len(engine.names) if engine else 0}


@app.post("/classify")
async def classify(request: FrameRequest):
    return await run_in_threadpool(app.state.engine.classify, request)


@app.post("/classify_batch")
async def classify_batch(request: BatchRequest):
    return await run_in_threadpool(app.state.engine.classify_batch, request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    os.environ["VISION_THREADS"] = str(args.threads)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
