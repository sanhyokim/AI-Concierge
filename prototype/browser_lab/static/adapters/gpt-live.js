// OpenAI GPT-Live 1 over WebRTC. The SDP offer is exchanged by the local server (POST /v1/live/sessions
// with the key), so no credential reaches the page.
// Business logic: Responses delegation. The backend model's function calls arrive on the data channel as
// response.event { event: response.output_item.done, item: function_call }; the page sends them to the
// common reception service and answers with response.item.create (function_call_output) + response.create.
// Counts reported to the server (in-app limits, not a cap on what OpenAI bills): backend responses
// (response.event / response.created), response.create sent by this page, and the backend usage when a
// response completes with a usage object.
// GPT-Live has no speech-start/stop or interruption events: a readback cut off by the caller cannot be detected.
// Ending: session.close, then up to 5 s for session.closed (and its usage, if any) before the connection is closed.
// Written from the public docs (2026-10-07); the connection has not been confirmed with a real account.
const CLOSE_WAIT_MS = 5000;

export async function connect(ctx) {
  const pc = new RTCPeerConnection();
  const audio = document.createElement("audio");
  audio.autoplay = true;
  pc.ontrack = (e) => { audio.srcObject = e.streams[0]; ctx.attachOutputStream(e.streams[0]); };
  pc.addTrack(ctx.micStream.getTracks()[0], ctx.micStream);
  const dc = pc.createDataChannel("oai-events");   // register before creating the offer
  const send = (ev) => dc.readyState === "open" && dc.send(JSON.stringify(ev));
  let userBuf = "", aiBuf = "", closeWaiter = null;
  const flushUser = () => { const t = userBuf; userBuf = ""; return t ? ctx.onTranscript("user", t) : Promise.resolve(null); };
  const flushAi = () => { if (aiBuf) { ctx.onTranscript("ai", aiBuf); aiBuf = ""; } };
  const usageOf = (u) => u && {
    input_tokens: u.input_tokens || 0, output_tokens: u.output_tokens || 0,
    cached_tokens: (u.input_tokens_details && u.input_tokens_details.cached_tokens) || 0 };
  dc.onmessage = async (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch (e) { return; }
    const inner = ev.type === "response.event" && ev.event ? ev.event : null;
    ctx.log("vendor_event", { type: ev.type, inner: inner && inner.type });
    if (ev.type === "session.started") {
      // no language field: Japanese and the first greeting are requested by instruction
      send({ type: "session.instructions.append", delegation_id: null,
             content: `最初に日本語で次のあいさつをしてください：「${ctx.credentials.greeting}」` });
    } else if (ev.type === "session.closed") {
      if (closeWaiter) closeWaiter(ev);
    } else if (ev.type === "session.input_transcript.delta") {
      flushAi(); userBuf += ev.delta || "";
    } else if (ev.type === "session.input_transcript.done" || ev.type === "session.input_transcript.completed") {
      flushUser();
    } else if (ev.type === "session.output_transcript.delta") {
      flushUser(); aiBuf += ev.delta || "";
    } else if (inner && inner.type === "response.created") {
      ctx.vendorEvent("backend_response");
    } else if (inner && ["response.completed", "response.done", "response.incomplete", "response.failed"].includes(inner.type)) {
      const u = usageOf(inner.response && inner.response.usage);
      if (u) ctx.vendorEvent("backend_usage", u);
    } else if (inner && inner.type === "response.output_item.done") {
      const item = inner.item || {};
      if (item.type !== "function_call") return;
      const heard = await flushUser();   // the caller's reply reaches the server before the tool call
      if (heard && heard.stop_ai) return;
      let args = {};
      try { args = JSON.parse(item.arguments || "{}"); } catch (e) { /* keep empty */ }
      const result = await ctx.toolCall(item.name, args);
      if (result && result.stop) return;
      send({ type: "response.item.create", item: { type: "function_call_output", call_id: item.call_id, output: JSON.stringify(result) } });
      const r = await ctx.vendorEvent("response_create");
      if (r && r.stop) return;
      send({ type: "response.create" });
    } else if (ev.type === "error") {
      ctx.log("vendor_error", ev.error || ev);
    }
  };
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const answer = await ctx.post(ctx.credentials.sdp_exchange, { session_id: ctx.session.session_id, sdp: offer.sdp });
  await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp });
  return {
    mute() { audio.muted = true; },
    async close() {
      flushUser(); flushAi();
      let closed = false, usage = null;
      if (dc.readyState === "open") {
        const got = new Promise((resolve) => { closeWaiter = resolve; setTimeout(() => resolve(null), CLOSE_WAIT_MS); });
        send({ type: "session.close" });
        const ev = await got;
        if (ev) { closed = true; usage = ev.usage || (ev.session && ev.session.usage) || null; }
      }
      try { dc.close(); } catch (e) { /* */ }
      pc.close(); audio.srcObject = null;
      ctx.log(closed ? "業者の終了を確認（session.closed）" : "業者の終了・使用量は未確認（session.closed が届かない）");
      return { closed, usage };
    },
  };
}
