"""Private, authenticated BirdNET worker in the original TensorFlow environment.

The optional LiveKit app uses its own Python environment. Only this loopback
worker imports birdnetlib/TensorFlow; recognition and allowlist logic are reused.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import math
from pathlib import Path
import secrets

from fastapi import FastAPI, HTTPException, Request
import numpy as np

import server as core


def build_app(key):
    model = core.Session('private-birdnet-worker')
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await model.close()

    app = FastAPI(lifespan=lifespan)

    def authorize(request):
        supplied = request.headers.get('x-field-worker', '')
        if not key or not secrets.compare_digest(supplied, key):
            raise HTTPException(403, 'Worker authorization required')

    def location(lat, lng):
        if not math.isfinite(lat) or not math.isfinite(lng) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(422, 'Invalid coordinates')
        model.lat, model.lng = lat, lng

    @app.get('/health')
    async def health():
        return {'ok': True, 'service': 'birdnet-worker'}

    @app.get('/allowed')
    async def allowed(request: Request, lat: float, lng: float):
        authorize(request)
        async with lock:
            location(lat, lng)
            try:
                result = await asyncio.get_running_loop().run_in_executor(model.executor, model.allowed_species)
                return {'species': sorted(result)}
            except Exception:
                raise HTTPException(503, 'BirdNET range lookup unavailable') from None

    @app.post('/analyze')
    async def analyze(request: Request, lat: float, lng: float):
        authorize(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 10 * core.SR * 4:
                raise HTTPException(413, 'Audio window too large')
        if not body or len(body) % 4:
            raise HTTPException(422, 'Expected float32 mono audio at 48 kHz')
        samples = np.frombuffer(body, dtype='<f4')
        if not np.isfinite(samples).all() or np.max(np.abs(samples)) > 1.01:
            raise HTTPException(422, 'Invalid audio samples')
        async with lock:
            location(lat, lng)
            try:
                result = await asyncio.get_running_loop().run_in_executor(model.executor, model.analyze, samples)
                return {'detections': result}
            except Exception:
                raise HTTPException(503, 'BirdNET analysis unavailable') from None

    return app


if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8811)
    parser.add_argument('--key-file', required=True)
    args = parser.parse_args()
    secret = Path(args.key_file).read_text(encoding='utf-8').strip()
    if len(secret) < 32:
        parser.error('Create a private worker key before starting')
    uvicorn.run(build_app(secret), host='127.0.0.1', port=args.port, log_level='warning')
