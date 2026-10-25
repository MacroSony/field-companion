/* Experimental media only. Tools, routing and investigations stay on the server. */
(function (root) {
  'use strict';
  class FieldWebRTCAudio {
    constructor({context, stream, output, endpoint, onPlayback, onEvent, onAmbient, onStatus, onFailure}) {
      Object.assign(this, {context, stream, output, endpoint, onPlayback, onEvent, onAmbient, onStatus, onFailure});
      this.playing = false; this.responseId = null; this.cancelled = new Set();
      this.duplex = true; this.micOn = true; this.auxPlaying = false; this.tailUntil = 0;
      this.closed = false; this.environmentStatus = 'Not opened';
      this.gain = context.createGain(); this.gain.connect(output);
    }
    event(name, fields = {}) { this.onEvent?.(name, fields); }
    settings() {
      return {voice: this.stream.getAudioTracks()[0]?.getSettings() || {},
        environment: this.environmentStream?.getAudioTracks()[0]?.getSettings() || {},
        status: this.environmentStatus};
    }
    updateMic() {
      const enabled = this.micOn && (this.duplex || (!this.playing && !this.auxPlaying && performance.now() >= this.tailUntil));
      for (const track of this.stream.getAudioTracks()) track.enabled = enabled && !!this.ready && !this.closed;
      for (const track of this.environmentStream?.getAudioTracks() || []) track.enabled = this.micOn && !this.closed;
    }
    setMic(on) { this.micOn = on; this.updateMic(); }
    setDuplex(on) { this.duplex = on; this.updateMic(); }
    setAuxPlayback(on) {
      if (this.auxPlaying && !on) this.tailUntil = performance.now() + 500;
      this.auxPlaying = on; this.updateMic();
    }
    async connect(token) {
      if (this.peer || this.closed) return;
      this.peer = new RTCPeerConnection();
      this.peer.onconnectionstatechange = () => {
        const state = this.peer.connectionState;
        this.event('rtc_connection', {state});
        if (state === 'failed') { this.close(); this.onFailure?.('Voice media connection failed. End the session and start again.'); }
      };
      this.peer.ontrack = event => {
        this.remoteSource?.disconnect();
        this.remoteSource = this.context.createMediaStreamSource(event.streams[0] || new MediaStream([event.track]));
        this.remoteSource.connect(this.gain);
        this.event('rtc_remote_track');
      };
      for (const track of this.stream.getAudioTracks()) { track.enabled = false; this.peer.addTrack(track, this.stream); }
      this.channel = this.peer.createDataChannel('oai-events');
      this.channel.onmessage = message => {
        try { this.handle(JSON.parse(message.data)); } catch (_) { this.event('rtc_unreadable_event'); }
      };
      const offer = await this.peer.createOffer();
      await this.peer.setLocalDescription(offer);
      this.abort = new AbortController();
      const timeout = setTimeout(() => this.abort.abort(), 30000);
      try {
        const response = await fetch(this.endpoint, {method: 'POST', headers: {
          'Content-Type': 'application/sdp', 'X-Field-RTC': token}, body: offer.sdp, signal: this.abort.signal});
        if (!response.ok) throw new Error('Voice setup failed (HTTP ' + response.status + '). End and restart the session.');
        const answer = await response.text();
        if (this.closed) return;
        await this.peer.setRemoteDescription({type: 'answer', sdp: answer});
        this.ready = true; this.updateMic();
        await this.openEnvironment();
        if (this.closed) return;
        this.timer = setInterval(() => { this.updateMic(); }, 50);
        this.statsTimer = setInterval(() => this.stats(), 2000);
      } finally { clearTimeout(timeout); }
    }
    handle(event) {
      if (this.closed) return;
      const type = event.type, id = event.response_id;
      if (type === 'output_audio_buffer.started') {
        if (this.cancelled.has(id)) {
          if (!this.playing || this.responseId === id) this.gain.gain.setValueAtTime(0, this.context.currentTime);
          return;
        }
        this.responseId = id; this.playing = true;
        this.gain.gain.setValueAtTime(1, this.context.currentTime);
        this.onPlayback?.(true, id); this.updateMic();
      } else if ((type === 'output_audio_buffer.stopped' || type === 'output_audio_buffer.cleared') && id === this.responseId) {
        this.playing = false; this.tailUntil = performance.now() + 500;
        this.onPlayback?.(false, id); this.updateMic();
      }
      if (['input_audio_buffer.speech_started', 'input_audio_buffer.speech_stopped', 'output_audio_buffer.started',
        'output_audio_buffer.stopped', 'output_audio_buffer.cleared', 'response.created', 'response.done'].includes(type)) {
        this.event(type, {response_id: id || event.response?.id, item_id: event.item_id,
          audio_start_ms: event.audio_start_ms, audio_end_ms: event.audio_end_ms});
      }
      // No client-side response.create, session.update or tool execution.
    }
    flush() {
      if (this.responseId) this.cancelled.add(this.responseId);
      if (this.cancelled.size > 100) this.cancelled.delete(this.cancelled.values().next().value);
      this.gain.gain.setValueAtTime(0, this.context.currentTime);
      this.playing = false; this.tailUntil = performance.now() + 500; this.updateMic();
    }
    async openEnvironment() {
      const track = this.stream.getAudioTracks()[0], constraints = track.getConstraints();
      try {
        const id = track.getSettings().deviceId;
        this.environmentStream = await navigator.mediaDevices.getUserMedia({audio: {
          ...(id ? {deviceId: {exact: id}} : {}), channelCount: 1, sampleRate: {ideal: 48000},
          echoCancellation: {exact: false}, noiseSuppression: {exact: false}, autoGainControl: {exact: false}}});
        if (this.closed) { this.environmentStream.getTracks().forEach(t => t.stop()); return; }
        const ambient = this.environmentStream.getAudioTracks()[0].getSettings(), voice = track.getSettings();
        if (ambient.echoCancellation !== false || ambient.noiseSuppression !== false || ambient.autoGainControl !== false
            || !(ambient.sampleRate >= 44100) || ![true, 'all', 'remote-only'].includes(voice.echoCancellation)) {
          throw new Error('Independent unprocessed input unavailable');
        }
        this.environmentSource = this.context.createMediaStreamSource(this.environmentStream);
        this.environmentCapture = new AudioWorkletNode(this.context, 'field-capture', {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
        this.environmentCapture.port.onmessage = event => {
          if (this.micOn && !this.closed) this.onAmbient?.(new Int16Array(event.data));
        };
        this.environmentSource.connect(this.environmentCapture).connect(this.context.destination);
        this.environmentStatus = 'Bird input opened; browser reports processing off. Bird listening pauses during assistant speech.';
        this.updateMic();
      } catch (_) {
        this.environmentStream?.getTracks().forEach(t => t.stop()); this.environmentStream = null;
        this.environmentSource?.disconnect(); this.environmentCapture?.disconnect();
        try { await track.applyConstraints(constraints); } catch (_) {}
        this.environmentStatus = 'Bird listening unavailable in this voice experiment: independent environmental input was not verified.';
      }
      if (!this.closed && ![true, 'all', 'remote-only'].includes(track.getSettings().echoCancellation)) {
        this.close();
        throw new Error('Voice echo cancellation could not be verified. Use the standard speaker mode on this device.');
      }
      this.onStatus?.(this.environmentStatus);
      this.event('rtc_capture_settings', this.settings());
    }
    async stats() {
      if (!this.peer || this.closed) return;
      try {
        const report = await this.peer.getStats();
        for (const row of report.values()) {
          if (row.type === 'inbound-rtp' && row.kind === 'audio') this.event('rtc_stats', {
            jitter: row.jitter, packetsLost: row.packetsLost, audioLevel: row.audioLevel});
          if (row.type === 'media-source' && row.kind === 'audio') this.event('rtc_aec_stats', {
            echoReturnLoss: row.echoReturnLoss, echoReturnLossEnhancement: row.echoReturnLossEnhancement, audioLevel: row.audioLevel});
        }
      } catch (_) {}
    }
    close() {
      this.closed = true; this.abort?.abort(); clearInterval(this.timer); clearInterval(this.statsTimer);
      this.environmentStream?.getTracks().forEach(t => t.stop()); this.environmentStream = null;
      this.environmentSource?.disconnect(); this.environmentCapture?.disconnect();
      this.remoteSource?.disconnect(); this.gain.disconnect(); this.channel?.close(); this.peer?.close(); this.playing = false;
    }
  }
  root.FieldWebRTCAudio = FieldWebRTCAudio;
  if (typeof module !== 'undefined') module.exports = {FieldWebRTCAudio};
})(typeof window !== 'undefined' ? window : globalThis);
