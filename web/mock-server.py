#!/usr/bin/env python3
"""Same-port static + protocol mock (NOT a bird identifier or persistent backend).
Run: /data/build/birding-demo/.venv/bin/python web/mock-server.py
Then open http://localhost:8766 (phones need an HTTPS tunnel).
"""
import argparse
import asyncio
import contextlib
import json
import math
import mimetypes
import struct
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import unquote, urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

ROOT = Path(__file__).resolve().parent
MOCK_BIRD = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 290"><defs><linearGradient id="b" x2="1" y2="1"><stop stop-color="#4e6540"/><stop offset="1" stop-color="#152e23"/></linearGradient></defs><rect width="480" height="290" fill="url(#b)"/><g fill="#b3c18b" opacity=".12"><circle cx="70" cy="40" r="55"/><circle cx="410" cy="110" r="65"/><circle cx="140" cy="240" r="85"/></g><path d="M0 255 480 200" stroke="#82715a" stroke-width="12"/><path d="m237 211-8 25m36-28-6 23" stroke="#5c4838" stroke-width="5"/><path d="m177 172-76 28 91 3" fill="#615e52"/><ellipse cx="248" cy="160" rx="70" ry="58" fill="#858779"/><path d="M207 146q70 1 80 59-65 32-83-9" fill="#c18a4f"/><path d="M194 147q35 1 58 50-52-3-58-50" fill="#666e62"/><circle cx="295" cy="111" r="36" fill="#656d60"/><path d="m328 112 31 10-32 9" fill="#d2b875"/><circle cx="304" cy="105" r="5" fill="#0e1812"/><circle cx="305" cy="103" r="1.7" fill="#fff"/><text x="17" y="274" fill="#d4e5a0" font-size="11" font-family="sans-serif" letter-spacing="2">MOCK FIELD ILLUSTRATION</text></svg>'''


def response(status, body, content_type):
    return Response(status, HTTPStatus(status).phrase, Headers({
        'Content-Type': content_type, 'Content-Length': str(len(body)),
        'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff',
    }), body)


async def process_request(connection, request):
    path = unquote(urlsplit(request.path).path)
    if path == '/ws':
        return None
    if path == '/mock-bird.svg':
        return response(200, MOCK_BIRD, 'image/svg+xml')
    target = (ROOT / (path.lstrip('/') or 'index.html')).resolve()
    if not target.is_relative_to(ROOT) or not target.is_file():
        return response(404, b'Not found', 'text/plain')
    # Never expose the mock's Python source or generated check artifacts.
    if target.suffix not in {'.html', '.css', '.js', '.json', '.svg', '.png'}:
        return response(404, b'Not found', 'text/plain')
    content_type = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
    return response(200, target.read_bytes(), content_type)


def initial_items():
    now = datetime.now(timezone.utc).isoformat()
    return [
        dict(id='o1', sci='Anas platyrhynchos', en='Mallard', zh='绿头鸭', group='Aves', count=4, notes='On the pond', first_seen=now, source='voice'),
        dict(id='o2', sci='Cardinalis cardinalis', en='Northern Cardinal', zh='北美红雀', group='Aves', count=2, notes='Singing nearby', first_seen=now, source='birdnet'),
        dict(id='o3', sci='Turdus migratorius', en='American Robin', zh='旅鸫', group='Aves', count=1, notes='On the lawn', first_seen=now, source='photo'),
        dict(id='o4', sci='Asclepias syriaca', en='Common Milkweed', zh='普通马利筋', group='Plantae', count=1, notes='All-taxa export example', first_seen=now, source='photo'),
    ]


async def handler(ws):
    items = initial_items()
    scenario = None
    quiet = False
    mic_frames = 0
    async def emit(message_type, **fields):
        await ws.send(json.dumps(dict(type=message_type, **fields), ensure_ascii=False))
    async def checklist():
        await emit('checklist', items=items)
    async def status(speaking=False, listening=True):
        await emit('state', connected=True, listening=listening, speaking=speaking, model='higgs-realtime-1.5-preview')
    async def tone():
        await status(speaking=True)
        # 40 ms PCM16 little-endian chunks at exactly 24 kHz.
        for chunk in range(20):
            values = []
            for i in range(960):
                t = (chunk * 960 + i) / 24000
                envelope = min(1, t / .025, (.8 - t) / .06)
                values.append(int(2000 * max(0, envelope) * math.sin(2 * math.pi * 440 * t)))
            await ws.send(struct.pack('<960h', *values))
        await asyncio.sleep(.3)
        await emit('flush')  # Demonstrate dropping the rest of the queued tone.
        await status()
    async def demo():
        await asyncio.sleep(.3)
        await emit('heard', sci='Cardinalis cardinalis', en='Northern Cardinal', zh='北美红雀', conf=.97, t=datetime.now(timezone.utc).isoformat(), new=True)
        await emit('earcon', kind='heard')
        await asyncio.sleep(.3)
        await emit('heard', sci='Turdus migratorius', en='American Robin', zh='旅鸫', conf=.84, t=datetime.now(timezone.utc).isoformat(), new=False)
        await emit('transcript', role='user', text='池塘边四只绿头鸭，还有两只北美红雀。', final=True)
        await emit('transcript', role='assistant', text='记下了。继续看鸟，清单交给我。', final=True)
        await emit('earcon', kind='logged')
        await emit('candidates', items=[
            dict(sci='Turdus migratorius', en='American Robin', zh='旅鸫', photo='/mock-bird.svg', why='灰色背部，橙色胸部；常在草坪觅食。'),
            dict(sci='Sialia sialis', en='Eastern Bluebird', zh='东蓝鸲', photo='/mock-bird.svg', why='蓝色背部与暖橙色胸部。'),
            dict(sci='Ixoreus naevius', en='Varied Thrush', zh='杂色鸫', photo='/mock-bird.svg', why='橙色眉纹与明显胸带。'),
        ])
        if not quiet:
            await tone()
    try:
        first = await ws.recv()
        if not isinstance(first, str) or json.loads(first).get('type') != 'hello':
            await emit('error', message='hello must be the first frame')
            await ws.close(1008, 'hello first')
            return
        print('hello', first, flush=True)
        await status()
        await checklist()
        scenario = asyncio.create_task(demo())
        async for raw in ws:
            if isinstance(raw, bytes):
                mic_frames += 1
                if mic_frames in {1, 50}:
                    print(f'mic PCM frame #{mic_frames}: {len(raw)} bytes', flush=True)
                continue
            message = json.loads(raw)
            kind = message.get('type')
            print('client', raw, flush=True)
            if kind == 'edit':
                for item in items:
                    if item['id'] == message.get('id'):
                        item['count'] = max(1, int(message.get('count', 1)))
                await checklist()
                await emit('earcon', kind='logged')
            elif kind == 'delete':
                items = [item for item in items if item['id'] != message.get('id')]
                await checklist()
            elif kind == 'mode':
                quiet = bool(message.get('quiet'))
            elif kind == 'mic':
                await status(listening=bool(message.get('on')))
            elif kind == 'tap':
                await emit('flush')
                await emit('transcript', role='assistant', text='我在。你刚才听见的是北美红雀。', final=True)
                await tone()
            elif kind == 'text':
                text = str(message.get('text', ''))
                await emit('transcript', role='user', text=text, final=True)
                await emit('transcript', role='assistant', text='演示模式：已记录一只绿头鸭。真实识别由后端完成。', final=True)
                mallard = next((item for item in items if item['id'] == 'o1'), None)
                if mallard:
                    mallard['count'] += 1
                else:
                    items.append(initial_items()[0] | {'count': 1})
                await checklist()
                await emit('earcon', kind='logged')
    except (ConnectionClosed, json.JSONDecodeError):
        pass
    finally:
        if scenario:
            scenario.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                await scenario
        print(f'closed; received {mic_frames} microphone frames', flush=True)


async def main(port, host):
    async with serve(handler, host, port, process_request=process_request, max_size=2**20):
        print(f'Mock: http://localhost:{port}  |  WebSocket: /ws', flush=True)
        print('Mock state resets on reconnect. Use an HTTPS tunnel for a physical phone.', flush=True)
        await asyncio.Future()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--host', default='0.0.0.0')
    args = parser.parse_args()
    try:
        asyncio.run(main(args.port, args.host))
    except KeyboardInterrupt:
        pass
