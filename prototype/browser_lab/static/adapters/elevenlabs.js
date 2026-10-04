// ElevenLabs ElevenAgents through the official JS client (@elevenlabs/client from jsDelivr) and a signed URL
// minted by the local server. The agent and its client tools are configured in the ElevenLabs dashboard
// (docs/browser-lab-setup.md); the tool bodies here call the local server.
// Written from the public docs (2026-10-05); the connection has not been confirmed with a real account.
export async function connect(ctx) {
  const { signed_url: signedUrl, client_esm: esm } = ctx.credentials;
  const { Conversation } = await import(esm);
  let lastUser = "", level = 0, conv = null, closed = false;
  const tool = (name) => async (params) => JSON.stringify(await ctx.toolCall(name, params || {}, lastUser));
  conv = await Conversation.startSession({
    signedUrl,
    clientTools: { save_field: tool("save_field"), request_readback: tool("request_readback"),
                   confirm_field: tool("confirm_field") },
    onMessage: (m) => {
      const role = m && m.source === "user" ? "user" : "ai";
      if (role === "user") lastUser = m.message || "";
      ctx.onTranscript(role, (m && m.message) || "");
    },
    onModeChange: (m) => ctx.log("vendor_event", { type: "mode", mode: m && m.mode }),
    onStatusChange: (s) => ctx.log("vendor_event", { type: "status", status: s && s.status }),
    onError: (e) => ctx.log("vendor_error", String(e)),
  });
  // the SDK plays the audio itself; poll its output volume for the page's measurement
  (async function poll() {
    while (!closed) {
      try { level = await conv.getOutputVolume(); } catch (e) { level = 0; }
      await new Promise((r) => setTimeout(r, 20));
    }
  })();
  return {
    outputLevel: () => (level > 0 ? 20 * Math.log10(level) : -100),
    async close() { closed = true; try { await conv.endSession(); } catch (e) { /* ended */ } },
  };
}
