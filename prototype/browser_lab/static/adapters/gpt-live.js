// OpenAI GPT-Live 1 over WebRTC. The SDP offer is exchanged by the local server (POST /v1/live/sessions
// with the key, as the docs describe), so no credential reaches the page.
// Business-logic delegation is not wired yet: delegation events are logged for inspection.
// Written from the public docs (2026-10-05); the connection has not been confirmed with a real account.
export async function connect(ctx) {
  const pc = new RTCPeerConnection();
  const audio = document.createElement("audio");
  audio.autoplay = true;
  pc.ontrack = (e) => { audio.srcObject = e.streams[0]; ctx.attachOutputStream(e.streams[0]); };
  pc.addTrack(ctx.micStream.getTracks()[0], ctx.micStream);
  const dc = pc.createDataChannel("oai-events");   // register before creating the offer
  dc.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch (e) { return; }
    ctx.log("vendor_event", { type: ev.type });
    if (/transcript/.test(ev.type || "")) {
      const text = ev.transcript || ev.text || ev.delta || "";
      if (text) ctx.onTranscript(/input|user/.test(ev.type) ? "user" : "ai", text);
    }
    if (ev.type === "session.delegation.created") ctx.log("delegation_not_wired", ev);
    if (ev.type === "error") ctx.log("vendor_error", ev.error || ev);
  };
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const answer = await ctx.post("/api/session/sdp", { session_id: ctx.session.session_id, sdp: offer.sdp });
  await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp });
  return { async close() { try { dc.close(); } catch (e) { /* */ } pc.close(); audio.srcObject = null; } };
}
