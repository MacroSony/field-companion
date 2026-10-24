"""Camera browser integration with fake media and real Observation/frame routes.

Start --serve on 8878, an isolated Chromium CDP on 9224 with fake media, then
run without --serve. No model/API requests; JSON/screenshots go to ignored out/.
"""
import argparse
import asyncio
import base64
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid
from urllib.request import urlopen

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "camera-checks"


def serve(port):
    sys.path.insert(0, str(ROOT / "backend"))
    import server as backend
    from expert import Settings, NaturalistExpert
    from PIL import Image
    import uvicorn

    class FixtureSession(backend.Session):
        def __init__(self, session_id):
            super().__init__(session_id)
            self.expert = NaturalistExpert(settings=Settings(enabled=False))
            self.names = {"Turdus migratorius": {"sci": "Turdus migratorius", "en": "American Robin"}}
            self.research_release = asyncio.Event()

        def allowed_species(self):
            return set()

        def start(self):
            self.connected = True
            self.turn_task = asyncio.create_task(self.turn_worker())
            self.tasks = [asyncio.create_task(self.focus_watch()),
                          self.turn_task, asyncio.create_task(self.suggestion_loop())]

        async def ingest_audio(self, pcm):
            pass  # The test deliberately makes no BirdNET or voice API request.

        async def rt_send(self, event):
            pass  # Model transport is a fixture too, including result injection.

        async def generate(self, phase, *, guard=None):
            if guard is not None and not guard():
                return {"status": "deferred"}
            if phase == "route" and self.turn_question == "What is that?":
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": "look", "call_id": "fixture-look-" + uuid.uuid4().hex,
                    "arguments": "{}"}]}
            if phase == "answer" and self.expert_readout:
                await self.phone_json({"type": "transcript", "role": "assistant",
                                       "text": self.expert_readout, "final": True})
            return {"status": "completed", "output": []}

        async def fixture_expert(self, question, observation, *, on_progress, **kwargs):
            await on_progress("analyzing")
            await self.research_release.wait()
            return {"ok": True, "status": "completed", "action": "answer",
                    "answer": "Fixture result for the original bird.", "hint": "", "sources": []}

        async def handle_phone(self, event, client=None):
            text = event.get("text", "")
            if event.get("type") == "text" and text.startswith("fixture:"):
                if text == "fixture:voice-look":
                    await self.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "What is that?", "item_id": "voice-" + uuid.uuid4().hex})
                elif text == "fixture:voice-start":
                    await self.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "Start.", "item_id": "voice-" + uuid.uuid4().hex})
                elif text == "fixture:finish":
                    self.research_release.set()
                elif text == "fixture:checklist":
                    await self.phone_json({"type": "transcript", "role": "assistant",
                                           "text": "Fixture checklist is still responsive.", "final": True})
                elif text == "fixture:explain-r1":
                    result = await self.tool("get_investigations", {"id": "R1", "explain": True})
                    await self.speak_expert(result["answer"])
                else:
                    self.expert = SimpleNamespace(available=True, answer=self.fixture_expert)
                    await self.tool("ask_expert", {"question": text})
                return
            return await super().handle_phone(event, client)

    def classify(jpeg, crop, allowed):
        image = Image.open(io.BytesIO(jpeg))
        if image.format != "JPEG" or max(image.size) > 1920:
            raise ValueError("Invalid camera JPEG")
        return {"candidates": [{"sci": "Turdus migratorius", "p": .91}], "sharpness": 50}

    backend.Session = FixtureSession
    backend.classify_frame = classify
    uvicorn.run(backend.app, host="127.0.0.1", port=port, log_level="warning")


async def test(port, cdp):
    OUT.mkdir(parents=True, exist_ok=True)
    # The hidden server can still be importing dependencies when Chrome starts.
    async with asyncio.timeout(30):
        while True:
            try:
                response = await asyncio.to_thread(urlopen, f"http://localhost:{port}/", timeout=1)
                response.close()
                break
            except OSError:
                await asyncio.sleep(.2)
    targets = json.load(urlopen(f"http://127.0.0.1:{cdp}/json/list"))
    target = next(t for t in targets if t["type"] == "page")
    pending, events, sequence = {}, [], 0
    results = {}
    origin = f"http://localhost:{port}"
    session = "camera-" + uuid.uuid4().hex[:8]
    async with connect(target["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as ws:
        async def reader():
            async for raw in ws:
                message = json.loads(raw)
                if "id" in message:
                    pending.pop(message["id"]).set_result(message)
                else:
                    events.append(message)

        reading = asyncio.create_task(reader())

        async def call(method, **params):
            nonlocal sequence
            sequence += 1
            future = asyncio.get_running_loop().create_future()
            pending[sequence] = future
            await ws.send(json.dumps(dict(id=sequence, method=method, params=params)))
            response = await asyncio.wait_for(future, 25)
            if "error" in response:
                raise RuntimeError(response["error"])
            return response.get("result", {})

        async def js(expression):
            response = await call("Runtime.evaluate", expression=expression, returnByValue=True,
                                  awaitPromise=True, userGesture=True)
            if response.get("exceptionDetails"):
                raise RuntimeError(response["exceptionDetails"])
            return response.get("result", {}).get("value")

        async def until(expression):
            for _ in range(120):
                if await js(expression):
                    return
                await asyncio.sleep(.1)
            state = await js("({text:document.body.innerText.slice(0,1600),requests:window.__requests?.length,uploads:window.__uploads,geometry:window.__geometry})")
            raise AssertionError(f"Timed out: {expression}; state={state}")

        await call("Page.enable")
        await call("Runtime.enable")
        await call("Network.enable")
        await call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
        await call("Browser.grantPermissions", permissions=["audioCapture", "videoCapture", "geolocation"], origin=origin)
        await call("Page.addScriptToEvaluateOnNewDocument", source="""
          window.__streams = []; window.__uploads = []; window.__requests = []; window.__sockets = []; window.__messages = [];
          window.__encodes = [];
          const encode = HTMLCanvasElement.prototype.toBlob;
          HTMLCanvasElement.prototype.toBlob = function(...args) { window.__encodes.push([this.width,this.height]); return encode.apply(this,args); };
          window.__denyCamera = false; window.__denyMic = false; window.__failUpload = false; window.__slowUpload = false;
          const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = async constraints => {
            window.__requests.push(constraints);
            if ((constraints.video && window.__denyCamera) || (constraints.audio && window.__denyMic))
              throw new DOMException('Synthetic permission denial', 'NotAllowedError');
            let stream;
            if (constraints.video) {
              // A deterministic video MediaStream exercises real video/canvas
              // capture without Windows fake-camera placeholder-frame races.
              const canvas = document.createElement('canvas'); canvas.width=1920; canvas.height=1080;
              const ctx = canvas.getContext('2d'); let frame=0;
              const draw = () => {
                ctx.fillStyle='#254d35'; ctx.fillRect(0,0,1920,1080);
                ctx.fillStyle='#b9d98a'; ctx.fillRect(700+(frame++%5)*2,300,400,450);
                for(let x=0;x<1920;x+=80){ctx.fillStyle=x%160?'#366849':'#172e20';ctx.fillRect(x,0,16,1080);}
              };
              draw(); stream=canvas.captureStream(20);
              const timer=setInterval(draw,50), track=stream.getVideoTracks()[0], stop=track.stop.bind(track);
              track.stop=()=>{clearInterval(timer);stop();};
            } else stream=await gum(constraints);
            window.__streams.push(stream); return stream;
          };
          navigator.geolocation.watchPosition = callback => { window.__gps = callback; return 1; };
          navigator.geolocation.clearWatch = () => {};
          const fetchOriginal = window.fetch.bind(window);
          window.fetch = async (url, options) => {
            if (String(url).includes('/api/frame?')) {
              const image = await createImageBitmap(options.body);
              window.__uploads.push({url:String(url),type:options.body.type,width:image.width,height:image.height}); image.close();
              if (window.__failUpload) return new Response('', {status:503});
              if (window.__slowUpload) {
                await new Promise((resolve,reject) => {
                  window.__releaseUpload = resolve;
                  options.signal.addEventListener('abort', () => reject(new DOMException('Aborted','AbortError')), {once:true});
                });
              }
            }
            return fetchOriginal(url, options);
          };
          const WS = window.WebSocket;
          window.WebSocket = class extends WS { constructor(...args) { super(...args); window.__sockets.push(this);
            this.addEventListener('message', e => { if(typeof e.data === 'string') window.__messages.push(JSON.parse(e.data)); });
          } };
        """)
        await call("Page.navigate", url=origin + "/?session=" + session)
        await until("document.readyState === 'complete' && typeof FieldCamera === 'function'")
        await js("window.__geometry=[]; const originalFrame=FieldCamera.prototype.frame; FieldCamera.prototype.frame=function(z){const v=document.getElementById('camera-preview'); window.__geometry.push({zoom:z,width:v.videoWidth,height:v.videoHeight}); return originalFrame.call(this,z);}")
        await js("document.getElementById('start').click()")
        await until("document.getElementById('connection').classList.contains('online')")
        assert await js("window.__requests.length === 1 && !window.__requests[0].video")
        await js("window.__gps({coords:{latitude:40.75,longitude:-73.98}})")
        await asyncio.sleep(.2)
        results["lateGpsSent"] = any(
            e.get("method") == "Network.webSocketFrameSent" and '"lat":40.75' in e["params"]["response"].get("payloadData", "")
            for e in events)
        assert results["lateGpsSent"]
        await js("window.__denyMic=true; document.getElementById('mic-device').add(new Option('Failing input','fail')); document.getElementById('mic-device').value='fail'; document.getElementById('mic-device').dispatchEvent(new Event('change'))")
        await until("document.getElementById('notice').textContent.includes('previous microphone')")
        assert await js("window.__streams[0].getAudioTracks()[0].readyState === 'live'")
        await js("window.__denyMic=false; window.__denyCamera=true; document.getElementById('camera-toggle').click()")
        await until("document.getElementById('camera-status').textContent.includes('permission denied')")
        assert await js("window.__streams[0].getAudioTracks()[0].readyState === 'live'")
        results["permissionAndMicIsolation"] = True
        await js("window.__denyCamera=false; document.getElementById('camera-toggle').click()")
        await until("!document.getElementById('camera-shutter').disabled")
        await until("document.getElementById('camera-preview').videoWidth >= 64 && document.getElementById('camera-preview').videoHeight >= 64")
        await until("window.__messages.some(e=>e.text==='Camera ready. Say start to take a photo.')")
        assert await js("window.__encodes.length === 0 && window.__uploads.length === 0")
        results["manualOpenWaitsForConfirmation"] = True
        await js("document.getElementById('camera-group').value='Plantae'; document.getElementById('camera-group').dispatchEvent(new Event('change'))")
        await until("window.__messages.at(-1)?.type==='camera_prepare' || window.__messages.some(e=>e.type==='observation' && e.group==='Plantae')")
        await until("window.__messages.some(e=>e.type==='observation' && e.group==='Plantae')")
        assert await js("window.__encodes.length === 0 && window.__uploads.length === 0")
        await js("document.getElementById('camera-group').value='Aves'; document.getElementById('camera-group').dispatchEvent(new Event('change'))")
        await until("window.__messages.filter(e=>e.type==='observation').at(-1)?.group==='Aves'")
        results["groupChangeRearmsWithoutCapturing"] = True
        await js("document.getElementById('camera-zoom').value='2'; document.getElementById('camera-zoom').dispatchEvent(new Event('input')); document.getElementById('camera-viewport').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        await js("document.getElementById('camera-shutter').click()")
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && !document.getElementById('camera-shutter').disabled")
        upload = await js("window.__uploads[0]")
        assert upload["type"] == "image/jpeg" and 100 <= max(upload["width"], upload["height"]) <= 1920, (upload, await js("window.__geometry"))
        assert "x=0.275000" in upload["url"] and "w=0.450000" in upload["url"], upload
        assert "capture=" in upload["url"], upload
        assert await js("window.__uploads.length") == 1
        assert await js("window.__encodes.length") == 3
        assert await js("document.getElementById('observation-candidates').textContent.includes('American Robin')")
        results["burstZoomCropJpeg"] = upload
        observations = [json.loads(e["params"]["response"]["payloadData"]) for e in events
                        if e.get("method") == "Network.webSocketFrameReceived" and e["params"]["response"]["opcode"] == 1]
        assert any(e.get("type") == "observation" and e.get("lat") == 40.75 and e.get("lng") == -73.98 for e in observations)
        first_obs = upload["url"].split("obs=")[1].split("&")[0]
        await js("document.getElementById('observation-again').click()")
        await until("document.querySelectorAll('#observation-evidence img').length === 2 && !document.getElementById('camera-shutter').disabled")
        assert first_obs in await js("window.__uploads[1].url")
        results["lookAgainSameFocus"] = True
        # A pending photo must not be sent after its subject is discarded.
        await js("window.__slowUpload=true; document.getElementById('observation-again').click()")
        await until("window.__uploads.length === 3")
        await js("window.__sockets[0].send(JSON.stringify({type:'observation_action',id:'" + first_obs + "',action:'discard'}))")
        await until("document.getElementById('observation-status').textContent === 'Closed'")
        await js("window.__slowUpload=false; document.getElementById('camera-new').click()")
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && !document.getElementById('observation-confirm').disabled")
        assert first_obs not in await js("window.__uploads[3].url")
        results["staleCaptureCancelled"] = True
        await js("document.getElementById('observation-count').value='3'; document.getElementById('observation-confirm').click()")
        await until("document.getElementById('observation-status').textContent === 'Added to checklist'")
        assert await js("document.querySelector('.counter output').value") == "3"
        results["confirmedDerivedChecklist"] = True
        shot = await call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        (OUT / "phone.png").write_bytes(base64.b64decode(shot["data"]))
        assert not await js("document.documentElement.scrollWidth > innerWidth")
        await js("window.__failUpload=true; document.getElementById('camera-new').click()")
        await until("document.getElementById('camera-status').textContent.includes('recognition is unavailable')")
        assert await js("document.querySelector('.counter output').value") == "3"
        results["serviceFailureKeepsChecklist"] = True
        await js("window.__failUpload=false; document.getElementById('camera-toggle').click()")
        assert await js("window.__streams.filter(s=>s.getVideoTracks().length).every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
        assert await js("window.__streams[0].getAudioTracks()[0].readyState === 'live'")
        # Look opens a usable preview and announces readiness, without even
        # encoding a frame. A second completed STT turn authorizes the photo.
        async def voice_ready(wait_for_drain=False):
            marker = await js("window.__messages.length")
            uploads = await js("window.__uploads.length")
            encodes = await js("window.__encodes.length")
            await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:voice-look'}))")
            await until("window.__messages.slice(" + str(marker) + ").some(e=>e.text==='Camera ready. Say start to take a photo.') && !document.getElementById('camera-shutter').disabled")
            await asyncio.sleep(3.6 if wait_for_drain else .3)
            assert await js("window.__uploads.length") == uploads
            assert await js("window.__encodes.length") == encodes

        def feedback_before_capture(start):
            messages = [json.loads(e["params"]["response"]["payloadData"]) for e in events[start:]
                        if e.get("method") == "Network.webSocketFrameReceived" and e["params"]["response"]["opcode"] == 1]
            feedback = next(i for i, e in enumerate(messages) if e.get("text") == "Let me take a look.")
            capture = next(i for i, e in enumerate(messages) if e.get("type") == "capture")
            assert feedback < capture
            assert any(e.get("type") == "audio_end" and e["id"].startswith("feedback-") for e in messages[:capture])

        before_voice = len(events)
        await voice_ready(wait_for_drain=True)
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:voice-start'}))")
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && !document.getElementById('camera-shutter').disabled")
        feedback_before_capture(before_voice)
        before_voice = len(events)
        await voice_ready()
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:voice-start'}))")
        await until("document.querySelectorAll('#observation-evidence img').length === 2 && !document.getElementById('camera-shutter').disabled")
        feedback_before_capture(before_voice)
        results["voiceCameraReadyThenStartWithCameraClosedAndOpen"] = True
        await voice_ready()
        uploads = await js("window.__uploads.length")
        await js("Object.defineProperty(document,'hidden',{configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange'))")
        assert await js("window.__streams.filter(s=>s.getVideoTracks().length).every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
        assert await js("window.__streams[0].getAudioTracks()[0].readyState === 'live'")
        await js("delete document.hidden; window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:voice-start'}))")
        await asyncio.sleep(.4)
        assert await js("window.__uploads.length") == uploads
        assert await js("document.getElementById('camera-panel').hidden")
        results["backgroundInvalidatesConfirmation"] = True
        await voice_ready()
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:voice-start'}))")
        await until("document.querySelectorAll('#observation-evidence img').length === 3 && !document.getElementById('camera-shutter').disabled")
        results["backgroundStopsCameraOnly"] = True
        await js("document.getElementById('stop').click()")
        await until("window.__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
        results["closeAndEndReleaseTracks"] = True
        # A pending permission request resolving after End cannot leave a live camera.
        await js("document.getElementById('start').click()")
        await until("document.getElementById('connection').classList.contains('online')")
        await js("window.__gumSaved=navigator.mediaDevices.getUserMedia; navigator.mediaDevices.getUserMedia=async c=>{const s=await window.__gumSaved(c); if(c.video)await new Promise(r=>window.__allowCamera=r); return s;}; document.getElementById('camera-toggle').click()")
        await until("typeof window.__allowCamera === 'function'")
        await js("document.getElementById('stop').click(); window.__allowCamera()")
        await until("window.__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
        results["latePermissionReleased"] = True
        await js("navigator.mediaDevices.getUserMedia=window.__gumSaved; document.getElementById('start').click()")
        await until("document.getElementById('connection').classList.contains('online')")
        async def typed(text):
            await js("document.getElementById('text-input').value=" + json.dumps(text) + "; document.getElementById('text-form').requestSubmit()")
        await typed("fixture:research-one")
        await until("document.querySelector('[data-id=R1] .investigation-status')?.textContent === 'Analyzing evidence'")
        await typed("fixture:research-two")
        await until("document.querySelector('[data-id=R2] .investigation-status')?.textContent.includes('Queued')")
        await typed("fixture:checklist")
        await until("document.getElementById('assistant-caption').textContent.includes('still responsive')")
        results["investigationDoesNotBlockConversation"] = True
        await js("document.querySelector('[data-id=R2]').open=true; document.querySelector('[data-id=R2] button[data-action=cancel]').click()")
        await until("document.querySelector('[data-id=R2] .investigation-status')?.textContent === 'Cancelled'")
        sockets_before_reconnect = await js("window.__sockets.length")
        await js("window.__sockets.at(-1).close()")
        await until("window.__sockets.length > " + str(sockets_before_reconnect) + " && window.__sockets.at(-1).readyState === 1 && document.getElementById('connection').classList.contains('online') && document.querySelector('[data-id=R1] .investigation-status')?.textContent === 'Analyzing evidence'")
        results["investigationReconnectSnapshot"] = True
        await js("document.getElementById('quiet').click()")
        await typed("fixture:finish")
        await until("document.querySelector('[data-id=R1] .investigation-status')?.textContent === 'Ready'")
        assert not await js("document.getElementById('assistant-caption').textContent.toLowerCase().includes('investigation r1')")
        assert await js("document.querySelector('[data-id=R1] .investigation-answer').textContent.includes('original bird')")
        await js("document.getElementById('quiet').click()")
        await until("document.getElementById('assistant-caption').textContent.includes('Would you like me to explain?')")
        assert not await js("document.getElementById('assistant-caption').textContent.includes('Fixture result')")
        await typed("fixture:explain-r1")
        await until("document.getElementById('assistant-caption').textContent.includes('Fixture result')")
        results["investigationResultsRequireConsent"] = True
        await js("document.querySelector('[data-id=R2]').open=true; document.querySelector('[data-id=R2] button[data-action=retry]').click()")
        await until("document.querySelector('[data-id=R3] .investigation-status')?.textContent === 'Ready'")
        assert await js("document.querySelector('[data-id=R3] .investigation-question').textContent === 'fixture:research-two'")
        results["investigationCancelRetryQuietAndCompletion"] = True
        shot = await call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        (OUT / "investigations-phone.png").write_bytes(base64.b64decode(shot["data"]))
        await js("document.getElementById('stop').click()")
        await js("navigator.mediaDevices.getUserMedia=window.__gumSaved; document.getElementById('start').click()")
        await until("document.getElementById('connection').classList.contains('online')")
        count = await js("window.__sockets.length")
        async with connect(f"ws://127.0.0.1:{port}/ws?session={session}&role=mic") as second:
            await second.send(json.dumps({"type": "hello"}))
            await until("!document.getElementById('reconnect').hidden")
            await asyncio.sleep(2)
            assert await js("window.__sockets.length") == count
        results["replacementStopsReconnect"] = True
        await call("Emulation.setDeviceMetricsOverride", width=1280, height=720, deviceScaleFactor=1, mobile=False)
        await call("Page.navigate", url=origin + "/?display=1&session=" + session)
        await until("document.getElementById('connection').classList.contains('online') && !document.getElementById('active-observation').hidden")
        assert await js("window.__requests.length") == 0
        assert await js("document.getElementById('display-status').textContent.includes('R3 ready')")
        assert await js("document.getElementById('investigations-section').offsetParent === null")
        assert not await js("document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight")
        shot = await call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        (OUT / "display.png").write_bytes(base64.b64decode(shot["data"]))
        results["displayObservationWithoutMedia"] = True
        await call("Emulation.setDeviceMetricsOverride", width=1920, height=1080, deviceScaleFactor=1, mobile=False)
        assert not await js("document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight")
        results["display1920Layout"] = True
        await js("window.__sockets[0].send(JSON.stringify({type:'reset'}))")
        await until("document.getElementById('active-observation').hidden && document.querySelectorAll('.observation').length === 0")
        assert await js("document.querySelectorAll('#observation-evidence img').length === 0")
        results["resetClearsObservationEvidence"] = True
        await until("document.querySelectorAll('.investigation').length === 0 && !document.getElementById('display-status').textContent.includes('R3')")
        results["resetClearsInvestigations"] = True
        errors = [e for e in events if e.get("method") == "Runtime.exceptionThrown"]
        assert not errors, errors
        results["runtimeErrors"] = 0
        (OUT / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(results, indent=2), flush=True)
        reading.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8878)
    parser.add_argument("--cdp", type=int, default=9224)
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    else:
        asyncio.run(test(args.port, args.cdp))
