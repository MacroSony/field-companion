'use strict';
(() => {
  const CLIENT_BUILD = '2026-10-25-voice-modes';
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const displayMode = params.get('display') === '1';
  const webrtcMode = params.get('transport') === 'webrtc';
  const livekitMode = params.get('transport') === 'livekit';
  const managedAudio = webrtcMode || livekitMode;
  let webrtcDuplex = params.get('duplex') !== '0', rtc = null, diagnosticBusy = false;
  const diagnostic = new FieldAudioDiagnostics();
  const audioEnabled = !displayMode || params.get('audio') === '1';
  const session = params.get('session') || (livekitMode ? 'livekit-experiment' : webrtcMode ? 'webrtc-experiment' : 'demo');
  document.body.classList.toggle('display-mode', displayMode);
  const state = {
    running: false, starting: false, connected: false, paused: false, quiet: false,
    listening: false, speaking: false, items: [], heard: [], investigations: [], coords: null,
    startedAt: null, endedAt: null, model: '', retry: 0
  };
  let earphones = false, assistantMuted = false, assistantGain = null;
  try {
    earphones = localStorage.getItem('field-earphones') === 'true';
  } catch (_) { /* Storage may be unavailable; speaker is the safe default. */ }
  let selectedMic = {deviceId: '', label: 'Default'};
  try {
    const saved = JSON.parse(localStorage.getItem('field-microphone'));
    if (typeof saved?.deviceId === 'string' && typeof saved?.label === 'string') selectedMic = saved;
  } catch (_) { /* Storage is optional. */ }
  let micSwitching = false, micRevision = 0, deviceRevision = 0;
  let enrollmentTimer = null, enrollmentBusy = false;
  $('voice-enrollment').hidden = displayMode;
  let silentAudio, silentURL, toastTimer;
  const headsetActions = ['play', 'pause', 'nexttrack', 'previoustrack'];
  let playbackReported = false, headsetEnabled = false, replaced = false;
  let playbackReport = '', playbackId = null, playbackComplete = true, playbackContext = null;
  let locationWatch = null, lastSentCoords = null;
  let socket, reconnectTimer, context, stream, capture, micSource, wakeLock, replyHintTimer;
  let nextPlaybackTime = 0, lastMeterAt = 0, resampler = null;
  const playback = new Set(), tones = new Set();
  const bars = document.querySelector('.soundscape');
  const nativeLittleEndian = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;
  const camera = new FieldCamera({displayMode, session, send,
    connected: () => state.running && socket?.readyState === WebSocket.OPEN && !replaced, backendURL: websocketURL});

  function notice(message) {
    $('notice').textContent = message;
    $('notice').hidden = !message;
  }
  function send(message) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      notice('Not connected. Your checklist stays on screen; reconnecting…');
      return false;
    }
    socket.send(JSON.stringify(message));
    return true;
  }
  function updateControls() {
    camera.updateConnection();
    $('enroll-voice').disabled = displayMode || !state.connected || !stream || state.paused || enrollmentBusy || !state.speakerEnabled;
    $('voice-profile').disabled = enrollmentBusy;
    $('conversation-mode').disabled = displayMode || !state.connected || !livekitMode;
    $('conversation-mode').value = state.addressingMode || (livekitMode ? 'scout' : 'ambient');
    $('conversation-mode-help').textContent = !livekitMode
      ? 'Standard audio uses ambient routing. Choose LiveKit voice to switch between both conversation modes.'
      : state.addressingMode === 'ambient'
      ? 'Natural conversation. Uncertain nature questions become silent cards; tap Answer to continue. Say Scout to make an explicit request or interrupt.'
      : 'Say Scout before a new request. Replies to the current question can omit the name.';
    $('turn-recording').disabled = displayMode || !state.connected || !stream;
    $('turn-recording').checked = !!state.recordingEnabled;
    $('turn-recording-status').textContent = state.recordingEnabled
      ? 'Recording voice clips and transcripts on the computer running the app. Turn this off to stop saving.'
      : 'Off. No input voice files are saved. Voice matching, when enabled, only keeps a short buffer in memory.';
    if ($('audio-mode')) {
      $('audio-mode').disabled = livekitMode;
      $('audio-mode').textContent = livekitMode ? (state.addressingMode === 'ambient' ? 'Ambient · silent question cards' : 'Scout · say name to talk') : webrtcMode ? (webrtcDuplex ? 'WebRTC · voice interruption on (experimental)' : 'WebRTC · tap to interrupt')
        : earphones ? 'Earphones · voice interruption on' : 'Speaker · tap to interrupt';
      $('audio-mode').setAttribute('aria-pressed', String(webrtcMode ? webrtcDuplex : earphones));
    }
    if ($('assistant-mute')) {
      $('assistant-mute').disabled = !audioEnabled;
      $('assistant-mute').setAttribute('aria-pressed', String(assistantMuted));
    }
    if ($('interrupt-hint')) $('interrupt-hint').textContent = livekitMode
      ? (state.replyWithoutName && Date.now() < state.replyWithoutNameUntil ? 'You can answer this question without saying Scout' : state.addressingMode === 'ambient' ? 'Talk naturally; say “Scout, …” to interrupt' : 'Say “Scout, …” to talk or interrupt')
      : (webrtcMode ? webrtcDuplex : earphones) ? 'Say something to interrupt' : 'Tap to interrupt; speak after the reply';
    if ($('earphone-tip')) $('earphone-tip').hidden = false;
    $('tap').disabled = $('send').disabled = !state.connected;
    $('reconnect').hidden = !replaced;
    $('reset').disabled = $('reset-display').disabled = !state.connected;
    $('mic').disabled = !stream || !state.running;
    if ($('mic-device')) $('mic-device').disabled = managedAudio || !capture || micSwitching || state.starting;
    if ($('diagnostics-record')) {
      $('diagnostics-record').disabled = !state.running || !micSource || !state.diagnosticsSupported || diagnosticBusy;
      $('diagnostics-record').textContent = diagnostic.active ? 'Stop recording' : 'Record audio diagnostic';
      $('diagnostics-save').disabled = !diagnostic.ring?.frames;
      $('diagnostics-clear').disabled = !diagnostic.ring;
    }
    $('mic').setAttribute('aria-pressed', String(state.paused));
    $('mic').setAttribute('aria-label', state.paused ? 'Resume mic' : 'Pause mic');
    $('mic-label').textContent = state.paused ? 'Resume mic' : 'Pause mic';
    $('mic-icon').textContent = state.paused ? '▷' : 'Ⅱ';
    $('start').hidden = state.running || replaced;
    $('start-note').textContent = assistantMuted ? 'Assistant muted · microphone and captions stay on' : 'Microphone needs HTTPS or localhost';
    $('start-note').hidden = state.running && !assistantMuted;
    $('stop').hidden = !state.running;
    $('start').disabled = state.starting;
    const playing = playback.size > 0 || state.speaking || rtc?.playing;
    document.body.classList.toggle('session-active', state.running);
    document.body.classList.toggle('running', state.running && !!stream && !state.paused);
    document.body.classList.toggle('speaking', playing);
    $('activity').textContent = !state.running ? 'Ready when you are' : playing ? 'Speaking' : !stream ? (displayMode ? 'Live session display' : 'Text only') : state.paused ? 'Mic paused' : state.connected && state.listening ? 'Listening' : state.connected ? 'Mic on' : 'Reconnecting';
    $('model').textContent = state.model || 'Not connected';
    const connection = $('connection');
    connection.className = `connection ${state.connected ? 'online' : state.running ? 'connecting' : ''}`;
    connection.lastElementChild.textContent = state.connected ? 'Connected' : state.running ? 'Connecting' : 'Offline';
    if ($('display-status')) {
      const job = state.investigations.find(item => item.status === 'running')
        || state.investigations.find(item => item.status === 'queued')
        || state.investigations[state.investigations.length - 1];
      const status = {running: 'investigating', queued: 'queued', completed: 'ready', cancelled: 'cancelled', failed: 'failed', timed_out: 'time limit'};
      $('display-status').textContent = `${state.connected ? '● Live' : '○ Connecting'} · ${playing ? 'Assistant speaking' : 'Session ' + session}${job ? ` · ${job.id} ${status[job.status] || ''}` : ''}`;
    }
    document.querySelectorAll('.counter button, .delete-button, .investigation-actions button, .silent-note-answer').forEach(button => { button.disabled = !state.connected; });
  }

  function saveMic(deviceId, label) {
    selectedMic = {deviceId, label: label || (deviceId ? 'Microphone' : 'Default')};
    try { localStorage.setItem('field-microphone', JSON.stringify(selectedMic)); } catch (_) {}
  }
  function captureSettings() {
    const line = $('capture-settings');
    if (!line) return;
    const track = stream?.getAudioTracks()[0];
    const rate = context?.sampleRate ?? 'unavailable';
    if (!track) { line.textContent = `Capture: ${micSwitching ? 'switching…' : 'off'} · AudioContext: ${rate} Hz`; return; }
    const settings = track.getSettings();
    const value = key => settings[key] === undefined ? 'not reported' : String(settings[key]);
    line.textContent = `${track.label || 'Microphone'} · sampleRate: ${value('sampleRate')} Hz · channelCount: ${value('channelCount')} · echoCancellation: ${value('echoCancellation')} · noiseSuppression: ${value('noiseSuppression')} · autoGainControl: ${value('autoGainControl')} · deviceId: ${value('deviceId')} · AudioContext: ${rate} Hz`;
  }
  async function requestMic(deviceId) {
    const audio = {echoCancellation: managedAudio ? {exact: true} : true,
      noiseSuppression: managedAudio, autoGainControl: managedAudio, channelCount: 1, sampleRate: 48000};
    try {
      return {input: await navigator.mediaDevices.getUserMedia({audio: {...audio, ...(deviceId ? {deviceId: {exact: deviceId}} : {})}}), deviceId};
    } catch (error) {
      if (!deviceId || !['NotFoundError', 'OverconstrainedError'].includes(error.name)) throw error;
      notice('Selected microphone unavailable; using the default.');
      return {input: await navigator.mediaDevices.getUserMedia({audio}), deviceId: ''};
    }
  }
  async function refreshDevices() {
    if (displayMode || !navigator.mediaDevices?.enumerateDevices) return;
    const revision = ++deviceRevision;
    try {
      const devices = (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === 'audioinput');
      if (revision !== deviceRevision) return;
      const missing = selectedMic.deviceId && !devices.some(device => device.deviceId === selectedMic.deviceId);
      if (missing) saveMic('', 'Default');
      const select = $('mic-device');
      if (select) {
        select.replaceChildren(new Option('System default', ''));
        devices.filter(device => device.deviceId).forEach((device, index) => {
          select.add(new Option(device.label || `Microphone ${index + 1}`, device.deviceId));
        });
        select.value = selectedMic.deviceId;
      }
      const selected = devices.find(device => device.deviceId === selectedMic.deviceId);
      if (selected?.label) saveMic(selected.deviceId, selected.label);
      const actualId = stream?.getAudioTracks()[0]?.getSettings().deviceId;
      if (state.running && capture && !micSwitching && (missing || (actualId && !devices.some(device => device.deviceId === actualId)))) {
        notice('Microphone disconnected; switching to the default.');
        await switchMic('');
      }
      captureSettings(); updateControls();
    } catch (error) { console.warn('Microphone list:', error); }
  }
  async function switchMic(deviceId) {
    if (!state.running || !capture || micSwitching) return;
    const revision = ++micRevision, activeContext = context, activeCapture = capture;
    micSwitching = true;
    captureSettings(); updateControls();
    let replacement, replacementSource;
    try {
      const result = await requestMic(deviceId);
      replacement = result.input;
      if (revision !== micRevision || !state.running || context !== activeContext) {
        result.input.getTracks().forEach(track => track.stop()); return;
      }
      replacementSource = activeContext.createMediaStreamSource(replacement);
      replacementSource.connect(activeCapture);
      micSource?.disconnect();
      stream?.getTracks().forEach(track => track.stop());
      stream = replacement; micSource = replacementSource;
      replacement = replacementSource = null;
      stream.getAudioTracks().forEach(track => { track.enabled = !state.paused; });
      resampler = activeContext.sampleRate === 48000 ? null : new LinearResampler(activeContext.sampleRate);
      saveMic(result.deviceId, result.deviceId ? stream.getAudioTracks()[0].label : 'Default');
      if (state.connected) send({type: 'mic', on: !state.paused});
      notice(result.deviceId !== deviceId ? 'Selected microphone unavailable; using the default.' : 'Microphone changed.');
    } catch (error) {
      replacementSource?.disconnect();
      replacement?.getTracks().forEach(track => track.stop());
      if (revision === micRevision) {
        notice(`Microphone: ${error.message}. ${stream ? 'Your previous microphone is still active.' : 'Choose another input or use text.'}`);
      }
    } finally {
      if (revision === micRevision) {
        micSwitching = false;
        captureSettings(); updateControls();
        if ($('mic-device')) $('mic-device').value = selectedMic.deviceId;
      }
    }
  }
  function headsetToast(message) {
    const toast = $('headset-toast');
    if (!toast) return;
    clearTimeout(toastTimer); toast.textContent = message; toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
  }
  function talk() {
    if (context && context.state !== 'running') context.resume().catch(() => {});
    if (livekitMode) rtc?.resumeOutput().catch(() => notice('Tap again to enable voice audio.'));
    flush(); send({type: 'tap'});
  }
  function startHeadsetSession() {
    if (displayMode || !headsetEnabled || silentAudio || !navigator.mediaSession) return;
    try {
      // Real zero-valued PCM, not muted media: no WebAudio connections or mic processing.
      // Exceed Chromium's short-media threshold so Android can expose media controls.
      const rate = 48000, samples = rate * 6, wav = new ArrayBuffer(44 + samples * 2), view = new DataView(wav);
      const text = (at, value) => [...value].forEach((char, i) => view.setUint8(at + i, char.charCodeAt(0)));
      text(0, 'RIFF'); view.setUint32(4, 36 + samples * 2, true); text(8, 'WAVE');
      text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
      view.setUint16(22, 1, true); view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
      view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, samples * 2, true);
      silentURL = URL.createObjectURL(new Blob([wav], {type: 'audio/wav'}));
      silentAudio = $('headset-audio') || document.createElement('audio');
      silentAudio.loop = true; silentAudio.src = silentURL;
      if (window.MediaMetadata) navigator.mediaSession.metadata = new MediaMetadata({title: 'Field Companion — listening'});
      for (const action of headsetActions) {
        try {
          navigator.mediaSession.setActionHandler(action, () => {
            if (!state.running) return;
            talk(); headsetToast('Headset button → talk');
            // Pause is a talk gesture here, not a command to pause the silent session.
            navigator.mediaSession.playbackState = 'playing';
            silentAudio?.play().catch(() => {});
          });
        } catch (_) { /* Individual actions are not supported by every browser. */ }
      }
      navigator.mediaSession.playbackState = 'playing';
      silentAudio.play().catch(error => {
        console.warn('Headset media session:', error);
        if (state.running || state.starting) headsetToast('Headset playback blocked; use Tap to talk.');
      });
    } catch (error) { console.warn('Headset media session:', error); }
  }
  function stopHeadsetSession() {
    silentAudio?.pause(); silentAudio?.removeAttribute('src'); silentAudio?.load(); silentAudio = null;
    if (silentURL) URL.revokeObjectURL(silentURL);
    silentURL = null;
    if (navigator.mediaSession) {
      for (const action of headsetActions) {
        try { navigator.mediaSession.setActionHandler(action, null); } catch (_) {}
      }
      navigator.mediaSession.playbackState = 'none'; navigator.mediaSession.metadata = null;
    }
    clearTimeout(toastTimer);
    if ($('headset-toast')) $('headset-toast').hidden = true;
  }

  // Stateful linear interpolation: preserve phase and the last sample across chunks.
  class LinearResampler {
    constructor(rate) { this.step = rate / 48000; this.phase = 0; this.tail = null; }
    process(input) {
      const data = new Int16Array(input.length + (this.tail === null ? 0 : 1));
      if (this.tail !== null) { data[0] = this.tail; data.set(input, 1); } else data.set(input);
      const result = [];
      let p = this.phase;
      while (p < data.length - 1) {
        const i = Math.floor(p), f = p - i;
        result.push(Math.round(data[i] * (1 - f) + data[i + 1] * f));
        p += this.step;
      }
      this.phase = p - (data.length - 1);
      this.tail = data[data.length - 1];
      return Int16Array.from(result);
    }
  }
  function pcmLittleEndian(samples) {
    if (nativeLittleEndian) return samples.buffer;
    const bytes = new ArrayBuffer(samples.length * 2), view = new DataView(bytes);
    samples.forEach((value, i) => view.setInt16(i * 2, value, true));
    return bytes;
  }
  function onCapture(event) {
    const samples = new Int16Array(event.data);
    const now = performance.now();
    if (now - lastMeterAt > 65) {
      let sum = 0;
      for (const value of samples) sum += (value / 32768) ** 2;
      const level = state.paused ? 0 : Math.min(1, Math.sqrt(sum / samples.length) * 7);
      $('level-fill').style.width = `${level * 100}%`;
      $('mic-meter').setAttribute('aria-valuenow', String(Math.round(level * 100)));
      bars.style.setProperty('--level', level.toFixed(3));
      lastMeterAt = now;
    }
    if (!state.running || state.paused || !state.connected) return;
    if (managedAudio) return; // Voice travels over RTP; never duplicate it as PCM.
    // Drop rather than buffer stale microphone audio on a slow tunnel.
    if (socket.bufferedAmount > 192000) return;
    const outgoing = resampler ? resampler.process(samples) : samples;
    if (outgoing.length) socket.send(pcmLittleEndian(outgoing));
  }
  function reportPlayback(active, force = false) {
    if (livekitMode) return; // RoomIO is the authoritative playout owner.
    const report = {type: 'playback', active, response_id: playbackId, drained: playbackComplete && !playback.size && !rtc?.playing};
    const encoded = JSON.stringify(report);
    if (!force && playbackReport === encoded) return;
    playbackReported = active;
    playbackReport = encoded;
    if (state.connected && socket?.readyState === WebSocket.OPEN) socket.send(encoded);
  }
  // WebAudio's clock, not receipt of a network chunk, determines actual start.
  setInterval(() => {
    const localPlaying = context?.state === 'running' && [...playback].some(source => source.playAt <= context.currentTime);
    rtc?.setAuxPlayback(!!localPlaying);
    if (state.connected) reportPlayback(!!(localPlaying || rtc?.playing));
  }, 20);
  function flush() {
    diagnostic.record('playback_flush', {response_id: playbackId});
    rtc?.flush();
    const hadPlayback = playback.size > 0 || playbackReported || !playbackComplete;
    for (const source of playback) { try { source.stop(); } catch (_) {} }
    for (const tone of tones) { try { tone.stop(); } catch (_) {} }
    playback.clear(); tones.clear();
    playbackComplete = true;
    if (hadPlayback) reportPlayback(false, true);
    playbackId = null; playbackReport = '';
    nextPlaybackTime = context?.currentTime || 0;
    playbackContext = context;
    state.speaking = false;
    updateControls();
  }
  function assistantOutput() {
    if (!assistantGain) {
      assistantGain = context.createGain();
      assistantGain.gain.value = assistantMuted ? 0 : 1;
      assistantGain.connect(context.destination);
    }
    return assistantGain;
  }
  function playPCM(bytes) {
    if (!audioEnabled || !context || !state.running || bytes.byteLength < 2) return;
    const count = Math.floor(bytes.byteLength / 2);
    const buffer = context.createBuffer(1, count, 24000);
    const channel = buffer.getChannelData(0), view = new DataView(bytes);
    for (let i = 0; i < count; i++) channel[i] = view.getInt16(i * 2, true) / 32768;
    const source = context.createBufferSource();
    source.buffer = buffer; // WebAudio resamples 24 kHz to the shared context rate.
    source.connect(assistantOutput());
    // Reset only when the AudioContext changes. A long queue in the same clock
    // is valid; resetting it would overlap the next chunk with earlier speech.
    if (playbackContext !== context) {
      playbackContext = context;
      nextPlaybackTime = context.currentTime;
    }
    const at = Math.max(context.currentTime + 0.025, nextPlaybackTime);
    nextPlaybackTime = at + buffer.duration;
    source.playAt = at;
    playback.add(source);
    reportPlayback(playbackReported);
    source.onended = () => {
      const removed = playback.delete(source);
      source.disconnect();
      if (removed && !playback.size) reportPlayback(false, true);
      updateControls();
    };
    source.start(at);
    updateControls();
    if (context.state === 'suspended') notice('Tap to resume audio.');
  }
  function earcon(kind) {
    if (displayMode || !context || !state.running || !['logged', 'heard', 'error'].includes(kind)) return;
    const now = context.currentTime + 0.005;
    const notes = kind === 'logged' ? [[660, 0, .12], [990, .12, .23]] : kind === 'heard' ? [[1400, 0, .11]] : [[130, 0, .23]];
    for (const [frequency, delay, duration] of notes) {
      const oscillator = context.createOscillator(), gain = context.createGain();
      oscillator.type = kind === 'error' ? 'triangle' : 'sine';
      oscillator.frequency.setValueAtTime(frequency, now + delay);
      if (kind === 'heard') oscillator.frequency.exponentialRampToValueAtTime(2100, now + duration);
      gain.gain.setValueAtTime(0, now + delay);
      gain.gain.linearRampToValueAtTime(.055, now + delay + .012);
      gain.gain.exponentialRampToValueAtTime(.0001, now + delay + duration);
      oscillator.connect(gain).connect(assistantOutput());
      tones.add(oscillator);
      oscillator.onended = () => { tones.delete(oscillator); oscillator.disconnect(); gain.disconnect(); };
      oscillator.start(now + delay); oscillator.stop(now + delay + duration + .01);
    }
  }
  async function requestWakeLock() {
    if (!state.running || document.visibilityState !== 'visible' || !navigator.wakeLock || wakeLock) return;
    try {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
      if (!state.running) { await wakeLock.release(); wakeLock = null; }
    } catch (_) { /* Unsupported or denied: audio/text still work while the screen stays on. */ }
  }
  function websocketURL() {
    const url = new URL(params.get('ws') || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`, location.href);
    url.searchParams.set('role', displayMode ? 'display' : 'mic');
    url.searchParams.set('session', session);
    if (displayMode) url.searchParams.set('audio', audioEnabled ? '1' : '0');
    if (managedAudio) url.searchParams.set('transport', livekitMode ? 'livekit' : 'webrtc');
    return url.href;
  }
  function helloMessage() {
    return {type: 'hello', client_build: CLIENT_BUILD, lang: 'en', playback_protocol: livekitMode ? 0 : 1,
      ...(!displayMode ? {earphones, ...(webrtcMode ? {duplex: webrtcDuplex} : {})} : {}), ...(state.coords || {})};
  }
  function connect() {
    if (!state.running || replaced) return;
    clearTimeout(reconnectTimer);
    updateControls();
    let ws;
    try { ws = new WebSocket(websocketURL()); } catch (error) { notice(`WebSocket: ${error.message}`); scheduleReconnect(); return; }
    socket = ws;
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      if (socket !== ws || !state.running) { ws.close(); return; }
      // hello MUST be the first frame; geolocation is optional and never blocks it.
      ws.send(JSON.stringify(helloMessage()));
      state.connected = true; state.retry = 0;
      lastSentCoords = state.coords ? {...state.coords} : null;
      if (audioEnabled) send({type: 'output_mute', muted: assistantMuted});
      if (!displayMode) {
        if (webrtcMode) send({type: 'duplex', duplex: webrtcDuplex});
        else send({type: 'audio_mode', earphones});
        send({type: 'mic', on: !!stream && !state.paused});
        send({type: 'mode', quiet: state.quiet});
      }
      notice(displayMode || stream ? '' : 'Microphone unavailable; use the text field.');
      updateControls();
    };
    ws.onmessage = event => {
      if (socket !== ws || !state.running) return;
      if (event.data instanceof ArrayBuffer) { if (!livekitMode) playPCM(event.data); return; }
      try { handleMessage(JSON.parse(event.data)); } catch (error) { console.warn('Ignored malformed server message', error); }
    };
    ws.onerror = () => { if (socket === ws) notice('Connection unavailable. Retrying automatically.'); };
    ws.onclose = event => {
      if (socket !== ws) return;
      if (isReplacement(event.reason)) { phoneReplaced(); return; }
      enrollmentStatus({status: 'error', error: 'Disconnected; capture cancelled.'});
      state.connected = false; state.listening = false; flush(); updateControls();
      if (managedAudio) {
        rtc?.close(); rtc = null;
        if (state.running) notice(event.reason || 'Voice disconnected. End and restart the session.');
        return;
      }
      if (state.running) scheduleReconnect();
    };
  }
  function scheduleReconnect() {
    if (!state.running || replaced) return;
    const delay = Math.min(15000, 800 * 2 ** Math.min(state.retry++, 5)) + Math.random() * 350;
    notice(`Reconnecting in ${Math.ceil(delay / 1000)} seconds. Checklist edits wait for connection.`);
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, delay);
    updateControls();
  }
  function isReplacement(value) {
    return /replac|new mic joined|another (?:phone|mic)|newer microphone|taken over|took over/i.test(String(value || ''));
  }
  function phoneReplaced() {
    replaced = true; clearTimeout(reconnectTimer);
    stop();
    notice('');
    updateControls();
  }
  function clearSessionUI() {
    enrollmentStatus({status: 'error', error: 'Session reset; capture cancelled.'});
    for (const card of $('silent-notes').children) clearTimeout(card.expiryTimer);
    $('silent-notes').replaceChildren();
    flush(); camera.reset(); state.items = []; state.heard = []; state.investigations = [];
    renderChecklist(); renderSuggestions([]); renderCandidates([]); renderInvestigations(); updateMap();
    $('heard').replaceChildren(element('p', 'empty-inline', 'Listening for the next discovery.'));
    $('user-caption').textContent = 'Say what you see, or ask about a bird song.';
    $('assistant-caption').textContent = 'Ready for your next discovery.';
    $('user-caption').classList.remove('partial'); $('assistant-caption').classList.remove('partial');
    $('text-input').value = '';
    state.startedAt = state.running ? new Date() : null; state.endedAt = null;
    $('session-time').textContent = '00:00';
  }
  function resetSession() {
    if (!state.connected || !confirm('Clear checklist, suggestions, heard history, investigations and conversation?')) return;
    send({type: 'reset'}); clearSessionUI();
  }
  function enrollmentStatus(message) {
    clearInterval(enrollmentTimer); enrollmentTimer = null;
    enrollmentBusy = ['capturing', 'processing'].includes(message.status);
    const seconds = Number(message.duration_s || 0).toFixed(1);
    $('enroll-progress').textContent = message.status === 'capturing' ? 'Read aloud: 0 / 20 s'
      : message.status === 'processing' ? `${seconds} s captured. Processing…`
      : message.status === 'ok' ? `${seconds} s captured. Voice enrolled.`
      : `${seconds} s captured. ${message.error || 'Enrollment cancelled.'}`;
    if (message.status === 'capturing') {
      const started = Date.now();
      enrollmentTimer = setInterval(() => {
        const elapsed = Math.min(20, (Date.now() - started) / 1000);
        $('enroll-progress').textContent = `Read aloud: ${elapsed.toFixed(0)} / 20 s`;
        if (elapsed >= 20) {
          clearInterval(enrollmentTimer); enrollmentTimer = null;
          send({type: 'enroll_stop'}); // Server also auto-stops; duplicate stop is harmless.
        }
      }, 250);
    }
    updateControls();
  }
  function handleMessage(message) {
    if (message.type === 'diagnostic') diagnostic.record(message.event, message);
    else if (['audio_start', 'audio_end', 'flush', 'state', 'error', 'earcon'].includes(message.type)
        || (message.type === 'transcript' && message.final)) diagnostic.record(message.type, message);
    if (['state', 'error'].includes(message.type) && (message.replaced === true ||
        [message.message, message.reason, message.status, message.code].some(isReplacement))) { phoneReplaced(); return; }
    switch (message.type) {
      case 'webrtc_ready':
        if (webrtcMode && !displayMode && rtc) {
          const active = rtc;
          active.connect(message.token).catch(error => {
            if (rtc === active && state.running) {
              diagnostic.record('rtc_setup_failed', {error_type: error.name});
              stop(); notice(error.message);
            }
          });
        }
        break;
      case 'livekit_ready':
        if (livekitMode && !displayMode && rtc) {
          const active = rtc;
          active.connect(message.token).catch(error => {
            if (rtc === active && state.running) { stop(); notice(error.message); }
          });
        }
        break;
      case 'audio_start':
        if (audioEnabled) { playbackId = message.id; playbackComplete = false; reportPlayback(playbackReported); }
        break;
      case 'audio_end':
        if (audioEnabled && message.id === playbackId) { playbackComplete = true; reportPlayback(playbackReported); }
        break;
      case 'enrollment': if (!displayMode) enrollmentStatus(message); break;
      case 'capture': camera.requestCapture(message); break;
      case 'camera_prepare': camera.prepare(message); break;
      case 'camera_cancelled': camera.cancelled(message); break;
      case 'observation': camera.observe(message); break;
      case 'reset': clearSessionUI(); break;
      case 'flush': flush(); break;
      case 'earcon': earcon(message.kind); break;
      case 'checklist':
        if (Array.isArray(message.items)) {
          const previous = new Map(state.items.map(item => [item.id, JSON.stringify(item)]));
          state.items = message.items; renderChecklist(previous); updateMap();
        }
        break;
      case 'heard': renderHeard(message); break;
      case 'suggestions': if (Array.isArray(message.items)) renderSuggestions(message.items); break;
      case 'investigations':
        if (Array.isArray(message.items)) { state.investigations = message.items; renderInvestigations(); updateControls(); }
        break;
      case 'silent_note': showSilentNote(message); break;
      case 'silent_note_removed': removeSilentNote(message.id); break;
      case 'transcript': {
        const target = message.role === 'user' ? $('user-caption') : message.role === 'assistant' ? $('assistant-caption') : null;
        if (target && typeof message.text === 'string') {
          target.textContent = message.text;
          target.classList.toggle('partial', message.final === false);
        }
        break;
      }
      case 'candidates': if (Array.isArray(message.items)) renderCandidates(message.items); break;
      case 'state':
        if (managedAudio && rtc) {
          if (livekitMode ? message.media_connected : message.connected) rtc.controlConnected = true;
          else if (rtc.controlConnected) { stop(); notice('Voice control disconnected. Start a new session to reconnect.'); return; }
        }
        state.diagnosticsSupported = !!message.features?.audio_diagnostics;
        if ($('webrtc-link')) $('webrtc-link').hidden = !message.features?.webrtc;
        if ($('livekit-link')) $('livekit-link').hidden = !message.features?.livekit;
        if (livekitMode) rtc?.handleState(message);
        if (typeof message.server_build === 'string') $('version-banner').hidden = message.server_build === CLIENT_BUILD;
        state.listening = !!message.listening; state.speaking = !!message.speaking;
        state.addressingMode = message.addressing_mode || (livekitMode ? 'scout' : 'ambient');
        state.recordingEnabled = !!message.recording_enabled;
        state.speakerEnabled = !!message.speaker_enabled;
        state.replyWithoutName = !!message.reply_without_name;
        state.replyWithoutNameUntil = Date.now() + Math.min(40, Math.max(0, Number(message.reply_without_name_for) || 0)) * 1000;
        clearTimeout(replyHintTimer);
        if (state.replyWithoutName) replyHintTimer = setTimeout(updateControls, Math.max(0, state.replyWithoutNameUntil - Date.now()) + 10);
        if (typeof message.model === 'string') state.model = message.model;
        // Socket transport status remains independent of backend/model connectivity.
        const connectionNotice = livekitMode ? (displayMode ? 'Waiting for the phone voice connection.' : 'Connecting to LiveKit voice…') : 'The server is reconnecting to the voice model.';
        if (message.connected === false) notice(connectionNotice);
        else if (message.connected && $('notice').textContent === connectionNotice) notice('');
        updateControls(); break;
      case 'error': notice(String(message.message || 'Unknown server error')); earcon('error'); break;
      case 'reply_retry': notice(String(message.message || 'Please say Scout and repeat that reply.')); break;
    }
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function removeSilentNote(id) {
    const card = [...$('silent-notes').children].find(node => node.dataset.id === id);
    if (card) { clearTimeout(card.expiryTimer); card.remove(); }
  }
  function showSilentNote(message) {
    if (typeof message.id !== 'string' || typeof message.question !== 'string') return;
    removeSilentNote(message.id);
    const list = $('silent-notes');
    while (list.children.length >= 3) removeSilentNote(list.firstElementChild.dataset.id);
    const card = element('div', 'silent-note');
    card.dataset.id = message.id;
    // textContent, never HTML: the original transcript may contain arbitrary text.
    card.append(element('p', '', `Not sure that was for me: “${message.question}”`));
    if (typeof message.note === 'string' && message.note) card.title = message.note;
    const answer = element('button', 'silent-note-answer', 'Answer');
    answer.type = 'button'; answer.disabled = !state.connected;
    answer.addEventListener('click', () => {
      if (send({type: 'answer_note', id: message.id})) removeSilentNote(message.id);
    });
    const dismiss = element('button', 'silent-note-dismiss', '×');
    dismiss.type = 'button'; dismiss.setAttribute('aria-label', 'Dismiss note');
    dismiss.addEventListener('click', () => removeSilentNote(message.id));
    card.append(answer, dismiss); list.append(card);
    card.expiryTimer = setTimeout(() => removeSilentNote(message.id), 30000);
  }
  function renderInvestigations() {
    const list = $('investigations'), section = $('investigations-section');
    if (!list || !section) return;
    const previous = new Map([...list.children].map(node => [node.dataset.id, {open: node.open, status: node.dataset.status}]));
    list.replaceChildren();
    section.hidden = !state.investigations.length;
    const active = state.investigations.filter(job => ['queued', 'running'].includes(job.status)).length;
    $('investigations-count').textContent = active ? `${active} active` : 'Up to date';
    for (const job of [...state.investigations].reverse()) {
      const card = element('details', 'investigation');
      card.dataset.id = job.id; card.dataset.status = job.status;
      const old = previous.get(job.id);
      card.open = old?.status === job.status ? old.open : job.status === 'completed';
      const summary = element('summary');
      summary.append(element('strong', '', `${job.id}${job.species ? ` · ${job.species}` : ''}`));
      const labels = {queued: `Queued · ${job.queue_position || 1}`, retrieving: 'Checking sources',
        analyzing: 'Analyzing evidence', completed: 'Ready', failed: 'Could not finish',
        timed_out: 'Time limit reached', cancelled: 'Cancelled'};
      const status = element('span', 'investigation-status', labels[job.stage] || labels[job.status] || 'Working');
      status.setAttribute('role', 'status'); summary.append(status); card.append(summary);
      card.append(element('p', 'investigation-question', job.question));
      if (job.result?.answer && job.status === 'completed') card.append(element('p', 'investigation-answer', job.result.answer));
      if (['failed', 'timed_out'].includes(job.status)) card.append(element('p', 'investigation-answer',
        'Your question and original evidence are saved. You can investigate again.'));
      if (Array.isArray(job.result?.sources)) {
        const sources = element('div', 'investigation-sources');
        for (const source of job.result.sources.slice(0, 4)) {
          let url;
          try { url = new URL(source.url); } catch (_) { continue; }
          if (!['https:', 'http:'].includes(url.protocol)) continue;
          const link = element('a', '', source.title || source.sci || 'Source');
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; sources.append(link);
        }
        card.append(sources);
      }
      if (!displayMode) {
        const actions = element('div', 'investigation-actions');
        const running = ['queued', 'running'].includes(job.status);
        const button = element('button', 'look-button', running ? 'Cancel investigation' : 'Investigate again');
        button.type = 'button'; button.disabled = !state.connected;
        button.dataset.action = running ? 'cancel' : 'retry';
        button.addEventListener('click', () => {
          if (send({type: 'investigation_action', id: job.id, action: button.dataset.action})) button.disabled = true;
        });
        actions.append(button); card.append(actions);
      }
      list.append(card);
    }
  }
  const icons = {
    voice: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 10v2a6 6 0 0 0 12 0v-2M12 18v4M9 22h6"/></svg>',
    birdnet: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10v4M7 6v12M12 3v18M17 7v10M21 10v4"/></svg>',
    photo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h5l2-3h4l2 3h5v13H3z"/><circle cx="12" cy="13" r="4"/></svg>'
  };
  // Use the same photo/fallback treatment on every species surface.
  function speciesPhoto(item, className = '') {
    const fallback = element('span', `source-icon species-photo ${className}`);
    fallback.innerHTML = icons[item.source] || icons.birdnet;
    fallback.title = `Source: ${item.source || 'birdnet'}`;
    fallback.setAttribute('aria-label', fallback.title);
    try {
      const url = new URL(item.photo, location.href);
      if (!item.photo || !['https:', 'http:'].includes(url.protocol)) return fallback;
      const image = element('img', `species-photo ${className}`);
      image.alt = item.en || item.sci || 'Species';
      image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
      image.addEventListener('error', () => image.replaceWith(fallback), {once: true});
      image.src = url.href;
      return image;
    } catch (_) { return fallback; }
  }
  function renderChecklist(previous = new Map()) {
    const list = $('checklist');
    list.replaceChildren();
    if (!state.items.length) {
      const empty = element('div', 'empty-checklist');
      empty.append(element('span', '', '⌁'), element('h3', '', 'Your next discovery starts here'), element('p', '', 'Try “two mallards on the pond.”'));
      list.append(empty);
    }
    for (const item of state.items) {
      const changed = previous.get(item.id) !== JSON.stringify(item);
      const card = element('article', `observation${changed ? ' updated' : ''}`);
      card.dataset.id = item.id;
      const body = element('div', 'observation-body');
      body.append(element('h3', '', item.en || item.sci || 'Unknown species'), element('p', 'scientific', item.sci || ''));
      const counter = element('div', 'counter');
      for (const delta of [-1, 1]) {
        const button = element('button', '', delta < 0 ? '−' : '+');
        button.type = 'button'; button.disabled = !state.connected;
        button.setAttribute('aria-label', `${delta < 0 ? 'Decrease' : 'Increase'} ${item.en || item.sci} count`);
        button.addEventListener('click', () => send({type: 'edit', id: item.id, count: Math.max(1, Number(item.count || 1) + delta)}));
        counter.append(button);
        if (delta < 0) counter.append(element('output', '', String(item.count ?? 1)));
      }
      const remove = element('button', 'delete-button', '×');
      remove.type = 'button'; remove.disabled = !state.connected;
      remove.setAttribute('aria-label', `Delete ${item.en || item.sci}`);
      const deleteItem = () => { if (state.connected && !displayMode) send({type: 'delete', id: item.id}); };
      remove.addEventListener('click', deleteItem);
      let touchStart;
      card.addEventListener('touchstart', event => { if (event.touches.length === 1) touchStart = {x: event.touches[0].clientX, y: event.touches[0].clientY}; }, {passive: true});
      card.addEventListener('touchend', event => {
        if (!touchStart || !event.changedTouches[0]) return;
        const touch = event.changedTouches[0];
        if (touchStart.x - touch.clientX > 95 && Math.abs(touchStart.y - touch.clientY) < 35) deleteItem();
        touchStart = null;
      }, {passive: true});
      card.append(speciesPhoto(item), body, counter, remove); list.append(card);
    }
    const species = new Set(state.items.map(item => item.sci || item.en)).size;
    const count = state.items.reduce((total, item) => total + (Number(item.count) || 0), 0);
    $('totals').textContent = `${species} species · ${count} individuals`;
  }
  function renderHeard(message) {
    const key = message.sci || message.en;
    state.heard = [message, ...state.heard.filter(item => displayMode || (item.sci || item.en) !== key)].slice(0, 10);
    $('heard').replaceChildren();
    for (const item of state.heard) {
      const card = element('article', `heard-card${item.new ? ' is-new' : ''}`);
      const body = element('div', 'heard-body');
      body.append(element('h3', '', item.en || item.sci), element('p', 'scientific', item.sci || ''));
      body.append(element('span', 'confidence', `${Math.round(Math.max(0, Math.min(1, Number(item.conf) || 0)) * 100)}%`));
      if (item.new) body.append(element('span', 'new-tag', 'NEW'));
      if (item.unusual) body.append(element('span', 'new-tag unusual-tag', 'RARE?'));
      if (item.t && !Number.isNaN(Date.parse(item.t))) body.append(element('time', 'heard-time', new Date(item.t).toLocaleTimeString('en', {hour: '2-digit', minute: '2-digit'})));
      card.append(speciesPhoto(item), body);
      $('heard').append(card);
    }
  }
  function renderSuggestions(items) {
    let row = $('suggestions');
    if (!row) {
      row = element('div', 'suggestion-row'); row.id = 'suggestions';
      row.setAttribute('aria-label', 'BirdNET suggestions — add or dismiss');
      document.querySelector('.checklist-section').before(row);
    }
    row.replaceChildren(); row.hidden = !items.length;
    for (const item of items) {
      const chip = element('span', 'suggestion-chip');
      chip.append(speciesPhoto(item));
      const add = element('button', '', `Add ${item.en || item.sci}?`);
      add.title = `BirdNET ${Math.round(item.conf * 100)}% — tap to add`;
      add.onclick = () => send({type: 'suggestion_action', sci: item.sci, action: 'accept'});
      const dismiss = element('button', 'suggestion-dismiss', '×');
      dismiss.setAttribute('aria-label', `Dismiss ${item.en || item.sci}`);
      dismiss.onclick = () => send({type: 'suggestion_action', sci: item.sci, action: 'dismiss'});
      chip.append(add, dismiss); row.append(chip);
    }
  }
  function renderCandidates(items) {
    $('candidates-section').hidden = !items.length;
    $('candidates').replaceChildren();
    items.forEach((item, index) => {
      const card = element('article', 'candidate-card');
      const body = element('div', 'candidate-info');
      body.append(element('p', 'candidate-index', `0${index + 1} · SAY “${index + 1}” TO CHOOSE`), element('h3', '', item.en || item.sci));
      body.append(element('p', 'scientific', item.sci || ''), element('p', '', item.why || ''));
      card.append(speciesPhoto({...item, source: item.source || 'photo'}, 'candidate-image'), body);
      $('candidates').append(card);
    });
  }

  let map, markers, youMarker, lastBounds = '';
  function validCoords(item) {
    return item && typeof item.lat === 'number' && typeof item.lng === 'number' &&
      Number.isFinite(item.lat) && Number.isFinite(item.lng) && Math.abs(item.lat) <= 90 && Math.abs(item.lng) <= 180;
  }
  function updateMap() {
    if (!window.L || !$('map') || (!$('map-card').open && !displayMode)) return;
    if (!map) {
      map = L.map('map', {scrollWheelZoom: false}).setView([43.65, -79.46], 13);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      }).addTo(map);
      markers = L.layerGroup().addTo(map);
      new ResizeObserver(() => map.invalidateSize()).observe($('map'));
    }
    markers.clearLayers();
    const points = [];
    for (const item of state.items.filter(validCoords)) {
      const popup = element('div', 'map-popup');
      popup.append(speciesPhoto(item), element('strong', '', item.en || item.sci), element('p', 'scientific', item.sci || ''), element('p', '', `Count: ${item.count ?? 1}`));
      L.marker([item.lat, item.lng]).bindPopup(popup).addTo(markers);
      points.push([item.lat, item.lng]);
    }
    if (youMarker) { map.removeLayer(youMarker); youMarker = null; }
    if (!displayMode && validCoords(state.coords)) {
      youMarker = L.circleMarker([state.coords.lat, state.coords.lng], {radius: 8, color: '#fff', weight: 3, fillColor: '#3888ff', fillOpacity: 1}).bindPopup('You are here').addTo(map);
      points.push([state.coords.lat, state.coords.lng]);
    }
    const signature = JSON.stringify(points);
    if (points.length && signature !== lastBounds) {
      map.fitBounds(L.latLngBounds(points), {padding: [28, 28], maxZoom: 16, animate: false});
      lastBounds = signature;
    }
    if ($('map-note')) $('map-note').textContent = points.length ? (displayMode ? 'Observation locations · OpenStreetMap' : 'Observations + your location · OpenStreetMap') : 'Waiting for location data';
    requestAnimationFrame(() => map.invalidateSize());
  }

  function distanceMeters(a, b) {
    const rad = Math.PI / 180, dLat = (b.lat - a.lat) * rad, dLng = (b.lng - a.lng) * rad;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
    return 6371000 * 2 * Math.asin(Math.sqrt(Math.min(1, h)));
  }
  async function start() {
    if (state.running || state.starting) return;
    state.starting = true; $('start').disabled = true;
    notice('Opening microphone…');
    startHeadsetSession();
    if (navigator.geolocation && locationWatch === null) locationWatch = navigator.geolocation.watchPosition(position => {
      state.coords = {lat: position.coords.latitude, lng: position.coords.longitude};
      if (state.connected && (!lastSentCoords || distanceMeters(lastSentCoords, state.coords) > 200)) {
        if (send({type: 'location', ...state.coords})) lastSentCoords = {...state.coords};
      }
      updateMap();
    }, () => {}, {enableHighAccuracy: false, timeout: 10000, maximumAge: 60000});
    try {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) throw new Error('WebAudio unavailable');
      try { context = new AudioContextClass({sampleRate: 48000, latencyHint: 'interactive'}); }
      catch (_) { context = new AudioContextClass(); }
      // Invoke resume synchronously in the Start gesture, before any permission awaits (iOS).
      const unlocked = context.resume();
      // A tiny silent source also unlocks playback on older Safari versions.
      const unlock = context.createBufferSource(); unlock.buffer = context.createBuffer(1, 1, context.sampleRate);
      unlock.connect(context.destination); unlock.start();
      await unlocked;
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone needs HTTPS or localhost');
      const result = await requestMic(selectedMic.deviceId);
      stream = result.input;
      saveMic(result.deviceId, result.deviceId ? stream.getAudioTracks()[0].label : 'Default');
      await refreshDevices();
      await context.audioWorklet.addModule('audio-worklet.js');
      micSource = context.createMediaStreamSource(stream);
      capture = new AudioWorkletNode(context, 'field-capture', {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
      resampler = context.sampleRate === 48000 ? null : new LinearResampler(context.sampleRate);
      capture.port.onmessage = onCapture;
      micSource.connect(capture).connect(context.destination);
      if (managedAudio) {
        const endpoint = new URL(websocketURL()); endpoint.protocol = endpoint.protocol === 'wss:' ? 'https:' : 'http:';
        endpoint.pathname = livekitMode ? '/api/livekit/connect' : '/api/realtime/webrtc'; endpoint.search = ''; endpoint.searchParams.set('session', session);
        const AudioTransport = livekitMode ? FieldLiveKitAudio : FieldWebRTCAudio;
        rtc = new AudioTransport({context, stream, output: assistantOutput(), endpoint: endpoint.href,
          onEvent: (event, fields) => diagnostic.record(event, fields),
          onPlayback: (active, id) => { playbackId = id; playbackComplete = !active; reportPlayback(active, true); updateControls(); },
          onAmbient: samples => {
            if (!state.running || state.paused || !state.connected || !socket || socket.bufferedAmount > 192000) return;
            const outgoing = resampler ? resampler.process(samples) : samples;
            if (outgoing.length) socket.send(pcmLittleEndian(outgoing));
          },
          onStatus: text => { $('environment-status').textContent = text; $('environment-status').hidden = false; captureSettings(); },
          onFailure: text => { stop(); notice(text); }});
        if (webrtcMode) rtc.setDuplex(webrtcDuplex);
        if (livekitMode) rtc.setMuted(assistantMuted);
      }
      notice(context.sampleRate === 48000 ? '' : `Audio ${context.sampleRate} Hz → 48000 Hz · Linear resampling enabled`);
    } catch (error) {
      console.warn('Microphone setup:', error);
      stream?.getTracks().forEach(track => track.stop()); stream = null;
      capture?.disconnect(); micSource?.disconnect(); capture = micSource = null;
      if (managedAudio) { state.starting = false; await stop(); notice(`${error.message}. Use the standard voice mode on this device.`); return; }
      notice(`${error.message}. You can still type observations.`);
    }
    state.running = true; state.starting = false; state.paused = false;
    state.startedAt = new Date(); state.endedAt = null;
    captureSettings(); requestWakeLock(); updateControls(); connect();
  }
  async function stop() {
    enrollmentStatus({status: 'error', error: 'Session ended; capture cancelled.'});
    camera.close();
    state.running = false; state.connected = false; state.listening = false;
    diagnostic.stop(); rtc?.close(); rtc = null;
    if (locationWatch !== null) navigator.geolocation?.clearWatch(locationWatch);
    locationWatch = null;
    ++micRevision; ++deviceRevision; micSwitching = false;
    stopHeadsetSession();
    state.endedAt = new Date();
    clearTimeout(reconnectTimer);
    socket?.close(1000, 'Session ended'); socket = null;
    flush();
    stream?.getTracks().forEach(track => track.stop()); stream = null;
    capture?.disconnect(); micSource?.disconnect(); capture = micSource = null;
    const oldWakeLock = wakeLock, oldContext = context;
    assistantGain?.disconnect(); assistantGain = null; assistantMuted = false;
    wakeLock = null; context = null; nextPlaybackTime = 0;
    $('level-fill').style.width = '0%'; $('mic-meter').setAttribute('aria-valuenow', '0');
    captureSettings();
    notice('Session ended. Your checklist is ready to export.'); updateControls();
    if (oldWakeLock) { try { await oldWakeLock.release(); } catch (_) {} }
    if (oldContext) { try { await oldContext.close(); } catch (_) {} }
  }

  const csvCell = value => `"${String(value ?? '').replaceAll('"', '""')}"`;
  const csv = rows => rows.map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
  function downloadCSV(rows, kind) {
    const blob = new Blob([csv(rows)], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob), link = element('a');
    link.href = url; link.download = `field-companion-${kind}-${localDate(state.startedAt || new Date(), '-')}.csv`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  function localDate(date, separator = '/') {
    const pad = number => String(number).padStart(2, '0');
    return separator === '/' ? `${pad(date.getMonth() + 1)}/${pad(date.getDate())}/${date.getFullYear()}` : `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }
  function exportEBird() {
    const birds = state.items.filter(item => item.group === 'Aves');
    if (!birds.length) { notice('No bird observations to export.'); $('export-dialog').close(); return; }
    const start = state.startedAt || new Date();
    const time = `${String(start.getHours()).padStart(2, '0')}:${String(start.getMinutes()).padStart(2, '0')}`;
    const rows = birds.map(item => {
      const scientific = String(item.sci || '').trim().split(/\s+/);
      // eBird Record Format: exactly 19 columns, no header. We do not collect
      // effort/track/all-species data, so Incidental is honest even after End.
      return [item.en || item.sci || '', scientific[0] || '', scientific[1] || '', item.count ?? 1, item.notes || '', $('location-name').value, state.coords?.lat ?? '', state.coords?.lng ?? '', localDate(start), time, $('export-state').value, $('export-country').value, 'Incidental', 1, '', 'N', '', '', 'Recorded with Field Companion'];
    });
    downloadCSV(rows, 'ebird');
  }
  function exportGeneric() {
    const rows = [['English Name', 'Scientific Name', 'Taxon Group', 'Count', 'Notes', 'First Seen', 'Source', 'Location Name', 'Latitude', 'Longitude']];
    state.items.forEach(item => rows.push([item.en, item.sci, item.group, item.count, item.notes, item.first_seen, item.source, $('location-name').value, item.lat ?? state.coords?.lat, item.lng ?? state.coords?.lng]));
    downloadCSV(rows, 'all-taxa');
  }
  $('client-build').textContent = CLIENT_BUILD;
  $('conversation-mode').addEventListener('change', () => send({type:'addressing_mode',value:$('conversation-mode').value}));
  $('turn-recording').addEventListener('change', () => send({type:'recording',enabled:$('turn-recording').checked}));
  $('enroll-voice').addEventListener('click', () => {
    const profile = $('voice-profile').value.trim() || 'owner';
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(profile)) {
      $('enroll-progress').textContent = 'Use letters, digits, underscores or hyphens for the profile.'; return;
    }
    if (send({type: 'enroll_start', profile})) {
      enrollmentBusy = true; $('enroll-progress').textContent = 'Waiting for capture…'; updateControls();
    }
  });
  $('settings-open').addEventListener('click', () => $('settings-dialog').showModal());
  $('reset').addEventListener('click', resetSession);
  $('reset-display').addEventListener('click', resetSession);
  $('reload').addEventListener('click', () => location.reload());
  $('reconnect').addEventListener('click', () => { replaced = false; start(); });
  $('headset-enabled').addEventListener('change', event => {
    headsetEnabled = event.target.checked;
    if (headsetEnabled && (state.running || state.starting)) startHeadsetSession();
    else stopHeadsetSession();
  });
  $('audio-mode')?.addEventListener('click', () => {
    if (livekitMode) return;
    if (webrtcMode) {
      webrtcDuplex = !webrtcDuplex; rtc?.setDuplex(webrtcDuplex);
      if (state.connected) send({type: 'duplex', duplex: webrtcDuplex});
      updateControls(); return;
    }
    earphones = !earphones;
    try { localStorage.setItem('field-earphones', String(earphones)); } catch (_) {}
    if (state.connected && !displayMode) send({type: 'audio_mode', earphones});
    updateControls();
  });
  $('assistant-mute')?.addEventListener('click', () => {
    assistantMuted = !assistantMuted;
    // Keep microphone capture, captions and the playback clock unchanged.
    // Muting the shared output also silences sources already queued or playing.
    if (assistantGain && context) assistantGain.gain.setValueAtTime(assistantMuted ? 0 : 1, context.currentTime);
    if (livekitMode) rtc?.setMuted(assistantMuted);
    if (state.connected) send({type: 'output_mute', muted: assistantMuted});
    updateControls();
  });
  $('start').addEventListener('click', start);
  $('stop').addEventListener('click', stop);
  $('quiet').addEventListener('click', () => {
    state.quiet = !state.quiet; $('quiet').setAttribute('aria-pressed', String(state.quiet));
    if (state.connected) send({type: 'mode', quiet: state.quiet});
  });
  $('mic').addEventListener('click', () => {
    state.paused = !state.paused;
    stream?.getAudioTracks().forEach(track => { track.enabled = !state.paused; });
    rtc?.setMic(!state.paused);
    if (state.connected) send({type: 'mic', on: !state.paused});
    if (resampler) resampler = new LinearResampler(context.sampleRate);
    if (!state.paused && context?.state === 'suspended') context.resume().catch(() => {});
    $('level-fill').style.width = '0%'; updateControls();
  });
  $('diagnostics-record')?.addEventListener('click', async () => {
    if (diagnosticBusy) return;
    diagnosticBusy = true; updateControls();
    try {
      if (diagnostic.active) {
        diagnostic.stop(); send({type: 'diagnostics', enabled: false});
        $('diagnostics-status').textContent = 'Recording stopped. Save or clear the local recording.';
      } else if (context && micSource && state.running) {
        await diagnostic.start(context, micSource, rtc?.diagnosticOutput || assistantOutput(), {transport: livekitMode ? 'livekit' : webrtcMode ? 'webrtc' : 'websocket',
          model: state.model, voice: stream.getAudioTracks()[0].getSettings(), environment: rtc?.settings().environment || {}});
        if (!state.running) { diagnostic.stop(); return; }
        send({type: 'diagnostics', enabled: true});
        $('diagnostics-status').textContent = 'Recording locally · last 60 seconds · left: mic, right: playback reference';
      }
    } catch (error) { notice('Diagnostic recording unavailable: ' + error.message); }
    finally { diagnosticBusy = false; updateControls(); }
  });
  $('diagnostics-save')?.addEventListener('click', () => {
    try { diagnostic.download(); } catch (error) { notice(error.message); }
  });
  $('diagnostics-clear')?.addEventListener('click', () => {
    diagnostic.clear(); if (state.connected) send({type: 'diagnostics', enabled: false});
    $('diagnostics-status').textContent = 'Recording cleared'; updateControls();
  });
  setInterval(() => { if (diagnostic.active) updateControls(); }, 1000);
  if ($('webrtc-link')) {
    const experimentalURL = new URL(location.href), stableURL = new URL(location.href);
    experimentalURL.searchParams.set('transport', 'webrtc');
    experimentalURL.searchParams.set('baseline', session);
    experimentalURL.searchParams.set('session', session.slice(0, 90) + '-rtc-' + Math.random().toString(36).slice(2, 10));
    stableURL.searchParams.delete('transport'); stableURL.searchParams.delete('duplex');
    stableURL.searchParams.set('session', params.get('baseline') || 'demo'); stableURL.searchParams.delete('baseline');
    $('webrtc-link').href = experimentalURL.href; $('stable-audio-link').href = stableURL.href;
    const livekitURL = new URL(experimentalURL.href);
    livekitURL.searchParams.set('transport', 'livekit');
    livekitURL.searchParams.set('session', session.slice(0, 90) + '-lk-' + Math.random().toString(36).slice(2, 10));
    if ($('livekit-link')) $('livekit-link').href = livekitURL.href;
    $('audio-transport-note').textContent = livekitMode ? 'LiveKit manages speech playback, interruption and recovery. Bird input is separate.' : webrtcMode
      ? 'Experimental WebRTC voice. Use the mode button to turn voice interruption off. Bird input is verified separately.'
      : 'Standard audio. Each voice experiment uses a separate checklist.';
    if (webrtcMode) $('earphone-tip').textContent = 'Experimental speaker interruption. Switch to Tap to interrupt if echo appears.';
    if (livekitMode) $('earphone-tip').textContent = 'Say “Scout” before a new request. You can answer Scout’s current question without the name, including “Start” after Camera ready. Tap also interrupts.';
  }
  $('tap').addEventListener('click', talk);
  $('mic-device')?.addEventListener('change', event => switchMic(event.target.value));
  navigator.mediaDevices?.addEventListener('devicechange', () => {
    if (state.running && capture) refreshDevices();
  });
  $('text-form').addEventListener('submit', event => {
    event.preventDefault(); const text = $('text-input').value.trim();
    if (text && send({type: 'text', text})) { $('text-input').value = ''; $('text-input').blur(); }
  });
  $('export').addEventListener('click', () => {
    const birds = state.items.filter(item => item.group === 'Aves').length;
    $('export-summary').textContent = `${state.items.length} entries · ${birds} bird entries for eBird · ${state.items.length - birds} other taxa`;
    $('export-ebird').disabled = !birds; $('export-all').disabled = !state.items.length;
    $('settings-dialog').close();
    $('export-dialog').showModal();
  });
  $('export-ebird').addEventListener('click', exportEBird);
  $('export-all').addEventListener('click', exportGeneric);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { requestWakeLock(); if (state.running && context?.state === 'suspended') notice('Tap to resume if audio was interrupted.'); }
  });
  window.addEventListener('online', () => { if (state.running && !state.connected && (!socket || socket.readyState === WebSocket.CLOSED)) connect(); });
  window.addEventListener('pagehide', () => { if (state.running) stop(); });
  setInterval(() => {
    if (!state.startedAt) return;
    const seconds = Math.floor(((state.endedAt || new Date()) - state.startedAt) / 1000);
    $('session-time').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }, 1000);
  if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('sw.js').catch(error => console.warn('Offline shell unavailable', error));
  $('map-card')?.addEventListener('toggle', updateMap);
  if (displayMode) {
    if ($('map-card')) $('map-card').open = true;
    $('heard-card').open = true;
    if ($('phone-hint')) {
      const phoneURL = new URL('/', location.href);
      phoneURL.searchParams.set('session', session);
      if (managedAudio) phoneURL.searchParams.set('transport', livekitMode ? 'livekit' : 'webrtc');
      if (webrtcMode) phoneURL.searchParams.set('duplex', webrtcDuplex ? '1' : '0');
      $('phone-hint').textContent = `Phone: ${phoneURL.host}${phoneURL.pathname}${phoneURL.search}`;
      $('phone-hint').href = phoneURL.href;
    }
    state.running = true; state.startedAt = new Date();
    connect(); updateMap();
    if (audioEnabled && $('audio-overlay')) {
      $('audio-overlay').hidden = false;
      const silentURL = new URL(location.href);
      silentURL.searchParams.delete('audio');
      $('silent-display').href = silentURL.href;
      $('enable-audio').addEventListener('click', async () => {
        try {
          const AudioContextClass = window.AudioContext || window.webkitAudioContext;
          context = context || new AudioContextClass({latencyHint: 'interactive'});
          await context.resume();
          $('audio-overlay').hidden = true;
          requestWakeLock();
        } catch (error) { notice(`Audio unavailable: ${error.message}`); }
      });
    }
  }
  updateControls();
})();
