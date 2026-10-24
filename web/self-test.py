#!/usr/bin/env python3
"""Optional Chromium CDP smoke test; run with mock on 8767 and CDP on 9223.
Uses only stdlib + websockets. Writes screenshot.png and self-test-results.json.
"""
import asyncio
import base64
import csv
import io
import json
from pathlib import Path
from urllib.request import urlopen
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parent


async def main():
    targets = json.load(urlopen('http://localhost:9223/json/list'))
    target = next(t for t in targets if t['type'] == 'page')
    pending, events, sequence = {}, [], 0
    async with connect(target['webSocketDebuggerUrl'], max_size=16 * 1024 * 1024) as ws:
        async def reader():
            async for raw in ws:
                message = json.loads(raw)
                if 'id' in message:
                    pending.pop(message['id']).set_result(message)
                else:
                    events.append(message)
        reading = asyncio.create_task(reader())
        async def call(method, **params):
            nonlocal sequence
            sequence += 1
            future = asyncio.get_running_loop().create_future()
            pending[sequence] = future
            await ws.send(json.dumps(dict(id=sequence, method=method, params=params)))
            result = await asyncio.wait_for(future, 20)
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result.get('result', {})
        async def evaluate(expression):
            result = await call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True, userGesture=True)
            if result.get('exceptionDetails'):
                raise RuntimeError(result['exceptionDetails'])
            return result.get('result', {}).get('value')
        await call('Page.enable')
        await call('Runtime.enable')
        await call('Network.enable')
        await call('Browser.setDownloadBehavior', behavior='deny')
        await call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=1, mobile=True)
        await call('Emulation.setTouchEmulationEnabled', enabled=True)
        await call('Emulation.setGeolocationOverride', latitude=43.66, longitude=-79.39, accuracy=10)
        await call('Browser.grantPermissions', permissions=['audioCapture', 'geolocation'], origin='http://localhost:8767')
        await call('Page.addScriptToEvaluateOnNewDocument', source='''
            window.__stops = 0; window.__contexts = []; window.__sockets = [];
            const WS = window.WebSocket;
            window.WebSocket = class extends WS { constructor(...args) { super(...args); window.__sockets.push(this); } };
            const AC = window.AudioContext;
            window.AudioContext = class extends AC {
              constructor(...args) { super(...args); window.__contexts.push(this); }
              createBufferSource() { const s = super.createBufferSource(); const stop = s.stop.bind(s); s.stop = (...args) => { window.__stops++; return stop(...args); }; return s; }
            };
            const originalURL = URL.createObjectURL.bind(URL);
            URL.createObjectURL = blob => { window.__csvBlob = blob; return originalURL(blob); };
        ''')
        await call('Page.navigate', url='http://localhost:8767/')
        await asyncio.sleep(1)
        events.clear()
        await evaluate("document.getElementById('start').click()")
        await asyncio.sleep(3)
        results = await evaluate('''({
          connection: document.getElementById('connection').innerText,
          items: document.querySelectorAll('.observation').length,
          heard: document.querySelectorAll('.heard-card').length,
          candidates: document.querySelectorAll('.candidate-card').length,
          sampleRate: window.__contexts[0]?.sampleRate,
          flushStoppedSources: window.__stops,
          overflow: document.documentElement.scrollWidth > innerWidth,
          serviceWorker: !!navigator.serviceWorker.controller
        })''')
        assert results['items'] == 4 and results['heard'] == 2 and results['candidates'] == 3, results
        assert results['sampleRate'] == 48000 and results['flushStoppedSources'] > 0 and not results['overflow'], results
        shot = await call('Page.captureScreenshot', format='png', captureBeyondViewport=False)
        (ROOT / 'screenshot.png').write_bytes(base64.b64decode(shot['data']))
        await evaluate("window.__sockets[0].close()")
        await asyncio.sleep(2.5)
        results['autoReconnect'] = await evaluate("window.__sockets.length >= 2 && document.getElementById('connection').classList.contains('online')")
        assert results['autoReconnect'], results
        await evaluate("document.querySelectorAll('.counter')[0].lastElementChild.click()")
        await asyncio.sleep(.2)
        assert await evaluate("document.querySelector('.counter output').value") == '5'
        await evaluate("document.querySelector('.delete-button').click()")
        await asyncio.sleep(.2)
        assert await evaluate("document.querySelectorAll('.observation').length") == 3
        await evaluate("document.getElementById('text-input').value='再加一只绿头鸭'; document.getElementById('text-form').requestSubmit()")
        await asyncio.sleep(.2)
        assert await evaluate("document.querySelectorAll('.observation').length") == 4
        await evaluate("document.getElementById('quiet').click(); document.getElementById('mic').click()")
        await asyncio.sleep(.2)
        assert await evaluate("document.getElementById('mic').getAttribute('aria-pressed')") == 'true'
        def binary_count():
            return sum(e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 2 for e in events)
        paused_count = binary_count()
        await asyncio.sleep(.3)
        results['micPauseStopsFrames'] = binary_count() == paused_count
        assert results['micPauseStopsFrames'], results
        await evaluate("document.getElementById('mic').click(); document.getElementById('tap').click()")
        await asyncio.sleep(.5)
        await evaluate("document.getElementById('export').click(); document.getElementById('export-ebird').click()")
        ebird = await evaluate('window.__csvBlob.text()')
        rows = list(csv.reader(io.StringIO(ebird)))
        assert len(rows) == 3 and all(len(row) == 19 and row[12] == 'Incidental' for row in rows), rows
        assert not any('Asclepias' in str(row) for row in rows), rows
        await evaluate("document.getElementById('export-all').click()")
        generic = await evaluate('window.__csvBlob.text()')
        assert len(list(csv.reader(io.StringIO(generic)))) == 5
        results['ebirdRows'] = len(rows)
        results['ebirdColumns'] = len(rows[0])
        results['genericRowsIncludingHeader'] = 5
        results['gpsInExport'] = rows[0][6:8]
        await evaluate("document.getElementById('export-dialog').close(); document.getElementById('stop').click()")
        await asyncio.sleep(.3)
        assert await evaluate("document.getElementById('start').hidden") is False
        sent = [e['params']['response'] for e in events if e.get('method') == 'Network.webSocketFrameSent']
        results['firstSentFrame'] = json.loads(sent[0]['payloadData'])
        results['microphoneBinaryFrames'] = sum(frame['opcode'] == 2 for frame in sent)
        results['microphoneFrameBytes'] = sorted({len(base64.b64decode(frame['payloadData'])) for frame in sent if frame['opcode'] == 2})
        assert results['microphoneFrameBytes'] == [1920]
        assert results['firstSentFrame']['type'] == 'hello' and results['microphoneBinaryFrames'] > 0
        results['runtimeExceptions'] = [e['params'] for e in events if e.get('method') == 'Runtime.exceptionThrown']
        assert not results['runtimeExceptions'], results['runtimeExceptions']
        # Offline-ish: only the cached app shell is expected to work without a server.
        await call('Network.emulateNetworkConditions', offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        await call('Page.reload')
        await asyncio.sleep(1)
        results['offlineShell'] = await evaluate("!!document.getElementById('start') && document.title.includes('Field companion')")
        await call('Network.emulateNetworkConditions', offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        assert results['offlineShell'], results
        (ROOT / 'self-test-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(results, ensure_ascii=False, indent=2))
        reading.cancel()


asyncio.run(main())
