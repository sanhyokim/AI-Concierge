// Offline check of the ElevenAgents adapter (elevenagents.js) against a scripted ElevenLabs agent: the WebSocket is
// answered inside Chromium (page.routeWebSocket), so nothing reaches ElevenLabs.
// node prototype/tests/elevenagents_smoke.cjs   (run from the repository root)
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "..", "browser_lab", "static");
function fail(msg) { console.error("FAIL:", msg); process.exitCode = 1; }

const OUT_RATE = 22050;   // the metadata's rate wins over the one the server guessed (24000)
const samples = (ms) => Math.round(OUT_RATE * ms / 1000);
const pcmB64 = (ms) => Buffer.alloc(samples(ms) * 2, 1).toString("base64");
const READ_BACK = "ゼロキュウゼロの、イチニーサンヨンの、ゴーロクナナハチでよろしいでしょうか。";
const SIGNED = "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=agent_lab&conversation_signature=sig_test";
const CREDS = { signed_url: SIGNED, agent_id: "agent_lab", llm: "gpt-5.6-terra", output_rate: 24000, input_rate: 16000 };
const METADATA = { type: "conversation_initiation_metadata",
                   conversation_initiation_metadata_event: { conversation_id: "conv_e1",
                     agent_output_audio_format: `pcm_${OUT_RATE}`, user_input_audio_format: "pcm_16000" } };

async function runPage(page, credentials, waitMs, toolResults, holdOn = "") {
  return page.evaluate(async ({ credentials, waitMs, toolResults, holdOn }) => {
    const log = [], order = [], buffers = [];
    let conn = null;
    const ac = new AudioContext(); await ac.resume();
    const make = ac.createBuffer.bind(ac);
    ac.createBuffer = (ch, len, rate) => { buffers.push([len, rate]); return make(ch, len, rate); };
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mod = await import("/adapters/elevenagents.js");
    const ctx = {
      credentials, micStream: mic, audioContext: ac,
      log: (k, d) => log.push([k, d]),
      attachOutputNode: (n) => n.connect(ac.destination),
      // the server answers a transcript after a while: the tool call that follows must wait for it
      onTranscript: (role, text) => new Promise((r) => setTimeout(() => {
        order.push(`${role}:${text}`);
        if (holdOn && role === "ai" && text.includes(holdOn)) conn.holdNewSpeech(true);   // as the lab page does
        r(null);
      }, 150)),
      vendorInterrupted: (src, ms) => order.push(`interrupted:${src}:${ms > 0}`),
      vendorClosed: (why) => order.push(`closed:${why}`),
      vendorEvent: (kind, data) => order.push(`vendor:${kind}:${data.conversation_id}`),
      micThresholdDb: () => -42,
      toolCall: async (name) => { order.push(`tool:${name}`); return toolResults[name] || { ok: true }; },
    };
    conn = await mod.connect(ctx);
    await new Promise((r) => setTimeout(r, waitMs));
    await conn.close();
    return { order, log, buffers };
  }, { credentials, waitMs, toolResults, holdOn });
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
    // --- a call: the first message, a number, the read-back as the tool's say, an interruption ------------------
    {
      const page = await browser.newPage();
      page.on("pageerror", (e) => errors.push(String(e)));
      const sent = [];
      let wsUrl = null, chunks = 0;
      await page.routeWebSocket(/api\.elevenlabs\.io/, (ws) => {
        wsUrl = ws.url();
        const out = (ev) => ws.send(JSON.stringify(ev));
        ws.onMessage((m) => {
          const ev = JSON.parse(m);
          if (ev.user_audio_chunk) { chunks++; return; }
          sent.push(ev);
          if (ev.type === "conversation_initiation_client_data") {
            out(METADATA);
            out({ type: "ping", ping_event: { event_id: 7, ping_ms: 40 } });
            out({ type: "audio", audio_event: { audio_base_64: pcmB64(300), event_id: 1 } });   // the first message
            out({ type: "agent_response", agent_response_event: { agent_response: "お電話ありがとうございます。" } });
            setTimeout(() => {
              // the caller gives the number; the text model saves it at once
              out({ type: "user_transcript", user_transcription_event: { user_transcript: "ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチです" } });
              out({ type: "client_tool_call", client_tool_call: { tool_name: "save_field", tool_call_id: "t1",
                    parameters: { field: "callback_number", value: "09012345678" }, expects_response: true, event_id: 2 } });
            }, 500);
          } else if (ev.type === "client_tool_result" && ev.tool_call_id === "t1") {
            // the text model reads the say as it is; ElevenLabs speaks exactly that text
            const say = JSON.parse(ev.result).read_back_now.say;
            out({ type: "agent_response", agent_response_event: { agent_response: say } });
            out({ type: "audio", audio_event: { audio_base_64: pcmB64(2500), event_id: 3 } });
            setTimeout(() => {
              out({ type: "interruption", interruption_event: { event_id: 3 } });
              out({ type: "audio", audio_event: { audio_base_64: pcmB64(777), event_id: 3 } });   // late, from the cut answer
              out({ type: "audio", audio_event: { audio_base_64: pcmB64(555), event_id: 4 } });   // the next answer
            }, 300);
          }
        });
      });
      await page.goto(base + "/");
      const { order, log, buffers } = await runPage(page, CREDS, 3000,
        { save_field: { ok: true, read_back_now: { field: "callback_number", say: READ_BACK } } });
      if (wsUrl !== SIGNED) fail(`url: ${wsUrl}`);
      if (!sent.length || sent[0].type !== "conversation_initiation_client_data") fail(`initiation first: ${JSON.stringify(sent)}`);
      const pong = sent.find((e) => e.type === "pong");
      if (!pong || pong.event_id !== 7) fail(`pong: ${JSON.stringify(pong)}`);
      const iUser = order.findIndex((o) => o.startsWith("user:")), iTool = order.indexOf("tool:save_field");
      if (iUser < 0 || iTool < 0 || iUser > iTool) fail(`the caller's words reach the server before the tool: ${order}`);
      const result = sent.find((e) => e.type === "client_tool_result");
      if (!result || result.tool_call_id !== "t1" || typeof result.result !== "string" || result.is_error !== false) {
        fail(`tool result: ${JSON.stringify(result)}`);
      } else if (JSON.parse(result.result).read_back_now.say !== READ_BACK) fail(`tool result body: ${result.result}`);
      if (order.filter((o) => o === `ai:${READ_BACK}`).length !== 1) fail(`the read-back reported once: ${order}`);
      if (!order.includes("ai:お電話ありがとうございます。")) fail(`the first message: ${order}`);
      if (!order.includes("vendor:vendor_session:conv_e1")) fail(`the conversation id goes to the server: ${order}`);
      if (!log.some(([k, d]) => k === "ElevenLabsの会話ID" && d === "conv_e1")) fail("the conversation id is not logged");
      if (!log.some(([k, d]) => k === "考えるAI" && d === "gpt-5.6-terra")) fail("the text model is not logged");
      if (!order.includes("interrupted:elevenagents.interruption:true")) fail(`the interruption: ${order}`);
      if (buffers.some(([, rate]) => rate !== OUT_RATE)) fail(`the metadata's rate is not used: ${JSON.stringify(buffers)}`);
      const lens = buffers.map(([len]) => len);
      if (!lens.includes(samples(300)) || !lens.includes(samples(2500))) fail(`audio not played: ${lens}`);
      if (lens.includes(samples(777))) fail(`audio of the cut answer was played: ${lens}`);
      if (!lens.includes(samples(555))) fail(`the next answer was dropped: ${lens}`);
      if (!chunks) fail("no mic audio sent");
      if (order.some((o) => o.startsWith("closed:"))) fail(`closed: ${order}`);
      console.log(JSON.stringify({ call: { order, sent: sent.map((e) => e.type), chunks } }));
      await page.close();
    }

    // --- the last words (2026-11-05, 「最後のあいさつが最後まで言い切らずに電話が切れました」): the text comes before the
    // end of its voice; the page then holds new speech, but the rest of the goodbye still plays, a later answer does not
    {
      const page = await browser.newPage();
      page.on("pageerror", (e) => errors.push(String(e)));
      const GOODBYE = "お電話ありがとうございました。株式会社野田、AIオペレーターがお受けいたしました。失礼いたします。";
      await page.routeWebSocket(/api\.elevenlabs\.io/, (ws) => {
        const out = (ev) => ws.send(JSON.stringify(ev));
        ws.onMessage((m) => {
          const ev = JSON.parse(m);
          if (ev.type !== "conversation_initiation_client_data") return;
          out(METADATA);
          out({ type: "agent_response", agent_response_event: { agent_response: GOODBYE, event_id: 5 } });
          out({ type: "audio", audio_event: { audio_base_64: pcmB64(400), event_id: 5 } });
          setTimeout(() => {   // after the page has held new speech
            out({ type: "audio", audio_event: { audio_base_64: pcmB64(900), event_id: 5 } });   // the rest of the goodbye
            out({ type: "agent_response", agent_response_event: { agent_response: "失礼いたします。", event_id: 6 } });
            out({ type: "audio", audio_event: { audio_base_64: pcmB64(444), event_id: 6 } });   // a repeat
          }, 600);
        });
      });
      await page.goto(base + "/");
      const { log, buffers } = await runPage(page, CREDS, 1500, {}, "お受けいたしました");
      const lens = buffers.map(([len]) => len);
      if (!log.some(([k, d]) => k === "vendor_event" && d && d.type === "hold_after_goodbye" && d.after_event_id === 5)) {
        fail(`the hold after the goodbye: ${JSON.stringify(log)}`);
      }
      if (!lens.includes(samples(400)) || !lens.includes(samples(900))) fail(`the whole goodbye is not played: ${lens}`);
      if (lens.includes(samples(444))) fail(`a later answer was played after the goodbye: ${lens}`);
      console.log(JSON.stringify({ goodbye: { played: lens } }));
      await page.close();
    }

    // --- the minimal agent settings, a tool with no answer wanted, ElevenLabs closing the call ------------------
    {
      const page = await browser.newPage();
      page.on("pageerror", (e) => errors.push(String(e)));
      const sent = [];
      await page.routeWebSocket(/api\.elevenlabs\.io/, (ws) => {
        const out = (ev) => ws.send(JSON.stringify(ev));
        ws.onMessage((m) => {
          const ev = JSON.parse(m);
          if (ev.user_audio_chunk) return;
          sent.push(ev);
          if (ev.type === "conversation_initiation_client_data") {
            out(METADATA);
            out({ type: "client_tool_call", client_tool_call: { tool_name: "flag_emergency", tool_call_id: "t9",
                  parameters: {}, expects_response: false } });
            setTimeout(() => ws.close({ code: 1008, reason: "quota exceeded" }), 500);
          }
        });
      });
      await page.goto(base + "/");
      const { order, log } = await runPage(page, { ...CREDS,
        agent_fallback: { dropped: "asr, turn", reason: "422 invalid field" } }, 1200, {});
      if (!log.some(([k, d]) => k === "注意" && String(d).includes("最小の設定") && String(d).includes("asr, turn"))) {
        fail(`the fallback is not logged: ${JSON.stringify(log)}`);
      }
      if (!order.includes("tool:flag_emergency")) fail(`the tool: ${order}`);
      if (sent.some((e) => e.type === "client_tool_result")) fail(`no result when none is wanted: ${JSON.stringify(sent)}`);
      if (!order.some((o) => o.startsWith("closed:") && o.includes("1008") && o.includes("quota exceeded"))) {
        fail(`the close reason is shown: ${order}`);
      }
      console.log(JSON.stringify({ closed: { order, sent: sent.map((e) => e.type) } }));
      await page.close();
    }
  } finally {
    await browser.close();
    srv.close();
  }
  if (errors.length) fail(`page errors: ${errors}`);
  if (!process.exitCode) console.log("OK elevenagents_smoke");
})();
