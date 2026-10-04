// Cartesia Managed Agents over WebSocket (wss://api.cartesia.ai/agents/stream/{agent_id}) with an access token.
// Protocol from the public docs: start -> ack, media_input / media_output (base64), clear (interrupt).
// Output audio format is assumed to follow the input format (pcm_16000): unconfirmed.
// Tool calls do not reach the browser in this protocol as documented; the agent's tools run on Cartesia.
// Written from the public docs (2026-10-05); the connection has not been confirmed with a real account.
import { createPlayer, fromBase64, startCapture, toBase64 } from "../pcm.js";

export async function connect(ctx) {
  const { token, ws_url: wsUrl, cartesia_version: version, start } = ctx.credentials;
  const ac = ctx.audioContext;
  const out = ac.createGain();
  ctx.attachOutputNode(out);
  const player = createPlayer(ac, out, 16000);
  let capture = null, streamId = null;
  const ws = new WebSocket(`${wsUrl}?cartesia_version=${encodeURIComponent(version)}&access_token=${encodeURIComponent(token)}`);
  const send = (obj) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));
  ws.onopen = () => send(start);
  ws.onerror = () => ctx.log("vendor_error", "websocket error");
  ws.onclose = (e) => ctx.log("vendor_closed", { code: e.code, reason: e.reason });
  ws.onmessage = async (msg) => {
    let ev;
    try { ev = JSON.parse(typeof msg.data === "string" ? msg.data : await msg.data.text()); } catch (e) { return; }
    ctx.log("vendor_event", { type: ev.event });
    if (ev.event === "ack") {
      streamId = ev.stream_id;
      capture = await startCapture(ac, ctx.micStream, 16000, (buf) =>
        send({ event: "media_input", stream_id: streamId, media: { payload: toBase64(buf) } }));
    } else if (ev.event === "media_output" && ev.media && ev.media.payload) {
      player.enqueue(fromBase64(ev.media.payload));
    } else if (ev.event === "clear") {
      player.clear();
    }
  };
  ctx.log("tools_not_in_browser", "Cartesiaの業務処理（ツール）はブラウザーに届かない前提。C2・C5のデータは会話の記録で確認する");
  return { async close() { if (capture) capture.stop(); player.clear(); ws.close(); } };
}
