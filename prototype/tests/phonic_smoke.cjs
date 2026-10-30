// Offline check of the Phonic-1 adapter (phonic.js) against a scripted Phonic: the WebSocket is answered inside
// Chromium (page.routeWebSocket), so nothing reaches Phonic.
// node prototype/tests/phonic_smoke.cjs   (run from the repository root)
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "..", "browser_lab", "static");
function fail(msg) { console.error("FAIL:", msg); process.exitCode = 1; }

const pcmB64 = (ms) => Buffer.alloc(Math.round(24000 * ms / 1000) * 2, 1).toString("base64");
const READ_BACK = "ゼロキュウゼロの、イチニーサンヨンの、ゴーロクナナハチでよろしいでしょうか。";
const WS_URL = "wss://api.phonic.ai/v1/sts/ws?session_token=ph_session_test";
const CONFIG = { type: "config", model: "phonic-1", system_prompt: "指示", welcome_message: "お電話ありがとうございます。",
                 voice_id: "hikari", input_format: "pcm_24000", output_format: "pcm_24000", default_language: "ja",
                 boosted_keywords: ["株式会社野田"], tools: [] };
const MINIMAL = { ...CONFIG, boosted_keywords: undefined };

async function runPage(page, credentials, waitMs, toolResults) {
  return page.evaluate(async ({ credentials, waitMs, toolResults }) => {
    const log = [], order = [];
    const ac = new AudioContext(); await ac.resume();
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mod = await import("/adapters/phonic.js");
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
    // --- a call: the welcome message, a number given while the tool is called, the read-back said with "say" ----
    {
      const page = await browser.newPage();
      page.on("pageerror", (e) => errors.push(String(e)));
      const sent = [];
      let wsUrl = null, chunks = 0, maxChunk = 0;
      await page.routeWebSocket(/api\.phonic\.ai/, (ws) => {
        wsUrl = ws.url();
        const out = (ev) => ws.send(JSON.stringify(ev));
        ws.onMessage((m) => {
          const ev = JSON.parse(m);
          if (ev.type === "audio_chunk") {
            chunks++;
            maxChunk = Math.max(maxChunk, Buffer.from(ev.audio, "base64").length);
            return;
          }
          sent.push(ev);
          if (ev.type === "config") {
            out({ type: "ready_to_start_conversation" });
            out({ type: "conversation_created", conversation_id: "conv_1" });
            out({ type: "assistant_started_speaking" });   // the welcome message
            out({ type: "audio_chunk", audio: pcmB64(300), text: "お電話ありがとうございます。" });
            out({ type: "assistant_finished_speaking" });
            setTimeout(() => {
              // Phonic calls the tool while the caller is still speaking: their words come after the call
              out({ type: "user_started_speaking" });
              out({ type: "tool_call", tool_call_id: "t1", tool_name: "save_field",
                    parameters: { field: "callback_number", value: "09012345678" } });
              setTimeout(() => {
                out({ type: "user_finished_speaking" });
                out({ type: "input_text", language: "ja", text: "ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチです" });
              }, 300);
            }, 500);
          } else if (ev.type === "say") {
            out({ type: "assistant_started_speaking" });
            out({ type: "audio_chunk", audio: pcmB64(300), text: ev.text });
            out({ type: "assistant_finished_speaking" });
          }
        });
      });
      await page.goto(base + "/");
      const { order, log } = await runPage(page, { ws_url: WS_URL, vendor: "phonic", config: CONFIG,
        config_minimal: MINIMAL, say: true }, 3500,
        { save_field: { ok: true, read_back_now: { field: "callback_number", say: READ_BACK } } });
      const types = sent.map((e) => e.type);
      if (wsUrl !== WS_URL) fail(`url: ${wsUrl}`);
      if (types[0] !== "config" || sent[0].voice_id !== "hikari") fail(`config first: ${types}`);
      const iUser = order.findIndex((o) => o.startsWith("user:")), iTool = order.indexOf("tool:save_field");
      if (iUser < 0 || iTool < 0 || iUser > iTool) fail(`the caller's words before the tool: ${order}`);
      const output = sent.find((e) => e.type === "tool_call_output");
      if (!output || output.tool_call_id !== "t1" || !output.output.said_by_system) fail(`tool output: ${JSON.stringify(output)}`);
      const sayMsg = sent.find((e) => e.type === "say");
      if (!sayMsg || sayMsg.text !== READ_BACK) fail(`the read-back must be said as it is: ${JSON.stringify(sayMsg)}`);
      if (types.indexOf("say") < types.indexOf("tool_call_output")) fail(`the output goes before the say: ${types}`);
      if (order.filter((o) => o === `ai:${READ_BACK}`).length !== 1) fail(`the read-back reported once: ${order}`);
      if (!order.includes("ai:お電話ありがとうございます。")) fail(`the welcome message: ${order}`);
      if (types.includes("generate_reply")) fail(`no generate_reply when say works: ${types}`);
      if (!chunks) fail("no mic audio sent");
      if (maxChunk > 960 * 2) fail(`a mic chunk longer than 40 ms: ${maxChunk} bytes`);
      if (!log.some(([k, d]) => k === "Phonicの会話ID" && d === "conv_1")) fail("the conversation id is not logged");
      if (order.some((o) => o.startsWith("closed:"))) fail(`closed: ${order}`);
      console.log(JSON.stringify({ call: { order, sent: types, chunks, maxChunk } }));
      await page.close();
    }

    // --- the config is refused (closed with 4204): one more connection with the minimal config; a silent say is read by the model --
    {
      const page = await browser.newPage();
      page.on("pageerror", (e) => errors.push(String(e)));
      const configs = [];
      const sent = [];
      await page.routeWebSocket(/api\.phonic\.ai/, (ws) => {
        const out = (ev) => ws.send(JSON.stringify(ev));
        ws.onMessage((m) => {
          const ev = JSON.parse(m);
          if (ev.type === "audio_chunk") return;
          sent.push(ev);
          if (ev.type === "config") {
            configs.push(ev);
            if (configs.length === 1) {
              // 2026-10-30, the user's first call: Phonic closed at once with 4204, no error message first
              ws.close({ code: 4204, reason: "Invalid parameters" });
              return;
            }
            out({ type: "ready_to_start_conversation" });
            setTimeout(() => out({ type: "tool_call", tool_call_id: "t2", tool_name: "request_summary", parameters: {} }), 300);
          }
          // a "say" gets no voice here: after SAY_WAIT_MS the page asks the model to read the words
        });
      });
      await page.goto(base + "/");
      const { order, log } = await runPage(page, { ws_url: WS_URL, vendor: "phonic", config: CONFIG,
        config_minimal: MINIMAL, say: true }, 5500, { request_summary: { ok: true, say: "まとめの文です。" } });
      if (configs.length !== 2 || configs[1].boosted_keywords !== undefined) fail(`minimal config on retry: ${JSON.stringify(configs)}`);
      if (!log.some(([k, d]) => k === "注意" && String(d).includes("最小の設定"))) fail("the retry is not logged");
      const reply = sent.find((e) => e.type === "generate_reply");
      if (!reply || !reply.system_message.includes("まとめの文です。")) fail(`a silent say falls back to the model: ${JSON.stringify(sent)}`);
      if (!log.some(([k, d]) => k === "注意" && String(d).includes("say"))) fail("the say fallback is not logged");
      if (order.some((o) => o.startsWith("closed:"))) fail(`closed after the retry: ${order}`);
      console.log(JSON.stringify({ retry: { order, sent: sent.map((e) => e.type) } }));
      await page.close();
    }
  } finally {
    await browser.close();
    srv.close();
  }
  if (errors.length) fail(`page errors: ${errors}`);
  if (!process.exitCode) console.log("OK phonic_smoke");
})();
