// Core of the browser conversation lab: candidate switching, device-side measurement, judgments, saving.
// Measurement uses only audio levels on this device (microphone and the AI audio being played),
// so every candidate is measured the same way regardless of what events its API sends.

const SCENARIOS = [
  { id: "C1", title: "相づち", how: "AIが話している途中で「はい」「うん」と短く相づちする",
    check: "不必要に止まらない。次の必要な内容を自然に話す" },
  { id: "C2", title: "「はい、違います」からの訂正", how: "AIの復唱の途中で「はい、違います。電話番号は…」と訂正する",
    check: "冒頭の「はい」を肯定にしない。訂正を聞き、正しい番号を復唱し、肯定まで確認済みにしない" },
  { id: "C3", title: "停止の求め", how: "AIの発話の最初・途中・終わり際で「ちょっと待って」と言う",
    check: "止まって待つ。言い終えていない部分を伝えた扱いにしない" },
  { id: "C4", title: "間と言い直し", how: "考えて一度間を空け、同じ内容を言い直す",
    check: "発話の終わりを早まって決めない。言い直した後の意味で受け付ける" },
  { id: "C5", title: "処理中の追加・訂正", how: "（業務処理を4秒遅らせる）その間に用件を追加・訂正する",
    check: "処理中も聞ける。結果を先に約束しない。保存・要約と会話が食い違わない" },
  { id: "C6", title: "専門用語・番号・日時", how: "株式会社野田、ダクト、無煙ロースター、電話番号、日付・時間を伝える",
    check: "発音・受付の正確さ。電話番号・日時を区切って明瞭に復唱し、確認する（部分減速は必須ではない）" },
];
const JUDGE_ITEMS = [
  ["needless_stop", "相づちで不要に止まった"], ["missed_correction", "訂正を取りこぼした"],
  ["stop_ok", "停止の求めに応じた"], ["data_ok", "受付情報が正しく残った"], ["natural", "自然さ（1〜5）"],
];
const C5_DELAY_MS = 4000;
const $ = (id) => document.getElementById(id);

const st = {
  candidates: [], session: null, adapter: null, conn: null, ac: null, mic: null,
  micAnalyser: null, aiAnalyser: null, t0: 0, timer: null, loop: null, scenario: "C1",
  events: [], transcripts: [], toolCalls: [], latencies: [], stops: [], flags: [],
  user: { on: false, since: null, below: null, startT: null }, ai: { on: false, since: null, below: null, startT: null },
  lastUserEnd: null, lastAiEnd: null, pendingInterrupt: null, userSpeechListeners: new Set(),
};

const now = () => Math.round(performance.now() - st.t0);

function log(kind, data) {
  const t = st.t0 ? now() : 0;
  st.events.push({ t_ms: t, scenario: st.scenario, kind, data });
  const line = document.createElement("div");
  line.textContent = `${(t / 1000).toFixed(2)}s [${st.scenario}] ${kind}${data === undefined ? "" : " " + (typeof data === "string" ? data : JSON.stringify(data))}`;
  $("log").appendChild(line);
  $("log").scrollTop = $("log").scrollHeight;
}

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

function levelDb(analyser) {
  if (!analyser) return -100;
  const buf = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(buf);
  let s = 0;
  for (const v of buf) s += v * v;
  const rms = Math.sqrt(s / buf.length);
  return rms > 0 ? 20 * Math.log10(rms) : -100;
}

function metric(kind, value, extra) {
  const row = { scenario: st.scenario, kind, value_ms: value, t_ms: now(), ...(extra || {}) };
  if (kind === "応答の遅延") st.latencies.push(row);
  if (kind === "割り込みで止まるまで") st.stops.push(row);
  if (kind === "相づちで停止の可能性") st.flags.push(row);
  const tr = document.createElement("tr");
  tr.innerHTML = `<td>${row.scenario}</td><td class="${kind.startsWith("相づち") ? "flag" : ""}">${kind}</td><td>${value === null ? "要確認" : value + " ms"}</td>`;
  $("metrics").prepend(tr);
  updateSummary();
}

function pct(list, q) {
  if (!list.length) return null;
  const v = list.map((r) => r.value_ms).sort((a, b) => a - b);
  return v[Math.min(v.length - 1, Math.floor(q * (v.length - 1)))];
}

function updateSummary() {
  const f = (x) => (x === null ? "—" : `${x} ms`);
  $("summary").textContent = `応答の遅延：中央値 ${f(pct(st.latencies, 0.5))}・p90 ${f(pct(st.latencies, 0.9))}（${st.latencies.length}件）／` +
    `割り込みで止まるまで：中央値 ${f(pct(st.stops, 0.5))}・p90 ${f(pct(st.stops, 0.9))}（${st.stops.length}件）／` +
    `相づちで停止の可能性：${st.flags.length}件（自動推定。判定欄で確認）`;
}

// Voice-activity detection on both sides with hangover; start/end times are the first frame over/under.
function step(side, db, thr, startHold, endHold, onStart, onEnd) {
  const s = st[side], t = now();
  if (db >= thr) {
    s.below = null;
    if (!s.on) {
      if (s.since === null) s.since = t;
      if (t - s.since >= startHold) { s.on = true; s.startT = s.since; onStart(s.since); }
    }
  } else {
    s.since = null;
    if (s.on) {
      if (s.below === null) s.below = t;
      if (t - s.below >= endHold) { s.on = false; onEnd(s.below, s.below - s.startT); }
    }
  }
}

function tick() {
  const micDb = levelDb(st.micAnalyser);
  const aiDb = st.conn && st.conn.outputLevel ? st.conn.outputLevel() : levelDb(st.aiAnalyser);
  $("micDb").textContent = `${micDb.toFixed(0)} dBFS`;
  $("aiDb").textContent = `${aiDb.toFixed(0)} dBFS`;
  $("micBar").style.width = `${Math.max(0, Math.min(100, (micDb + 70) * 1.6))}%`;
  $("aiBar").style.width = `${Math.max(0, Math.min(100, (aiDb + 70) * 1.6))}%`;
  const micThr = Number($("micThr").value), aiThr = Number($("aiThr").value);
  const STOP_WINDOW_MS = 2500;   // a stop counts as a reaction to the caller only within this window
  const p = st.pendingInterrupt;
  if (p && p.stoppedAt === undefined && now() - p.t > STOP_WINDOW_MS) {
    log("AIは話し続けた", { after_user_start_ms: STOP_WINDOW_MS });   // e.g. a backchannel correctly ignored
    st.pendingInterrupt = null;
  }
  step("ai", aiDb, aiThr, 40, 250, (t) => {
    if (st.lastUserEnd !== null && (st.lastAiEnd === null || st.lastUserEnd > st.lastAiEnd) && !st.user.on) {
      metric("応答の遅延", t - st.lastUserEnd);
    }
    log("ai_audio_start");
  }, (t) => {
    st.lastAiEnd = t;
    const q = st.pendingInterrupt;
    if (q && q.stoppedAt === undefined && t - q.t <= STOP_WINDOW_MS) {
      q.stoppedAt = t;
      metric("割り込みで止まるまで", t - q.t);
      if (q.dur !== undefined && q.dur < 600) metric("相づちで停止の可能性", null, { utterance_ms: q.dur });
      if (q.dur !== undefined) st.pendingInterrupt = null;
    }
    log("ai_audio_end");
  });
  step("user", micDb, micThr, 60, 300, (t) => {
    if (st.ai.on) st.pendingInterrupt = { t };
    log("user_speech_start", st.ai.on ? "AIの発話中" : undefined);
    for (const cb of st.userSpeechListeners) cb("start");
  }, (t, dur) => {
    st.lastUserEnd = t;
    const q = st.pendingInterrupt;
    if (q) {
      q.dur = dur;
      if (q.stoppedAt !== undefined) {
        if (dur < 600) metric("相づちで停止の可能性", null, { utterance_ms: dur });
        st.pendingInterrupt = null;
      }
    }
    log("user_speech_end", { duration_ms: dur });
    for (const cb of st.userSpeechListeners) cb("end");
  });
}

function makeCtx(creds) {
  return {
    session: st.session, credentials: creds, micStream: st.mic, audioContext: st.ac, post,
    log, onTranscript: (role, text) => { if (!text) return; st.transcripts.push({ t_ms: now(), scenario: st.scenario, role, text }); log(role === "user" ? "お客様" : "AI", text); },
    attachOutputNode(node) { node.connect(st.aiAnalyser); node.connect(st.ac.destination); },
    attachOutputStream(stream) { st.ac.createMediaStreamSource(stream).connect(st.aiAnalyser); },
    onUserSpeech(cb) { st.userSpeechListeners.add(cb); return () => st.userSpeechListeners.delete(cb); },
    async toolCall(name, args, callerUtterance) {
      const delay = st.scenario === "C5" ? C5_DELAY_MS : 0;
      const call = { t_ms: now(), scenario: st.scenario, name, args, delay_ms: delay };
      log("業務処理", { name, args, delay_ms: delay || undefined });
      try {
        call.result = await post("/api/tool", { session_id: st.session.session_id, name, args, caller_utterance: callerUtterance || "", delay_ms: delay });
      } catch (e) { call.result = { ok: false, reason: String(e.message || e) }; }
      st.toolCalls.push(call);
      log("業務処理の結果", call.result);
      return call.result;
    },
  };
}

async function start() {
  const cand = st.candidates.find((c) => c.id === $("candidate").value);
  if (!cand || !cand.ready) return;
  $("startBtn").disabled = true;
  Object.assign(st, { events: [], transcripts: [], toolCalls: [], latencies: [], stops: [], flags: [],
    lastUserEnd: null, lastAiEnd: null, pendingInterrupt: null });
  st.user = { on: false, since: null, below: null, startT: null };
  st.ai = { on: false, since: null, below: null, startT: null };
  $("metrics").innerHTML = ""; $("log").innerHTML = ""; $("saved").textContent = ""; updateSummary();
  try {
    $("status").textContent = "マイクを準備中";
    const echo = $("echo").checked;
    st.mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: echo, noiseSuppression: echo, autoGainControl: echo } });
    st.ac = new AudioContext();
    await st.ac.resume();
    st.micAnalyser = st.ac.createAnalyser(); st.micAnalyser.fftSize = 1024;
    st.aiAnalyser = st.ac.createAnalyser(); st.aiAnalyser.fftSize = 1024;
    st.ac.createMediaStreamSource(st.mic).connect(st.micAnalyser);
    $("status").textContent = "接続中";
    st.session = await post("/api/session/start", { candidate: cand.id });
    st.t0 = performance.now();
    log("session_start", { candidate: cand.id, verified: st.session.verified });
    if (!st.session.verified) log("注意", "この構成は公式資料に沿って書いたが、接続はまだ確かめていない");
    const mod = await import(`./adapters/${st.session.adapter}.js`);
    st.conn = await mod.connect(makeCtx(st.session.credentials));
    st.loop = setInterval(tick, 20);
    const maxMs = st.session.max_session_min * 60 * 1000;
    st.timer = setInterval(() => {
      const t = now();
      $("timer").textContent = `${Math.floor(t / 60000)}:${String(Math.floor((t % 60000) / 1000)).padStart(2, "0")}`;
      if (t >= maxMs) stop("最大時間で自動終了");
    }, 250);
    $("status").textContent = "会話中";
    $("stopBtn").disabled = false;
  } catch (e) {
    log("エラー", String(e.message || e));
    $("status").textContent = "開始できませんでした";
    await stop("開始の失敗");
  }
}

async function stop(reason) {
  $("stopBtn").disabled = true;
  clearInterval(st.loop); clearInterval(st.timer);
  st.userSpeechListeners.clear();
  const duration = st.t0 ? (performance.now() - st.t0) / 1000 : 0;
  try { if (st.conn) await st.conn.close(); } catch (e) { log("close_error", String(e)); }
  if (st.mic) st.mic.getTracks().forEach((t) => t.stop());
  if (st.session) {
    try { st.session.ended = await post("/api/session/end", { session_id: st.session.session_id, duration_s: duration, reason: reason || "試験者が終了" }); }
    catch (e) { log("end_error", String(e.message || e)); }
    st.session.duration_s = duration;
    log("session_end", { reason: reason || "試験者が終了", duration_s: Math.round(duration), estimate: st.session.ended });
    $("saveBtn").disabled = false;
  }
  st.conn = null;
  if (st.ac) { try { await st.ac.close(); } catch (e) { /* closed */ } }
  $("status").textContent = "終了";
  $("startBtn").disabled = false;
}

async function save() {
  const judgments = {};
  for (const s of SCENARIOS) {
    judgments[s.id] = {};
    for (const [key] of JUDGE_ITEMS) judgments[s.id][key] = $(`j-${s.id}-${key}`).value || null;
  }
  const payload = {
    candidate: st.session.candidate, session_id: st.session.session_id, adapter: st.session.adapter,
    verified_connection: st.session.verified, round: Number($("round").value),
    condition: { output: $("output").value, echo_cancellation: $("echo").checked,
                 thresholds_db: { mic: Number($("micThr").value), ai: Number($("aiThr").value) }, user_agent: navigator.userAgent },
    duration_s: st.session.duration_s, ledger_estimate: st.session.ended || null,
    metrics: { latencies: st.latencies, interruption_stops: st.stops, backchannel_stop_flags: st.flags,
               note: "端末の音量で測った値。相づちの停止は自動推定で、判定欄の記入が正" },
    judgments, notes: $("notes").value, transcripts: st.transcripts, tool_calls: st.toolCalls, events: st.events,
  };
  const r = await post("/api/results", payload);
  $("saved").textContent = `保存しました：${r.saved}`;
}

function renderStatic() {
  $("cards").innerHTML = SCENARIOS.map((s, i) => `<label class="card"><input type="radio" name="sc" value="${s.id}" ${i === 0 ? "checked" : ""}>` +
    `<b>${s.id} ${s.title}</b><p>${s.how}</p><p>見ること：${s.check}</p></label>`).join("");
  document.querySelectorAll("input[name=sc]").forEach((el) => el.addEventListener("change", () => { st.scenario = el.value; log("台本を切り替え", el.value); }));
  const opts = (key) => key === "natural"
    ? `<option value="">—</option>${[1, 2, 3, 4, 5].map((n) => `<option>${n}</option>`).join("")}`
    : `<option value="">—</option><option value="yes">はい</option><option value="no">いいえ</option><option value="na">該当なし</option>`;
  $("judge").innerHTML = `<div></div>${JUDGE_ITEMS.map(([, l]) => `<div><b>${l}</b></div>`).join("")}` +
    SCENARIOS.map((s) => `<div><b>${s.id}</b></div>${JUDGE_ITEMS.map(([k]) => `<select id="j-${s.id}-${k}" aria-label="${s.id} ${k}">${opts(k)}</select>`).join("")}`).join("");
}

async function loadCandidates() {
  const r = await fetch("/api/candidates");
  st.candidates = (await r.json()).candidates;
  $("candidate").innerHTML = st.candidates.map((c) => `<option value="${c.id}" ${c.ready ? "" : "disabled"}>` +
    `${c.name}${c.ready ? "" : `（未設定：${c.missing_env.join("・")}）`}${c.verified ? "" : "［接続未確認］"}</option>`).join("");
  const first = st.candidates.find((c) => c.ready);
  if (first) $("candidate").value = first.id;
  const showNote = () => {
    const c = st.candidates.find((x) => x.id === $("candidate").value);
    $("candNote").textContent = c ? `${c.verified ? "" : "接続未確認の構成です。"}${c.note || ""}` : "";
    $("limit").textContent = c ? `1回の会話は最大${c.max_session_min}分（自動で終了）。上限の目安 $${c.upper_usd_per_min}/分で台帳に留保します。` : "";
  };
  $("candidate").addEventListener("change", showNote);
  showNote();
}

$("startBtn").addEventListener("click", start);
$("stopBtn").addEventListener("click", () => stop());
$("saveBtn").addEventListener("click", save);
renderStatic();
loadCandidates();
window.__lab = st;   // for the automated smoke test
