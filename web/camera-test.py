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
    import threading
    classification_release = threading.Event()
    classification_release.set()

    class FixtureSession(backend.Session):
        def __init__(self, session_id):
            super().__init__(session_id)
            self.expert = NaturalistExpert(settings=Settings(enabled=False))
            self.names = {"Turdus migratorius": {"sci": "Turdus migratorius", "en": "American Robin"},
                          "Haliaeetus leucocephalus": {"sci": "Haliaeetus leucocephalus", "en": "Bald Eagle"}}
            self.fixture_species = "Haliaeetus leucocephalus" if session_id.startswith("new-subject-") else None
            self.research_release = asyncio.Event()
            self.worker_release = asyncio.Event()
            self.worker_release.set()

        def allowed_species(self):
            return {self.fixture_species} if self.fixture_species else set()

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
            if phase == "route" and self.turn_question == "Fixture slow reply.":
                await self.phone_json({"type": "fixture_busy"})
                await self.worker_release.wait()
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": "ignore", "call_id": "fixture-resumed", "arguments": "{}"}]}
            if phase == "route" and self.turn_question in ("What is that?", "Look again.",
                    "I found another animal. Could you help me check it out?"):
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": "look", "call_id": "fixture-look-" + uuid.uuid4().hex,
                    "arguments": "{}"}]}
            if phase == "route" and self.turn_question == "I saw another animal.":
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": "new_observation", "call_id": "fixture-new-subject-" + uuid.uuid4().hex,
                    "arguments": "{}"}]}
            if phase == "route" and self.turn_question == "Take the photo for me.":
                await self.phone_json({"type": "fixture_polite_photo_routed"})
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": "look", "call_id": "fixture-photo-" + uuid.uuid4().hex,
                    "arguments": "{}"}]}
            if phase == "route" and self.turn_question == "What do you think?":
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": "get_observation", "call_id": "fixture-progress", "arguments": "{}"}]}
            if phase == "route" and self.turn_question == "Second.":
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": "confirm_observation", "call_id": "fixture-ambiguous", "arguments": '{"count":1}'}]}
            if phase == "answer" and self.expert_readout:
                await self.phone_json({"type": "transcript", "role": "assistant",
                                       "text": self.expert_readout, "final": True})
            elif phase == "answer":
                await self.phone_json({"type": "fixture_visual_answer"})
            return {"status": "completed", "output": []}

        async def fixture_expert(self, question, observation, *, on_progress, **kwargs):
            await on_progress("analyzing")
            await self.research_release.wait()
            return {"ok": True, "status": "completed", "action": "answer",
                    "answer": "Fixture result for the original bird.", "hint": "", "sources": []}

        async def handle_phone(self, event, client=None):
            text = event.get("text", "")
            if event.get("type") == "text" and text.startswith("fixture:"):
                if text in ("fixture:voice-look", "fixture:voice-retake"):
                    await self.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "Look again." if text == "fixture:voice-retake" else "What is that?",
                        "item_id": "voice-" + uuid.uuid4().hex})
                elif text == "fixture:voice-start":
                    await self.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "Start.", "item_id": "voice-" + uuid.uuid4().hex})
                elif text == "fixture:new-subject":
                    await self.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "I found another animal. Could you help me check it out?",
                        "item_id": "voice-" + uuid.uuid4().hex})
                elif text in ("fixture:bare-new-subject", "fixture:take-photo"):
                    await self.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "I saw another animal." if text == "fixture:bare-new-subject" else "Take the photo for me.",
                        "item_id": "voice-" + uuid.uuid4().hex})
                elif text == "fixture:classify-robin":
                    self.fixture_species = "Turdus migratorius"
                elif text in ("fixture:progress", "fixture:ambiguous", "fixture:status"):
                    await self.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": {"fixture:progress": "What do you think?",
                                       "fixture:ambiguous": "Second.",
                                       "fixture:status": "Check out the identification process."}[text],
                        "item_id": "voice-" + uuid.uuid4().hex})
                elif text == "fixture:hold-photo":
                    classification_release.clear()
                elif text == "fixture:release-photo":
                    classification_release.set()
                elif text == "fixture:hold-worker":
                    self.worker_release.clear()
                    await super().handle_phone({"type": "text", "text": "Fixture slow reply."}, client)
                elif text == "fixture:release-worker":
                    self.worker_release.set()
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
        if not classification_release.wait(20):
            raise RuntimeError("Fixture classification not released")
        image = Image.open(io.BytesIO(jpeg))
        if image.format != "JPEG" or max(image.size) > 1920:
            raise ValueError("Invalid camera JPEG")
        sci = "Haliaeetus leucocephalus" if allowed and "Haliaeetus leucocephalus" in allowed else "Turdus migratorius"
        return {"candidates": [{"sci": sci, "p": .91}], "sharpness": 50}

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

        async def voice_input(text, *, earphones=False):
            # Speaker mode deliberately excludes speech during output. Match a
            # real user who waits for all browser audio and the echo tail, rather
            # than injecting final STT while a cached camera cue is playing.
            await until("document.getElementById('audio-mode').getAttribute('aria-pressed') === '" + str(earphones).lower() + "' && "
                        "window.__lastPlaybackReport?.active === false && window.__lastPlaybackReport?.drained === true && "
                        "performance.now() - window.__lastPlaybackReport.at >= 900 && "
                        "window.__messages.filter(e=>e.type==='state').at(-1)?.speaking === false")
            await js("window.__sockets[0].send(JSON.stringify({type:'text',text:" + json.dumps(text) + "}))")

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
            const send=this.send.bind(this);
            this.send=data=>{
              if(typeof data==='string') {
                const message=JSON.parse(data);
                if(message.type==='playback') window.__lastPlaybackReport={...message,at:performance.now()};
              }
              return send(data);
            };
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
        before_manual = await js("window.__messages.length")
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:hold-photo'}))")
        await js("document.getElementById('camera-shutter').click()")
        await until("document.getElementById('observation-status').textContent === 'Analyzing photo'")
        await voice_input("fixture:progress")
        await until("window.__messages.slice(" + str(before_manual) + ").some(e=>e.text===\"I'm still analyzing the photo. I'll tell you when it's ready.\")")
        assert await js("document.querySelectorAll('#observation-evidence img').length === 0")
        results["processingQueryDoesNotInventIdentification"] = True
        status_marker = await js("window.__messages.length")
        status_uploads = await js("window.__uploads.length")
        status_encodes = await js("window.__encodes.length")
        await voice_input("fixture:status")
        await until("window.__messages.slice(" + str(status_marker) + ").some(e=>e.text===\"The photo has been received; the image model is identifying the subject.\")")
        assert await js("window.__uploads.length") == status_uploads
        assert await js("window.__encodes.length") == status_encodes
        assert not await js("window.__messages.slice(" + str(status_marker) + ").some(e=>e.type==='camera_prepare' || e.type==='capture' || e.text==='Camera ready. Say start to take a photo.')")
        results["identificationProgressReadsCanonicalStateWithoutRecapture"] = True
        # Finish the image while a conversation occupies the worker, leaving
        # the existing visual answer queued behind a genuine status question.
        ready_marker = await js("window.__messages.length")
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:hold-worker'}))")
        await until("window.__messages.slice(" + str(ready_marker) + ").some(e=>e.type==='fixture_busy')")
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:release-photo'}))")
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && !document.getElementById('camera-shutter').disabled")
        assert not await js("window.__messages.slice(" + str(ready_marker) + ").some(e=>e.type==='fixture_visual_answer')")
        await voice_input("fixture:status")
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:release-worker'}))")
        await until("window.__messages.slice(" + str(ready_marker) + ").some(e=>e.text==='Photo identification is complete; the result is ready.')")
        await until("window.__messages.slice(" + str(ready_marker) + ").some(e=>e.type==='fixture_visual_answer')")
        ready_messages = await js("window.__messages.slice(" + str(ready_marker) + ")")
        status_done = next(i for i, e in enumerate(ready_messages)
                           if e.get("text") == "Photo identification is complete; the result is ready.")
        auto_answer = next(i for i, e in enumerate(ready_messages) if e.get("type") == "fixture_visual_answer")
        assert status_done < auto_answer
        assert not any(e.get("type") in ("camera_prepare", "capture")
                       or e.get("text") == "Camera ready. Say start to take a photo." for e in ready_messages)
        assert await js("window.__uploads.length") == status_uploads
        assert await js("window.__encodes.length") == status_encodes
        results["completedUnreadPhotoProgressDoesNotRestartCamera"] = True
        await until("window.__messages.slice(" + str(before_manual) + ").some(e=>e.text==='Photo ready. Starting identification.')")
        manual_messages = await js("window.__messages.slice(" + str(before_manual) + ")")
        taking = next(i for i, e in enumerate(manual_messages) if e.get("text") == "Start taking photo.")
        capture = next(i for i, e in enumerate(manual_messages) if e.get("type") == "capture")
        ready = next(i for i, e in enumerate(manual_messages) if e.get("text") == "Photo ready. Starting identification.")
        assert taking < capture < ready
        results["identifySpeaksTakingThenPhotoReady"] = True
        ambiguous_marker = await js("window.__messages.length")
        await voice_input("fixture:ambiguous")
        await until("window.__messages.slice(" + str(ambiguous_marker) + ").some(e=>e.text===\"I didn't understand. Please say that again.\")")
        assert await js("document.querySelectorAll('.counter output').length === 0")
        assert await js("document.getElementById('observation-status').textContent !== 'Added to checklist'")
        results["ambiguousSpeechCannotLogSightings"] = True
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
        async def voice_ready(*, retake=False):
            marker = await js("window.__messages.length")
            uploads = await js("window.__uploads.length")
            encodes = await js("window.__encodes.length")
            await voice_input("fixture:voice-retake" if retake else "fixture:voice-look")
            await until("window.__messages.slice(" + str(marker) + ").some(e=>e.text==='Camera ready. Say start to take a photo.') && !document.getElementById('camera-shutter').disabled")
            await asyncio.sleep(.3)
            assert await js("window.__uploads.length") == uploads
            assert await js("window.__encodes.length") == encodes

        def feedback_before_capture(start):
            messages = [json.loads(e["params"]["response"]["payloadData"]) for e in events[start:]
                        if e.get("method") == "Network.webSocketFrameReceived" and e["params"]["response"]["opcode"] == 1]
            feedback = next(i for i, e in enumerate(messages) if e.get("text") == "Start taking photo.")
            capture = next(i for i, e in enumerate(messages) if e.get("type") == "capture")
            assert feedback < capture
            assert any(e.get("type") == "audio_end" and e["id"].startswith("feedback-") for e in messages[:capture])

        before_voice = len(events)
        await voice_ready()
        voice_marker = await js("window.__messages.length")
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:hold-worker'}))")
        await until("window.__messages.slice(" + str(voice_marker) + ").some(e=>e.type==='fixture_busy')")
        await voice_input("fixture:voice-start")
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && !document.getElementById('camera-shutter').disabled")
        results["captureBypassesBlockedConversationWorker"] = True
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:release-worker'}))")
        await until("window.__messages.slice(" + str(voice_marker) + ").some(e=>e.text==='Photo ready. Starting identification.')")
        feedback_before_capture(before_voice)
        before_voice = len(events)
        await voice_ready(retake=True)
        voice_marker = await js("window.__messages.length")
        await voice_input("fixture:voice-start")
        await until("document.querySelectorAll('#observation-evidence img').length === 2 && !document.getElementById('camera-shutter').disabled")
        await until("window.__messages.slice(" + str(voice_marker) + ").some(e=>e.text==='Photo ready. Starting identification.')")
        feedback_before_capture(before_voice)
        results["voiceCameraReadyThenStartWithCameraClosedAndOpen"] = True
        results["voiceStartSpeaksTakingThenPhotoReady"] = True
        await voice_ready(retake=True)
        uploads = await js("window.__uploads.length")
        await js("Object.defineProperty(document,'hidden',{configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange'))")
        assert await js("window.__streams.filter(s=>s.getVideoTracks().length).every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
        assert await js("window.__streams[0].getAudioTracks()[0].readyState === 'live'")
        await js("delete document.hidden")
        await voice_input("fixture:voice-start")
        await asyncio.sleep(.4)
        assert await js("window.__uploads.length") == uploads
        assert await js("document.getElementById('camera-panel').hidden")
        results["backgroundInvalidatesConfirmation"] = True
        await voice_ready(retake=True)
        await voice_input("fixture:voice-start")
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
        # Reproduce the logged new-animal request against a finished Bald Eagle
        # photo. The model deliberately chooses look; the server must create a
        # fresh subject and keep the usual ready -> Start camera handshake.
        new_subject_session = "new-subject-" + uuid.uuid4().hex[:8]
        await call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
        await call("Page.navigate", url=origin + "/?session=" + new_subject_session)
        await until("document.readyState === 'complete' && typeof FieldCamera === 'function'")
        await js("document.getElementById('start').click()")
        await until("document.getElementById('connection').classList.contains('online')")
        await js("document.getElementById('audio-mode').click()")
        await voice_input("fixture:voice-look", earphones=True)
        await until("window.__messages.some(e=>e.text==='Camera ready. Say start to take a photo.') && !document.getElementById('camera-shutter').disabled")
        await voice_input("fixture:voice-start", earphones=True)
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && document.getElementById('observation-candidates').textContent.includes('Bald Eagle') && !document.getElementById('camera-shutter').disabled")
        old_subject = await js("window.__messages.filter(e=>e.type==='observation' && e.focus).at(-1).id")
        new_subject_marker = await js("window.__messages.length")
        uploads_before_new = await js("window.__uploads.length")
        encodes_before_new = await js("window.__encodes.length")
        await voice_input("fixture:new-subject", earphones=True)
        await until("window.__messages.slice(" + str(new_subject_marker) + ").some(e=>e.text==='Camera ready. Say start to take a photo.') && !document.getElementById('camera-shutter').disabled")
        new_subject = await js("window.__messages.filter(e=>e.type==='observation' && e.focus).at(-1)")
        assert new_subject["id"] != old_subject
        assert new_subject["fused"] == [] and new_subject["detections"] == []
        assert new_subject["image_revision"] == 0
        assert await js("document.querySelectorAll('#observation-evidence img').length === 0 && document.querySelectorAll('#observation-candidates li').length === 0")
        assert not await js("document.getElementById('observation-candidates').textContent.includes('Bald Eagle')")
        assert await js("window.__uploads.length") == uploads_before_new
        assert await js("window.__encodes.length") == encodes_before_new
        assert not await js("window.__messages.slice(" + str(new_subject_marker) + ").some(e=>e.type==='capture')")
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:classify-robin'}))")
        await voice_input("fixture:voice-start", earphones=True)
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && document.getElementById('observation-candidates').textContent.includes('American Robin') && !document.getElementById('camera-shutter').disabled")
        fresh_photo = await js("window.__messages.filter(e=>e.type==='observation' && e.focus).at(-1)")
        assert fresh_photo["id"] == new_subject["id"] and fresh_photo["image_revision"] == 1
        assert len(fresh_photo["detections"]) == 1
        assert await js("window.__uploads.length") == uploads_before_new + 1
        assert new_subject["id"] in await js("window.__uploads.at(-1).url")
        await js("document.getElementById('observation-confirm').click()")
        await until("document.getElementById('observation-status').textContent === 'Added to checklist'")
        assert await js("document.querySelector('.counter output').value") == "1"
        assert not await js("document.getElementById('observation-candidates').textContent.includes('Bald Eagle')")
        results["newSubjectVoiceClearsOldPhotoAndWaitsForStart"] = True
        await js("document.getElementById('stop').click()")
        await until("window.__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
        # A bare new-animal notice is enough to prepare its own camera. Once
        # that camera really is ready, a polite photo request confirms locally.
        bare_session = "new-subject-bare-" + uuid.uuid4().hex[:8]
        await call("Page.navigate", url=origin + "/?session=" + bare_session)
        await until("document.readyState === 'complete' && typeof FieldCamera === 'function'")
        await js("document.getElementById('start').click()")
        await until("document.getElementById('connection').classList.contains('online')")
        await js("if(document.getElementById('audio-mode').getAttribute('aria-pressed')!=='true')document.getElementById('audio-mode').click()")
        await voice_input("fixture:voice-look", earphones=True)
        await until("window.__messages.some(e=>e.text==='Camera ready. Say start to take a photo.') && !document.getElementById('camera-shutter').disabled")
        await voice_input("fixture:voice-start", earphones=True)
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && document.getElementById('observation-candidates').textContent.includes('Bald Eagle') && !document.getElementById('camera-shutter').disabled")
        old_bare_subject = await js("window.__messages.filter(e=>e.type==='observation' && e.focus).at(-1).id")
        bare_marker = await js("window.__messages.length")
        bare_uploads = await js("window.__uploads.length")
        bare_encodes = await js("window.__encodes.length")
        await voice_input("fixture:bare-new-subject", earphones=True)
        await until("window.__messages.slice(" + str(bare_marker) + ").some(e=>e.text==='Camera ready. Say start to take a photo.') && !document.getElementById('camera-shutter').disabled")
        bare_subject = await js("window.__messages.filter(e=>e.type==='observation' && e.focus).at(-1)")
        assert bare_subject["id"] != old_bare_subject
        assert bare_subject["fused"] == [] and bare_subject["detections"] == [] and bare_subject["image_revision"] == 0
        assert await js("document.querySelectorAll('#observation-evidence img').length === 0 && document.querySelectorAll('#observation-candidates li').length === 0")
        assert not await js("document.getElementById('observation-candidates').textContent.includes('Bald Eagle')")
        assert await js("window.__uploads.length") == bare_uploads
        assert await js("window.__encodes.length") == bare_encodes
        assert await js("window.__messages.slice(" + str(bare_marker) + ").filter(e=>e.type==='camera_prepare').length") == 1
        assert not await js("window.__messages.slice(" + str(bare_marker) + ").some(e=>e.type==='capture')")
        results["bareNewAnimalNoticePreparesFreshCameraWithoutCapture"] = True
        await js("window.__sockets[0].send(JSON.stringify({type:'text',text:'fixture:classify-robin'}))")
        polite_marker = await js("window.__messages.length")
        polite_network_marker = len(events)
        await voice_input("fixture:take-photo", earphones=True)
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && document.getElementById('observation-candidates').textContent.includes('American Robin') && !document.getElementById('camera-shutter').disabled")
        polite_messages = await js("window.__messages.slice(" + str(polite_marker) + ")")
        assert not any(e.get("type") in ("fixture_polite_photo_routed", "camera_prepare") for e in polite_messages)
        assert sum(e.get("type") == "capture" for e in polite_messages) == 1
        feedback_before_capture(polite_network_marker)
        polite_photo = await js("window.__messages.filter(e=>e.type==='observation' && e.focus).at(-1)")
        assert polite_photo["id"] == bare_subject["id"] and polite_photo["image_revision"] == 1
        assert len(polite_photo["detections"]) == 1
        assert await js("window.__uploads.length") == bare_uploads + 1
        assert bare_subject["id"] in await js("window.__uploads.at(-1).url")
        await js("document.getElementById('observation-confirm').click()")
        await until("document.getElementById('observation-status').textContent === 'Added to checklist'")
        assert await js("document.querySelector('.counter output').value") == "1"
        results["politePhotoRequestConfirmsReadyCameraWithoutModelRouting"] = True
        await js("document.getElementById('stop').click()")
        await until("window.__streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
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
