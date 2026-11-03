// ElevenAgents (ElevenLabs), the three-stage path (2026-11-03, user: 「「聞き取り → 考えるAI → 読み上げ」の3段の作りで
// 一本化がもっとも安定しそうですね。あなたの提案で進めましょう」). ElevenLabs hears the caller, the text model chosen on
// the page writes the answer, and ElevenLabs reads exactly that text in the chosen voice. The local server has
// updated the lab's agent for this call and minted a signed URL (credentials.signed_url); this page talks to the
// agent over its WebSocket: the mic as base64 16-bit PCM (user_audio_chunk), the agent's voice back as audio events.
// The opening (A-01) is the agent's first message. Client tools come here and go to the reception service.
// As with the other candidates, the caller's short sounds while the AI talks (back-channel 「はい」) are held back.
// Written from the public API reference (2026-11-03); the connection has not been confirmed with a real account.
import { createPlayer, fromBase64, startCapture, toBase64 } from "../pcm.js";
import { createBackchannelGate } from "../backchannel.js";

const READY_WAIT_MS = 10000;   // conversation_initiation_metadata must come within this after the socket opens
const TAIL_S = 1.5;            // under this much of the AI's voice left, a short 「はい」 is an answer
const rateOf = (format, fallback) => {
  const m = /^pcm_(\d+)$/.exec(format || "");
  return m ? Number(m[1]) : fallback;
};

export async function connect(ctx) {
  const c = ctx.credentials;
  if (c.agent_fallback) {
    ctx.log("注意", `ElevenLabs がエージェントの設定の一部を断ったため、最小の設定でつないでいます（外したもの：${c.agent_fallback.dropped}）。理由：${c.agent_fallback.reason}`);
  }
  ctx.log("考えるAI", c.llm || "");
  const ac = ctx.audioContext;
  const out = ac.createGain();
  ctx.attachOutputNode(out);
  let player = null, capture = null, ready = false, closing = false, holding = false;
  let outRate = c.output_rate || 24000, inRate = c.input_rate || 16000;
  let cutAt = -1;                       // audio of an answer the caller cut off: event ids up to this are dropped
  let posting = Promise.resolve();      // the transcripts reach the server before the tool calls that follow them
  const queue = (role, words) => { posting = posting.then(() => ctx.onTranscript(role, words)).catch(() => null); };

  const ws = new WebSocket(c.signed_url);
  const send = (obj) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));
  ws.onopen = () => {
    send({ type: "conversation_initiation_client_data" });
    setTimeout(() => {
      if (!ready && !closing) ctx.vendorClosed("10秒たっても conversation_initiation_metadata が届かない");
    }, READY_WAIT_MS);
  };
  ws.onerror = () => ctx.log("vendor_error", "websocket error");
  ws.onclose = (e) => {
    ctx.log("vendor_closed", { code: e.code, reason: e.reason });
    if (!closing) ctx.vendorClosed(`code ${e.code}${e.reason ? "：" + e.reason : ""}`);
  };

  ws.onmessage = async (msg) => {
    let ev;
    try { ev = JSON.parse(typeof msg.data === "string" ? msg.data : await msg.data.text()); } catch (e) { return; }
    const t = ev.type || "";
    if (t !== "audio" && t !== "ping" && t !== "vad_score") ctx.log("vendor_event", t === "client_error" ? ev : { type: t });
    if (t === "conversation_initiation_metadata") {
      if (ready) return;
      ready = true;
      const m = ev.conversation_initiation_metadata_event || {};
      outRate = rateOf(m.agent_output_audio_format, outRate);
      inRate = rateOf(m.user_input_audio_format, inRate);
      player = createPlayer(ac, out, outRate);
      if (m.conversation_id) {
        ctx.log("ElevenLabsの会話ID", m.conversation_id);
        if (ctx.vendorEvent) ctx.vendorEvent("vendor_session", { conversation_id: m.conversation_id });
      }
      startMic();
    } else if (t === "ping") {
      send({ type: "pong", event_id: (ev.ping_event || {}).event_id });
    } else if (t === "audio") {
      const a = ev.audio_event || {};
      if (holding || !player || !a.audio_base_64) return;
      if (a.event_id != null && a.event_id <= cutAt) return;
      player.enqueue(fromBase64(a.audio_base_64));
    } else if (t === "interruption") {
      const i = ev.interruption_event || {};
      if (i.event_id != null) cutAt = Math.max(cutAt, i.event_id);
      const unplayedMs = player ? player.pendingSeconds() * 1000 : 0;
      if (player) player.clear();
      if (unplayedMs > 0) ctx.vendorInterrupted("elevenagents.interruption", unplayedMs);
    } else if (t === "user_transcript") {
      const words = ((ev.user_transcription_event || {}).user_transcript || "").trim();
      if (words) queue("user", words);
    } else if (t === "agent_response") {
      // the text model's answer: exactly what ElevenLabs reads aloud
      const words = ((ev.agent_response_event || {}).agent_response || "").trim();
      if (words) queue("ai", words);
    } else if (t === "agent_response_correction") {
      const k = ev.agent_response_correction_event || {};
      ctx.log("AIの返事（途中で止まった）", { heard: k.corrected_agent_response, whole: k.original_agent_response });
    } else if (t === "client_tool_call") {
      const call = ev.client_tool_call || {};
      // the caller's 「はい」 and the AI's read-back reach the server before the tool that they confirm
      await posting;
      const result = await ctx.toolCall(call.tool_name, call.parameters || {});
      if (call.expects_response === false) return;
      send({ type: "client_tool_result", tool_call_id: call.tool_call_id, result: JSON.stringify(result), is_error: false });
    }
  };

  async function startMic() {
    if (capture || closing) return;
    const gate = createBackchannelGate({
      send: (buf) => send({ user_audio_chunk: toBase64(buf) }),
      aiTalking: () => !!player && player.pendingSeconds() > TAIL_S,
      thresholdDb: () => (ctx.micThresholdDb ? ctx.micThresholdDb() : -42),
      sampleRate: inRate,
      onBackchannel: (d) => ctx.log("相づち", `AIの話し中の短い声（${d.ms} ms）は、AIに渡さなかった（話を止めない）`),
      onRelease: (d) => ctx.log("話し中の割り込み", d.why === "long" ? `AIの話し中に${d.ms} ms以上話したので、AIに渡した`
        : "AIが話し終えたので、お客様の声をそのままAIに渡した"),
    });
    capture = await startCapture(ac, ctx.micStream, inRate, (buf) => gate.push(buf));
    if (closing) capture.stop();
  }

  return {
    mute() { out.gain.value = 0; if (player) player.clear(); },
    // after end_call the closing words are queued; anything said after it is a repeat (2026-10-20, Gemini)
    holdNewSpeech(on) { holding = !!on; ctx.log("vendor_event", { type: on ? "hold_after_goodbye" : "resume_speech" }); },
    async close() { closing = true; if (capture) capture.stop(); if (player) player.clear(); ws.close(); },
  };
}
