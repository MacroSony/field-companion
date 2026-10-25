/* Run explicitly: node --test web/test_audio_lab.cjs. No mic or API access. */
const test = require('node:test');
const assert = require('node:assert/strict');
const {FrameRing, wavBytes, safe, buildHTML, FieldAudioDiagnostics} = require('./audio-diagnostics.js');
const {FieldWebRTCAudio} = require('./webrtc-audio.js');

test('rolling stereo capture keeps channel alignment and a strict frame bound', () => {
  const ring = new FrameRing(3);
  ring.push(100, Int16Array.from([1,11,2,12]));
  ring.push(102, Int16Array.from([3,13,4,14]));
  assert.equal(ring.frames, 3); assert.equal(ring.chunks[0].frame, 101);
  const bytes = wavBytes(ring, 48000), view = new DataView(bytes.buffer);
  assert.equal(view.getUint16(22, true), 2); assert.equal(view.getUint32(24, true), 48000);
  assert.equal(view.getUint32(40, true), 12);
  assert.deepEqual(Array.from({length: 6}, (_, i) => view.getInt16(44+i*2, true)), [2,12,3,13,4,14]);
});

test('diagnostic metadata excludes SDP, tokens, device identifiers and location', () => {
  assert.deepEqual(safe({token: 'secret', sdp: 'secret', lat: 43, voice: {deviceId: 'secret', echoCancellation: true},
    fields: {arguments: {secret: 'x'}, response_id: 'safe'}}), {voice: {echoCancellation: true}, fields: {response_id: 'safe'}});
});

test('portable replay embeds PCM and escapes text instead of injecting HTML', () => {
  const ring = new FrameRing(1); ring.push(0, Int16Array.from([1,2]));
  const html = buildHTML(wavBytes(ring, 48000), {metadata: {}, events: [{text: '</script><img src=x onerror=alert(1)>'}]});
  assert.ok(html.includes('data:audio/wav;base64,UklGR'));
  assert.ok(!html.includes('</script><img'));
  assert.ok(html.includes('default-src &#39;none&#39;') || html.includes("default-src 'none'"));
});

test('ending a session during worklet loading cannot start a recording afterwards', async () => {
  let finish;
  const diagnostic = new FieldAudioDiagnostics();
  const context = {sampleRate: 48000, state: 'running', audioWorklet: {addModule: () => new Promise(r => { finish = r; })}};
  const pending = diagnostic.start(context, {}, {});
  diagnostic.stop(); finish(); await pending;
  assert.equal(diagnostic.active, false); assert.equal(diagnostic.node, null);
});

test('missing AEC after a failed independent environmental capture stops the experiment', async () => {
  const {rtc} = player(); let failure = false;
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  Object.defineProperty(globalThis, 'navigator', {configurable: true, value: {mediaDevices: {
    getUserMedia: async () => { throw new Error('Unsupported'); }}}});
  rtc.stream = {getAudioTracks: () => [{getConstraints: () => ({}), getSettings: () => ({echoCancellation: false}), applyConstraints: async () => {}}]};
  try { await assert.rejects(rtc.openEnvironment(), /echo cancellation/); failure = rtc.closed; }
  finally { if (originalNavigator) Object.defineProperty(globalThis, 'navigator', originalNavigator); else delete globalThis.navigator; }
  assert.equal(failure, true);
});

function player() {
  const track = {enabled: true}, values = [];
  const gain = {gain: {setValueAtTime: v => values.push(v)}, connect() {}, disconnect() {}};
  const rtc = new FieldWebRTCAudio({context: {currentTime: 0, createGain: () => gain},
    stream: {getAudioTracks: () => [track]}, output: {}, onPlayback() {}});
  return {rtc, track, values};
}

test('voice input stays disabled until control setup is ready; tap mode gates playback', () => {
  const {rtc, track} = player(); rtc.setDuplex(true); assert.equal(track.enabled, false);
  rtc.ready = true; rtc.updateMic(); assert.equal(track.enabled, true);
  rtc.playing = true; rtc.setDuplex(false); assert.equal(track.enabled, false);
  rtc.setDuplex(true); assert.equal(track.enabled, true);
  rtc.setMic(false); assert.equal(track.enabled, false);
});

test('a late output-start for a cancelled response cannot unmute a new queue', () => {
  const {rtc, values} = player();
  rtc.handle({type: 'output_audio_buffer.started', response_id: 'old'}); rtc.flush();
  rtc.handle({type: 'output_audio_buffer.started', response_id: 'old'});
  assert.equal(values.at(-1), 0); assert.equal(rtc.playing, false);
  rtc.handle({type: 'output_audio_buffer.started', response_id: 'new'});
  rtc.handle({type: 'output_audio_buffer.started', response_id: 'old'});
  rtc.handle({type: 'output_audio_buffer.stopped', response_id: 'old'});
  assert.equal(values.at(-1), 1); assert.equal(rtc.playing, true);
});
