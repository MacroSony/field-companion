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

        def allowed_species(self):
            return set()

        def start(self):
            self.connected = True
            self.tasks = [asyncio.create_task(self.focus_watch())]

        async def ingest_audio(self, pcm):
            pass  # The test deliberately makes no BirdNET or voice API request.

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
          window.__streams = []; window.__uploads = []; window.__requests = []; window.__sockets = [];
          window.__encodes = [];
          const encode = HTMLCanvasElement.prototype.toBlob;
          HTMLCanvasElement.prototype.toBlob = function(...args) { window.__encodes.push([this.width,this.height]); return encode.apply(this,args); };
          window.__denyCamera = false; window.__denyMic = false; window.__failUpload = false; window.__slowUpload = false;
          const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = async constraints => {
            window.__requests.push(constraints);
            if ((constraints.video && window.__denyCamera) || (constraints.audio && window.__denyMic))
              throw new DOMException('Synthetic permission denial', 'NotAllowedError');
            const stream = await gum(constraints); window.__streams.push(stream); return stream;
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
          window.WebSocket = class extends WS { constructor(...args) { super(...args); window.__sockets.push(this); } };
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
        await js("document.getElementById('camera-zoom').value='2'; document.getElementById('camera-zoom').dispatchEvent(new Event('input')); document.getElementById('camera-viewport').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        await js("document.getElementById('camera-shutter').click()")
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && !document.getElementById('camera-shutter').disabled")
        upload = await js("window.__uploads[0]")
        assert upload["type"] == "image/jpeg" and 100 <= max(upload["width"], upload["height"]) <= 1920, (upload, await js("window.__geometry"))
        assert "x=0.275000" in upload["url"] and "w=0.450000" in upload["url"], upload
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
        # Spoken Look requests open the camera and capture without a shutter click.
        await js("window.__sockets[0].send(JSON.stringify({type:'look'}))")
        await until("document.querySelectorAll('#observation-evidence img').length === 1 && !document.getElementById('camera-shutter').disabled")
        await js("Object.defineProperty(document,'hidden',{configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange'))")
        assert await js("window.__streams.filter(s=>s.getVideoTracks().length).every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
        assert await js("window.__streams[0].getAudioTracks()[0].readyState === 'live'")
        await js("delete document.hidden; window.__sockets[0].send(JSON.stringify({type:'look'}))")
        await until("document.querySelectorAll('#observation-evidence img').length === 2 && !document.getElementById('camera-shutter').disabled")
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
