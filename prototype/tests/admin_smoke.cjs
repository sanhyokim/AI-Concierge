// Browser test of the admin app (Playwright + Chromium). Run from the repository root:
//   node prototype/tests/admin_smoke.cjs [screenshot-dir]
// It starts the server on a temporary database, operates the screens at PC (1280 px) and smartphone (390 px)
// widths, restarts the server to check persistence, and runs fictitious calls end to end.
// No vendor, phone line or notification service is contacted. A fake long-lived key is put in the server's
// environment to check that it never reaches the page, the API or the console.
const { chromium, devices } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const FAKE_KEY = "sk-test-FAKE-LONG-LIVED-KEY-0000000000";
const PASSWORD = "smoke-test-password-1";
const shots = process.argv[2] || null;
const checks = [];
function check(name, ok, detail) { checks.push({ name, ok: !!ok, detail }); if (!ok) { console.error("FAIL:", name, detail || ""); process.exitCode = 1; } }

function writeWav(file) {   // 6 s loop at 16 kHz with two "caller" tones, for the lab's fake microphone
  const rate = 16000, total = rate * 6, data = new Int16Array(total);
  for (let i = 0; i < total; i++) {
    const t = i / rate;
    if ((t >= 2.5 && t < 3.5) || (t >= 4.3 && t < 4.7)) data[i] = Math.round(0.4 * 32767 * Math.sin(2 * Math.PI * 180 * t));
  }
  const h = Buffer.alloc(44);
  h.write("RIFF", 0); h.writeUInt32LE(36 + data.length * 2, 4); h.write("WAVE", 8); h.write("fmt ", 12); h.writeUInt32LE(16, 16);
  h.writeUInt16LE(1, 20); h.writeUInt16LE(1, 22); h.writeUInt32LE(rate, 24); h.writeUInt32LE(rate * 2, 28); h.writeUInt16LE(2, 32);
  h.writeUInt16LE(16, 34); h.write("data", 36); h.writeUInt32LE(data.length * 2, 40);
  fs.writeFileSync(file, Buffer.concat([h, Buffer.from(data.buffer)]));
}

function startServer(tmp, logs) {
  const env = { ...process.env, ADMIN_PASSWORD: PASSWORD, OPENAI_API_KEY: FAKE_KEY };
  for (const k of ["GEMINI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID", "CARTESIA_API_KEY", "CARTESIA_AGENT_ID"]) delete env[k];
  const srv = spawn("python3", ["-m", "prototype.admin", "--port", "0", "--db", path.join(tmp, "app.db"),
    "--results-dir", path.join(tmp, "results"), "--ledger", path.join(tmp, "ledger.jsonl")], { env });
  srv.stdout.on("data", (d) => logs.push(String(d)));
  srv.stderr.on("data", (d) => logs.push(String(d)));
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error("server did not start")), 15000);
    srv.stdout.on("data", (d) => { const m = /http:\/\/127\.0\.0\.1:\d+/.exec(String(d)); if (m) { clearTimeout(to); resolve({ srv, url: m[0] }); } });
    srv.on("exit", (c) => reject(new Error(`server exited ${c}`)));
  });
}
async function stopServer(srv) { srv.kill(); await new Promise((r) => srv.on("exit", r)); }
async function shot(page, name) {
  if (shots) await page.screenshot({ path: path.join(shots, `${name}.jpg`), fullPage: true, type: "jpeg", quality: 72 });
}
async function toastSays(page, text) {
  await page.waitForFunction((t) => document.getElementById("toast").textContent.includes(t), text, { timeout: 10000 });
  await page.waitForTimeout(500);   // the screen re-renders after a save
}
async function login(page, url) {
  await page.goto(url + "/");
  await page.waitForSelector("#password, #homeMode");
  if (await page.$("#password")) {   // after a restart the session (stored in the database) may still be valid
    await page.fill("#password", PASSWORD);
    await page.click("button[type=submit]");
  }
  await page.waitForSelector("#homeMode");
}
async function demoCall(page, url, at, opts = {}) {
  await page.goto(url + "/#/calls");          // leave the demo screen so it is drawn afresh (no stale result)
  await page.waitForSelector("table.stack, .card");
  await page.goto(url + "/#/demo");
  await page.waitForSelector("#startCall");
  await page.fill("#at", at);
  if (opts.aiDown) await page.check("#aiDown");
  const started = page.waitForResponse((r) => r.url().endsWith("/api/demo/start"));
  await page.click("#startCall");
  await started;
  await page.waitForSelector("#demoRoute");
  return (await page.textContent("#demoRoute")).trim();
}
async function say(page, text) {
  const n = await page.locator("#chat .msg").count();
  await page.fill("#say", text);
  await page.click("#sayBtn");
  await page.waitForFunction((k) => document.querySelectorAll("#chat .msg").length > k + 1 || document.querySelector("#chat .msg.blocked"), n);
  await page.waitForTimeout(150);
}
const lastAi = (page) => page.$$eval("#chat .msg.ai", (els) => (els.at(-1) || {}).textContent || "");

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "admin-smoke-"));
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const logs = [];
  let { srv, url } = await startServer(tmp, logs);
  const wav = path.join(tmp, "caller.wav");
  writeWav(wav);
  const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${wav}`, "--autoplay-policy=no-user-gesture-required"] });
  const pc = await browser.newContext({ viewport: { width: 1280, height: 860 }, locale: "ja-JP", timezoneId: "Asia/Tokyo" });
  const page = await pc.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const seen = [];   // every API response body, to check that the key never appears
  page.on("response", async (r) => { try { if (r.url().includes("/api/")) seen.push(await r.text()); } catch (e) { /* ignore */ } });
  try {
    // --- 7: no access without login -------------------------------------------------------------------
    for (const p of ["/api/config", "/api/calls", "/api/faqs", "/lab/api/candidates"]) {
      const r = await fetch(url + p, { redirect: "manual" });
      check(`unauthenticated ${p} refused`, r.status === 401 || r.status === 302, r.status);
    }
    const post = await fetch(url + "/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    check("unauthenticated POST refused", post.status === 401, post.status);

    await login(page, url);
    await shot(page, "pc-home");

    // --- 1: save a mode, restart the server, still there ------------------------------------------------
    await page.goto(url + "/#/mode");
    await page.check("input[name=mode][value=always_ai]");
    await page.fill("#note", "ブラウザー試験");
    await page.click("#saveMode");
    await toastSays(page, "保存しました");
    await shot(page, "pc-mode");
    await stopServer(srv);
    ({ srv, url } = await startServer(tmp, logs));
    await login(page, url);
    check("mode survives a server restart", (await page.textContent("#homeMode")).includes("常時AI受電"), await page.textContent("#homeMode"));
    await page.goto(url + "/#/mode");
    await page.check("input[name=mode][value=schedule]");
    await page.click("#saveMode");
    await toastSays(page, "保存しました");

    // --- 2: JST boundaries through the schedule screen (server-side rule) --------------------------------
    await page.goto(url + "/#/schedule");
    await page.waitForSelector("#checkBtn");
    const expect = { "2026-10-05T08:59:59": "AI受電", "2026-10-05T09:00:00": "普通受電", "2026-10-05T16:59:59": "普通受電",
      "2026-10-05T17:00:00": "AI受電", "2026-10-10T10:00:00": "AI受電" };
    for (const [at, want] of Object.entries(expect)) {
      await page.$eval("#checkAt", (el, v) => { el.value = v; }, at);   // Chrome drops ":00" seconds on fill
      await page.click("#checkBtn");
      await page.waitForFunction((a) => (document.querySelector("#checkOut").textContent || "").includes(a.replace("T", " ")), at);
      const got = (await page.textContent("#checkRoute")).trim();
      check(`schedule ${at} -> ${want}`, got === want, got);
    }
    await shot(page, "pc-schedule");
    // edit: add a Saturday band, save, check it
    await page.click("button[data-add=sat]");
    await page.click("#saveSchedule");
    await toastSays(page, "保存しました");
    await page.$eval("#checkAt", (el) => { el.value = "2026-10-10T10:00:00"; });
    await page.click("#checkBtn");
    await page.waitForFunction(() => document.querySelector("#checkOut").textContent.includes("土曜日"));
    check("saturday band applies after save", (await page.textContent("#checkRoute")).trim() === "普通受電");

    // --- FAQ and voice screens ----------------------------------------------------------------------------
    await page.goto(url + "/#/faq");
    await page.waitForSelector("[data-toggle]");
    await shot(page, "pc-faq");
    await page.goto(url + "/#/voice");
    await page.waitForSelector("#voiceRows tr");
    const voiceText = await page.textContent("main");
    check("voices distinguish available and unverified", voiceText.includes("利用可能（オフラインの模擬）") && voiceText.includes("接続未確認"));
    await shot(page, "pc-voice");

    // --- end to end: AI call with number/date corrections --------------------------------------------------
    check("evening call goes to AI", (await demoCall(page, url, "2026-10-05T18:00")) === "AI受電");
    await page.click("#runScript");
    await page.waitForFunction(() => [...document.querySelectorAll("#chat .msg.ai")].some((m) => m.textContent.includes("失礼いたします")), null, { timeout: 30000 });
    const chatText = await page.textContent("#chat");
    check("guard refused 「はい、違います」", chatText.includes("サーバーが確認を拒否"));
    await page.click("#endCall");
    await page.waitForSelector("#summaryText");
    const summary = await page.textContent("#summaryText");
    const result = await page.textContent("#result");
    check("summary has the corrected number as confirmed", summary.includes("折り返し先：090-1234-5679（本人確認済み）"), summary);
    check("summary never has the wrong number", !summary.includes("5678"), summary);
    check("summary has the corrected date as confirmed", summary.includes("10月9日、金曜日の、午後3時（本人確認済み）"), summary);
    check("every preview has the corrected number", (result.match(/090-1234-5679/g) || []).length >= 3 && !result.includes("5678"));
    check("per-target results recorded (A accepted, B retrying)", result.includes("受付済み（シミュレーション）") && result.includes("再試行待ち"));
    await shot(page, "pc-demo-result");
    const callHref = await page.getAttribute("#result a", "href");

    // --- 6: retry, re-inject (no duplicates), correction --------------------------------------------------
    await page.goto(url + "/" + callHref);
    await page.waitForSelector("#renotify");
    const before = await page.locator("[data-label=宛先]").count();
    await page.click("#renotify");
    await toastSays(page, "新しく作った通知 0件");
    await page.waitForTimeout(400);
    check("re-injected event does not duplicate", (await page.locator("[data-label=宛先]").count()) === before);
    await page.click("#process");
    await toastSays(page, "受付済み");
    await page.waitForTimeout(400);
    const notes = await page.$$eval("[data-label=状態] .badge", (b) => b.map((x) => x.textContent));
    check("after the retry both targets accepted", notes.filter((t) => t.includes("受付済み")).length >= 2, notes);
    await page.click("button[data-fix=callback_number]");
    await page.fill("[data-fixrow=callback_number] [data-v]", "090-1234-5670");
    await page.fill("[data-fixrow=callback_number] [data-r]", "折り返したら番号違いだった（架空）");
    await page.click("button[data-fixsave=callback_number]");
    await toastSays(page, "補正しました");
    await page.waitForSelector("text=補正の記録");
    const detail = await page.textContent("main");
    check("correction keeps before and after", detail.includes("09012345679") && detail.includes("09012345670"));
    check("staff correction is not shown as caller-confirmed", detail.includes("担当者が補正"));
    check("correction notification marked as correction", detail.includes("先ほどの通知を訂正します"));
    await shot(page, "pc-call-detail");

    // --- 3 and 4: change settings during a call --------------------------------------------------------------
    check("call A is AI", (await demoCall(page, url, "2026-10-05T18:00")) === "AI受電");
    const admin2 = await pc.newPage();
    await admin2.goto(url + "/#/faq");
    await admin2.waitForSelector("[data-toggle]");
    const faq01 = admin2.locator("section[data-id]").filter({ hasText: "FAQ-01" }).locator("[data-toggle]");
    await faq01.uncheck();
    await toastSays(admin2, "無効");
    await admin2.goto(url + "/#/mode");
    await admin2.check("input[name=mode][value=always_normal]");
    await admin2.click("#saveMode");
    await toastSays(admin2, "保存しました");
    await say(page, "点検は無料ですか？");
    check("ongoing call keeps its FAQ (FAQ-01 still answered)", (await lastAi(page)).includes("点検は無料です") ||
      (await page.textContent("#chat")).includes("点検は無料です。"));
    check("ongoing call keeps its route", (await page.textContent("#demoRoute")).trim() === "AI受電");
    await page.click("#endCall");
    await page.waitForSelector("#summaryText");
    check("next call follows the new mode (always normal)", (await demoCall(page, url, "2026-10-05T18:00")) === "普通受電");
    // 8: normal route never sends to the AI
    await say(page, "点検は無料ですか？");
    check("normal route: utterance not sent to the AI", (await page.textContent("#chat")).includes("AIへは送っていません"));
    await shot(page, "pc-demo-normal");
    await admin2.goto(url + "/#/mode");
    await admin2.check("input[name=mode][value=schedule]");
    await admin2.click("#saveMode");
    await toastSays(admin2, "保存しました");
    check("call C is AI again", (await demoCall(page, url, "2026-10-05T18:00")) === "AI受電");
    await say(page, "点検は無料ですか？");
    check("next call uses the disabled FAQ state", (await page.textContent("#chat")).includes("担当者が確認してご連絡します"));
    // 8: AI refusal stops sending and deletes the earlier transcript
    await say(page, "AIとは話したくないです");
    await say(page, "090-1234-5678です");
    const chat2 = await page.textContent("#chat");
    check("after refusal nothing goes to the AI", chat2.includes("AIへは送っていません") && chat2.includes("AIでの処理を拒否済み"));
    check("refusal deletes the earlier transcript and stops the AI stream", chat2.includes("AIへの音声送信を停止") || chat2.includes("ai_send_stopped"));
    check("push-button path offered", await page.isVisible("#dtmfArea"));
    await page.fill("#digits", "09012345678");
    await page.click("#dtmfSend");
    await page.click("button[data-key='1']");
    await page.waitForSelector("#summaryText");
    check("push-button number saved as confirmed", (await page.textContent("#summaryText")).includes("090-1234-5678（本人確認済み）"));
    await admin2.goto(url + "/#/faq");
    await admin2.waitForSelector("[data-toggle]");
    await admin2.locator("section[data-id]").filter({ hasText: "FAQ-01" }).locator("[data-toggle]").check();
    await toastSays(admin2, "有効");
    await admin2.close();

    // --- browser lab behind the same login, using the same reception service ----------------------------------------
    await page.goto(url + "/lab/");
    await page.waitForFunction(() => document.querySelectorAll("#candidate option").length > 1);
    await page.selectOption("#candidate", "fake");
    await page.click("#startBtn");
    await page.waitForFunction(() => document.getElementById("status").textContent === "会話中", null, { timeout: 15000 });
    await page.waitForTimeout(9000);
    await page.click("#stopBtn");
    await page.waitForFunction(() => document.getElementById("status").textContent === "終了");
    await page.click("#saveBtn");
    await page.waitForFunction(() => document.getElementById("saved").textContent.includes("保存しました"));
    const labCall = await page.evaluate(() => window.__lab.session.call_id);
    await shot(page, "pc-lab");
    const labView = await (await page.request.get(url + "/api/calls/" + labCall)).json();
    check("lab session is a call in the common service", labView.source === "browser_lab" && labView.events.some((e) => e.kind === "tool"));

    // --- history and notification screens ----------------------------------------------------------------------
    await page.goto(url + "/#/calls");
    await page.waitForSelector("table.stack");
    await shot(page, "pc-calls");
    await page.goto(url + "/#/notify");
    await page.waitForSelector("#pvBtn");
    await page.click("#pvBtn");
    await page.waitForSelector("#pvOut pre");
    await shot(page, "pc-notify");

    // --- smartphone width ----------------------------------------------------------------------------------------
    const mobile = await browser.newContext({ ...devices["iPhone 13"], deviceScaleFactor: 2, locale: "ja-JP", timezoneId: "Asia/Tokyo" });
    const m = await mobile.newPage();
    m.on("pageerror", (e) => errors.push(String(e)));
    await login(m, url);
    const overflow = await m.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check("no horizontal page scroll at 390 px", overflow <= 1, overflow);
    await shot(m, "sp-home");
    await m.goto(url + "/#/mode");
    await m.check("input[name=mode][value=schedule]");
    await m.click("#saveMode");
    await toastSays(m, "保存しました");
    await m.goto(url + "/#/schedule");
    await m.waitForSelector("#checkBtn");
    await shot(m, "sp-schedule");
    check("phone call works", (await demoCall(m, url, "2026-10-06T20:30")) === "AI受電");
    await m.click("#runScript");
    await m.waitForFunction(() => [...document.querySelectorAll("#chat .msg.ai")].some((x) => x.textContent.includes("失礼いたします")), null, { timeout: 30000 });
    await m.click("#endCall");
    await m.waitForSelector("#summaryText");
    const mOverflow = await m.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check("no horizontal page scroll on the demo screen", mOverflow <= 1, mOverflow);
    await shot(m, "sp-demo-result");
    await m.goto(url + "/#/calls");
    await m.waitForSelector("table.stack");
    await shot(m, "sp-calls");
    await mobile.close();

    // --- 7: the long-lived key never appears ---------------------------------------------------------------
    const html = await page.content();
    check("key not in API responses", !seen.some((t) => t.includes(FAKE_KEY)), seen.length);
    check("key not in the page", !html.includes(FAKE_KEY));
    check("key not in the server console", !logs.join("").includes(FAKE_KEY));
    check("no page errors", errors.length === 0, errors);
  } catch (e) {
    check("no exception", false, String(e && e.stack || e));
  } finally {
    await browser.close();
    await stopServer(srv);
    fs.rmSync(tmp, { recursive: true, force: true });
    console.log(JSON.stringify({ passed: checks.filter((c) => c.ok).length, failed: checks.filter((c) => !c.ok).length,
      checks: checks.map((c) => `${c.ok ? "ok " : "NG "} ${c.name}`) }, null, 1));
  }
})();
