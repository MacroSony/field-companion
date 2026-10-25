(function (root) {
  'use strict';
  const SAFE_KEYS = new Set(['sampleRate', 'channelCount', 'echoCancellation', 'noiseSuppression', 'autoGainControl',
    'transport', 'provider', 'model', 'state', 'type', 'event', 'role', 'text', 'final', 'id', 'response_id',
    'item_id', 'phase', 'reason', 'confirmation', 'hold_ms', 'active', 'drained', 'kind', 'status', 'name', 'ok',
    'enabled', 'connected', 'experimental_duplex', 'echo_guard', 'server_monotonic', 'fields', 'message',
    'audio_start_ms', 'audio_end_ms', 'playout_ms', 'environment', 'voice', 'output', 'error_type',
    'echoReturnLoss', 'echoReturnLossEnhancement', 'jitter', 'packetsLost', 'audioLevel']);
  function safe(value, depth = 0) {
    if (depth > 4) return undefined;
    if (typeof value === 'string') return value.slice(0, 1000);
    if (typeof value === 'boolean' || value === null || (typeof value === 'number' && Number.isFinite(value))) return value;
    if (!value || Array.isArray(value) || typeof value !== 'object') return undefined;
    return Object.fromEntries(Object.entries(value).filter(([key]) => SAFE_KEYS.has(key))
      .map(([key, v]) => [key, safe(v, depth + 1)]).filter(([, v]) => v !== undefined));
  }
  class FrameRing {
    constructor(maxFrames) { this.maxFrames = maxFrames; this.chunks = []; this.frames = 0; }
    push(frame, pcm) {
      if (!Number.isInteger(frame) || frame < 0 || !(pcm instanceof Int16Array) || pcm.length % 2) return;
      const row = {frame, pcm, frames: pcm.length / 2};
      this.chunks.push(row); this.frames += row.frames;
      while (this.chunks.length && this.frames > this.maxFrames) {
        const first = this.chunks[0], extra = this.frames - this.maxFrames;
        if (extra >= first.frames) { this.frames -= first.frames; this.chunks.shift(); }
        else {
          first.pcm = first.pcm.slice(extra * 2); first.frame += extra; first.frames -= extra;
          this.frames -= extra;
        }
      }
    }
  }
  function wavBytes(ring, rate) {
    const buffer = new ArrayBuffer(44 + ring.frames * 4), view = new DataView(buffer);
    const ascii = (offset, text) => [...text].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
    ascii(0, 'RIFF'); view.setUint32(4, buffer.byteLength - 8, true); ascii(8, 'WAVE'); ascii(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 2, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 4, true); view.setUint16(32, 4, true);
    view.setUint16(34, 16, true); ascii(36, 'data'); view.setUint32(40, ring.frames * 4, true);
    let offset = 44;
    for (const chunk of ring.chunks) for (const sample of chunk.pcm) { view.setInt16(offset, sample, true); offset += 2; }
    return new Uint8Array(buffer);
  }
  function base64(bytes) {
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 32768) binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
    return btoa(binary);
  }
  function buildHTML(wav, report) {
    const payload = JSON.stringify(report).replace(/</g, '\\u003c');
    return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; media-src data: blob:">
<title>Field Companion audio diagnostic</title><style>body{max-width:1000px;margin:32px auto;padding:0 20px;font:16px system-ui;background:#11251e;color:#edf5ea}audio{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}button{padding:10px;margin:8px}li{padding:6px;border-bottom:1px solid #456}</style>
<h1>Field Companion audio diagnostic</h1><p>Local recording. Left channel: captured microphone (may already have browser/system processing). Right channel: app playback reference after app volume/mute, before hardware volume. This is not an acoustic recording of the speaker.</p>
<audio id="audio" controls src="data:audio/wav;base64,${base64(wav)}"></audio>
<button id="wav">Save stereo WAV</button><button id="json">Save timeline JSON</button><pre id="meta"></pre><h2>Events in this audio window</h2><ol id="events"></ol>
<script type="application/json" id="data">${payload}</script><script>
const report=JSON.parse(document.getElementById('data').textContent),audio=document.getElementById('audio');
document.getElementById('meta').textContent=JSON.stringify(report.metadata,null,2);
function save(blob,name){const a=document.createElement('a'),url=URL.createObjectURL(blob);a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
document.getElementById('wav').onclick=()=>{const raw=atob(audio.src.split(',')[1]),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));save(new Blob([bytes],{type:'audio/wav'}),'field-audio.wav')};
document.getElementById('json').onclick=()=>save(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}),'field-timeline.json');
for(const row of report.events){const at=row.context_time-report.audio_start_context;if(at<0||at>report.audio_seconds)continue;const li=document.createElement('li'),button=document.createElement('button');button.textContent=at.toFixed(2)+'s · '+row.event;button.onclick=()=>{audio.currentTime=Math.max(0,at-.5);audio.play()};li.append(button,document.createTextNode(JSON.stringify(row.fields)));document.getElementById('events').append(li)}
</script></html>`;
  }
  class FieldAudioDiagnostics {
    constructor() { this.events = []; this.active = false; this.ring = null; this.node = null; this.revision = 0; }
    async start(context, mic, render, metadata = {}) {
      if (this.active) return;
      const revision = ++this.revision;
      this.context = context; this.ring = new FrameRing(Math.round(context.sampleRate * 60));
      this.events = []; this.started = performance.now(); this.metadata = safe(metadata);
      await context.audioWorklet.addModule('diagnostics-worklet.js');
      if (revision !== this.revision || context.state === 'closed') return;
      this.node = new AudioWorkletNode(context, 'field-diagnostic-capture', {numberOfInputs: 2, numberOfOutputs: 1, outputChannelCount: [1]});
      this.mic = mic; this.render = render;
      this.node.port.onmessage = event => {
        if (this.active) this.ring.push(event.data.frame, new Int16Array(event.data.pcm));
      };
      mic.connect(this.node, 0, 0); render.connect(this.node, 0, 1); this.node.connect(context.destination);
      this.active = true; this.record('recording_started', {sampleRate: context.sampleRate});
    }
    record(event, fields = {}) {
      if (!this.active) return;
      this.events.push({event: String(event).slice(0, 100), received_ms: performance.now() - this.started,
        context_time: this.context.currentTime, fields: safe(fields)});
      if (this.events.length > 5000) this.events.splice(0, this.events.length - 5000);
    }
    stop() {
      ++this.revision;
      if (!this.active) return;
      this.record('recording_stopped'); this.active = false;
      try { this.mic.disconnect(this.node); } catch (_) {}
      try { this.render.disconnect(this.node); } catch (_) {}
      this.node?.disconnect(); this.node = null;
    }
    clear() { this.stop(); this.ring = null; this.events = []; }
    exportHTML() {
      if (!this.ring?.frames) throw new Error('No audio has been recorded yet.');
      const rate = this.context.sampleRate, first = this.ring.chunks[0].frame;
      return buildHTML(wavBytes(this.ring, rate), {version: 1, metadata: this.metadata,
        audio_start_context: first / rate, audio_seconds: this.ring.frames / rate, events: this.events,
        notes: ['Rolling last 60 seconds only', 'Event timestamps are client receipt times; server clock is separate',
                'Microphone is not guaranteed raw; inspect capture settings', 'No audio or diagnostic file was uploaded by this recorder']});
    }
    download() {
      const blob = new Blob([this.exportHTML()], {type: 'text/html;charset=utf-8'}), url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = 'field-audio-diagnostic.html';
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    }
  }
  root.FieldAudioDiagnostics = FieldAudioDiagnostics;
  if (typeof module !== 'undefined') module.exports = {FrameRing, wavBytes, safe, buildHTML, FieldAudioDiagnostics};
})(typeof window !== 'undefined' ? window : globalThis);
