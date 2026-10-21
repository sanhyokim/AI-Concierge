// Offline check of the Eleven Realtime adapter (OpenAI-compatible events over WebSocket) against a scripted
// vendor: the WebSocket is answered inside Chromium (page.routeWebSocket), so nothing reaches ElevenLabs.
// node prototype/tests/eleven_realtime_smoke.cjs   (run from the repository root)
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "..", "browser_lab", "static");
function fail(msg) { console.error("FAIL:", msg); process.exitCode = 1; }

const pcmB64 = (ms) => Buffer.alloc(Math.round(24000 * ms / 1000) * 2, 1).toString("base64");

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
  const page = await browser.newPage();
  const errors = [], fromPage = [];
  let wsUrl = null, opened = false;
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.routeWebSocket(/api\.elevenlabs\.io/, (ws) => {
    wsUrl = ws.url();
    ws.onMessage((m) => {
      const ev = JSON.parse(m);
      if (ev.type === "input_audio_buffer.append") return;
      fromPage.push(ev);
      if (ev.type === "response.create" && !opened) {
        opened = true;
        // the opening, then the caller speaks over it, then a tool call after the caller's words
        ws.send(JSON.stringify({ type: "response.created", response: {} }));
        ws.send(JSON.stringify({ type: "response.output_audio.delta", item_id: "item_1", delta: pcmB64(3000) }));
        ws.send(JSON.stringify({ type: "response.output_audio_transcript.done", transcript: "お電話ありがとうございます。" }));
        setTimeout(() => {
          ws.send(JSON.stringify({ type: "input_audio_buffer.speech_started" }));
          ws.send(JSON.stringify({ type: "input_audio_buffer.speech_stopped" }));
          ws.send(JSON.stringify({ type: "response.function_call_arguments.done", name: "lookup_faq", call_id: "c1",
                                   arguments: JSON.stringify({ question: "料金" }) }));
          setTimeout(() => ws.send(JSON.stringify({ type: "conversation.item.input_audio_transcription.completed",
                                                    transcript: "料金を教えてください" })), 300);
        }, 500);
      }
    });
    ws.send(JSON.stringify({ type: "session.created", session: {} }));
  });
  try {
    await page.goto(base + "/");
    const out = await page.evaluate(async () => {
      const log = [], order = [];
      const ac = new AudioContext(); await ac.resume();
      const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mod = await import("/adapters/eleven-realtime.js");
      const ctx = {
        credentials: { client_secret: "ek_test", ws_url: "wss://api.elevenlabs.io/v1/convai/realtime",
                       model: "eleven_realtime_v1_max", opening_request: "最初の案内" },
        micStream: mic, audioContext: ac,
        log: (k, d) => log.push([k, d]),
        attachOutputNode: (n) => n.connect(ac.destination),
        onTranscript: async (role, text) => { order.push(`${role}:${text}`); return null; },
        vendorInterrupted: (src, ms) => order.push(`interrupted:${src}:${Math.round(ms)}`),
        vendorClosed: (why) => order.push(`closed:${why}`),
        toolCall: async (name, args) => { order.push(`tool:${name}`); return { ok: true, answer: "目安です" }; },
      };
      const conn = await mod.connect(ctx);
      await new Promise((r) => setTimeout(r, 3500));   // past the opening's wait: no second request
      await conn.close();
      return { order, log };
    });
    const order = out.order;
    if (!wsUrl.includes("model=eleven_realtime_v1_max")) fail(`model not in the URL: ${wsUrl}`);
    const types = fromPage.map((e) => e.type);
    // the opening is asked as a conversation item (2026-10-21: response.create with instructions was ignored)
    const first = fromPage[0];
    if (!(first && first.type === "conversation.item.create" && first.item.role === "user"
          && first.item.content[0].text === "最初の案内") || types[1] !== "response.create") {
      fail(`the opening was not requested first: ${types}`);
    }
    if (fromPage.some((e) => e.type === "response.create" && e.response && e.response.instructions)) {
      fail("the second form of the opening request must not be sent when the AI has started");
    }
    if (!order.some((o) => o.startsWith("interrupted:eleven.input_audio_buffer.speech_started:"))) fail(`no interruption: ${order}`);
    const trunc = fromPage.find((e) => e.type === "conversation.item.truncate");
    if (!trunc || trunc.item_id !== "item_1" || !(trunc.audio_end_ms < 3000)) fail(`truncate missing or wrong: ${JSON.stringify(trunc)}`);
    const iUser = order.indexOf("user:料金を教えてください"), iTool = order.indexOf("tool:lookup_faq");
    if (iUser < 0 || iTool < 0 || iUser > iTool) fail(`the caller's words must reach the server before the tool call: ${order}`);
    const output = fromPage.find((e) => e.type === "conversation.item.create" && e.item.type === "function_call_output");
    if (!output || output.item.call_id !== "c1" || !output.item.output.includes("目安です")) fail("tool output not sent back");
    if (types[types.length - 1] !== "response.create") fail("no response.create after the tool output");
    if (!order.includes("ai:お電話ありがとうございます。")) fail("AI transcript missing");
    if (order.some((o) => o.startsWith("closed:"))) fail(`closed unexpectedly: ${order}`);
    if (errors.length) fail(`page errors: ${errors}`);
    console.log(JSON.stringify({ order, sent: types }, null, 1));
  } catch (e) {
    fail(String(e.stack || e));
  } finally {
    await browser.close();
    srv.close();
  }
})();
