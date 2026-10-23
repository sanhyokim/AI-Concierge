// Short sounds from the caller while the AI is talking (「はい」「うん」) are back-channel, not a turn.
// 2026-10-23, Boss on Gemini 3.8 Live: 「途中で相槌をうつと話が止まってやり直しで言い直す」. In the record a 「はい」 of
// 0.2 s cut the read-back each time, even with the vendor's start-of-speech sensitivity set to low.
// So while the AI is talking, the caller's sound is held back here. It reaches the AI only once it has lasted
// minMs (a real interruption, such as 「ちょっと待って」 or a correction); a shorter sound is replaced by silence of the
// same length, so the stream keeps its timing. When the AI is silent, or has under tailMs left to say, every
// sound passes at once: a short 「はい」 then is an answer (the server's read-back tail rule already accepts it).

const SAMPLE_RATE = 16000;

export function levelDb(buf) {
  const pcm = new Int16Array(buf);
  if (!pcm.length) return -100;
  let s = 0;
  for (let i = 0; i < pcm.length; i++) { const v = pcm[i] / 32768; s += v * v; }
  const rms = Math.sqrt(s / pcm.length);
  return rms > 0 ? 20 * Math.log10(rms) : -100;
}

// send(buf): to the vendor. aiTalking(): true while the AI's voice still has more than the tail to play.
// thresholdDb(): the level that counts as the caller's voice (the lab's microphone threshold).
export function createBackchannelGate({ send, aiTalking, thresholdDb = () => -42, onBackchannel = () => {},
                                        onRelease = () => {}, minMs = 600, endSilenceMs = 300, openSilenceMs = 800 }) {
  let mode = "pass";          // pass | hold (sound while the AI talks, not yet long enough) | open (let through)
  let held = [], heldMs = 0, voicedMs = 0, quietMs = 0;
  const ms = (buf) => new Int16Array(buf).length / SAMPLE_RATE * 1000;
  const release = (why) => {
    for (const b of held) send(b);
    onRelease({ ms: Math.round(voicedMs), why });
    held = []; mode = "open"; quietMs = 0;
  };
  return {
    push(buf) {
      const dur = ms(buf);
      const voiced = levelDb(buf) > thresholdDb();
      if (mode === "pass") {
        if (voiced && aiTalking()) { mode = "hold"; held = [buf]; heldMs = dur; voicedMs = dur; quietMs = 0; return; }
        send(buf);
        return;
      }
      if (mode === "open") {   // the rest of a real interruption: through until the caller pauses
        send(buf);
        quietMs = voiced ? 0 : quietMs + dur;
        if (quietMs >= openSilenceMs) mode = "pass";
        return;
      }
      held.push(buf); heldMs += dur;
      if (voiced) { voicedMs += dur; quietMs = 0; } else quietMs += dur;
      if (voicedMs >= minMs) { release("long"); return; }
      if (!aiTalking()) { release("ai_done"); return; }   // the AI finished: from here on it is the caller's turn
      if (quietMs >= endSilenceMs) {   // a short sound over the AI: back-channel. Silence instead, same length
        for (const b of held) send(new ArrayBuffer(b.byteLength));
        onBackchannel({ ms: Math.round(voicedMs) });
        held = []; mode = "pass";
      }
    },
    get mode() { return mode; },
  };
}
