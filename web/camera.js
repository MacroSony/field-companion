'use strict';

// Camera permissions, tracks and uploads are independent of microphone capture.
window.FieldCamera = class FieldCamera {
  constructor({displayMode, session, send, connected, backendURL}) {
    Object.assign(this, {displayMode, session, send, connected, backendURL});
    this.$ = id => document.getElementById(id);
    this.stream = null;
    this.opening = null;
    this.revision = 0;
    this.job = 0;
    this.busy = false;
    this.crop = null;
    this.observation = null;
    this.focus = false;
    this.wasConnected = false;
    this.prepared = null;
    this.openId = null;
    this.$('camera-toggle').addEventListener('click', () => {
      if (this.stream || this.opening) this.close(); else this.open();
    });
    this.$('camera-shutter').addEventListener('click', () => this.look());
    this.$('camera-new').addEventListener('click', () => this.newSubject());
    this.$('observation-again').addEventListener('click', () => this.look());
    this.$('observation-confirm').addEventListener('click', () => this.action('confirm'));
    this.$('observation-discard').addEventListener('click', () => this.action('discard'));
    this.$('camera-clear-crop').addEventListener('click', () => this.selectCrop(null));
    this.$('camera-group').addEventListener('change', () => {
      if (this.stream && this.connected() && !this.busy) this.send({type: 'camera_ready',
        open_id: this.openId, group: this.$('camera-group').value});
    });
    this.$('camera-zoom').addEventListener('input', () => {
      this.$('camera-preview').style.transform = `scale(${this.zoom()})`;
      this.$('camera-zoom-value').textContent = `${this.zoom().toFixed(1)}×`;
      this.selectCrop(null);
    });
    const viewport = this.$('camera-viewport');
    viewport.addEventListener('click', event => {
      if (!this.stream || this.busy) return;
      const rect = viewport.getBoundingClientRect();
      this.selectCrop({x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height});
    });
    viewport.addEventListener('keydown', event => {
      if (['Enter', ' '].includes(event.key) && this.stream && !this.busy) {
        event.preventDefault(); this.selectCrop({x: .5, y: .5});
      }
    });
    document.addEventListener('visibilitychange', () => { if (document.hidden) this.close(); });
    window.addEventListener('pagehide', () => this.close());
    this.updateConnection();
  }

  zoom() { return Math.max(1, Math.min(4, Number(this.$('camera-zoom').value) || 1)); }
  status(text, error = false) {
    this.$('camera-status').textContent = text;
    this.$('camera-status').classList.toggle('is-error', error);
  }
  endpoint(path) {
    const url = new URL(this.backendURL());
    url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
    url.pathname = path; url.search = ''; url.hash = '';
    return url;
  }
  cancelCapture() {
    ++this.job;
    this.upload?.abort(); this.upload = null;
    this.busy = false;
    this.prepared = null;
  }
  close() {
    const request = this.prepared, openId = this.openId;
    this.openId = null;
    if (this.connected() && (request || openId)) this.send({type: 'camera_closed', request_id: request?.request_id, open_id: openId});
    ++this.revision;
    this.cancelCapture(); this.opening = null;
    this.stream?.getTracks().forEach(track => track.stop()); this.stream = null;
    this.$('camera-preview').srcObject = null;
    this.$('camera-panel').hidden = true;
    this.updateConnection();
  }
  reset() {
    this.close();
    this.observation = null; this.focus = false;
    this.$('active-observation').hidden = true;
    this.$('observation-candidates').replaceChildren();
    this.$('observation-evidence').replaceChildren();
    this.updateConnection();
  }
  updateConnection() {
    const connected = this.connected();
    if (this.wasConnected && !connected) {
      this.cancelCapture(); this.focus = false;
      this.status('Connection lost. Reconnect, then tap Identify to try again.', true);
    }
    this.wasConnected = connected;
    const live = !this.displayMode && connected;
    const open = !!(this.stream || this.opening);
    this.$('camera-toggle').disabled = !live;
    this.$('camera-toggle').textContent = open ? 'Close camera' : 'Open camera';
    this.$('camera-toggle').setAttribute('aria-expanded', String(open));
    this.$('camera-shutter').disabled = !live || !this.stream || this.busy;
    this.$('camera-new').disabled = !live || !this.stream || this.busy;
    this.$('camera-zoom').disabled = !this.stream || this.busy;
    this.$('camera-group').disabled = this.busy;
    this.$('camera-clear-crop').disabled = this.busy;
    const active = live && this.focus && this.observation?.status === 'open';
    this.$('observation-again').disabled = !active || !this.stream || this.busy;
    this.$('observation-discard').disabled = !active || this.busy;
    this.$('observation-confirm').disabled = !active || this.busy || !this.observation?.fused?.length;
    this.$('observation-count').disabled = !active || this.busy;
    document.body.classList.toggle('has-observation', !this.$('active-observation').hidden);
  }
  async open(notify = true) {
    if (this.displayMode || !this.connected() || document.hidden) return false;
    if (this.stream) return true;
    if (this.opening) return this.opening;
    const openId = this.openId = crypto.randomUUID();
    const revision = ++this.revision;
    this.$('camera-panel').hidden = false;
    this.status('Opening camera…');
    this.opening = (async () => {
      await Promise.resolve(); // Keep opening/finally ordered even when the API is absent.
      let input;
      try {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error('Camera needs HTTPS or localhost.');
        input = await navigator.mediaDevices.getUserMedia({audio: false, video: {
          facingMode: {ideal: 'environment'}, width: {ideal: 1920}, height: {ideal: 1080}
        }});
        if (revision !== this.revision || !this.connected() || document.hidden) {
          input.getTracks().forEach(track => track.stop()); return false;
        }
        const video = this.$('camera-preview');
        video.srcObject = input;
        await video.play();
        if (!video.videoWidth || !video.videoHeight) throw new Error('No camera frame. Close the camera and try again.');
        if (revision !== this.revision || !this.connected() || document.hidden) {
          input.getTracks().forEach(track => track.stop()); return false;
        }
        this.stream = input;
        this.$('camera-viewport').style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`;
        this.$('camera-zoom').value = '1'; video.style.transform = '';
        this.$('camera-zoom-value').textContent = '1×'; this.selectCrop(null);
        this.$('camera-panel').scrollIntoView({block: 'start', behavior: 'instant'});
        input.getVideoTracks()[0].addEventListener('ended', () => {
          if (this.stream === input) {
            this.close(); this.$('camera-panel').hidden = false;
            this.status('Camera stopped. Tap Open camera to resume.', true);
          }
        });
        this.status('Camera ready. Aim at the subject, then say “start” or tap Identify.');
        if (notify) this.send({type: 'camera_ready', open_id: openId, group: this.$('camera-group').value});
        return true;
      } catch (error) {
        input?.getTracks().forEach(track => track.stop());
        if (revision === this.revision) {
          this.$('camera-preview').srcObject = null;
          const messages = {
            NotAllowedError: 'Camera permission denied. Allow it in browser settings, then tap Open camera.',
            NotFoundError: 'No camera found. Connect a camera, then try again.',
            NotReadableError: 'Camera is busy. Close other camera apps, then try again.'
          };
          this.status(messages[error.name] || error.message || 'Camera unavailable. Try again.', true);
        }
        return false;
      } finally {
        if (revision === this.revision) { this.opening = null; this.updateConnection(); }
      }
    })();
    this.updateConnection();
    return this.opening;
  }
  selectCrop(point) {
    this.crop = point ? {x: Math.max(0, Math.min(.55, point.x - .225)),
      y: Math.max(0, Math.min(.55, point.y - .225)), w: .45, h: .45} : null;
    const box = this.$('camera-crop');
    box.hidden = !this.crop;
    this.$('camera-clear-crop').hidden = !this.crop;
    if (this.crop) Object.assign(box.style, {left: `${this.crop.x * 100}%`, top: `${this.crop.y * 100}%`, width: '45%', height: '45%'});
  }
  look() {
    if (!this.connected() || this.displayMode || this.busy || !this.stream) return;
    this.status('Start taking photo…');
    this.send({type: 'look', group: this.$('camera-group').value, confirmed: true});
  }
  newSubject() {
    if (!this.connected() || this.displayMode || this.busy) return;
    if (this.focus && this.observation?.status === 'open' && !this.action('discard')) return;
    this.selectCrop(null); this.look();
  }
  action(action) {
    if (!this.connected() || this.displayMode || !this.focus || this.observation?.status !== 'open' || this.busy) return false;
    const message = {type: 'observation_action', id: this.observation.id, action};
    if (action === 'confirm') {
      const input = this.$('observation-count'), count = Number(input.value);
      if (!input.reportValidity() || !Number.isSafeInteger(count) || count < 1 || !this.observation.fused?.length) return false;
      message.count = count;
    }
    return this.send(message);
  }
  observe(message) {
    if (typeof message.id !== 'string' || !['open', 'confirmed', 'discarded'].includes(message.status)) return;
    if (!message.focus && message.id !== this.observation?.id) return;
    const different = message.id !== this.observation?.id;
    if (different || !message.focus || message.status !== 'open') this.cancelCapture();
    this.observation = message; this.focus = !!message.focus;
    if (different || message.status !== 'open') this.$('observation-count').value = String(message.count || 1);
    if (this.focus && message.group) this.$('camera-group').value =
      'target_group' in message ? (message.target_group || 'Any') : message.group;
    const card = this.$('active-observation'); card.hidden = false;
    this.$('observation-status').textContent = message.status === 'open'
      ? (this.focus ? (message.analysis_status === 'analyzing' ? 'Analyzing photo'
        : message.analysis_status === 'capturing' ? 'Taking photo'
        : message.uncertain ? 'Tentative' : 'Awaiting confirmation') : 'Previous subject')
      : message.status === 'confirmed' ? 'Added to checklist' : 'Closed';
    const hint = this.$('observation-hint');
    hint.textContent = message.hint || ''; hint.hidden = !message.hint;
    const warning = this.$('observation-warning');
    warning.hidden = !message.visual_assessment?.category_conflict;
    warning.textContent = warning.hidden ? '' :
      'The photo and your selected category disagree. Tap the subject in the preview, frame it more closely, then choose Look again.';
    const candidates = this.$('observation-candidates'); candidates.replaceChildren();
    for (const candidate of (message.fused || []).slice(0, 3)) {
      const item = document.createElement('li'), name = document.createElement('strong');
      name.textContent = candidate.en || candidate.sci || 'Unknown species';
      const scientific = document.createElement('small'); scientific.textContent = candidate.sci || '';
      const score = Math.max(0, Math.min(1, Number(candidate.p) || 0));
      const meter = document.createElement('span'); meter.className = 'candidate-rank';
      meter.setAttribute('aria-label', `Ranking score ${score.toFixed(2)}`);
      const bar = document.createElement('i'); bar.style.width = `${score * 100}%`; meter.append(bar);
      item.append(name, scientific, meter); candidates.append(item);
    }
    this.$('observation-empty').hidden = !!candidates.children.length;
    const evidence = this.$('observation-evidence'); evidence.replaceChildren();
    for (const detection of (message.detections || []).slice(-6)) {
      const item = document.createElement('figure'); item.className = 'evidence-item';
      if (detection.kind === 'image' && /^\/api\/frame\/[\w-]+\.jpg$/.test(detection.thumb || '')) {
        const image = document.createElement('img');
        image.src = this.endpoint(detection.thumb).href; image.alt = 'Captured evidence'; image.loading = 'lazy';
        image.addEventListener('error', () => { image.hidden = true; }); item.append(image);
      }
      const caption = document.createElement('figcaption');
      const label = {image: 'Photo', sound: 'Bird song', description: 'Description'}[detection.kind] || 'Evidence';
      const date = new Date(detection.t);
      caption.textContent = label + (Number.isFinite(date.getTime()) ? ` · ${date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}` : '');
      if (detection.text) caption.title = String(detection.text).slice(0, 500);
      item.append(caption); evidence.append(item);
    }
    this.updateConnection();
  }
  async frame(zoom) {
    const video = this.$('camera-preview'), canvas = document.createElement('canvas');
    const width = video.videoWidth / zoom, height = video.videoHeight / zoom;
    if (!Number.isFinite(width) || !Number.isFinite(height) || width < 32 || height < 32 || video.readyState < 2)
      throw new Error('Camera has no usable image yet. Try again.');
    const scale = Math.min(1, 1920 / Math.max(width, height));
    canvas.width = Math.max(1, Math.round(width * scale)); canvas.height = Math.max(1, Math.round(height * scale));
    canvas.getContext('2d').drawImage(video, (video.videoWidth - width) / 2, (video.videoHeight - height) / 2,
      width, height, 0, 0, canvas.width, canvas.height);
    // Score the selected subject, not the background, at a bounded resolution.
    const sample = document.createElement('canvas'); sample.width = 160; sample.height = 120;
    const ctx = sample.getContext('2d', {willReadFrequently: true});
    const crop = this.crop || {x: 0, y: 0, w: 1, h: 1};
    ctx.drawImage(canvas, crop.x * canvas.width, crop.y * canvas.height,
      crop.w * canvas.width, crop.h * canvas.height, 0, 0, 160, 120);
    const pixels = ctx.getImageData(0, 0, 160, 120).data, gray = new Float32Array(160 * 120);
    for (let i = 0; i < gray.length; i++) gray[i] = .299 * pixels[i * 4] + .587 * pixels[i * 4 + 1] + .114 * pixels[i * 4 + 2];
    let sum = 0, squares = 0, count = 0;
    for (let y = 1; y < 119; y++) for (let x = 1; x < 159; x++) {
      const i = y * 160 + x, value = gray[i - 1] + gray[i + 1] + gray[i - 160] + gray[i + 160] - 4 * gray[i];
      sum += value; squares += value * value; count++;
    }
    const sharpness = squares / count - (sum / count) ** 2;
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .88));
    if (!blob || blob.size > 8 * 1024 * 1024) throw new Error('Could not prepare the photo. Try again.');
    return {blob, sharpness};
  }
  async prepare(message) {
    if (this.displayMode || !this.connected() || document.hidden || typeof message.request_id !== 'string') return;
    if (!this.focus || this.observation?.id !== message.obs || this.observation.status !== 'open') return;
    if (message.open_id && message.open_id !== this.openId) {
      this.send({type: 'camera_closed', request_id: message.request_id}); return;
    }
    this.cancelCapture();
    const request = this.prepared = message;
    const opened = await this.open(false);
    if (this.prepared !== request) return;
    if (!opened || !this.stream || document.hidden || !this.connected()) {
      this.send({type: 'camera_closed', request_id: message.request_id});
      this.prepared = null; return;
    }
    this.status(`${message.hint ? message.hint + ' · ' : ''}Camera ready. Say “start” or tap Identify when ready.`);
    this.send({type: 'camera_ready', request_id: message.request_id});
    this.updateConnection();
  }
  cancelled(message) {
    if (this.prepared?.request_id === message.request_id) {
      this.cancelCapture();
      if (this.stream) this.status('Preview only. Ask to identify, or tap Identify when ready.');
    }
    this.updateConnection();
  }
  async requestCapture(message) {
    if (this.displayMode || !this.connected() || document.hidden || typeof message.obs !== 'string') return;
    if (!this.focus || this.observation?.id !== message.obs || this.observation.status !== 'open' || this.busy) return;
    if (!this.stream || this.prepared?.request_id !== message.request_id) return;
    const job = ++this.job;
    this.busy = true; this.updateConnection();
    const current = () => job === this.job && this.connected() && this.focus && this.observation?.id === message.obs && this.observation.status === 'open';
    try {
      if (!current()) return;
      const n = Math.max(1, Math.min(3, Math.floor(Number(message.n) || 3)));
      const crop = this.crop && {...this.crop}, zoom = this.zoom();
      let best;
      for (let i = 0; i < n; i++) {
        if (!current()) return;
        this.status(`${message.hint ? message.hint + ' · ' : ''}Taking photo ${i + 1} of ${n}…`);
        const frame = await this.frame(zoom);
        if (!best || frame.sharpness > best.sharpness) best = frame;
        if (i + 1 < n) await new Promise(resolve => setTimeout(resolve, 400));
      }
      if (!current()) return;
      this.status('Photo ready. Starting identification…');
      const url = this.endpoint('/api/frame');
      url.searchParams.set('session', this.session); url.searchParams.set('obs', message.obs);
      url.searchParams.set('capture', message.request_id);
      if (crop) for (const [key, value] of Object.entries(crop)) url.searchParams.set(key, value.toFixed(6));
      const controller = this.upload = new AbortController();
      let timedOut = false;
      const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 35000);
      let response;
      try {
        response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'image/jpeg'},
          body: best.blob, signal: controller.signal});
      } catch (error) {
        if (timedOut) throw new Error('Identification timed out. Tap Identify to try again.');
        throw error;
      } finally { clearTimeout(timer); }
      if (!current()) return;
      if (!response.ok) {
        const messages = {409: 'The subject changed. Tap Identify to capture the current subject.',
          503: 'Image recognition is unavailable. Keep observing and try again later.',
          404: 'Session expired. Reconnect, then try again.'};
        throw new Error(messages[response.status] || 'Photo could not be identified. Try again.');
      }
      this.status('Photo added to this observation. Review the candidates below.');
      this.prepared = null;
    } catch (error) {
      if (current() && error.name !== 'AbortError') this.status(error.message || 'Photo upload failed. Try again.', true);
    } finally {
      if (job === this.job) { this.busy = false; this.upload = null; this.updateConnection(); }
    }
  }
};
