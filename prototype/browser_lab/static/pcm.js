// Shared audio helpers for adapters that exchange raw PCM over WebSocket (Gemini Live, Cartesia).

export function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export function fromBase64(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

// Capture the microphone as 16-bit PCM at targetRate; onChunk(ArrayBuffer) every ~40 ms.
export async function startCapture(audioContext, micStream, targetRate, onChunk) {
  await audioContext.audioWorklet.addModule(new URL("./pcm-capture-worklet.js", import.meta.url));
  const src = audioContext.createMediaStreamSource(micStream);
  const node = new AudioWorkletNode(audioContext, "pcm-capture", { processorOptions: { targetRate } });
  node.port.onmessage = (e) => onChunk(e.data);
  const sink = audioContext.createGain();
  sink.gain.value = 0;                 // keep the graph pulling without sending the mic to the speakers
  src.connect(node).connect(sink).connect(audioContext.destination);
  return { stop() { try { src.disconnect(); node.disconnect(); sink.disconnect(); } catch (e) { /* closed */ } } };
}

// Play 16-bit PCM chunks back to back; clear() drops everything not yet heard (interruption).
export function createPlayer(audioContext, outputNode, sampleRate) {
  let playAt = 0;
  const live = new Set();
  return {
    enqueue(buffer) {
      const pcm = new Int16Array(buffer);
      if (!pcm.length) return;
      const ab = audioContext.createBuffer(1, pcm.length, sampleRate);
      const ch = ab.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
      const src = audioContext.createBufferSource();
      src.buffer = ab;
      src.connect(outputNode);
      const now = audioContext.currentTime;
      playAt = Math.max(playAt, now + 0.02);
      src.start(playAt);
      playAt += ab.duration;
      live.add(src);
      src.onended = () => live.delete(src);
    },
    clear() {
      for (const src of live) { try { src.stop(); } catch (e) { /* already stopped */ } }
      live.clear();
      playAt = 0;
    },
    pendingSeconds() { return Math.max(0, playAt - audioContext.currentTime); },
  };
}
