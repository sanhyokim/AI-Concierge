// Google Gemini 3.8 Live over WebSocket with a one-use ephemeral token (BidiGenerateContentConstrained).
// Mic: 16 kHz PCM. Model audio: PCM (24 kHz unless the mimeType says otherwise). On serverContent.interrupted
// everything not yet heard is dropped and the interruption is reported to the server (an unanswered readback
// is then not confirmed). Caller transcripts are sent before tool calls so the server sees the reply first.
// While the AI talks, the caller's short sounds (back-channel 「はい」) are held back and not sent (backchannel.js).
// Written from the public docs (2026-10-05); the connection has not been confirmed with a real account.
import { createPlayer, fromBase64, startCapture, toBase64 } from "../pcm.js";
import { createBackchannelGate } from "../backchannel.js";

const OPENING_WAIT_MS = 4000;   // the opening's voice has this long to arrive before it is asked for once more
const MIC_WAIT_MS = 8000;       // the mic is sent once the opening is heard, or after this long at the latest
const TAIL_S = 1.5;             // under this much of the AI's voice left, a short 「はい」 is an answer, not back-channel

export async function connect(ctx) {
  const { token, ws_url: wsUrl, setup } = ctx.credentials;
  const ac = ctx.audioContext;
  const out = ac.createGain();
  ctx.attachOutputNode(out);
  let player = createPlayer(ac, out, 24000), playerRate = 24000;
  let capture = null, userBuf = "", aiBuf = "";
  let toolsPending = 0, userTimer = null, generationDone = true;
  // after end_call: the closing words are already queued; anything the model says after the tool's answer is a
  // repeat (2026-10-20: 「お電話ありがとうございました。…失礼いたします。」 was heard twice), so it is not played
  let holding = false;
  // 2026-10-21: the opening came back as words only (its transcript, no sound) and the caller heard nothing
  let micStarted = false, openingAudio = false, openingRetried = false, turnAudioMs = 0, turnTextParts = 0, turnSpoke = false;
  const flushUser = () => {
    clearTimeout(userTimer);
    const t = userBuf; userBuf = "";
    return t ? ctx.onTranscript("user", t) : Promise.resolve(null);
  };
  // the model's own transcript sometimes holds only a marker (「...」「<no speech>{pause}」「___」): not words
  const spoken = (t) => t.replace(/<[^>]*>|\{[^}]*\}|[.…_\s。、]/g, "") ? t : "";
  const ws = new WebSocket(`${wsUrl}?access_token=${encodeURIComponent(token)}`);
  const send = (obj) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));

  let ready = false, closing = false;
  ws.onopen = () => {
    send(setup);
    setTimeout(() => { if (!ready && !closing) ctx.vendorClosed("10秒たっても setupComplete が届かない"); }, 10000);
  };
  ws.onerror = () => ctx.log("vendor_error", "websocket error");
  ws.onclose = (e) => {
    ctx.log("vendor_closed", { code: e.code, reason: e.reason });
    if (!closing) ctx.vendorClosed(`code ${e.code}${e.reason ? "：" + e.reason : ""}`);
  };
  ws.onmessage = async (msg) => {
    const text = typeof msg.data === "string" ? msg.data : await msg.data.text();
    let ev;
    try { ev = JSON.parse(text); } catch (e) { return; }
    if (ev.setupComplete) {
      ready = true;
      ctx.log("vendor_event", { type: "setupComplete" });
      const opening = ctx.credentials.opening_request;
      if (!opening) { await startMic(); return; }
      // the opening (A-01) in the model's own voice (user decision 2026-10-20), then it waits for the caller.
      // The mic waits until the opening is heard, so a sound in the room cannot end or cut the request.
      send({ realtimeInput: { text: opening } });
      setTimeout(() => askOpeningAgain("4秒たってもAIの声が届かない"), OPENING_WAIT_MS);
      setTimeout(startMic, MIC_WAIT_MS);
      return;
    }
    // one answer's bill (the whole context is read again each time): the server adds them up (2026-10-29)
    if (ev.usageMetadata) ctx.vendorEvent("voice_usage", ev.usageMetadata);
    const sc = ev.serverContent;
    if (sc) {
      for (const part of (sc.modelTurn && sc.modelTurn.parts) || []) {
        const d = part.inlineData;
        if (d && d.data && !holding) {
          const m = /rate=(\d+)/.exec(d.mimeType || "");
          const rate = m ? Number(m[1]) : 24000;
          if (rate !== playerRate) { player = createPlayer(ac, out, rate); playerRate = rate; }
          const buf = fromBase64(d.data);
          player.enqueue(buf);
          turnAudioMs += (buf.byteLength / 2) / rate * 1000;
          generationDone = false;
          if (!openingAudio) { openingAudio = true; startMic(); }
        } else if (part.text) {
          turnTextParts++;
        }
      }
      if (sc.generationComplete) generationDone = true;
      if (sc.interrupted) {
        // how much of the answer was not heard yet: only known once the whole answer has been generated
        const unplayedMs = generationDone ? player.pendingSeconds() * 1000 : null;
        player.clear();
        ctx.vendorInterrupted("gemini.serverContent.interrupted", unplayedMs);
      }
      if (sc.inputTranscription && sc.inputTranscription.text) {
        userBuf += sc.inputTranscription.text;
        // the caller's words can arrive after the model's tool call (2026-10-20: a 「はい」 to the read-back
        // reached the server only after confirm_field had been refused): send them while a tool call waits
        if (toolsPending) { clearTimeout(userTimer); userTimer = setTimeout(flushUser, 500); }
      }
      if (sc.outputTranscription && sc.outputTranscription.text) aiBuf += sc.outputTranscription.text;
      if (sc.turnComplete || sc.generationComplete) {
        flushUser();
        if (spoken(aiBuf)) { ctx.onTranscript("ai", aiBuf); turnSpoke = true; }
        if (sc.turnComplete) {
          ctx.log("vendor_event", { type: "turnComplete", audio_ms: Math.round(turnAudioMs), text_parts: turnTextParts });
          if (!openingAudio && turnSpoke) askOpeningAgain("AIの文字だけが届き、声が届かなかった");
          turnAudioMs = 0; turnTextParts = 0; turnSpoke = false;
        }
        aiBuf = "";
      }
    }
    if (ev.toolCall) {
      const responses = [];
      const heard = await flushUser();
      if (heard && heard.stop_ai) return;
      toolsPending++;
      try {
        for (const fc of ev.toolCall.functionCalls || []) {
          const result = await ctx.toolCall(fc.name, fc.args || {});
          responses.push({ id: fc.id, name: fc.name, response: { result } });
        }
      } finally { toolsPending--; }
      send({ toolResponse: { functionResponses: responses } });
    }
  };
  async function startMic() {
    if (micStarted || closing) return;
    micStarted = true;
    const gate = createBackchannelGate({
      send: (buf) => send({ realtimeInput: { audio: { data: toBase64(buf), mimeType: "audio/pcm;rate=16000" } } }),
      // the AI is talking while its voice is queued; once the whole answer is in, only beyond the last TAIL_S
      aiTalking: () => (generationDone ? player.pendingSeconds() > TAIL_S : player.pendingSeconds() > 0.05),
      thresholdDb: () => (ctx.micThresholdDb ? ctx.micThresholdDb() : -42),
      onBackchannel: (d) => ctx.log("相づち", `AIの話し中の短い声（${d.ms} ms）は、AIに渡さなかった（話を止めない）`),
      onRelease: (d) => ctx.log("話し中の割り込み", d.why === "long" ? `AIの話し中に${d.ms} ms以上話したので、AIに渡した`
        : "AIが話し終えたので、お客様の声をそのままAIに渡した"),
    });
    capture = await startCapture(ac, ctx.micStream, 16000, (buf) => gate.push(buf));
    if (closing) capture.stop();
  }
  function askOpeningAgain(why) {
    if (openingAudio || openingRetried || closing) return;
    openingRetried = true;
    ctx.log("冒頭の案内", `${why}ので、もう一度頼みました`);
    send({ realtimeInput: { text: ctx.credentials.opening_request } });
  }
  return {
    mute() { out.gain.value = 0; player.clear(); },
    holdNewSpeech(on) { holding = !!on; ctx.log("vendor_event", { type: on ? "hold_after_goodbye" : "resume_speech" }); },
    async close() { closing = true; if (capture) capture.stop(); player.clear(); ws.close(); },
  };
}
