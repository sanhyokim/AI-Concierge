// Cartesia Managed Agents over the agent WebSocket (wss://api.cartesia.ai/v1/agents/websocket/{agent_id})
// with a short-lived access token. Protocol from the public docs (2026-10-07):
//   session_create -> session_ready; audio_input / audio_output (base64, same format both ways: pcm_16000);
//   audio_output_clear (barge-in); turn_output_text_delta / turn_ended (transcripts);
//   client_tool_call -> client_tool_result (business logic through the common reception service).
// The client tools must be created in Cartesia (dashboard "Client function" or POST /v1/agents/tools) and
// attached to the agent: docs/browser-lab-setup.md. The connection has not been confirmed with a real account.
import { createPlayer, fromBase64, startCapture, toBase64 } from "../pcm.js";

export async function connect(ctx) {
  const { token, ws_url: wsUrl, cartesia_version: version, start } = ctx.credentials;
  const ac = ctx.audioContext;
  const out = ac.createGain();
  ctx.attachOutputNode(out);
  const player = createPlayer(ac, out, 16000);
  let capture = null, lastUser = "", aiBuf = "";
  const ws = new WebSocket(`${wsUrl}?cartesia_version=${encodeURIComponent(version)}&access_token=${encodeURIComponent(token)}`);
  const send = (obj) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));
  ws.onopen = () => send(start);   // session_create must be the first message, within 10 s
  ws.onerror = () => ctx.log("vendor_error", "websocket error");
  ws.onclose = (e) => ctx.log("vendor_closed", { code: e.code, reason: e.reason });
  ws.onmessage = async (msg) => {
    let ev;
    try { ev = JSON.parse(typeof msg.data === "string" ? msg.data : await msg.data.text()); } catch (e) { return; }
    if (ev.type !== "audio_output") ctx.log("vendor_event", { type: ev.type });
    if (ev.type === "session_ready") {
      capture = await startCapture(ac, ctx.micStream, 16000, (buf) => send({ type: "audio_input", audio: toBase64(buf) }));
    } else if (ev.type === "audio_output" && ev.audio) {
      player.enqueue(fromBase64(ev.audio));
    } else if (ev.type === "audio_output_clear") {
      player.clear();
    } else if (ev.type === "turn_output_text_delta") {
      aiBuf += ev.text || "";
    } else if (ev.type === "turn_ended") {
      if (ev.role === "user") { lastUser = ev.text || ""; ctx.onTranscript("user", lastUser); }
      else { ctx.onTranscript("ai", aiBuf || ev.text || ""); aiBuf = ""; if (ev.interrupted) ctx.log("vendor_event", { type: "turn_interrupted" }); }
    } else if (ev.type === "client_tool_call") {
      const result = await ctx.toolCall(ev.tool_name, ev.parameters || {}, lastUser);
      if (ev.expects_response !== false) {
        send({ type: "client_tool_result", tool_call_id: ev.tool_call_id, result: JSON.stringify(result).slice(0, 4000), is_error: false });
      }
    } else if (ev.type === "error") {
      ctx.log("vendor_error", { code: ev.code, message: ev.message, fatal: ev.fatal });
    }
  };
  return { async close() { if (capture) capture.stop(); player.clear(); ws.close(1000); } };
}
