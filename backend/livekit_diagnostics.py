"""Native RTC voice capture for diagnostics; never use the BirdNET mic ring."""
from collections import deque
import time
import uuid

import numpy as np
from session_data import extract_turn_audio


class VoiceCapture:
    rate = 48000

    def __init__(self, owner):
        self.owner = owner
        self.ring = np.zeros(30 * self.rate, dtype=np.float32)
        self.position = self.available_from = 0
        self.was_enabled = False
        self.turns = deque(maxlen=128)
        self.active = None
        self.final_ids = deque(maxlen=256)

    @property
    def enabled(self):
        return bool(self.owner.recording_enabled or self.owner.speaker_service or self.owner.enrollment)

    def feed(self, samples):
        samples = np.asarray(samples, dtype=np.float32)
        count = len(samples)
        start = self.position
        self.position += count
        if not self.enabled:
            if self.was_enabled:
                self.ring.fill(0)
            self.available_from = self.position
            self.was_enabled = False
            return
        self.was_enabled = True
        keep = samples[-len(self.ring):]
        indices = np.arange(self.position-len(keep), self.position) % len(self.ring)
        self.ring[indices] = keep
        if self.active and self.owner.enrollment:
            self.active['enrollment'] = True
        if self.owner.recorder:
            self.owner.recorder.poll_audio(self.position)

    def extract(self, start, stop, padding=.3):
        requested_start = start - round(padding*self.rate)
        requested_end = stop + round(padding*self.rate)
        actual_start = max(requested_start, self.available_from)
        samples, bounds = extract_turn_audio(self.ring, self.position, actual_start,
            max(actual_start, requested_end), sample_rate=self.rate, padding=0)
        bounds.update(speech_start_sample=start, speech_end_sample=stop,
            requested_start_sample=requested_start, requested_end_sample=requested_end,
            clipped=bounds['clipped'] or bounds['start_sample'] != requested_start)
        return samples, bounds

    def begin(self):
        if self.active:
            return
        record_id = None
        if self.enabled and self.owner.recorder:
            record_id = self.owner.recorder.start('livekit-'+uuid.uuid4().hex,
                self.position, time.monotonic(), 'livekit-native-voice')
        self.active = {'recording_id':record_id, 'final':False, 'stopped':False,
                       'enrollment':bool(self.owner.enrollment),
                       'overlap':bool(self.owner.voice_speaking)}
        if record_id:
            self.owner.recorder.update(record_id, alignment='livekit_vad_native_voice_position',
                transport='livekit', voice_sample_rate=self.rate)
        self.turns.append(self.active)

    def end(self):
        if self.active:
            self.active['stopped'] = True
            if self.active['recording_id'] and self.owner.recorder:
                self.owner.recorder.stop(self.active['recording_id'],self.position,time.monotonic())
            self.active = None

    def final(self, text, item_id=None):
        if item_id and item_id in self.final_ids:
            return {'duplicate':True,'recording_id':None}
        if item_id:
            self.final_ids.append(item_id)
        turn = next((row for row in self.turns if not row['final']), None)
        if turn is None:
            turn = {'recording_id':None,'enrollment':bool(self.owner.enrollment),
                    'overlap':bool(self.owner.voice_speaking),'final':False}
        turn['final'] = True
        if turn is self.active:
            self.end()
        if turn['recording_id'] and self.owner.recorder:
            self.owner.recorder.update(turn['recording_id'], item_id=item_id)
            self.owner.recorder.input_final(turn['recording_id'],text,self.position,time.monotonic())
        return turn

    def settle(self, turn, outcome, reason=None):
        if turn.get('recording_id') and self.owner.recorder:
            self.owner.recorder.settle(turn['recording_id'],outcome=outcome,reason=reason)

    def clear(self):
        self.ring.fill(0)
        self.available_from = self.position
        self.turns.clear()
        self.active = None
        self.final_ids.clear()
