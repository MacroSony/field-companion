"""A local-only configuration form; secrets never travel through a chat tool."""
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from livekit_settings import LiveKitSettings, save_local


def install_setup(app, calls_active, *, write_link=True):
    existing_routes = list(app.router.routes)
    token = secrets.token_urlsafe(32)
    app.state.livekit_setup_token = token
    output = Path(__file__).resolve().parents[1] / 'out' / 'livekit'
    if write_link:
        output.mkdir(parents=True, exist_ok=True)
        # The fragment is never sent in HTTP requests or access logs.
        (output / 'setup-fragment.txt').write_text(token, encoding='utf-8')

    def local(request, *, writing=False):
        host = urlsplit('http://' + request.headers.get('host', '')).hostname
        if request.client.host not in ('127.0.0.1', '::1', 'testclient') or host not in ('localhost', '127.0.0.1', '::1'):
            raise HTTPException(404, 'Not found')
        if writing and not secrets.compare_digest(request.headers.get('x-field-setup', ''), token):
            raise HTTPException(403, 'Open the private setup link on this computer')

    @app.get('/setup/livekit', response_class=HTMLResponse)
    async def form(request: Request):
        local(request)
        return HTMLResponse(FORM, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'"})

    @app.post('/api/livekit/setup')
    async def configure(request: Request):
        local(request, writing=True)
        if calls_active():
            raise HTTPException(409, 'End LiveKit calls before changing projects')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, 'Configuration too large')
        try:
            values = json.loads(body)
            if not isinstance(values, dict) or any(not isinstance(values.get(n), str) for n in ('url', 'api_key', 'api_secret')):
                raise ValueError('Enter the three project credentials')
            settings = LiveKitSettings(*(values[n].strip() for n in ('url', 'api_key', 'api_secret')))
            save_local(settings)
            settings.bind_inference_environment()
        except (ValueError, TypeError):
            raise HTTPException(422, 'Check the project wss:// URL, API key and full API secret') from None
        return {'ok': True, 'configured': True}

    # server.py already mounted the web shell at '/'. Insert these explicit
    # routes before that catch-all mount when this opt-in module is installed.
    added_routes = app.router.routes[len(existing_routes):]
    app.router.routes[:] = added_routes + existing_routes


FORM = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Field Companion · LiveKit setup</title><style>body{max-width:580px;margin:48px auto;padding:0 24px;font:17px system-ui;background:#11251e;color:#eef5e8}label{display:block;margin:24px 0}input{display:block;width:100%;box-sizing:border-box;padding:12px;margin-top:8px;font:inherit}button{padding:14px 24px;font:inherit;background:#d4e5a0;border:0;border-radius:8px}p{line-height:1.6}#status{white-space:pre-wrap}</style>
<h1>Connect LiveKit</h1><p>Enter the credentials for your LiveKit Cloud project. Save writes them only to this computer's ignored <code>.env.livekit.local</code> file. They are never returned to the phone.</p>
<form id="form"><label>Project WebSocket URL<input id="url" type="url" placeholder="wss://your-project.livekit.cloud" autocomplete="off" required></label>
<label>API key<input id="key" type="password" autocomplete="off" required></label>
<label>API secret<input id="secret" type="password" autocomplete="off" required></label>
<button>Save locally</button></form><p id="status" role="status"></p>
<script>document.getElementById('form').onsubmit=async event=>{event.preventDefault();const button=document.querySelector('button');button.disabled=true;try{const response=await fetch('/api/livekit/setup',{method:'POST',headers:{'Content-Type':'application/json','X-Field-Setup':location.hash.slice(1)},body:JSON.stringify({url:document.getElementById('url').value,api_key:document.getElementById('key').value,api_secret:document.getElementById('secret').value})});if(!response.ok)throw new Error(response.status===409?'End existing LiveKit calls first.':'Could not save. Check the project credentials and use the private setup link.');document.getElementById('form').reset();history.replaceState(null,'',location.pathname);document.getElementById('status').textContent='Saved. Return to Field Companion and start a new LiveKit session.';}catch(error){document.getElementById('status').textContent=error.message;}finally{button.disabled=false}};</script></html>'''
