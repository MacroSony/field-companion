#!/usr/bin/env python3
"""Offline unittest coverage + opt-in real-model HTTP smoke/latency test.

Run: python -B backend/test_vision.py --integration
Default: python -B -m unittest discover -s backend -p test_vision.py -v
No images, logs or model artifacts are written by this test.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("FIELD_COMPANION_TEST_DATA", str(HERE.parent / "testdata"))).expanduser()


def jpeg_payload(path, candidates=None, crop=None, k=5):
    return {"jpeg_b64": base64.b64encode(Path(path).read_bytes()).decode(),
            "candidates": candidates, "crop": crop, "k": k}


def local_names(path):
    rows = json.loads(path.read_text())
    names = [row if isinstance(row, str) else row.get("scientific", row.get("sci"))
             for row in rows]
    if not names or not all(isinstance(name, str) for name in names):
        raise ValueError("Invalid candidates JSON")
    return list(dict.fromkeys(names))


class InputTests(unittest.TestCase):
    def test_crop_validation(self):
        from vision_service import FrameRequest
        from pydantic import ValidationError
        for crop in ([0, 0, 0, 1], [-.1, 0, .5, .5], [0, 0, 1.1, 1],
                     [0, 0, float("nan"), 1], [0, 0, .5]):
            with self.assertRaises(ValidationError):
                FrameRequest(jpeg_b64="abcd", crop=crop)

    def test_decode_and_square_padding(self):
        from PIL import Image
        from vision_service import FrameRequest, prepare_image
        stream = io.BytesIO()
        Image.new("RGB", (200, 100), "green").save(stream, format="JPEG")
        encoded = base64.b64encode(stream.getvalue()).decode()
        frame = FrameRequest(jpeg_b64=encoded, crop=[.4, .4, .1, .2])
        image, crop, sharpness = prepare_image(frame)
        self.assertEqual(image.size, (26, 26))
        self.assertAlmostEqual(crop[2] * 200, crop[3] * 100)
        self.assertEqual(sharpness, 0)
        edge = FrameRequest(jpeg_b64=encoded, crop=[.95, .9, .05, .1])
        _, crop, _ = prepare_image(edge)
        self.assertLessEqual(crop[0] + crop[2], 1)
        self.assertLessEqual(crop[1] + crop[3], 1)
        # Positive subpixel crops at the boundary still select at least one pixel.
        tiny = FrameRequest(jpeg_b64=encoded, crop=[1, 1, 1e-20, 1e-20])
        image, _, _ = prepare_image(tiny)
        self.assertEqual(image.size, (1, 1))

    def test_invalid_image(self):
        from fastapi import HTTPException
        from vision_service import FrameRequest, prepare_image
        for encoded in ("!!!!", base64.b64encode(b"not jpeg").decode()):
            with self.assertRaises(HTTPException):
                prepare_image(FrameRequest(jpeg_b64=encoded))

    def test_limits_and_deduplication(self):
        from pydantic import ValidationError
        from vision_service import BatchRequest, FrameRequest
        frame = FrameRequest(jpeg_b64="abcd", candidates=[" Genus  species ", "Genus species"])
        self.assertEqual(frame.candidates, ["Genus species"])
        for kwargs in ({"k": 0}, {"k": 513}, {"candidates": [""]}):
            with self.assertRaises(ValidationError):
                FrameRequest(jpeg_b64="abcd", **kwargs)
        with self.assertRaises(ValidationError):
            BatchRequest(frames=[])
        with self.assertRaises(ValidationError):
            BatchRequest(frames=[frame] * 9)


def verify_subset(candidates_path):
    """Independent full TreeOfLife probabilities, restricted/renormalized."""
    import torch
    from bioclip import Rank
    from vision_service import FrameRequest, VisionEngine, prepare_image
    engine = VisionEngine()
    # Fail immediately if request-time code attempts any text encoding.
    engine.tree.model.encode_text = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Text re-encoding is forbidden"))
    names = local_names(candidates_path)
    request = FrameRequest(**jpeg_payload(DATA / "img/cardinal.jpg", names, k=len(names)))
    image, _, _ = prepare_image(request)
    with torch.inference_mode():
        full = engine.tree.create_probabilities_for_images(
            [image], ["reference"], engine.tree.txt_embeddings)["reference"]
        own_top = engine.tree.predict([image], rank=Rank.SPECIES, k=3,
                                      callback=lambda *a: None)
    actual = engine.classify(request)
    rows = {name: full[indices].sum().item() for name, indices in engine.name_indices.items()
            if name in names}
    denominator = sum(rows.values())
    expected = {name: p / denominator for name, p in rows.items()}
    error = max(abs(item["p"] - expected[item["sci"]]) for item in actual["candidates"])
    assert len(actual["candidates"]) == len(expected)
    assert error < 2e-6, error
    assert abs(sum(item["p"] for item in actual["candidates"]) - 1) < 1e-5
    for actual_top, reference in zip(actual["open_top"], own_top):
        assert actual_top["sci"] == reference["species"]
        assert abs(actual_top["p"] - reference["score"]) < 2e-6
    print(json.dumps({"subset_equivalence": "PASS", "max_abs_error": error,
                      "matched": len(expected), "missing": actual["missing"],
                      "open_matches_tree_predict": True,
                      "duplicate_binomials": sum(len(v) > 1 for v in engine.name_indices.values())}),
          flush=True)


def http(base, route, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + route, data=data,
                                     headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.load(response)
    return result, (time.perf_counter() - started) * 1000


def check_result(result):
    assert len(result["open_top"]) == 3
    for key in ("open_top", "candidates"):
        assert all(0 <= row["p"] <= 1 for row in result[key])
        assert all(result[key][i]["p"] >= result[key][i+1]["p"]
                   for i in range(len(result[key]) - 1))
    assert result["ms"] > 0 and result["sharpness"] >= 0


def integration(args):
    from PIL import Image, ImageFilter
    names = local_names(args.candidates)
    assert len(names) == 60, f"Expected 60 local species, got {len(names)}"
    # Verify in its own process so the model is released before HTTP service starts.
    subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--verify-subset",
                    "--candidates", str(args.candidates), "--data-dir", str(DATA)], check=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", args.port))  # Refuse to reuse/stop someone else's service.
    process = subprocess.Popen([sys.executable, "-B", str(HERE / "vision_service.py"),
                                "--port", str(args.port)], cwd=HERE.parent)
    base = f"http://127.0.0.1:{args.port}"
    rows = []
    try:
        deadline = time.monotonic() + 120
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Service exited {process.returncode}")
            try:
                health, _ = http(base, "/health")
                if health["loaded"]:
                    break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("Model startup exceeded 120s")
            time.sleep(.25)
        print("HEALTH " + json.dumps(health), flush=True)
        for path in sorted((DATA / "img").glob("*.jpg")):
            for mode, candidates in (("open", None), ("local60", names)):
                result, elapsed = http(base, "/classify", jpeg_payload(path, candidates))
                check_result(result)
                nonbirds = {"ladybug.jpg": "Coccinella septempunctata",
                            "monarch.jpg": "Danaus plexippus",
                            "trillium.jpg": "Trillium grandiflorum"}
                if mode == "open" and path.name in nonbirds:
                    assert result["candidates"][0]["sci"] == nonbirds[path.name]
                row = {"image": path.name, "mode": mode, "top": result["candidates"][:3],
                       "missing": result["missing"], "service_ms": result["ms"],
                       "http_ms": round(elapsed, 2)}
                rows.append(row)
                print(json.dumps(row), flush=True)
        # Visually estimated bird ROI in 00-6.jpg (not a detector/ground-truth box).
        synthetic = DATA / "vision-imgs/00-6.jpg"
        crop = [.485, .46, .05, .085]
        for roi in (None, crop):
            for mode, candidates in (("open", None), ("local60", names)):
                result, elapsed = http(base, "/classify", jpeg_payload(synthetic, candidates, roi))
                check_result(result)
                row = {"image": synthetic.name, "mode": mode, "crop": roi,
                       "crop_used": result["crop_used"], "top": result["candidates"][:3],
                       "service_ms": result["ms"], "http_ms": round(elapsed, 2)}
                rows.append(row)
                print(json.dumps(row), flush=True)
        good = jpeg_payload(DATA / "img/cardinal.jpg", names, k=60)
        known, _ = http(base, "/classify", good)
        assert abs(sum(row["p"] for row in known["candidates"]) - 1) < 1e-5
        missing_request = dict(good, candidates=[names[0], "Notagenus notaspecies", names[0]])
        missing, _ = http(base, "/classify", missing_request)
        assert missing["missing"] == ["Notagenus notaspecies"]
        assert missing["candidates"] == [{"sci": names[0], "p": 1.0}]
        all_missing, _ = http(base, "/classify", dict(good, candidates=["Notagenus notaspecies"]))
        assert not all_missing["candidates"] and all_missing["open_top"]
        for invalid in (dict(good, crop=[0, 0, 2, 1]), dict(good, jpeg_b64="!!!!"),
                        dict(good, k=0)):
            try:
                http(base, "/classify", invalid)
            except urllib.error.HTTPError as exc:
                assert exc.code == 422
            else:
                raise AssertionError("Invalid request was accepted")
        with Image.open(DATA / "img/cardinal.jpg") as image:
            stream = io.BytesIO()
            image.filter(ImageFilter.GaussianBlur(12)).save(stream, format="JPEG")
        blurred = dict(good, jpeg_b64=base64.b64encode(stream.getvalue()).decode())
        batch, elapsed = http(base, "/classify_batch", {"frames": [blurred, good]})
        assert batch["sharpest_index"] == 1
        assert len(batch["frames"]) == 2
        print("BATCH " + json.dumps({"sharpest_index": batch["sharpest_index"],
              "sharpness": [frame["sharpness"] for frame in batch["frames"]],
              "service_ms": batch["ms"], "http_ms": round(elapsed, 2)}), flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            inference = pool.submit(http, base, "/classify_batch", {"frames": [good] * 3})
            time.sleep(.15)
            _, health_ms = http(base, "/health")
            assert health_ms < 500, f"Health blocked for {health_ms}ms"
            assert not inference.done(), "Inference finished before health responsiveness check"
            inference.result()
        print(f"HEALTH_DURING_INFERENCE {health_ms:.2f}ms", flush=True)
        for mode in ("open", "local60"):
            values = [row["http_ms"] for row in rows if row["mode"] == mode]
            print(f"LATENCY {mode} n={len(values)} median={statistics.median(values):.2f}ms "
                  f"min={min(values):.2f}ms max={max(values):.2f}ms", flush=True)
        print("INTEGRATION PASS (scores, normalization, crop, nonbirds, missing, validation, batch, health)",
              flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", action="store_true")
    parser.add_argument("--verify-subset", action="store_true")
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--data-dir", type=Path, default=DATA,
                        help="External image fixture directory (FIELD_COMPANION_TEST_DATA)")
    parser.add_argument("--candidates", type=Path)
    args = parser.parse_args()
    DATA = args.data_dir.expanduser()
    args.candidates = args.candidates or DATA / "vision-imgs/candidates.json"
    if args.integration or args.verify_subset:
        required = [args.candidates, DATA / "img/cardinal.jpg"]
        if args.integration:
            required.append(DATA / "vision-imgs/00-6.jpg")
        missing = [str(p) for p in required if not p.is_file()]
        if missing:
            print("SKIP vision integration: missing external fixtures: " + ", ".join(missing)
                  + "; supply --data-dir / FIELD_COMPANION_TEST_DATA and --candidates.")
            raise SystemExit(0)
    if args.verify_subset:
        verify_subset(args.candidates)
    elif args.integration:
        integration(args)
    else:
        unittest.main(argv=[sys.argv[0]], verbosity=2)
