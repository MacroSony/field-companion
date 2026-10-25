/* LiveKit owns RTP audio and interruption. This class only connects the phone. */
(function (root) {
  'use strict';
  class FieldLiveKitAudio {
    constructor({context, stream, output, endpoint, onEvent, onAmbient, onStatus, onFailure}) {
      Object.assign(this, {context, stream, endpoint, onEvent, onAmbient, onStatus, onFailure});
      this.closed = false; this.ready = false; this.playing = false; this.micOn = true; this.muted = false;
      this.elements = new Set(); this.references = new Set(); this.remoteTracks = new Map();
      this.environmentStatus = 'Bird input not yet opened';
      // Diagnostic tap only. It never drives the speaker: LiveKit track.attach() does.
      this.diagnosticOutput = context.createGain(); output.connect(this.diagnosticOutput);
      this.localOutput = output;
    }
    event(type, fields = {}) { this.onEvent?.(type, fields); }
    settings() {
      return {voice: this.stream.getAudioTracks()[0]?.getSettings() || {},
        environment: this.environmentStream?.getAudioTracks()[0]?.getSettings() || {}, status: this.environmentStatus};
    }
    updateMic() {
      for (const track of this.stream.getAudioTracks()) track.enabled = this.micOn && this.ready && !this.closed;
      for (const track of this.environmentStream?.getAudioTracks() || []) track.enabled = this.micOn && !this.closed;
    }
    setMic(on) { this.micOn = on; this.updateMic(); }
    setAuxPlayback() {}
    setMuted(on) {
      this.muted = on;
      for (const element of this.elements) element.muted = on;
      for (const reference of this.references) reference.gain.gain.value = on ? 0 : 1;
    }
    async resumeOutput() { if (this.room) await this.room.startAudio(); }
    handleState(state) { this.playing = !!state.speaking; }
    flush() { this.event('livekit_playback_owned_by_framework'); }
    removeRemote(key) {
      const attached = this.remoteTracks.get(key);
      if (!attached) return;
      const {track, element, reference} = attached;
      element.pause();
      try { track.detach(element); } catch (_) {}
      element.srcObject = null; element.remove(); this.elements.delete(element);
      reference.source.disconnect(); reference.gain.disconnect(); this.references.delete(reference);
      this.remoteTracks.delete(key);
    }
    async connect(capability) {
      if (this.room || this.closed) return;
      this.abort = new AbortController();
      const response = await fetch(this.endpoint, {method: 'POST',
        headers: {'X-Field-RTC': capability}, signal: this.abort.signal});
      if (!response.ok) throw new Error(response.status === 503 ? 'LiveKit project setup is required on the computer.' : 'LiveKit connection failed. End and start again.');
      const connection = await response.json();
      if (this.closed) return;
      const sdk = root.LivekitClient;
      if (!sdk) throw new Error('LiveKit browser SDK was not loaded. Refresh this page.');
      this.room = new sdk.Room({adaptiveStream: false, dynacast: false, disconnectOnPageLeave: true});
      this.room.on(sdk.RoomEvent.TrackSubscribed, (track, publication, participant) => {
        if (track.kind !== sdk.Track.Kind.Audio || participant.identity !== 'agent-' + connection.room || this.closed) return;
        const key = publication.trackSid || track.sid || track;
        if (this.remoteTracks.get(key)?.track === track) return;
        // The agent publishes one speech track. A replacement must never leave
        // the old HTML audio element playing alongside the new one.
        for (const existing of this.remoteTracks.keys()) this.removeRemote(existing);
        const element = track.attach(); element.autoplay = true; element.playsInline = true; element.muted = this.muted;
        element.dataset.fieldLivekitAudio = 'true'; document.body.append(element); this.elements.add(element);
        element.play().catch(() => { if (!this.closed) this.onStatus?.('Tap to talk once to enable assistant audio.'); });
        const source = this.context.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
        const gain = this.context.createGain(); gain.gain.value = this.muted ? 0 : 1;
        const reference = {source, gain};
        source.connect(gain).connect(this.diagnosticOutput); this.references.add(reference);
        this.remoteTracks.set(key, {track, element, reference});
        this.event('livekit_remote_audio_attached');
      });
      this.room.on(sdk.RoomEvent.TrackUnsubscribed, (track, publication) => {
        const key = publication.trackSid || track.sid || track;
        if (this.remoteTracks.get(key)?.track === track) this.removeRemote(key);
      });
      this.room.on(sdk.RoomEvent.Disconnected, () => {
        if (!this.closed) { this.close(); this.onFailure?.('LiveKit disconnected. Start a new session.'); }
      });
      this.room.on(sdk.RoomEvent.Reconnecting, () => this.onStatus?.('LiveKit is reconnecting…'));
      await this.room.connect(connection.url, connection.token, {autoSubscribe: true});
      if (this.closed) { await this.room.disconnect(); return; }
      await this.room.localParticipant.publishTrack(this.stream.getAudioTracks()[0], {source: sdk.Track.Source.Microphone});
      if (this.closed) return;
      this.ready = true; this.updateMic();
      this.event('livekit_connected', {state: 'connected', transport: 'livekit'});
      await this.openEnvironment();
      if (!this.closed) this.onStatus?.(this.environmentStatus);
    }
    async openEnvironment() {
      // Reuse verified independent raw-input capture, never the processed voice track.
      await root.FieldWebRTCAudio.prototype.openEnvironment.call(this);
    }
    close() {
      if (this.closed) return;
      this.closed = true; this.ready = false; this.abort?.abort();
      this.updateMic();
      Promise.resolve(this.room?.disconnect()).catch(() => {});
      for (const key of this.remoteTracks.keys()) this.removeRemote(key);
      for (const element of this.elements) { element.pause(); element.srcObject = null; element.remove(); }
      this.elements.clear();
      for (const {source, gain} of this.references) { source.disconnect(); gain.disconnect(); }
      this.references.clear();
      try { this.localOutput.disconnect(this.diagnosticOutput); } catch (_) {}
      this.diagnosticOutput.disconnect();
      this.environmentStream?.getTracks().forEach(track => track.stop()); this.environmentStream = null;
      this.environmentSource?.disconnect(); this.environmentCapture?.disconnect(); this.playing = false;
    }
  }
  root.FieldLiveKitAudio = FieldLiveKitAudio;
  if (typeof module !== 'undefined') module.exports = {FieldLiveKitAudio};
})(typeof window !== 'undefined' ? window : globalThis);
