/* Both inputs share AudioContext's clock: mic (left), post-gain playback (right). */
class FieldDiagnosticCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frames = Math.round(sampleRate * .02);
    this.samples = new Int16Array(this.frames * 2);
    this.offset = 0;
    this.startFrame = 0;
  }
  process(inputs, outputs) {
    const n = outputs[0]?.[0]?.length || 128;
    for (let i = 0; i < n; i++) {
      if (!this.offset) this.startFrame = currentFrame + i;
      for (let side = 0; side < 2; side++) {
        const channel = inputs[side]?.[0];
        const value = Math.max(-1, Math.min(1, channel?.[i] || 0));
        this.samples[this.offset * 2 + side] = value < 0 ? value * 32768 : value * 32767;
      }
      if (++this.offset === this.frames) {
        this.port.postMessage({frame: this.startFrame, rate: sampleRate, pcm: this.samples.buffer}, [this.samples.buffer]);
        this.samples = new Int16Array(this.frames * 2);
        this.offset = 0;
      }
    }
    for (const output of outputs) for (const channel of output) channel.fill(0);
    return true;
  }
}
registerProcessor('field-diagnostic-capture', FieldDiagnosticCapture);
