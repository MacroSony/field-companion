"""Opt-in, bounded live expert check. Uses synthetic evidence; never prints keys."""
import argparse
import asyncio
import json
import time

from expert import NaturalistExpert


async def main():
    provider = NaturalistExpert()
    if not provider.available:
        print(json.dumps({"ok": False, "status": "not_configured"}))
        return 1
    obs = {"id": "synthetic-expert-check", "status": "open", "uncertain": True,
           "group": "Aves", "lat": 43.66, "lng": -79.39, "t_start": "2026-10-23",
           "fused": [{"sci": "Dryobates pubescens", "en": "Downy Woodpecker", "p": .55},
                     {"sci": "Leuconotopicus villosus", "en": "Hairy Woodpecker", "p": .45}],
           "detections": [{"id": "synthetic-image", "kind": "image", "t": "synthetic",
                           "candidates": [{"sci": "Dryobates pubescens", "en": "Downy Woodpecker", "p": .55},
                                          {"sci": "Leuconotopicus villosus", "en": "Hairy Woodpecker", "p": .45}]}]}
    success = True
    for question in ("Could it be a Hairy Woodpecker?", "它吃什么？"):
        start = time.monotonic()
        result = await provider.answer(question, obs)
        success &= result["ok"]
        print(json.dumps({"question": question, "elapsed_ms": round((time.monotonic() - start) * 1000),
                          **result}, ensure_ascii=False))
        if result["status"] in ("quota_exhausted", "authentication_failed", "access_denied", "rate_limited"):
            break
    return 0 if success else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True,
                        help="Allow two real OpenAI requests and species-source lookups")
    parser.parse_args()
    raise SystemExit(asyncio.run(main()))
