// Smoke test of the browser lab page with Chromium's fake microphone and the offline adapter.
// node prototype/tests/browser_lab_smoke.cjs   (run from the repository root)
// No vendor is contacted: the server runs with no keys, so only the offline candidate can start.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

function writeWav(file) {
  // 6 s loop at 16 kHz: silence, 1.0 s "caller" tone, silence, 0.4 s tone, silence
  const rate = 16000, total = rate * 6, data = new Int16Array(total);
  const bursts = [[2.5, 3.5], [4.3, 4.7]];
  for (let i = 0; i < total; i++) {
    const t = i / rate;
    if (bursts.some(([a, b]) => t >= a && t < b)) data[i] = Math.round(0.4 * 32767 * Math.sin(2 * Math.PI * 180 * t));
  }
  const header = Buffer.alloc(44);
  header.write("RIFF", 0); header.writeUInt32LE(36 + data.length * 2, 4); header.write("WAVE", 8);
  header.write("fmt ", 12); header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
  header.writeUInt32LE(rate, 24); header.writeUInt32LE(rate * 2, 28); header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
  header.write("data", 36); header.writeUInt32LE(data.length * 2, 40);
  fs.writeFileSync(file, Buffer.concat([header, Buffer.from(data.buffer)]));
}

function fail(msg) { console.error("FAIL:", msg); process.exitCode = 1; }

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "lab-smoke-"));
  const wav = path.join(tmp, "caller.wav");
  writeWav(wav);
  const env = { ...process.env };
  for (const k of ["OPENAI_API_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID", "CARTESIA_API_KEY", "CARTESIA_AGENT_ID"]) delete env[k];
  const srv = spawn("python3", ["-m", "prototype.browser_lab", "--port", "0", "--results-dir", path.join(tmp, "results"),
                                "--ledger", path.join(tmp, "ledger.jsonl"), "--db", path.join(tmp, "lab.db")], { env });
  const url = await new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error("server did not start")), 15000);
    srv.stdout.on("data", (d) => { const m = /http:\/\/127\.0\.0\.1:\d+/.exec(String(d)); if (m) { clearTimeout(to); resolve(m[0]); } });
    srv.on("exit", (c) => reject(new Error(`server exited ${c}`)));
  });
  const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${wav}`, "--autoplay-policy=no-user-gesture-required"] });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  try {
    await page.goto(url + "/");
    await page.waitForFunction(() => document.querySelectorAll("#candidate option").length > 1);
    const options = await page.$$eval("#candidate option", (os) => os.map((o) => ({ v: o.value, d: o.disabled, t: o.textContent })));
    if (options.find((o) => o.v === "fake").d) fail("offline candidate should be ready");
    if (!options.filter((o) => o.v !== "fake").every((o) => o.d)) fail("vendor candidates must be disabled without keys");
    if (!options.filter((o) => o.v !== "fake").every((o) => o.t.includes("接続未確認"))) fail("vendor candidates must be marked unverified");
    await page.selectOption("#candidate", "fake");
    await page.click("#startBtn");
    await page.waitForFunction(() => document.getElementById("status").textContent === "会話中", null, { timeout: 15000 });
    await page.waitForTimeout(14000);
    const stats = await page.evaluate(() => ({ lat: window.__lab.latencies.length, stops: window.__lab.stops.length,
      tools: window.__lab.toolCalls.length, kinds: [...new Set(window.__lab.events.map((e) => e.kind))] }));
    await page.click("#stopBtn");
    await page.waitForFunction(() => document.getElementById("status").textContent === "終了");
    await page.selectOption("#j-C1-natural", "3");
    await page.click("#saveBtn");
    await page.waitForFunction(() => document.getElementById("saved").textContent.includes("保存しました"));
    const files = fs.readdirSync(path.join(tmp, "results"));
    const saved = JSON.parse(fs.readFileSync(path.join(tmp, "results", files[0]), "utf8"));
    if (stats.lat < 1) fail("no response latency measured");
    if (stats.tools < 1) fail("no tool call reached the server");
    if (!saved.server_tool_calls || saved.server_tool_calls.length < 1) fail("server-side tool record missing");
    if (saved.judgments.C1.natural !== "3") fail("judgment not saved");
    if (saved.path !== "browser_lab") fail("path tag missing");
    const ints = saved.metrics.interruptions || [];
    if (!ints.every((r) => r.outcome && "value_ms" in r)) fail("every interruption must carry an outcome");
    if (ints.length < stats.stops) fail("measured stops must also be in the interruption list");
    if (!String((saved.comparison || {}).business_logic_status || "").startsWith("評価対象")) fail("business-logic status missing");
    if (!saved.comparison.audio_only || !saved.comparison.business_logic) fail("audio-only and business-logic parts must be separate");
    if (!saved.server_call_id || !saved.server_fields) fail("server-side intake record missing");
    // AI refusal: the server refuses further AI work and the page closes the connection
    await page.click("#startBtn");
    await page.waitForFunction(() => document.getElementById("status").textContent === "会話中", null, { timeout: 15000 });
    await page.click("#refuseBtn");
    await page.waitForFunction(() => document.getElementById("status").textContent === "終了", null, { timeout: 15000 });
    const refused = await page.evaluate(() => window.__lab.events.some((e) => e.kind === "AIの拒否（模擬）"));
    if (!refused) fail("AI refusal not recorded");
    if (errors.length) fail("page errors: " + errors.join(" | "));
    console.log(JSON.stringify({ latencies: stats.lat, interruption_stops: stats.stops, interruptions: ints.map((r) => r.outcome),
      tool_calls: stats.tools, saved: files[0], business_logic: saved.comparison.business_logic_status, event_kinds: stats.kinds }, null, 1));
  } catch (e) {
    fail(String(e && e.stack || e));
  } finally {
    await browser.close();
    srv.kill();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
})();
