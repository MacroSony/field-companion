/* Native-rate mono capture; app.js linearly resamples if the context is not 48 kHz. */
class FieldCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Int16Array(Math.round(sampleRate * 0.02));
    this.offset = 0;
  }
  process(inputs, outputs) {
    const channel = inputs[0]?.[0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        const sample = Math.max(-1, Math.min(1, channel[i]));
        this.chunk[this.offset++] = sample < 0 ? sample * 32768 : sample * 32767;
        if (this.offset === this.chunk.length) {
          this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
          this.chunk = new Int16Array(Math.round(sampleRate * 0.02));
          this.offset = 0;
        }
      }
    }
    // Keep the graph alive without monitoring the microphone through the speaker.
    for (const output of outputs) for (const channel of output) channel.fill(0);
    return true;
  }
}
registerProcessor('field-capture', FieldCapture);
