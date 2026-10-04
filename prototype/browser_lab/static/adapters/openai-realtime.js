// OpenAI Realtime (GA) over WebRTC with an ephemeral key minted by the local server.
// Written from the public docs (2026-10-05); the connection has not been confirmed with a real account.
export async function connect(ctx) {
  const { ephemeral_key: key, calls_url: callsUrl } = ctx.credentials;
  const pc = new RTCPeerConnection();
  const audio = document.createElement("audio");
  audio.autoplay = true;
  pc.ontrack = (e) => { audio.srcObject = e.streams[0]; ctx.attachOutputStream(e.streams[0]); };
  pc.addTrack(ctx.micStream.getTracks()[0], ctx.micStream);
  const dc = pc.createDataChannel("oai-events");
  let lastUser = "";
  const send = (ev) => dc.readyState === "open" && dc.send(JSON.stringify(ev));
  dc.onmessage = async (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch (e) { return; }
    ctx.log("vendor_event", { type: ev.type });
    if (ev.type === "conversation.item.input_audio_transcription.completed") {
      lastUser = ev.transcript || "";
      ctx.onTranscript("user", lastUser);
    } else if (ev.type === "response.output_audio_transcript.done") {
      ctx.onTranscript("ai", ev.transcript || "");
    } else if (ev.type === "response.function_call_arguments.done") {
      let args = {};
      try { args = JSON.parse(ev.arguments || "{}"); } catch (e) { /* keep empty */ }
      const result = await ctx.toolCall(ev.name, args, lastUser);
      send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: ev.call_id,
             output: JSON.stringify(result) } });
      send({ type: "response.create" });
    } else if (ev.type === "error") {
      ctx.log("vendor_error", ev.error || ev);
    }
  };
  dc.onopen = () => send({ type: "response.create", response: { instructions: "最初のあいさつをしてください。" } });
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const resp = await fetch(callsUrl, { method: "POST", body: offer.sdp,
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/sdp" } });
  if (!resp.ok) throw new Error(`realtime/calls: HTTP ${resp.status}`);
  await pc.setRemoteDescription({ type: "answer", sdp: await resp.text() });
  return { async close() { try { dc.close(); } catch (e) { /* */ } pc.close(); audio.srcObject = null; } };
}
