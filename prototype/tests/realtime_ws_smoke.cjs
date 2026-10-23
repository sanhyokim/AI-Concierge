// Offline check of the OpenAI-compatible WebSocket adapter (realtime-ws.js) used by GPT-Realtime-2.1 and Grok Voice
// 3.0, against scripted vendors: each WebSocket is answered inside Chromium (page.routeWebSocket), so nothing
// reaches OpenAI or xAI.
// node prototype/tests/realtime_ws_smoke.cjs   (run from the repository root)
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "..", "browser_lab", "static");
function fail(msg) { console.error("FAIL:", msg); process.exitCode = 1; }

const pcmB64 = (ms) => Buffer.alloc(Math.round(24000 * ms / 1000) * 2, 1).toString("base64");
const READ_BACK = "ゼロキュウゼロの、イチニーサンヨンの、ゴーロクナナハチでよろしいでしょうか。";

// what the page runs: connect with the given credentials, wait, close; returns the server-facing calls in order
async function runPage(page, credentials, waitMs, toolResults) {
  return page.evaluate(async ({ credentials, waitMs, toolResults }) => {
    const log = [], order = [];
    const ac = new AudioContext(); await ac.resume();
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mod = await import("/adapters/realtime-ws.js");
    const ctx = {
      credentials, micStream: mic, audioContext: ac,
      log: (k, d) => log.push([k, d]),
      attachOutputNode: (n) => n.connect(ac.destination),
      onTranscript: async (role, text) => { order.push(`${role}:${text}`); return null; },
      vendorInterrupted: (src) => order.push(`interrupted:${src}`),
      vendorClosed: (why) => order.push(`closed:${why}`),
      toolCall: async (name) => { order.push(`tool:${name}`); return toolResults[name] || { ok: true }; },
    };
    const conn = await mod.connect(ctx);
    await new Promise((r) => setTimeout(r, waitMs));
    await conn.close();
    return { order, log };
  }, { credentials, waitMs, toolResults });
}

(async () => {
  const srv = http.createServer((req, res) => {
    const file = req.url === "/" ? null : path.join(STATIC, decodeURIComponent(req.url.split("?")[0]));
    if (!file) { res.setHeader("Content-Type", "text/html"); res.end("<!doctype html><title>t</title>"); return; }
    if (!file.startsWith(STATIC) || !fs.existsSync(file)) { res.statusCode = 404; res.end(); return; }
    res.setHeader("Content-Type", "text/javascript");
    res.end(fs.readFileSync(file));
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${srv.address().port}`;
  const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required"] });
  const errors = [];
  try {
    // --- GPT-Realtime-2.1: settings minted with the secret; the AI reads the read-back itself -------------------
    {
      const page = await browser.newPage();
      page.on("pageerror", (e) => errors.push(String(e)));
      const sent = [];
      let protocols = null, wsUrl = null, step = 0;
      await page.routeWebSocket(/api\.openai\.com/, (ws) => {
        wsUrl = ws.url();
        protocols = ws.protocols ? ws.protocols() : null;
        const out = (ev) => ws.send(JSON.stringify(ev));
        ws.onMessage((m) => {
          const ev = JSON.parse(m);
          if (ev.type === "input_audio_buffer.append") return;
          sent.push(ev);
          if (ev.type === "response.create" && step === 0) {
            step = 1;   // the opening, then two tool calls in one response (parallel calls)
            out({ type: "response.created", response: {} });
            out({ type: "response.output_audio.delta", item_id: "i1", delta: pcmB64(400) });
            out({ type: "response.output_audio_transcript.done", transcript: "お電話ありがとうございます。" });
            out({ type: "response.done", response: {} });
            setTimeout(() => {
              out({ type: "input_audio_buffer.speech_started" });
              out({ type: "input_audio_buffer.speech_stopped" });
              out({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1",
                    transcript: "ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチです" });
              out({ type: "response.created", response: {} });
              out({ type: "response.function_call_arguments.done", name: "save_field", call_id: "c1",
                    arguments: JSON.stringify({ field: "callback_number", value: "09012345678" }) });
              out({ type: "response.function_call_arguments.done", name: "lookup_faq", call_id: "c2",
                    arguments: JSON.stringify({ question: "料金" }) });
              out({ type: "response.done", response: {} });
            }, 800);
          }
        });
        out({ type: "session.created", session: {} });
      });
      await page.goto(base + "/");
      const { order } = await runPage(page, {
        ws_url: "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
        protocols: ["realtime", "openai-insecure-api-key.ek_test"], vendor: "openai", opening_request: "最初の案内",
      }, 2500, { save_field: { ok: true, read_back_now: { field: "callback_number", say: READ_BACK } } });
      const types = sent.map((e) => e.type);
      if (!wsUrl.includes("model=gpt-realtime-2.1")) fail(`openai: model not in the URL: ${wsUrl}`);
      if (protocols && !protocols.includes("openai-insecure-api-key.ek_test")) fail(`openai: protocols ${protocols}`);
      if (types[0] !== "conversation.item.create" || types[1] !== "response.create") fail(`openai: opening first: ${types}`);
      if (types.includes("session.update")) fail("openai: the settings came with the secret; no session.update");
      const outputs = sent.filter((e) => e.type === "conversation.item.create" && e.item.type === "function_call_output");
      if (outputs.map((e) => e.item.call_id).join() !== "c1,c2") fail(`openai: tool outputs ${JSON.stringify(outputs)}`);
      const creates = types.filter((t) => t === "response.create").length;
      if (creates !== 2) fail(`openai: one response.create for the opening and one after both tools, got ${creates}: ${types}`);
      if (types[types.length - 1] !== "response.create") fail(`openai: last must be response.create: ${types}`);
      if (sent.some((e) => e.item && e.item.type === "force_message")) fail("openai: no force_message for OpenAI");
      const iUser = order.findIndex((o) => o.startsWith("user:")), iTool = order.indexOf("tool:save_field");
      if (iUser < 0 || iUser > iTool) fail(`openai: the caller's words before the tool: ${order}`);
      if (!order.includes("ai:お電話ありがとうございます。")) fail(`openai: AI words missing: ${order}`);
      if (order.some((o) => o.startsWith("closed:"))) fail(`openai: closed: ${order}`);
      console.log(JSON.stringify({ openai: { order, sent: types } }));
      await page.close();
    }

    // --- Grok Voice 3.0: the page sends the session; the fixed words are spoken with force_message -------------
    {
      const page = await browser.newPage();
      page.on("pageerror", (e) => errors.push(String(e)));
      const sent = [];
      let protocols = null, forcedSeen = 0;
      await page.routeWebSocket(/api\.x\.ai/, (ws) => {
        protocols = ws.protocols ? ws.protocols() : null;
        const out = (ev) => ws.send(JSON.stringify(ev));
        ws.onMessage((m) => {
          const ev = JSON.parse(m);
          if (ev.type === "input_audio_buffer.append") return;
          sent.push(ev);
          if (ev.type === "session.update") { out({ type: "session.updated", session: {} }); return; }
          if (ev.type === "conversation.item.create" && ev.item.type === "force_message") {
            forcedSeen++;
            if (forcedSeen === 1) {   // the opening sounds; then the caller gives a number and the model saves it
              out({ type: "response.created", response: {} });
              out({ type: "response.output_audio.delta", item_id: "f1", delta: pcmB64(300) });
              out({ type: "response.output_audio_transcript.done", transcript: ev.item.text });
              out({ type: "response.done", response: {} });
              setTimeout(() => {
                out({ type: "input_audio_buffer.speech_started" });
                out({ type: "conversation.item.input_audio_transcription.updated", item_id: "u1", transcript: "ゼロキュウゼロ" });
                out({ type: "conversation.item.input_audio_transcription.updated", item_id: "u1",
                      transcript: "ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチです" });
                out({ type: "input_audio_buffer.speech_stopped" });
                out({ type: "response.created", response: {} });
                out({ type: "response.function_call_arguments.done", name: "save_field", call_id: "g1",
                      arguments: JSON.stringify({ field: "callback_number", value: "09012345678" }) });
                out({ type: "response.done", response: {} });
              }, 600);
            } else if (forcedSeen === 2) {   // the read-back: refused once, so the page asks the model instead
              out({ type: "error", error: { message: "unknown item type force_message" } });
            }
          }
        });
        // xAI starts with conversation.created; the page waits for session.updated before the opening
        out({ type: "conversation.created", conversation: { id: "conv_1" } });
      });
      await page.goto(base + "/");
      const { order, log } = await runPage(page, {
        ws_url: "wss://api.x.ai/v1/realtime?model=grok-voice-3.0", protocols: ["xai-client-secret.xs_test"],
        vendor: "xai", session_update: { instructions: "指示", voice: "ara" }, force_message: true, truncate: false,
        greeting: "お電話ありがとうございます。株式会社野田でございます。", opening_request: "最初の案内",
      }, 3000, { save_field: { ok: true, read_back_now: { field: "callback_number", say: READ_BACK } } });
      const types = sent.map((e) => e.type);
      if (protocols && !protocols.includes("xai-client-secret.xs_test")) fail(`xai: protocols ${protocols}`);
      if (types[0] !== "session.update" || sent[0].session.voice !== "ara") fail(`xai: session.update first: ${types}`);
      const forced = sent.filter((e) => e.item && e.item.type === "force_message").map((e) => e.item.text);
      if (forced[0] !== "お電話ありがとうございます。株式会社野田でございます。") fail(`xai: the opening is forced: ${forced}`);
      if (forced[1] !== READ_BACK) fail(`xai: the read-back is forced word for word: ${forced}`);
      const iOut = sent.findIndex((e) => e.item && e.item.type === "function_call_output");
      const iForce2 = sent.findIndex((e, i) => i > iOut && e.item && e.item.type === "force_message");
      if (iOut < 0 || iForce2 < iOut) fail(`xai: the tool output comes before the forced read-back: ${types}`);
      // the forced opening needs no response.create; after the refused force_message, one response.create
      const creates = types.filter((t) => t === "response.create").length;
      if (creates !== 1 || types[types.length - 1] !== "response.create") fail(`xai: response.create only as fallback: ${types}`);
      if (!log.some(([k]) => k === "注意")) fail("xai: the fallback must be shown on the page");
      if (sent.some((e) => e.type === "conversation.item.truncate")) fail("xai: no truncate (not documented by xAI)");
      const iUser = order.indexOf("user:ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチです"), iTool = order.indexOf("tool:save_field");
      if (iUser < 0 || iUser > iTool) fail(`xai: the caller's last words before the tool: ${order}`);
      if (order.filter((o) => o.startsWith("user:")).length !== 1) fail(`xai: the growing transcript is reported once: ${order}`);
      if (order.filter((o) => o === "ai:お電話ありがとうございます。株式会社野田でございます。").length !== 1) {
        fail(`xai: the forced opening is reported once as the AI's words: ${order}`);
      }
      // the refused read-back never sounded: it is not reported as the AI's words (the model reads it instead)
      if (order.includes(`ai:${READ_BACK}`)) fail(`xai: a forced text is the AI's words only once it sounds: ${order}`);
      if (order.some((o) => o.startsWith("closed:"))) fail(`xai: closed: ${order}`);
      console.log(JSON.stringify({ xai: { order, sent: types } }));
      await page.close();
    }
    if (errors.length) fail(`page errors: ${errors}`);
  } catch (e) {
    fail(String(e.stack || e));
  } finally {
    await browser.close();
    srv.close();
  }
})();
