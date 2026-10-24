#!/usr/bin/env python3
"""Same-port static + session-aware protocol mock, not a real identifier.
Run: python web/mock-server.py --port 8767
"""
import argparse
import asyncio
import contextlib
import json
import math
import mimetypes
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

ROOT = Path(__file__).resolve().parent
CLIENT_BUILD = '2026-10-24-camera-intent'
PHOTOS = {}
SESSIONS = {}
SPECIES = [
    ('Anas platyrhynchos', 'Mallard'),
    ('Cardinalis cardinalis', 'Northern Cardinal'),
    ('Turdus migratorius', 'American Robin'),
    ('Asclepias syriaca', 'Common Milkweed'),
    ('Sialia sialis', 'Eastern Bluebird'),
    ('Ixoreus naevius', 'Varied Thrush'),
]


def fetch_photo(species):
    sci, _ = species
    try:
        url = 'https://api.inaturalist.org/v1/taxa?' + urlencode({'q': sci, 'per_page': 1})
        request = Request(url, headers={'User-Agent': 'FieldCompanion-Mock/0.1'})
        with urlopen(request, timeout=4) as result:
            taxa = json.load(result).get('results', [])
        photo = taxa[0].get('default_photo') if taxa else None
        if photo:
            return sci, photo.get('medium_url') or photo.get('square_url')
    except Exception as error:
        print(f'Photo unavailable for {sci}: {type(error).__name__}', flush=True)
    return sci, None


def response(status, body, content_type):
    return Response(status, HTTPStatus(status).phrase, Headers({
        'Content-Type': content_type, 'Content-Length': str(len(body)),
        'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff',
    }), body)


async def process_request(connection, request):
    path = unquote(urlsplit(request.path).path)
    if path == '/ws':
        return None
    target = (ROOT / (path.lstrip('/') or 'index.html')).resolve()
    if not target.is_relative_to(ROOT) or not target.is_file():
        return response(404, b'Not found', 'text/plain')
    if target.suffix not in {'.html', '.css', '.js', '.json', '.svg', '.png'}:
        return response(404, b'Not found', 'text/plain')
    return response(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or 'application/octet-stream')


def now():
    return datetime.now(timezone.utc).isoformat()


def taxon(index, **fields):
    sci, en = SPECIES[index]
    return dict(sci=sci, en=en, photo=PHOTOS.get(sci), **fields)


def initial_items():
    return [taxon(i, id=f'o{i + 1}', group='Plantae' if i == 3 else 'Aves',
                  count=[4, 2, 1, 1][i], notes=['On the pond', 'Singing nearby', 'On the lawn', 'By the trail'][i],
                  first_seen=now(), source=['voice', 'birdnet', 'photo', 'photo'][i],
                  lat=[43.6464, 43.6491, 43.6515, 43.6478][i],
                  lng=[-79.4638, -79.4608, -79.4650, -79.4589][i]) for i in range(4)]


class Session:
    def __init__(self):
        self.items = initial_items()
        self.clients = {}
        self.coords = None
        self.suggestions = []
        self.history = []
        self.task = None
        self.quiet = False
        self.speaking_until = 0

    async def emit(self, message_type, **fields):
        data = json.dumps(dict(type=message_type, **fields))
        if message_type in {'heard', 'transcript', 'candidates'}:
            self.history = (self.history + [data])[-12:]
        for ws in list(self.clients):
            with contextlib.suppress(ConnectionClosed):
                await ws.send(data)

    async def status(self, speaking=False):
        await self.emit('state', connected=True, listening=True, speaking=speaking, model='higgs-realtime-1.5-preview', server_build=CLIENT_BUILD)

    async def checklist(self):
        await self.emit('checklist', items=self.items)

    async def tone(self):
        await self.status(speaking=True)
        self.speaking_until = time.monotonic() + 1.1
        for chunk in range(20):
            values = []
            for i in range(960):
                t = (chunk * 960 + i) / 24000
                envelope = max(0, min(1, t / .025, (.8 - t) / .06))
                values.append(int(2000 * envelope * math.sin(2 * math.pi * 440 * t)))
            data = struct.pack('<960h', *values)
            for ws, client in list(self.clients.items()):
                if client['role'] == 'mic' or client['audio']:
                    with contextlib.suppress(ConnectionClosed):
                        await ws.send(data)
        await asyncio.sleep(.3)
        await self.emit('flush')
        await self.status()

    async def demo(self):
        await asyncio.sleep(.3)
        await self.emit('heard', **taxon(1, conf=.97, t=now(), new=True))
        self.suggestions = [taxon(1, conf=.97, t=now())]
        await self.emit('suggestions', items=self.suggestions)
        await self.emit('earcon', kind='heard')
        await asyncio.sleep(.3)
        await self.emit('heard', **taxon(2, conf=.84, t=now(), new=False))
        await self.emit('transcript', role='user', text='Four mallards by the pond, and two cardinals.', final=True)
        await self.emit('transcript', role='assistant', text='Noted. Keep your eyes on the birds.', final=True)
        await self.emit('earcon', kind='logged')
        await self.emit('candidates', items=[
            taxon(2, source='photo', why='Gray back, orange breast; often forages on lawns.'),
            taxon(4, source='photo', why='Blue back and a warm orange breast.'),
            taxon(5, source='photo', why='Orange eyebrow and a bold dark breast band.'),
        ])
        if not self.quiet:
            await self.tone()


async def handler(ws):
    query = parse_qs(urlsplit(ws.request.path).query)
    role = query.get('role', ['mic'])[0]
    session_id = query.get('session', ['demo'])[0]
    audio = role == 'display' and query.get('audio', ['0'])[0] == '1'
    session = None
    mic_frames = 0
    try:
        first = await ws.recv()
        if not isinstance(first, str) or json.loads(first).get('type') != 'hello':
            await ws.close(1008, 'hello first')
            return
        if role not in {'mic', 'display'}:
            await ws.close(1008, 'invalid role')
            return
        print(f'hello role={role} session={session_id} audio={audio}: {first}', flush=True)
        session = SESSIONS.setdefault(session_id, Session())
        if role == 'mic':
            for older, client in list(session.clients.items()):
                if client['role'] == 'mic':
                    await older.close(1000, 'Replaced by newer microphone')
        session.clients[ws] = dict(role=role, audio=audio)
        await ws.send(json.dumps(dict(type='state', connected=True, listening=True, speaking=False, model='higgs-realtime-1.5-preview', server_build=CLIENT_BUILD)))
        hello = json.loads(first)
        if role == 'mic' and all(isinstance(hello.get(k), (float, int)) for k in ('lat', 'lng')):
            session.coords = {k: hello[k] for k in ('lat', 'lng')}
        await ws.send(json.dumps(dict(type='checklist', items=session.items)))
        await ws.send(json.dumps(dict(type='suggestions', items=session.suggestions)))
        for event in session.history:
            await ws.send(event)
        if not session.task:
            session.task = asyncio.create_task(session.demo())
        async for raw in ws:
            if isinstance(raw, bytes):
                if role != 'mic':
                    await ws.close(1008, 'Display must not send microphone audio')
                    break
                # Echo guard simulation: BirdNET is not implemented by this mock.
                if any(c['audio'] for c in session.clients.values()) and time.monotonic() < session.speaking_until:
                    continue
                mic_frames += 1
                if mic_frames in {1, 50}:
                    print(f'mic PCM frame #{mic_frames}: {len(raw)} bytes', flush=True)
                continue
            message = json.loads(raw)
            kind = message.get('type')
            print('client', raw, flush=True)
            if kind == 'reset':
                if session.task and not session.task.done():
                    session.task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await session.task
                session.items = []
                session.history = []
                session.suggestions = []
                session.speaking_until = 0
                await session.emit('reset')
                await session.emit('flush')
                await session.checklist()
                await session.emit('suggestions', items=[])
                await session.emit('candidates', items=[])
                await session.status()
            elif kind == 'location' and role == 'mic':
                lat, lng = message.get('lat'), message.get('lng')
                if (isinstance(lat, (int, float)) and isinstance(lng, (int, float))
                        and math.isfinite(lat) and math.isfinite(lng) and abs(lat) <= 90 and abs(lng) <= 180):
                    session.coords = dict(lat=lat, lng=lng)
            elif kind == 'suggestion_action':
                sci = message.get('sci')
                suggestion = next((item for item in session.suggestions if item['sci'] == sci), None)
                if suggestion and message.get('action') in {'accept', 'dismiss'}:
                    session.suggestions = [item for item in session.suggestions if item['sci'] != sci]
                    if message['action'] == 'accept':
                        item = next((item for item in session.items if item['sci'] == sci), None)
                        if item:
                            item['count'] += 1
                        else:
                            session.items.append(suggestion | dict(id='o' + str(time.time_ns()), group='Aves', count=1,
                                                                  source='birdnet', first_seen=now(), **(session.coords or {})))
                        await session.checklist()
                    await session.emit('suggestions', items=session.suggestions)
            elif kind == 'edit':
                for item in session.items:
                    if item['id'] == message.get('id'):
                        item['count'] = max(1, int(message.get('count', 1)))
                await session.checklist()
                await session.emit('earcon', kind='logged')
            elif kind == 'delete':
                session.items = [item for item in session.items if item['id'] != message.get('id')]
                await session.checklist()
            elif kind == 'mode' and role == 'mic':
                session.quiet = bool(message.get('quiet'))
            elif kind == 'mic' and role == 'mic':
                await session.emit('state', connected=True, listening=bool(message.get('on')), speaking=False)
            elif kind == 'tap' and role == 'mic':
                await session.emit('flush')
                await session.emit('transcript', role='assistant', text='I am here. That was a Northern Cardinal.', final=True)
                await session.tone()
            elif kind == 'text':
                await session.emit('transcript', role='user', text=str(message.get('text', '')), final=True)
                await session.emit('transcript', role='assistant', text='Demo mode: one mallard added. The live backend handles identification.', final=True)
                mallard = next((item for item in session.items if item['id'] == 'o1'), None)
                if mallard:
                    mallard['count'] += 1
                else:
                    session.items.append(initial_items()[0] | {'count': 1})
                await session.checklist()
                await session.emit('earcon', kind='logged')
    except (ConnectionClosed, json.JSONDecodeError, ValueError):
        pass
    finally:
        if session:
            session.clients.pop(ws, None)
        print(f'closed role={role}; received {mic_frames} microphone frames', flush=True)


async def main(port, host, offline=False):
    if not offline:
        with ThreadPoolExecutor(max_workers=6) as pool:
            PHOTOS.update(pool.map(fetch_photo, SPECIES))
    print(f'iNaturalist photos: {sum(bool(p) for p in PHOTOS.values())}/{len(SPECIES)}', flush=True)
    async with serve(handler, host, port, process_request=process_request, max_size=2**20):
        print(f'Mock: http://localhost:{port} | /?display=1&session=demo', flush=True)
        print('Sessions persist in memory until mock restart. HTTPS required on phones.', flush=True)
        await asyncio.Future()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--offline', action='store_true', help='Skip remote photos for deterministic headless/offline runs')
    args = parser.parse_args()
    try:
        asyncio.run(main(args.port, args.host, args.offline))
    except KeyboardInterrupt:
        pass
