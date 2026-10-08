// Google Gemini 3.8 Live over WebSocket with a one-use ephemeral token (BidiGenerateContentConstrained).
// Mic: 16 kHz PCM. Model audio: PCM (24 kHz unless the mimeType says otherwise). On serverContent.interrupted
// everything not yet heard is dropped and the interruption is reported to the server (an unanswered readback
// is then not confirmed). Caller transcripts are sent before tool calls so the server sees the reply first.
// Written from the public docs (2026-10-05); the connection has not been confirmed with a real account.
import { createPlayer, fromBase64, startCapture, toBase64 } from "../pcm.js";

export async function connect(ctx) {
  const { token, ws_url: wsUrl, setup } = ctx.credentials;
  const ac = ctx.audioContext;
  const out = ac.createGain();
  ctx.attachOutputNode(out);
  let player = createPlayer(ac, out, 24000);
  let capture = null, userBuf = "", aiBuf = "";
  const flushUser = () => { const t = userBuf; userBuf = ""; return t ? ctx.onTranscript("user", t) : Promise.resolve(null); };
  const ws = new WebSocket(`${wsUrl}?access_token=${encodeURIComponent(token)}`);
  const send = (obj) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));

  ws.onopen = () => send(setup);
  ws.onerror = () => ctx.log("vendor_error", "websocket error");
  ws.onclose = (e) => ctx.log("vendor_closed", { code: e.code, reason: e.reason });
  ws.onmessage = async (msg) => {
    const text = typeof msg.data === "string" ? msg.data : await msg.data.text();
    let ev;
    try { ev = JSON.parse(text); } catch (e) { return; }
    if (ev.setupComplete) {
      ctx.log("vendor_event", { type: "setupComplete" });
      capture = await startCapture(ac, ctx.micStream, 16000, (buf) =>
        send({ realtimeInput: { audio: { data: toBase64(buf), mimeType: "audio/pcm;rate=16000" } } }));
      send({ realtimeInput: { text: "最初のあいさつをしてください。" } });
      return;
    }
    const sc = ev.serverContent;
    if (sc) {
      for (const part of (sc.modelTurn && sc.modelTurn.parts) || []) {
        const d = part.inlineData;
        if (d && d.data) {
          const m = /rate=(\d+)/.exec(d.mimeType || "");
          if (m && Number(m[1]) !== 24000) player = createPlayer(ac, out, Number(m[1]));
          player.enqueue(fromBase64(d.data));
        }
      }
      if (sc.interrupted) { player.clear(); ctx.vendorInterrupted("gemini.serverContent.interrupted"); }
      if (sc.inputTranscription && sc.inputTranscription.text) userBuf += sc.inputTranscription.text;
      if (sc.outputTranscription && sc.outputTranscription.text) aiBuf += sc.outputTranscription.text;
      if (sc.turnComplete || sc.generationComplete) {
        flushUser();
        if (aiBuf) { ctx.onTranscript("ai", aiBuf); aiBuf = ""; }
      }
    }
    if (ev.toolCall) {
      const responses = [];
      const heard = await flushUser();
      if (heard && heard.stop_ai) return;
      for (const fc of ev.toolCall.functionCalls || []) {
        const result = await ctx.toolCall(fc.name, fc.args || {});
        responses.push({ id: fc.id, name: fc.name, response: { result } });
      }
      send({ toolResponse: { functionResponses: responses } });
    }
  };
  return {
    mute() { out.gain.value = 0; player.clear(); },
    async close() { if (capture) capture.stop(); player.clear(); ws.close(); },
  };
}
