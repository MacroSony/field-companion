"""Experiment E: isolated real-upstream server benchmark, synthetic local fixtures."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = Path('/data/build/tmp/overnight/E')
sys.path.insert(0, str(ROOT / 'backend'))


def serve():
    import logging
    import server as s
    import uvicorn
    init, log, audio, tool, inject = s.Session.__init__, s.Session.log, s.Session.phone_audio, s.Session.tool, s.Session.inject
    def seeded(self, *args):
        init(self, *args)
        for i, (sci, en, count) in enumerate([('Cardinalis cardinalis', 'Northern Cardinal', 7), ('Turdus migratorius', 'American Robin', 8)]):
            self.observations[str(i)] = s.Observation(str(i), status='confirmed', count=count, fused=[{'sci': sci, 'en': en, 'p': 1}])
    def logged(self, event, **fields):
        log(self, event, perf=time.perf_counter(), **fields)
        if event == 'route_done':
            self._measure_first = True
    async def sent(self, pcm):
        await audio(self, pcm)
        if getattr(self, '_measure_first', False):
            self._measure_first = False
            self.log('client_first_audio', bytes=len(pcm))
    async def fixture_tool(self, name, args):
        if name == 'recent_sounds':
            self.history.clear()
            for en in ['Northern Cardinal', 'American Robin']:
                self.history.append((time.monotonic(), {'en': en}))
        return await tool(self, name, args)
    async def clean_inject(self, text):
        # Keep the live connection/HTTP pool but remove failed-turn pollution.
        # This is a controlled empty-context measurement, not production history.
        for item_id in list(self.conversation_ids):
            await self.rt_send({'type': 'conversation.item.delete', 'item_id': item_id})
        self.conversation_ids.clear()
        await inject(self, text)
    s.Session.__init__, s.Session.log, s.Session.phone_audio, s.Session.tool = seeded, logged, sent, fixture_tool
    s.Session.inject = clean_inject
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('websockets').setLevel(logging.WARNING)
    uvicorn.run(s.app, host='127.0.0.1', port=8797, log_level='warning')


async def bench():
    import websockets
    for flag in [0, 1]:
        logpath = OUT / f'server-{flag}.log'
        with logpath.open('w') as log:
            process = subprocess.Popen([sys.executable, __file__, '--serve'], cwd=ROOT,
                env={**os.environ, 'TEMPLATE_ANSWERS': str(flag)}, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        ws = await websockets.connect('ws://127.0.0.1:8797/ws?session=experiment-e', max_size=None)
                        break
                    except OSError:
                        await asyncio.sleep(.1)
                else:
                    raise RuntimeError('server did not start')
                await asyncio.sleep(.1)
                await ws.close()
                async with websockets.connect('ws://127.0.0.1:8797/ws?session=experiment-e', max_size=None) as ws:
                    while True:
                        event = json.loads(await asyncio.wait_for(ws.recv(), 30))
                        if event.get('type') == 'state' and event.get('connected'):
                            break
                    for rep in range(8):
                        for case, question in [('sounds', 'What did you hear?'), ('checklist', 'How many species do we have?')]:
                            for attempt in range(5):
                                t = time.perf_counter()
                                await ws.send(json.dumps({'type': 'text', 'text': question}))
                                first = None
                                transcript = ''
                                errors = []
                                while True:
                                    raw = await asyncio.wait_for(ws.recv(), 40)
                                    if isinstance(raw, bytes):
                                        if first is None:
                                            first = time.perf_counter() - t
                                        continue
                                    event = json.loads(raw)
                                    if event.get('role') == 'assistant':
                                        transcript = event.get('text', '')
                                    if event.get('type') == 'error':
                                        errors.append(event.get('message'))
                                    # Turn worker always emits a final state. Let queued
                                    # upstream errors settle before submitting another turn.
                                    if (first is not None and event.get('type') == 'state' and not event.get('speaking')) or (errors and event.get('type') == 'state'):
                                        break
                                row = dict(flag=flag, rep=rep, case=case, attempt=attempt, text_submit_first_client_s=first, transcript=transcript, errors=errors, submit_perf=t)
                                with (OUT / 'trials.jsonl').open('a') as f:
                                    f.write(json.dumps(row) + '\n')
                                print(json.dumps(row), flush=True)
                                await asyncio.sleep(.35)
                                if first is not None:
                                    break
            finally:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    if '--serve' in sys.argv:
        serve()
    else:
        asyncio.run(bench())
