// Offline check of the Gemini Live adapter's opening against a scripted vendor: the WebSocket is answered inside
// Chromium (page.routeWebSocket), so nothing reaches Google.
// node prototype/tests/gemini_live_smoke.cjs   (run from the repository root)
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "..", "browser_lab", "static");
function fail(msg) { console.error("FAIL:", msg); process.exitCode = 1; }

const pcmB64 = (ms) => Buffer.alloc(Math.round(24000 * ms / 1000) * 2, 1).toString("base64");
const OPENING = "最初の案内";

// silentFirst: the first answer to the opening is its transcript only, with no sound (2026-10-21, Boss's call)
async function run(browser, base, silentFirst) {
  const page = await browser.newPage();
  const errors = [], fromPage = [];
  let answered = 0;
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.routeWebSocket(/generativelanguage\.googleapis\.com/, (ws) => {
    ws.onMessage((m) => {
      const ev = JSON.parse(m);
      if (ev.setup) { ws.send(JSON.stringify({ setupComplete: {} })); return; }
      const ri = ev.realtimeInput || {};
      fromPage.push({ t: Date.now(), kind: ri.text ? "text" : ri.audio ? "audio" : Object.keys(ev)[0], text: ri.text });
      if (ri.text === OPENING) {
        answered++;
        const sound = !(silentFirst && answered === 1);
        const parts = sound ? [{ inlineData: { mimeType: "audio/pcm;rate=24000", data: pcmB64(1000) } }] : [];
        ws.send(JSON.stringify({ serverContent: { modelTurn: { parts },
                                                  outputTranscription: { text: "お電話ありがとうございます。" } } }));
        ws.send(JSON.stringify({ serverContent: { generationComplete: true } }));
        ws.send(JSON.stringify({ serverContent: { turnComplete: true } }));
      }
    });
  });
  await page.goto(base + "/");
  const out = await page.evaluate(async (opening) => {
    const log = [], order = [];
    const ac = new AudioContext(); await ac.resume();
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mod = await import("/adapters/gemini-live.js");
    const ctx = {
      credentials: { token: "t", ws_url: "wss://generativelanguage.googleapis.com/ws/x", setup: { setup: {} },
                     opening_request: opening },
      micStream: mic, audioContext: ac,
      log: (k, d) => log.push([k, d]),
      attachOutputNode: (n) => n.connect(ac.destination),
      onTranscript: async (role, text) => { order.push(`${role}:${text}`); return null; },
      vendorInterrupted: () => order.push("interrupted"),
      vendorClosed: (why) => order.push(`closed:${why}`),
      toolCall: async () => ({ ok: true }),
    };
    const conn = await mod.connect(ctx);
    await new Promise((r) => setTimeout(r, 1500));
    const early = order.slice();
    await new Promise((r) => setTimeout(r, 3500));   // past the opening's wait
    await conn.close();
    return { order, early, log };
  }, OPENING);
  await page.close();
  return { ...out, fromPage, errors };
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
  try {
    // 1. the opening is heard: asked once, then the mic is sent
    const ok = await run(browser, base, false);
    const kinds = ok.fromPage.map((e) => e.kind);
    if (kinds[0] !== "text" || ok.fromPage.filter((e) => e.text === OPENING).length !== 1) fail(`heard opening: asked ${kinds}`);
    if (!kinds.includes("audio")) fail("the mic was not sent after the opening was heard");
    if (ok.log.some(([k]) => k === "冒頭の案内")) fail("asked again although the opening was heard");
    const turn = ok.log.find(([k, d]) => k === "vendor_event" && d.type === "turnComplete");
    if (!turn || !(turn[1].audio_ms > 900)) fail(`turnComplete without the sound's length: ${JSON.stringify(turn)}`);
    if (ok.errors.length) fail(`page errors: ${ok.errors}`);
    // 2. words only, no sound: asked once more; no mic until the sound arrives
    const silent = await run(browser, base, true);
    const texts = silent.fromPage.filter((e) => e.text === OPENING);
    if (texts.length !== 2) fail(`words-only opening: asked ${texts.length} times`);
    const firstAudio = silent.fromPage.findIndex((e) => e.kind === "audio");
    const secondText = silent.fromPage.indexOf(texts[1]);
    if (firstAudio >= 0 && firstAudio < secondText) fail("the mic was sent before the opening was heard");
    if (!silent.log.some(([k, d]) => k === "冒頭の案内" && String(d).includes("声が届かなかった"))) fail("no note on the retry");
    if (silent.errors.length) fail(`page errors: ${silent.errors}`);
    console.log(JSON.stringify({ heard: kinds.slice(0, 3), silent: silent.fromPage.slice(0, 4).map((e) => e.kind),
                                 notes: silent.log.filter(([k]) => k === "冒頭の案内") }, null, 1));
  } catch (e) {
    fail(String(e.stack || e));
  } finally {
    await browser.close();
    srv.close();
  }
})();
