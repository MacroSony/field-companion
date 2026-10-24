#!/usr/bin/env python3
"""Optional Chromium CDP smoke test; run with mock on 8877 and CDP on 9223.
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
        await call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=1, mobile=True)
        await call('Emulation.setTouchEmulationEnabled', enabled=True)
        await call('Emulation.setGeolocationOverride', latitude=43.66, longitude=-79.39, accuracy=10)
        await call('Browser.grantPermissions', permissions=['audioCapture', 'geolocation'], origin='http://localhost:8877')
        await call('Page.addScriptToEvaluateOnNewDocument', source='''
            window.__stops = 0; window.__starts = []; window.__contexts = []; window.__sockets = []; window.__micRequests = 0; window.__gains = []; window.__sources = [];
            const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
            navigator.mediaDevices.getUserMedia = (...args) => { window.__micRequests++; return gum(...args); };
            const WS = window.WebSocket;
            window.WebSocket = class extends WS { constructor(...args) { super(...args); window.__sockets.push(this); } };
            const AC = window.AudioContext;
            window.AudioContext = class extends AC {
              constructor(...args) { super(...args); window.__contexts.push(this); }
              createGain() {
                const gain = super.createGain(), connect = gain.connect.bind(gain);
                gain.__destinations = [];
                gain.connect = (...args) => { gain.__destinations.push(args[0]); return connect(...args); };
                window.__gains.push(gain); return gain;
              }
              createBufferSource() {
                const s = super.createBufferSource(), stop = s.stop.bind(s), start = s.start.bind(s), connect = s.connect.bind(s);
                s.__destinations = []; window.__sources.push(s);
                s.connect = (...args) => { s.__destinations.push(args[0]); return connect(...args); };
                s.stop = (...args) => { window.__stops++; return stop(...args); };
                s.start = (...args) => { if (Number.isFinite(args[0])) window.__starts.push({scheduled: args[0], now: this.currentTime, duration: s.buffer?.duration || 0}); return start(...args); };
                return s;
              }
            };
            const originalURL = URL.createObjectURL.bind(URL);
            URL.createObjectURL = blob => { window.__csvBlob = blob; return originalURL(blob); };
        ''')
        await call('Page.navigate', url='http://localhost:8877/?session=phone-test')
        await asyncio.sleep(1)
        assert await evaluate("""document.readyState === 'complete' ? true : new Promise(resolve => {
          window.addEventListener('load', () => resolve(true), {once:true});
          setTimeout(() => resolve(false), 10000);
        })"""), 'Page did not finish loading'
        # A cold Chromium worker can start after the audio/UI are ready.
        worker_ready = await evaluate("""navigator.serviceWorker.controller ? true : new Promise(resolve => {
          navigator.serviceWorker.addEventListener('controllerchange', () => resolve(true), {once:true});
          setTimeout(() => resolve(false), 15000);
        })""")
        assert worker_ready, await evaluate("""(async () => ({secure:isSecureContext,url:location.href,
          workers:(await navigator.serviceWorker.getRegistrations()).map(r=>({scope:r.scope,active:r.active?.state,installing:r.installing?.state,waiting:r.waiting?.state})),
          caches:await caches.keys()}))()""")
        # Apply download interception after the app shell has initialized.
        await call('Browser.setDownloadBehavior', behavior='deny')
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
        assert await evaluate("window.__contexts[0] && document.getElementById('capture-settings').textContent.includes('echoCancellation: true')")
        assert await evaluate("document.getElementById('interrupt-hint').textContent.includes('Tap to interrupt') && document.getElementById('audio-mode').textContent.includes('tap to interrupt')")
        results['speakerEchoCancellation'] = True
        assert await evaluate("document.getElementById('audio-mode').getAttribute('aria-pressed')") == 'false'
        await evaluate("document.getElementById('audio-mode').click()")
        assert await evaluate("document.getElementById('audio-mode').getAttribute('aria-pressed') === 'true' && localStorage.getItem('field-earphones') === 'true'")
        assert await evaluate("document.getElementById('interrupt-hint').textContent.includes('Say something to interrupt') && document.getElementById('audio-mode').textContent.includes('voice interruption on')")
        await evaluate("document.getElementById('audio-mode').click()")
        assert await evaluate("document.getElementById('interrupt-hint').textContent.includes('Tap to interrupt') && document.getElementById('earphone-tip').textContent.includes('ignores voice')")
        results['speakerTapAndEarphoneVoiceGuidance'] = True
        await evaluate("""window.__sockets[0].dispatchEvent(new MessageEvent('message', {data: JSON.stringify({type:'suggestions',items:[
          {sci:'Cardinalis cardinalis',en:'Northern Cardinal',conf:.87}, {sci:'Turdus migratorius',en:'American Robin',conf:.82}
        ]})}));""")
        assert await evaluate("document.querySelectorAll('#suggestions .suggestion-chip').length") == 2
        await evaluate("document.querySelector('#suggestions .suggestion-chip button').click(); document.querySelectorAll('#suggestions .suggestion-dismiss')[1].click()")
        await asyncio.sleep(.2)
        messages = [json.loads(e['params']['response']['payloadData']) for e in events
                    if e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 1]
        assert {'type': 'audio_mode', 'earphones': True} in messages
        assert {'type': 'audio_mode', 'earphones': False} in messages
        assert {'type': 'suggestion_action', 'sci': 'Cardinalis cardinalis', 'action': 'accept'} in messages
        assert {'type': 'suggestion_action', 'sci': 'Turdus migratorius', 'action': 'dismiss'} in messages
        assert any(m.get('type') == 'playback' and m.get('active') is True for m in messages)
        assert any(m.get('type') == 'playback' and m.get('active') is False for m in messages)
        results['audioModesAndPlaybackReports'] = True
        results['suggestionChipActions'] = True
        # Silence assistant output without changing microphone audio or the
        # existing playback timeline; both current and future chunks share a bus.
        await evaluate("""(() => {
          const ws = window.__sockets[0], json = message => ws.dispatchEvent(new MessageEvent('message', {data:JSON.stringify(message)}));
          json({type:'flush'}); json({type:'audio_start',id:'mute-check'});
          for (let i=0;i<2;i++) ws.dispatchEvent(new MessageEvent('message',{data:new ArrayBuffer(48000)}));
          json({type:'audio_end',id:'mute-check'});
          window.__muteBaseline = {starts:window.__starts.length, stops:window.__stops, micRequests:window.__micRequests};
        })()""")
        await asyncio.sleep(.1)
        muted = await evaluate("""(async () => {
          document.getElementById('assistant-mute').click();
          // Scheduled AudioParam values take effect on the audio render thread.
          await new Promise(resolve => setTimeout(resolve, 50));
          const ws=window.__sockets[0], ac=window.__contexts[0];
          ws.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({type:'transcript',role:'assistant',text:'Muted reply stays in captions.',final:true})}));
          const output=window.__gains.filter(g=>g.__destinations.includes(ac.destination));
          ws.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({type:'earcon',kind:'heard'})}));
          return {outputCount:output.length,gain:output[0]?.gain.value,
            pcmSharesOutput:window.__sources.slice(-2).every(s=>s.__destinations.length===1 && s.__destinations[0]===output[0]),
            toneSharesOutput:window.__gains.some(g=>g.__destinations.includes(output[0])),
            pressed:document.getElementById('assistant-mute').getAttribute('aria-pressed'),
            visibleStatus:!document.getElementById('start-note').hidden && document.getElementById('start-note').textContent.includes('Assistant muted'),
            caption:document.getElementById('assistant-caption').textContent,
            sameStarts:window.__starts.length===window.__muteBaseline.starts,sameStops:window.__stops===window.__muteBaseline.stops};
        })()""")
        assert muted == {'outputCount': 1, 'gain': 0, 'pcmSharesOutput': True, 'toneSharesOutput': True, 'pressed': 'true', 'visibleStatus': True,
                         'caption': 'Muted reply stays in captions.', 'sameStarts': True, 'sameStops': True}, muted
        mic_before_mute = sum(e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 2 for e in events)
        await asyncio.sleep(.2)
        assert sum(e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 2 for e in events) > mic_before_mute
        mute_reports = [json.loads(e['params']['response']['payloadData']) for e in events
                        if e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 1]
        assert any(m.get('type') == 'playback' and m.get('response_id') == 'mute-check' and m.get('active') is True for m in mute_reports)
        assert {'type': 'output_mute', 'muted': True} in mute_reports
        unmuted = await evaluate("""(async () => {
          document.getElementById('assistant-mute').click();
          await new Promise(resolve => setTimeout(resolve, 50));
          const output=window.__gains.find(g=>g.__destinations.includes(window.__contexts[0].destination));
          return {gain:output.gain.value,sameStarts:window.__starts.length===window.__muteBaseline.starts,
            sameStops:window.__stops===window.__muteBaseline.stops,micUnchanged:window.__micRequests===window.__muteBaseline.micRequests};
        })()""")
        assert unmuted == {'gain': 1, 'sameStarts': True, 'sameStops': True, 'micUnchanged': True}, unmuted
        await asyncio.sleep(.05)
        mute_messages = [json.loads(e['params']['response']['payloadData']) for e in events
                         if e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 1]
        assert [m for m in mute_messages if m.get('type') == 'output_mute'][-1] == {'type': 'output_mute', 'muted': False}
        results['assistantMutePreservesCaptureCaptionsAndPlayback'] = True
        # Queue 40 seconds immediately in a suspended real AudioContext. Stream
        # synthesis can run much faster than playback; no waiting for 40 seconds.
        burst = await evaluate("""(async () => {
          const ws = window.__sockets[0], ac = window.__contexts[0];
          const json = message => ws.dispatchEvent(new MessageEvent('message', {data:JSON.stringify(message)}));
          json({type:'flush'}); await ac.suspend();
          const before = window.__starts.length;
          json({type:'audio_start',id:'long-reply'});
          for (let i=0;i<40;i++) ws.dispatchEvent(new MessageEvent('message',{data:new ArrayBuffer(48000)}));
          json({type:'audio_end',id:'long-reply'});
          const starts=window.__starts.slice(before);
          return {count:starts.length, nonoverlap:starts.every((s,i)=>!i || s.scheduled+1e-6>=starts[i-1].scheduled+starts[i-1].duration),
            lead:starts.at(-1).scheduled-ac.currentTime};
        })()""")
        assert burst['count'] == 40 and burst['nonoverlap'] and burst['lead'] > 38, burst
        await asyncio.sleep(.1)
        messages = [json.loads(e['params']['response']['payloadData']) for e in events
                    if e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 1]
        assert any(m.get('response_id') == 'long-reply' and m.get('drained') is False for m in messages), messages[-5:]
        assert not any(m.get('response_id') == 'long-reply' and m.get('drained') is True for m in messages)
        await evaluate("window.__sockets[0].dispatchEvent(new MessageEvent('message',{data:JSON.stringify({type:'flush'})})); window.__contexts[0].resume()")
        results['longSpeechQueueNoOverlap'] = burst
        await evaluate("window.__sockets[0].dispatchEvent(new MessageEvent('message', {data:JSON.stringify({type:'suggestions',items:[]})}))")
        shot = await call('Page.captureScreenshot', format='png', captureBeyondViewport=False)
        (ROOT / 'screenshot.png').write_bytes(base64.b64decode(shot['data']))
        results['phoneSessionURL'] = await evaluate("window.__sockets[0].url")
        assert 'role=mic' in results['phoneSessionURL'] and 'session=phone-test' in results['phoneSessionURL']
        await evaluate("document.getElementById('map-card').open = true")
        await asyncio.sleep(.4)
        results['phoneMapMarkers'] = await evaluate("document.querySelectorAll('.leaflet-marker-icon').length")
        results['youAreHereDot'] = await evaluate("!!document.querySelector('.leaflet-interactive')")
        assert results['phoneMapMarkers'] == 4 and results['youAreHereDot']
        await evaluate("document.getElementById('map-card').open = false; document.getElementById('assistant-mute').click()")
        await asyncio.sleep(.05)
        mute_sent_before_reconnect = sum(e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 1
            and json.loads(e['params']['response']['payloadData']) == {'type': 'output_mute', 'muted': True} for e in events)
        await evaluate("window.__sockets[0].close()")
        await asyncio.sleep(2.5)
        results['autoReconnect'] = await evaluate("window.__sockets.length >= 2 && document.getElementById('connection').classList.contains('online')")
        assert results['autoReconnect'], results
        assert sum(e.get('method') == 'Network.webSocketFrameSent' and e['params']['response']['opcode'] == 1
            and json.loads(e['params']['response']['payloadData']) == {'type': 'output_mute', 'muted': True} for e in events) > mute_sent_before_reconnect
        assert await evaluate("document.getElementById('assistant-mute').getAttribute('aria-pressed')") == 'true'
        await evaluate("document.getElementById('assistant-mute').click()")
        await evaluate("document.querySelectorAll('.counter')[0].lastElementChild.click()")
        await asyncio.sleep(.2)
        assert await evaluate("document.querySelector('.counter output').value") == '5'
        await evaluate("document.querySelector('.delete-button').click()")
        await asyncio.sleep(.2)
        assert await evaluate("document.querySelectorAll('.observation').length") == 3
        await evaluate("document.getElementById('text-input').value='One more mallard'; document.getElementById('text-form').requestSubmit()")
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
        await evaluate("document.getElementById('assistant-mute').click()")
        await evaluate("document.getElementById('export-dialog').close(); document.getElementById('stop').click()")
        await asyncio.sleep(.3)
        assert await evaluate("document.getElementById('start').hidden") is False
        assert await evaluate("document.getElementById('assistant-mute').getAttribute('aria-pressed')") == 'false'
        await evaluate("window.__starts=[]; document.getElementById('start').click()")
        await asyncio.sleep(1)
        await evaluate("document.getElementById('tap').click()")
        await asyncio.sleep(.7)
        results['restartPlayback'] = await evaluate("({contexts:window.__contexts.length, starts:window.__starts.length, maxDelay:Math.max(...window.__starts.map(s=>s.scheduled-s.now))})")
        assert results['restartPlayback']['contexts'] == 2 and results['restartPlayback']['starts'] > 0, results['restartPlayback']
        assert results['restartPlayback']['maxDelay'] < 1, results['restartPlayback']
        await evaluate("document.getElementById('stop').click()")
        await asyncio.sleep(.2)
        sent = [e['params']['response'] for e in events if e.get('method') == 'Network.webSocketFrameSent']
        results['firstSentFrame'] = json.loads(sent[0]['payloadData'])
        results['microphoneBinaryFrames'] = sum(frame['opcode'] == 2 for frame in sent)
        results['microphoneFrameBytes'] = sorted({len(base64.b64decode(frame['payloadData'])) for frame in sent if frame['opcode'] == 2})
        assert results['microphoneFrameBytes'] == [1920]
        assert results['firstSentFrame']['type'] == 'hello' and results['firstSentFrame']['lang'] == 'en' and results['microphoneBinaryFrames'] > 0
        results['runtimeExceptions'] = [e['params'] for e in events if e.get('method') == 'Runtime.exceptionThrown']
        assert not results['runtimeExceptions'], results['runtimeExceptions']
        # Offline-ish: only the cached app shell is expected to work without a server.
        await call('Network.emulateNetworkConditions', offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        await call('Page.reload')
        await asyncio.sleep(1)
        results['offlineShell'] = await evaluate("!!document.getElementById('start') && document.title.includes('Field Companion')")
        await call('Network.emulateNetworkConditions', offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        assert results['offlineShell'], results
        # Display never opens a microphone, and silent display never creates an audio context.
        await call('Emulation.setDeviceMetricsOverride', width=1280, height=720, deviceScaleFactor=1, mobile=False)
        events.clear()
        await call('Page.navigate', url='http://localhost:8877/?display=1&session=display-test')
        await asyncio.sleep(3)
        results['display'] = await evaluate("""({
          items: document.querySelectorAll('.observation').length,
          heard: document.querySelectorAll('.heard-card').length,
          markers: document.querySelectorAll('.leaflet-marker-icon').length,
          micRequests: window.__micRequests,
          audioContexts: window.__contexts.length,
          socketURL: window.__sockets[0].url,
          phoneHint: document.getElementById('phone-hint').innerText,
          photos: document.querySelectorAll('.observation img').length,
          loadedPhotos: [...document.querySelectorAll('.observation img')].filter(i => i.complete && i.naturalWidth > 0).length,
          userCaption: document.getElementById('user-caption').innerText,
          assistantCaption: document.getElementById('assistant-caption').innerText,
          overflow: document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight,
          checklistRight: document.querySelector('.checklist-section').getBoundingClientRect().right,
          mapLeft: document.getElementById('map-card').getBoundingClientRect().left,
          mapHeight: document.getElementById('map').getBoundingClientRect().height
        })""")
        d = results['display']
        assert d['items'] == 4 and d['heard'] == 2 and d['markers'] == 4 and not d['overflow'], d
        assert d['micRequests'] == 0 and d['audioContexts'] == 0 and 'role=display' in d['socketURL'] and 'audio=0' in d['socketURL'], d
        assert d['checklistRight'] < d['mapLeft'] and d['mapHeight'] > 100, d
        shot = await call('Page.captureScreenshot', format='png', captureBeyondViewport=False)
        (ROOT / 'screenshot-display.png').write_bytes(base64.b64decode(shot['data']))
        # Shared session edits reach a second display socket and highlight the updated card.
        await evaluate("""window.__peer = new WebSocket('ws://localhost:8877/ws?role=display&session=display-test');
          window.__peer.onopen = () => { window.__peer.send(JSON.stringify({type:'hello',lang:'en'})); setTimeout(() => window.__peer.send(JSON.stringify({type:'edit',id:'o1',count:9})), 100); };""")
        await asyncio.sleep(.4)
        results['display']['sharedUpdate'] = await evaluate("document.querySelector('.counter output').value === '9' && document.querySelector('.observation').classList.contains('updated')")
        assert results['display']['sharedUpdate']
        await evaluate('window.__peer.close()')
        await evaluate("document.querySelector('.leaflet-marker-icon').click()")
        results['display']['mapPopup'] = await evaluate("document.querySelector('.leaflet-popup-content').innerText.includes('Mallard')")
        assert results['display']['mapPopup']
        await evaluate("""window.__sockets[0].dispatchEvent(new MessageEvent('message', {data: JSON.stringify({type:'checklist',items:[
          {id:'fallback1',en:'Missing photo',sci:'Test species',count:1,source:'voice'},
          {id:'fallback2',en:'Failed photo',sci:'Test species two',count:1,source:'photo',photo:'/missing-photo.png'}
        ]})}));
        window.__sockets[0].dispatchEvent(new MessageEvent('message', {data: JSON.stringify({type:'candidates',items:[{en:'Candidate without photo',sci:'Test species',source:'photo'}]})}));""")
        await asyncio.sleep(.4)
        results['photoFallbacks'] = await evaluate("({checklist: document.querySelectorAll('.observation .source-icon').length, candidates: document.querySelectorAll('.candidate-card .source-icon').length})")
        assert results['photoFallbacks'] == {'checklist': 2, 'candidates': 1}, results['photoFallbacks']
        await call('Emulation.setDeviceMetricsOverride', width=1920, height=1080, deviceScaleFactor=1, mobile=False)
        await asyncio.sleep(.3)
        results['display1080'] = await evaluate("({overflow: document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight, captionFont: getComputedStyle(document.getElementById('user-caption')).fontSize})")
        assert not results['display1080']['overflow']
        await call('Page.navigate', url='http://localhost:8877/?display=1&session=audio-test&audio=1')
        await asyncio.sleep(.2)
        assert await evaluate("!document.getElementById('audio-overlay').hidden && window.__micRequests === 0 && window.__contexts.length === 0")
        await evaluate("document.getElementById('enable-audio').click()")
        await asyncio.sleep(.2)
        # Explicitly trigger a mock response after audio is unlocked; network/image
        # timing must not decide whether the initial demo tone precedes the click.
        await evaluate("""window.__audioPeer = new WebSocket('ws://localhost:8877/ws?role=mic&session=audio-test');
          window.__audioPeer.onopen = () => { window.__audioPeer.send(JSON.stringify({type:'hello',lang:'en'})); window.__audioPeer.send(JSON.stringify({type:'tap'})); };""")
        await asyncio.sleep(1)
        await evaluate('window.__audioPeer.close()')
        results['displayAudio'] = await evaluate("({overlayHidden: document.getElementById('audio-overlay').hidden, micRequests: window.__micRequests, audioState: window.__contexts[0]?.state, flushStoppedSources: window.__stops, socketURL: window.__sockets[0].url})")
        a = results['displayAudio']
        assert a['overlayHidden'] and a['micRequests'] == 0 and a['audioState'] == 'running' and a['flushStoppedSources'] > 0 and 'audio=1' in a['socketURL'], a
        display_sent = [e['params']['response'] for e in events if e.get('method') == 'Network.webSocketFrameSent']
        assert not any(frame['opcode'] == 2 for frame in display_sent)
        results['displayMicrophoneBinaryFrames'] = 0
        results['displayRuntimeExceptions'] = [e['params'] for e in events if e.get('method') == 'Runtime.exceptionThrown']
        assert not results['displayRuntimeExceptions'], results['displayRuntimeExceptions']
        (ROOT / 'self-test-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(results, ensure_ascii=False, indent=2))
        reading.cancel()


asyncio.run(main())
