// Collects microphone samples for the workbench and posts them to the page at 16 kHz.
// Served as a fixed same-origin file (/voice-worklet.js); the page turns the samples into a WAV.
class ZhiqiuRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.last = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || !input.length) return true;
    if (this.ratio === 1) {
      this.port.postMessage(input.slice(0));
      return true;
    }
    // Linear resampling for browsers that ignore AudioContext({sampleRate: 16000}).
    const out = [];
    while (this.pos < input.length - 1) {
      const i = Math.floor(this.pos), f = this.pos - i;
      const a = i < 0 ? this.last : input[i];
      out.push(a + (input[i + 1] - a) * f);
      this.pos += this.ratio;
    }
    this.pos -= input.length;
    this.last = input[input.length - 1];
    if (out.length) this.port.postMessage(Float32Array.from(out));
    return true;
  }
}

registerProcessor("zhiqiu-recorder", ZhiqiuRecorder);
