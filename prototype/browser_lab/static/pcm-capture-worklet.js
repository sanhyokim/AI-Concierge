// AudioWorklet: microphone (context rate) -> 16-bit PCM at a target rate, posted in ~40 ms chunks.
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.target = (options.processorOptions && options.processorOptions.targetRate) || 16000;
    this.ratio = sampleRate / this.target;
    this.buf = [];
    this.pos = 0;
    this.chunk = Math.round(this.target * 0.04);
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    // simple decimation with averaging; adequate for speech at 16 kHz
    for (; this.pos < ch.length; this.pos += this.ratio) {
      const i = Math.floor(this.pos);
      const j = Math.min(ch.length - 1, Math.floor(this.pos + this.ratio) - 1);
      let s = 0, n = 0;
      for (let k = i; k <= Math.max(i, j); k++) { s += ch[k]; n++; }
      this.buf.push(Math.max(-1, Math.min(1, s / n)));
    }
    this.pos -= ch.length;
    if (this.buf.length >= this.chunk) {
      const out = new Int16Array(this.buf.length);
      for (let k = 0; k < this.buf.length; k++) out[k] = this.buf[k] * 32767;
      this.port.postMessage(out.buffer, [out.buffer]);
      this.buf = [];
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);
