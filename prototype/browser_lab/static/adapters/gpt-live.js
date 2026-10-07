// OpenAI GPT-Live 1 over WebRTC. The SDP offer is exchanged by the local server (POST /v1/live/sessions
// with the key), so no credential reaches the page.
// Business logic: Responses delegation. The backend model's function calls arrive on the data channel as
// response.event { event: response.output_item.done, item: function_call }; the page sends them to the
// common reception service and answers with response.item.create (function_call_output) + response.create.
// GPT-Live has no speech-start/stop or interruption events: the page measures on the device audio only.
// Written from the public docs (2026-10-07); the connection has not been confirmed with a real account.
export async function connect(ctx) {
  const pc = new RTCPeerConnection();
  const audio = document.createElement("audio");
  audio.autoplay = true;
  pc.ontrack = (e) => { audio.srcObject = e.streams[0]; ctx.attachOutputStream(e.streams[0]); };
  pc.addTrack(ctx.micStream.getTracks()[0], ctx.micStream);
  const dc = pc.createDataChannel("oai-events");   // register before creating the offer
  const send = (ev) => dc.readyState === "open" && dc.send(JSON.stringify(ev));
  let userBuf = "", aiBuf = "", lastUser = "";
  const flushUser = () => { if (userBuf) { lastUser = userBuf; ctx.onTranscript("user", userBuf); userBuf = ""; } };
  const flushAi = () => { if (aiBuf) { ctx.onTranscript("ai", aiBuf); aiBuf = ""; } };
  dc.onmessage = async (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch (e) { return; }
    ctx.log("vendor_event", { type: ev.type, inner: ev.event && ev.event.type });
    if (ev.type === "session.started") {
      // no language field: Japanese and the first greeting are requested by instruction
      send({ type: "session.instructions.append", delegation_id: null,
             content: `最初に日本語で次のあいさつをしてください：「${ctx.credentials.greeting}」` });
    } else if (ev.type === "session.input_transcript.delta") {
      flushAi(); userBuf += ev.delta || "";
    } else if (ev.type === "session.output_transcript.delta") {
      flushUser(); aiBuf += ev.delta || "";
    } else if (ev.type === "response.event" && ev.event && ev.event.type === "response.output_item.done") {
      const item = ev.event.item || {};
      if (item.type !== "function_call") return;
      let args = {};
      try { args = JSON.parse(item.arguments || "{}"); } catch (e) { /* keep empty */ }
      const result = await ctx.toolCall(item.name, args, userBuf || lastUser);
      send({ type: "response.item.create", item: { type: "function_call_output", call_id: item.call_id, output: JSON.stringify(result) } });
      send({ type: "response.create" });
    } else if (ev.type === "error") {
      ctx.log("vendor_error", ev.error || ev);
    }
  };
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const answer = await ctx.post(ctx.credentials.sdp_exchange, { session_id: ctx.session.session_id, sdp: offer.sdp });
  await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp });
  return { async close() { flushUser(); flushAi(); try { dc.close(); } catch (e) { /* */ } pc.close(); audio.srcObject = null; } };
}
