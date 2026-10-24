"""Check a running BioCLIP service against local JPEGs; no model is loaded here.

Example: python backend/vision_smoke.py bird.jpg --candidate 'Cardinalis cardinalis'
Use --report out/vision-check.json to retain the results. Scores are rankings,
not calibrated identification confidence; an HTTP pass is not an accuracy claim.
"""
import argparse
import base64
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(base, route, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(base.rstrip('/') + route, data=data, headers={'Content-Type': 'application/json'})
    started = time.perf_counter()
    with urlopen(req, timeout=90) as response:
        result = json.load(response)
    return result, round((time.perf_counter() - started) * 1000, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', type=Path, nargs='+')
    parser.add_argument('--url', default='http://127.0.0.1:8795')
    parser.add_argument('--candidate', action='append', help='Exact scientific name; repeat for a shortlist')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    health, _ = request(args.url, '/health')
    if not health.get('loaded') or health.get('model') != 'imageomics/bioclip-2':
        raise RuntimeError('Expected a loaded BioCLIP 2 service')
    report = {'health': health, 'images': []}
    for path in args.images:
        payload = {'jpeg_b64': base64.b64encode(path.read_bytes()).decode(),
                   'candidates': args.candidate, 'k': 5}
        result, elapsed = request(args.url, '/classify', payload)
        if not result.get('candidates') or len(result.get('open_top', [])) != 3:
            raise RuntimeError('Classifier returned no candidates or invalid open predictions')
        scores = [float(row['p']) for row in result['candidates']]
        if any(not 0 <= score <= 1 for score in scores) or scores != sorted(scores, reverse=True):
            raise RuntimeError('Invalid candidate ranking')
        row = {'image': str(path), 'roundtrip_ms': elapsed, **result}
        report['images'].append(row)
        print(f"{path.name}: {result['candidates'][0]['sci']} ({elapsed:.0f} ms)", flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    else:
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (HTTPError, URLError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f'Vision check failed: {exc}')
