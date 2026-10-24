'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const displayMode = params.get('display') === '1';
  const audioEnabled = !displayMode || params.get('audio') === '1';
  const session = params.get('session') || 'demo';
  document.body.classList.toggle('display-mode', displayMode);
  const state = {
    running: false, starting: false, connected: false, paused: false, quiet: false,
    listening: false, speaking: false, items: [], heard: [], coords: null,
    startedAt: null, endedAt: null, model: '', retry: 0
  };
  let socket, reconnectTimer, context, stream, capture, micSource, wakeLock;
  let nextPlaybackTime = 0, lastMeterAt = 0, resampler = null;
  const playback = new Set(), tones = new Set();
  const bars = document.querySelector('.soundscape');
  const nativeLittleEndian = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;

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
    $('tap').disabled = $('send').disabled = !state.connected;
    $('mic').disabled = !stream || !state.running;
    $('mic').setAttribute('aria-pressed', String(state.paused));
    $('mic').setAttribute('aria-label', state.paused ? 'Resume microphone' : 'Pause microphone');
    $('mic-label').textContent = state.paused ? 'Resume' : 'Pause';
    $('mic-icon').textContent = state.paused ? '▷' : 'Ⅱ';
    $('start').hidden = state.running;
    $('start-note').hidden = state.running;
    $('stop').hidden = !state.running;
    $('start').disabled = state.starting;
    const playing = playback.size > 0 || state.speaking;
    document.body.classList.toggle('session-active', state.running);
    document.body.classList.toggle('running', state.running && !!stream && !state.paused);
    document.body.classList.toggle('speaking', playing);
    $('activity').textContent = !state.running ? 'Ready when you are' : playing ? 'Speaking' : !stream ? (displayMode ? 'Live session display' : 'Text only') : state.paused ? 'Mic paused' : state.connected && state.listening ? 'Listening' : state.connected ? 'Mic on' : 'Reconnecting';
    $('model').textContent = state.model || 'Open mic · hands free';
    const connection = $('connection');
    connection.className = `connection ${state.connected ? 'online' : state.running ? 'connecting' : ''}`;
    connection.lastElementChild.textContent = state.connected ? 'Connected' : state.running ? 'Connecting' : 'Offline';
    if ($('display-status')) $('display-status').textContent = `${state.connected ? '● Live' : '○ Connecting'} · ${playing ? 'Assistant speaking' : 'Session ' + session}`;
    document.querySelectorAll('.counter button, .delete-button').forEach(button => { button.disabled = !state.connected; });
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
    // Drop rather than buffer stale microphone audio on a slow tunnel.
    if (socket.bufferedAmount > 192000) return;
    const outgoing = resampler ? resampler.process(samples) : samples;
    if (outgoing.length) socket.send(pcmLittleEndian(outgoing));
  }
  function flush() {
    for (const source of playback) { try { source.stop(); } catch (_) {} }
    for (const tone of tones) { try { tone.stop(); } catch (_) {} }
    playback.clear(); tones.clear();
    nextPlaybackTime = context?.currentTime || 0;
    state.speaking = false;
    updateControls();
  }
  function playPCM(bytes) {
    if (!audioEnabled || !context || !state.running || bytes.byteLength < 2) return;
    const count = Math.floor(bytes.byteLength / 2);
    const buffer = context.createBuffer(1, count, 24000);
    const channel = buffer.getChannelData(0), view = new DataView(bytes);
    for (let i = 0; i < count; i++) channel[i] = view.getInt16(i * 2, true) / 32768;
    const source = context.createBufferSource();
    source.buffer = buffer; // WebAudio resamples 24 kHz to the shared context rate.
    source.connect(context.destination);
    const at = Math.max(context.currentTime + 0.025, nextPlaybackTime);
    nextPlaybackTime = at + buffer.duration;
    playback.add(source);
    source.onended = () => { playback.delete(source); source.disconnect(); updateControls(); };
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
      oscillator.connect(gain).connect(context.destination);
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
    return url.href;
  }
  function connect() {
    if (!state.running) return;
    clearTimeout(reconnectTimer);
    updateControls();
    let ws;
    try { ws = new WebSocket(websocketURL()); } catch (error) { notice(`WebSocket: ${error.message}`); scheduleReconnect(); return; }
    socket = ws;
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      if (socket !== ws || !state.running) { ws.close(); return; }
      // hello MUST be the first frame; geolocation is optional and never blocks it.
      ws.send(JSON.stringify({type: 'hello', lang: 'en', ...(state.coords || {})}));
      state.connected = true; state.retry = 0;
      if (!displayMode) {
        send({type: 'mic', on: !!stream && !state.paused});
        send({type: 'mode', quiet: state.quiet});
      }
      notice(displayMode || stream ? '' : 'Microphone unavailable; use the text field.');
      updateControls();
    };
    ws.onmessage = event => {
      if (socket !== ws || !state.running) return;
      if (event.data instanceof ArrayBuffer) { playPCM(event.data); return; }
      try { handleMessage(JSON.parse(event.data)); } catch (error) { console.warn('Ignored malformed server message', error); }
    };
    ws.onerror = () => { if (socket === ws) notice('Connection unavailable. Retrying automatically.'); };
    ws.onclose = () => {
      if (socket !== ws) return;
      state.connected = false; state.listening = false; flush(); updateControls();
      if (state.running) scheduleReconnect();
    };
  }
  function scheduleReconnect() {
    if (!state.running) return;
    const delay = Math.min(15000, 800 * 2 ** Math.min(state.retry++, 5)) + Math.random() * 350;
    notice(`Reconnecting in ${Math.ceil(delay / 1000)} seconds. Checklist edits wait for connection.`);
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, delay);
    updateControls();
  }
  function handleMessage(message) {
    switch (message.type) {
      case 'flush': flush(); break;
      case 'earcon': earcon(message.kind); break;
      case 'checklist':
        if (Array.isArray(message.items)) {
          const previous = new Map(state.items.map(item => [item.id, JSON.stringify(item)]));
          state.items = message.items; renderChecklist(previous); updateMap();
        }
        break;
      case 'heard': renderHeard(message); break;
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
        state.listening = !!message.listening; state.speaking = !!message.speaking;
        if (typeof message.model === 'string') state.model = message.model;
        // Socket transport status remains independent of backend/model connectivity.
        if (message.connected === false) notice('The server is reconnecting to the voice model.');
        updateControls(); break;
      case 'error': notice(String(message.message || 'Unknown server error')); earcon('error'); break;
    }
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
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
      if (item.t && !Number.isNaN(Date.parse(item.t))) body.append(element('time', 'heard-time', new Date(item.t).toLocaleTimeString('en', {hour: '2-digit', minute: '2-digit'})));
      card.append(speciesPhoto(item), body);
      $('heard').append(card);
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

  async function start() {
    if (state.running || state.starting) return;
    state.starting = true; $('start').disabled = true;
    notice('Opening microphone…');
    if (navigator.geolocation) navigator.geolocation.getCurrentPosition(position => {
      state.coords = {lat: position.coords.latitude, lng: position.coords.longitude};
      updateMap();
    }, () => {}, {enableHighAccuracy: false, timeout: 6000, maximumAge: 60000});
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
      stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1, sampleRate: 48000}});
      await context.audioWorklet.addModule('audio-worklet.js');
      micSource = context.createMediaStreamSource(stream);
      capture = new AudioWorkletNode(context, 'field-capture', {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
      resampler = context.sampleRate === 48000 ? null : new LinearResampler(context.sampleRate);
      capture.port.onmessage = onCapture;
      micSource.connect(capture).connect(context.destination);
      notice(context.sampleRate === 48000 ? '' : `Audio ${context.sampleRate} Hz → 48000 Hz · Linear resampling enabled`);
    } catch (error) {
      console.warn('Microphone setup:', error);
      stream?.getTracks().forEach(track => track.stop()); stream = null;
      capture?.disconnect(); micSource?.disconnect(); capture = micSource = null;
      notice(`${error.message}. You can still type observations.`);
    }
    state.running = true; state.starting = false; state.paused = false;
    state.startedAt = new Date(); state.endedAt = null;
    requestWakeLock(); updateControls(); connect();
  }
  async function stop() {
    state.running = false; state.connected = false; state.listening = false;
    state.endedAt = new Date();
    clearTimeout(reconnectTimer);
    socket?.close(1000, 'Session ended'); socket = null;
    flush();
    stream?.getTracks().forEach(track => track.stop()); stream = null;
    capture?.disconnect(); micSource?.disconnect(); capture = micSource = null;
    const oldWakeLock = wakeLock, oldContext = context;
    wakeLock = null; context = null;
    $('level-fill').style.width = '0%'; $('mic-meter').setAttribute('aria-valuenow', '0');
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
  $('start').addEventListener('click', start);
  $('stop').addEventListener('click', stop);
  $('quiet').addEventListener('click', () => {
    state.quiet = !state.quiet; $('quiet').setAttribute('aria-pressed', String(state.quiet));
    if (state.connected) send({type: 'mode', quiet: state.quiet});
  });
  $('mic').addEventListener('click', () => {
    state.paused = !state.paused;
    stream?.getAudioTracks().forEach(track => { track.enabled = !state.paused; });
    if (state.connected) send({type: 'mic', on: !state.paused});
    if (resampler) resampler = new LinearResampler(context.sampleRate);
    if (!state.paused && context?.state === 'suspended') context.resume().catch(() => {});
    $('level-fill').style.width = '0%'; updateControls();
  });
  $('tap').addEventListener('click', () => {
    if (context && context.state !== 'running') context.resume().catch(() => {});
    flush(); send({type: 'tap'});
  });
  $('text-form').addEventListener('submit', event => {
    event.preventDefault(); const text = $('text-input').value.trim();
    if (text && send({type: 'text', text})) { $('text-input').value = ''; $('text-input').blur(); }
  });
  $('export').addEventListener('click', () => {
    const birds = state.items.filter(item => item.group === 'Aves').length;
    $('export-summary').textContent = `${state.items.length} entries · ${birds} bird entries for eBird · ${state.items.length - birds} other taxa`;
    $('export-ebird').disabled = !birds; $('export-all').disabled = !state.items.length;
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
    if ($('phone-hint')) {
      const phoneURL = new URL('/', location.href);
      phoneURL.searchParams.set('session', session);
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
