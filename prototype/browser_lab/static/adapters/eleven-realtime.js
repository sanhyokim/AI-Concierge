// ElevenLabs Eleven Realtime (v1 Max / v1 Mini) through its OpenAI-compatible Realtime API, over WebSocket only
// (ElevenLabs offers no WebRTC here). The local server mints a client secret with the session settings
// (instructions, tools, voice); the browser passes it as the OpenAI SDK does in a browser (WebSocket
// subprotocols). Mic and model audio: 24 kHz PCM, played by the page.
// Like Gemini, one model listens, thinks, speaks and calls the reception tools itself.
// When the caller starts speaking over the AI, everything not yet heard is dropped, the model is told how much
// was heard (conversation.item.truncate), and the interruption is reported to the server.
// Written from the public docs (2026-10-21); the connection has not been confirmed with a real account.
import { createPlayer, fromBase64, startCapture, toBase64 } from "../pcm.js";

const RATE = 24000;
const TRANSCRIPT_WAIT_MS = 1500;   // how long a tool call waits for the caller's words that came just before it
const OPENING_WAIT_MS = 3000;      // the model has this long to start the opening before it is asked another way

export async function connect(ctx) {
  const { client_secret: secret, ws_url: wsUrl, model, opening_request: opening, session_fallback: fallback } =
    ctx.credentials;
  if (fallback) ctx.log("注意", `ElevenLabs が設定の一部を断ったため、最小の設定でつないでいます（外したもの：${fallback.dropped}）。理由：${fallback.reason}`);
  const ac = ctx.audioContext;
  const out = ac.createGain();
  ctx.attachOutputNode(out);
  const player = createPlayer(ac, out, RATE);
  let capture = null, ready = false, closing = false, holding = false;
  let aiItem = null, aiQueuedMs = 0;              // the AI's audio item now playing, and how much of it was queued
  let transcriptWait = null;                       // resolves when the caller's words after a pause arrive
  let responded = false;                           // the model has started a response (the opening or a reply)
  const ws = new WebSocket(`${wsUrl}?model=${encodeURIComponent(model)}`,
                           ["realtime", `openai-insecure-api-key.${secret}`]);
  const send = (obj) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));

  ws.onopen = () => setTimeout(() => {
    if (!ready && !closing) ctx.vendorClosed("10秒たっても session.created が届かない");
  }, 10000);
  ws.onerror = () => ctx.log("vendor_error", "websocket error");
  ws.onclose = (e) => {
    ctx.log("vendor_closed", { code: e.code, reason: e.reason });
    if (!closing) ctx.vendorClosed(`code ${e.code}${e.reason ? "：" + e.reason : ""}`);
  };

  const waitForTranscript = () => {
    let done;
    const p = new Promise((r) => { done = r; });
    const timer = setTimeout(() => done(null), TRANSCRIPT_WAIT_MS);
    transcriptWait = { promise: p, resolve: (v) => { clearTimeout(timer); done(v); } };
  };

  ws.onmessage = async (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch (e) { return; }
    if (ev.type !== "response.output_audio.delta" && ev.type !== "input_audio_buffer.append") {
      ctx.log("vendor_event", { type: ev.type });
    }
    if (ev.type === "session.created") {
      if (ready) return;
      ready = true;
      capture = await startCapture(ac, ctx.micStream, RATE, (buf) =>
        send({ type: "input_audio_buffer.append", audio: toBase64(buf) }));
      // the opening (A-01) in the model's own voice (user decision 2026-10-20), then it waits for the caller.
      // 2026-10-21, first real call: a response.create carrying its own instructions was ignored without an error
      // (the AI spoke only after the caller's 「もしもし」). The request is now a conversation item, as with
      // Gemini; if nothing starts, the other form is tried once.
      if (opening) {
        send({ type: "conversation.item.create",
               item: { type: "message", role: "user", content: [{ type: "input_text", text: opening }] } });
        send({ type: "response.create" });
        setTimeout(() => {
          if (responded || closing) return;
          ctx.log("冒頭の案内", "AIが話し始めないので、別の頼み方でもう一度頼みました");
          send({ type: "response.create", response: { instructions: opening } });
        }, OPENING_WAIT_MS);
      }
    } else if (ev.type === "response.created") {
      responded = true;
    } else if (ev.type === "response.output_audio.delta") {
      if (holding || !ev.delta) return;
      if (ev.item_id !== aiItem) { aiItem = ev.item_id; aiQueuedMs = 0; }
      const buf = fromBase64(ev.delta);
      aiQueuedMs += (buf.byteLength / 2) / RATE * 1000;
      player.enqueue(buf);
    } else if (ev.type === "input_audio_buffer.speech_started") {
      const unplayedMs = player.pendingSeconds() * 1000;
      if (unplayedMs > 0) {
        player.clear();
        if (aiItem) {
          send({ type: "conversation.item.truncate", item_id: aiItem, content_index: 0,
                 audio_end_ms: Math.max(0, Math.round(aiQueuedMs - unplayedMs)) });
        }
        ctx.vendorInterrupted("eleven.input_audio_buffer.speech_started", unplayedMs);
      }
    } else if (ev.type === "input_audio_buffer.speech_stopped") {
      waitForTranscript();
    } else if (ev.type === "conversation.item.input_audio_transcription.completed") {
      const heard = await ctx.onTranscript("user", ev.transcript || "");
      if (transcriptWait) { transcriptWait.resolve(heard); transcriptWait = null; }
    } else if (ev.type === "response.output_audio_transcript.done") {
      ctx.onTranscript("ai", ev.transcript || "");
    } else if (ev.type === "response.function_call_arguments.done") {
      // the caller's 「はい」 can arrive after the model's tool call: let the server see it first
      const heard = transcriptWait ? await transcriptWait.promise : null;
      if (heard && heard.stop_ai) return;
      let args = {};
      try { args = JSON.parse(ev.arguments || "{}"); } catch (e) { /* keep empty */ }
      const result = await ctx.toolCall(ev.name, args);
      send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: ev.call_id,
             output: JSON.stringify(result) } });
      if (!(result && result.hangup)) send({ type: "response.create" });   // end_call: no new response
    } else if (ev.type === "error") {
      ctx.log("vendor_error", ev.error || ev);
    }
  };
  return {
    mute() { out.gain.value = 0; player.clear(); },
    // after end_call the closing words are queued; anything said after it is a repeat (2026-10-20, Gemini)
    holdNewSpeech(on) { holding = !!on; ctx.log("vendor_event", { type: on ? "hold_after_goodbye" : "resume_speech" }); },
    async close() { closing = true; if (capture) capture.stop(); player.clear(); ws.close(); },
  };
}
