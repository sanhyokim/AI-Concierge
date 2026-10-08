// Core of the browser conversation lab: candidate switching, device-side measurement, judgments, saving.
// Measurement uses only audio levels on this device (microphone and the AI audio being played),
// so every candidate is measured the same way regardless of what events its API sends.
// The device level cannot tell "the AI finished" from "the AI was cut off", so it never invalidates a readback:
// only an interruption the vendor reports (ctx.vendorInterrupted) does, on the server.
// Caller transcripts go to the server as numbered utterances (the only replies the confirmation guard accepts).
// The server can tell the page to stop (spoken AI refusal, in-app limits, maximum length) through the replies
// and a heartbeat every 10 s.

// Each card says exactly what to say, in order (fictitious data only). The AI may ask in another order:
// answer what it asks, using these lines.
const SCENARIOS = [
  { id: "C1", title: "相づち", how: "AIが話している途中で「はい」「うん」と短く相づちする",
    check: "不必要に止まらない。次の必要な内容を自然に話す",
    lines: ["AIのあいさつを最後まで聞く",
            "「焼肉ほのか博多店の田中です。無煙ロースターの清掃をお願いしたいのですが。」",
            "AIが話している途中で、短く「はい」「うん」と言う（2〜3回）",
            "AIが止まらずに、そのまま話し続けるかを聞く",
            "「以上です。ありがとうございました。」と言い、「終える」を押す"] },
  { id: "C2", title: "「はい、違います」からの訂正", how: "AIの復唱の途中で「はい、違います。電話番号は…」と訂正する",
    check: "冒頭の「はい」を肯定にしない。訂正を聞き、正しい番号を復唱し、肯定まで確認済みにしない",
    lines: ["AIのあいさつを最後まで聞く",
            "「焼肉ほのか博多店の田中です。無煙ロースター8台の清掃をお願いしたいです。」",
            "電話番号を聞かれたら「折り返しは、090-1234-5678です。」",
            "AIが番号を読み上げ始めたら、途中で「はい、違います。末尾は5679です。」",
            "AIが正しい番号（…5679）を読み直したら「はい、合っています。」",
            "ほかに聞かれたら答え、最後に「以上です。」と言って「終える」を押す"] },
  { id: "C3", title: "停止の求め", how: "AIの発話の最初・途中・終わり際で「ちょっと待って」と言う",
    check: "止まって待つ。言い終えていない部分を伝えた扱いにしない",
    lines: ["AIのあいさつを聞いてから「点検は無料ですか？」",
            "AIが答え始めたら、すぐに「ちょっと待って」",
            "2〜3秒黙ってから「すみません、続けてください」",
            "AIの返事の途中と、終わり際でも「ちょっと待って」と言ってみる",
            "「以上です。」と言い、「終える」を押す"] },
  { id: "C4", title: "間と言い直し", how: "考えて一度間を空け、同じ内容を言い直す",
    check: "発話の終わりを早まって決めない。言い直した後の意味で受け付ける",
    lines: ["AIのあいさつを聞く",
            "「えーと……（2秒黙る）……焼肉ほのか博多店です。」",
            "「用件は、清掃の……いや、点検をお願いしたいです。」",
            "AIが「点検」として受け付けたか（清掃と取り違えていないか）を聞く",
            "「以上です。」と言い、「終える」を押す"] },
  { id: "C5", title: "処理中の追加・訂正", how: "（業務処理を4秒遅らせる）その間に用件を追加・訂正する",
    check: "処理中も聞ける。結果を先に約束しない。保存・要約と会話が食い違わない",
    lines: ["「焼肉ほのか博多店の田中です。無煙ロースター8台の清掃をお願いします。」",
            "AIが確認や処理をしている間（約4秒）に「あと、ダクトの点検もお願いします。」",
            "「希望日は10月8日の午後3時です。……すみません、9日の金曜日に変更してください。」",
            "最後にAIがまとめた内容（清掃＋ダクトの点検、9日の金曜日）が合っているかを聞く",
            "「以上です。」と言い、「終える」を押す"] },
  { id: "C6", title: "専門用語・番号・日時", how: "株式会社野田、ダクト、無煙ロースター、電話番号、日付・時間を伝える",
    check: "発音・受付の正確さ。電話番号・日時を区切って明瞭に復唱し、確認する（部分減速は必須ではない）",
    lines: ["「株式会社野田さんですよね。無煙ロースターとダクトの清掃をお願いしたいです。」",
            "電話番号を聞かれたら「090-1234-5678です。」",
            "日時を聞かれたら「10月9日、金曜日の午後3時でお願いします。」",
            "AIの読み方（会社名・ダクト・番号・日時）が正しく、聞き取りやすいかを聞く",
            "「以上です。」と言い、「終える」を押す"] },
];
// Audio-only judgments (any candidate) and business-logic judgments (only where tool calls reached the server).
const JUDGE_AUDIO = [
  ["needless_stop", "相づちで不要に止まった"], ["missed_correction", "訂正を取りこぼした"],
  ["stop_ok", "停止の求めに応じた"], ["audible_stop", "実際に再生が止まった（耳で確認）"],
  ["unsaid_ok", "聞かれていない部分を、伝えた扱いにしていない"], ["natural", "自然さ（1〜5）"],
];
const JUDGE_LOGIC = [["readback_ok", "番号・日時の復唱と確認が正しい"], ["data_ok", "受付記録が会話と一致"]];
const JUDGE_ITEMS = [...JUDGE_AUDIO, ...JUDGE_LOGIC];
const STOP_WINDOW_MS = 2500;   // a guide for reading the numbers, not a pass criterion
const SLOW_LIMIT_MS = 6000;    // a later stop is still measured and reported as slow
const AI_END_HOLD_MS = 250;    // the AI's end is confirmed this long after its audio drops
const SHORT_UTTERANCE_MS = 600;
const OUTCOME_LABELS = { stopped: "2.5秒以内に停止（目安）", slow_stop: "遅い停止（2.5〜6秒）", not_stopped: "止まらず（6秒以内に停止なし）",
  superseded: "判定前に次の発話（未判定）", open_at_end: "会話の終了で未判定" };
const C5_DELAY_MS = 4000;
const HEARTBEAT_MS = 10000;
const $ = (id) => document.getElementById(id);

const st = {
  candidates: [], session: null, adapter: null, conn: null, ac: null, mic: null,
  micAnalyser: null, aiAnalyser: null, t0: 0, timer: null, loop: null, scenario: "C1",
  events: [], transcripts: [], toolCalls: [], latencies: [], stops: [], flags: [], interruptions: [], csrf: "", lastInterrupt: null,
  user: { on: false, since: null, below: null, startT: null }, ai: { on: false, since: null, below: null, startT: null },
  lastUserEnd: null, lastAiEnd: null, pendingInterrupt: null, userSpeechListeners: new Set(),
  uttSeq: 0, aiStopped: false, closing: false, hb: null,
};
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": st.csrf }, body: JSON.stringify(body) });
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
  if (kind === "相づちで停止の可能性") st.flags.push(row);
  addMetricRow(row.scenario, kind, value === null ? "要確認" : `${value} ms`, kind.startsWith("相づち"));
  updateSummary();
}

function addMetricRow(scenario, kind, text, flag) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td>${scenario}</td><td class="${flag ? "flag" : ""}">${kind}</td><td>${text}</td>`;
  $("metrics").prepend(tr);
}

// Every interruption gets an outcome. Nothing is dropped: slow, missing and undecided stops are kept and counted.
function resolveInterrupt(p, outcome, value) {
  if (p.row) return;
  p.row = { scenario: p.scenario, kind: "割り込み", outcome, outcome_label: OUTCOME_LABELS[outcome], value_ms: value,
            t_ms: p.t, user_utterance_ms: p.dur ?? null };
  st.interruptions.push(p.row);
  if (outcome === "stopped" || outcome === "slow_stop") st.stops.push({ ...p.row, kind: "割り込みで止まるまで" });
  const short = p.dur !== undefined && p.dur < SHORT_UTTERANCE_MS;
  const label = outcome === "not_stopped" && short ? "相づちの後も話し続けた（C1では期待どおりの可能性）" : OUTCOME_LABELS[outcome];
  addMetricRow(p.scenario, `割り込み：${label}`, value === null ? "—" : `${value} ms`, outcome !== "stopped");
  if ((outcome === "stopped" || outcome === "slow_stop") && short) metric("相づちで停止の可能性", null, { utterance_ms: p.dur });
  if (st.pendingInterrupt === p) st.pendingInterrupt = null;
  st.lastInterrupt = p;
  updateSummary();
}

function pct(list, q) {
  if (!list.length) return null;
  const v = list.map((r) => r.value_ms).sort((a, b) => a - b);
  return v[Math.min(v.length - 1, Math.floor(q * (v.length - 1)))];
}

function updateSummary() {
  const f = (x) => (x === null ? "—" : `${x} ms`);
  const n = (o) => st.interruptions.filter((r) => r.outcome === o).length;
  const shortNot = st.interruptions.filter((r) => r.outcome === "not_stopped" && r.user_utterance_ms !== null && r.user_utterance_ms < SHORT_UTTERANCE_MS).length;
  $("summary").textContent = `応答の遅延：中央値 ${f(pct(st.latencies, 0.5))}・p90 ${f(pct(st.latencies, 0.9))}（${st.latencies.length}件）／` +
    `割り込み ${st.interruptions.length}件：2.5秒以内に停止（目安。合格の基準ではない） ${n("stopped")}件・遅い停止 ${n("slow_stop")}件・止まらず ${n("not_stopped")}件（うち短い発話 ${shortNot}件）・` +
    `未判定 ${n("superseded") + n("open_at_end")}件。停止までの中央値 ${f(pct(st.stops, 0.5))}・p90 ${f(pct(st.stops, 0.9))}は、停止を測れた${st.stops.length}件だけで計算／` +
    `相づちで停止の可能性：${st.flags.length}件（自動推定。判定欄で確認）。AIが自然に話し終えた場合も停止に数えることがあり、自動では区別できない`;
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
  step("ai", aiDb, aiThr, 40, AI_END_HOLD_MS, (t) => {
    if (st.lastUserEnd !== null && (st.lastAiEnd === null || st.lastUserEnd > st.lastAiEnd) && !st.user.on) {
      metric("応答の遅延", t - st.lastUserEnd);
    }
    log("ai_audio_start");
  }, (t) => {
    st.lastAiEnd = t;
    const q = st.pendingInterrupt;
    if (q) {   // t is the first quiet frame, so the measured time does not include the end hold
      const ms = t - q.t;
      resolveInterrupt(q, ms <= STOP_WINDOW_MS ? "stopped" : ms <= SLOW_LIMIT_MS ? "slow_stop" : "not_stopped", ms <= SLOW_LIMIT_MS ? ms : null);
    }
    log("ai_audio_end");
  });
  // checked after the AI side, with the end hold added, so a stop at 2.4 s is never mistaken for "kept talking"
  const p = st.pendingInterrupt;
  if (p && now() - p.t > SLOW_LIMIT_MS + AI_END_HOLD_MS + 40) {
    log("AIは話し続けた", { after_user_start_ms: SLOW_LIMIT_MS, user_utterance_ms: p.dur ?? null });
    resolveInterrupt(p, "not_stopped", null);
  }
  step("user", micDb, micThr, 60, 300, (t) => {
    if (st.ai.on) {
      if (st.pendingInterrupt) resolveInterrupt(st.pendingInterrupt, "superseded", null);
      st.pendingInterrupt = { t, scenario: st.scenario };
    }
    log("user_speech_start", st.ai.on ? "AIの発話中" : undefined);
    for (const cb of st.userSpeechListeners) cb("start");
  }, (t, dur) => {
    st.lastUserEnd = t;
    const q = st.pendingInterrupt || (st.lastInterrupt && st.lastInterrupt.dur === undefined && st.lastInterrupt.t === t - dur ? st.lastInterrupt : null);
    if (q) {
      q.dur = dur;
      if (q.row) {
        q.row.user_utterance_ms = dur;
        if ((q.row.outcome === "stopped" || q.row.outcome === "slow_stop") && dur < SHORT_UTTERANCE_MS) metric("相づちで停止の可能性", null, { utterance_ms: dur });
      }
    }
    log("user_speech_end", { duration_ms: dur });
    for (const cb of st.userSpeechListeners) cb("end");
  });
}

// --- what the server must hear: caller utterances, vendor interruptions, vendor counts ----------------------

async function sendUtterance(text) {
  const seq = ++st.uttSeq;
  try {
    const r = await post("api/utterance", { session_id: st.session.session_id, text, seq });
    if (r.recording_stopped) log("録音の拒否（発話）", "録音を止め、拒否より前の録音を削除する。AIの利用は拒否されていないので、会話は続ける");
    if (r.human_request) log("人との会話の希望", "ライブ転送はできない。折り返しを案内する（X-02）");
    if (r.stop_ai) await stopAi(r.reason || "ai_refused_by_speech", "AIの拒否（発話）");
    return r;
  } catch (e) { log("utterance_error", String(e.message || e)); return null; }
}

async function reportInterrupted(source) {
  log("業者の割り込みの通知", source);
  if (!st.session || st.aiStopped || st.closing) return null;
  try {
    const r = await post("api/interrupted", { session_id: st.session.session_id, source });
    if (r.invalidated && r.invalidated.length) log("復唱を無効にした（返事の前に遮られた）", r.invalidated);
    return r;
  } catch (e) { log("interrupted_error", String(e.message || e)); return null; }
}

async function reportVendorEvent(kind, data) {
  if (!st.session) return null;
  try {
    const r = await post("api/vendor_event", { session_id: st.session.session_id, kind, data: data || {} });
    if (r.stop && !st.closing) { log("アプリ内の制限で終了", r.reason); stop(`アプリ内の制限（${r.reason}）`); }
    return r;
  } catch (e) { log("vendor_event_error", String(e.message || e)); return null; }
}

// AI refusal: stop sending and playing at once, then close the connection. Later transcripts are not kept.
async function stopAi(reason, label) {
  if (st.aiStopped) return;
  st.aiStopped = true;
  if (st.mic) st.mic.getTracks().forEach((t) => { t.enabled = false; });
  try { if (st.conn && st.conn.mute) st.conn.mute(); } catch (e) { /* closing anyway */ }
  if (st.ac) { try { await st.ac.suspend(); } catch (e) { /* closed */ } }
  const n = st.transcripts.length;
  st.transcripts = [];
  st.events = st.events.filter((e) => e.kind !== "お客様" && e.kind !== "AI");
  log(label, `AIへの送信と再生を止め、接続を閉じる。拒否より前の文字起こし${n}件を結果から除いた（サーバーでも削除）。` +
    "認識されるまでに業者へ送った音声は取り消せない");
  await stop(`${label}（${reason}）`);
}

function makeCtx(creds) {
  return {
    session: st.session, credentials: creds, micStream: st.mic, audioContext: st.ac, post, log,
    // returns the server's answer for a caller transcript (adapters may await it; most do not need to)
    onTranscript: (role, text) => {
      if (!text || st.aiStopped) return Promise.resolve(null);
      st.transcripts.push({ t_ms: now(), scenario: st.scenario, role, text });
      log(role === "user" ? "お客様" : "AI", text);
      return role === "user" && st.session && !st.closing ? sendUtterance(text) : Promise.resolve(null);
    },
    vendorInterrupted: (source) => reportInterrupted(source),
    vendorEvent: (kind, data) => reportVendorEvent(kind, data),
    attachOutputNode(node) { node.connect(st.aiAnalyser); node.connect(st.ac.destination); },
    attachOutputStream(stream) { st.ac.createMediaStreamSource(stream).connect(st.aiAnalyser); },
    onUserSpeech(cb) { st.userSpeechListeners.add(cb); return () => st.userSpeechListeners.delete(cb); },
    async toolCall(name, args) {   // the caller's words are never attached here: the server uses utterances only
      if (st.aiStopped) return { ok: false, reason: "ai_refused" };
      const delay = st.scenario === "C5" ? C5_DELAY_MS : 0;
      const call = { t_ms: now(), scenario: st.scenario, name, args, delay_ms: delay };
      log("業務処理", { name, args, delay_ms: delay || undefined });
      try {
        call.result = await post("api/tool", { session_id: st.session.session_id, name, args, delay_ms: delay });
      } catch (e) { call.result = { ok: false, reason: String(e.message || e) }; }
      st.toolCalls.push(call);
      log("業務処理の結果", call.result);
      if (call.result && call.result.stop && !st.closing) stop(`アプリ内の制限（${call.result.reason}）`);
      return call.result;
    },
  };
}

function heartbeat() {
  st.hb = setInterval(async () => {
    if (!st.session || st.closing) return;
    try {
      const r = await post("api/session/heartbeat", { session_id: st.session.session_id });
      if (!r.stop || st.closing) return;
      log("サーバーの停止の指示", r.reason);
      if (String(r.reason).startsWith("ai_refused")) await stopAi(r.reason, "AIの拒否");
      else await stop(`サーバーの指示で終了（${r.reason}）`);
    } catch (e) { log("heartbeat_error", String(e.message || e)); }
  }, HEARTBEAT_MS);
}

function appliedText(a) {
  if (!a) return "";
  return `<br>この会話に反映する設定：指示 ${esc(a.instructions)}／声 ${esc(a.voice)}／FAQ ${esc(a.faq)}`;
}

async function start() {
  const cand = st.candidates.find((c) => c.id === $("candidate").value);
  if (!cand || !cand.ready) return;
  $("startBtn").disabled = true;
  Object.assign(st, { events: [], transcripts: [], toolCalls: [], latencies: [], stops: [], flags: [], interruptions: [],
    lastUserEnd: null, lastAiEnd: null, pendingInterrupt: null, lastInterrupt: null, session: null, conn: null,
    uttSeq: 0, aiStopped: false, closing: false });
  $("saveBtn").disabled = true;
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
    const fakeScript = new URLSearchParams(location.search).get("fake_script") || undefined;
    st.session = await post("api/session/start", { candidate: cand.id, fake_script: fakeScript });
    st.t0 = performance.now();
    log("session_start", { candidate: cand.id, verified: st.session.verified, call_id: st.session.call_id, voice: st.session.voice,
      applied: st.session.applied, counts: st.session.counts });
    if (!st.session.verified) log("注意", "この構成は公式資料に沿って書いたが、接続はまだ確かめていない");
    const c = st.session.counts || {};
    $("callInfo").innerHTML = `業務処理：${esc(st.session.logic_path)}。受付の記録 <code>${esc(st.session.call_id)}</code>（設定 v${esc(st.session.config_version)}、FAQ ${esc(st.session.faq_codes.join(", "))}）` +
      `。開始 ${c.sessions}回目${c.limit != null ? `（上限${c.limit}回）` : "（回数の上限なし）"}` +
      (location.pathname.startsWith("/lab/") ? ` <a href="../#/call/${encodeURIComponent(st.session.call_id)}" target="_blank" rel="noopener">管理画面で開く</a>` : "") +
      appliedText(st.session.applied);
    const mod = await import(`./adapters/${st.session.adapter}.js`);
    st.conn = await mod.connect(makeCtx(st.session.credentials));
    st.loop = setInterval(tick, 20);
    heartbeat();
    const maxMs = st.session.max_session_min * 60 * 1000;
    st.timer = setInterval(() => {
      const t = now();
      $("timer").textContent = `${Math.floor(t / 60000)}:${String(Math.floor((t % 60000) / 1000)).padStart(2, "0")}`;
      if (t >= maxMs) stop("最大時間で自動終了");
    }, 250);
    $("status").textContent = cand.id === "fake" ? "会話中（オフラインの模擬：AIには接続していません。音は電子音です）" : "会話中";
    $("stopBtn").disabled = false;
    $("refuseBtn").disabled = false;
  } catch (e) {
    log("エラー", String(e.message || e));
    $("status").textContent = "開始できませんでした";
    await stop("開始の失敗");
    $("status").textContent = `開始できませんでした：${String(e.message || e).slice(0, 900)}`;
    loadCandidates();   // counts changed
  }
}

async function stop(reason) {
  if (st.closing) return;
  st.closing = true;
  $("stopBtn").disabled = true;
  $("refuseBtn").disabled = true;
  if (st.pendingInterrupt) resolveInterrupt(st.pendingInterrupt, "open_at_end", null);
  clearInterval(st.loop); clearInterval(st.timer); clearInterval(st.hb);
  st.userSpeechListeners.clear();
  const duration = st.t0 ? (performance.now() - st.t0) / 1000 : 0;
  if (st.mic) st.mic.getTracks().forEach((t) => { t.enabled = false; });
  let closeInfo = {};
  try { if (st.conn) closeInfo = (await st.conn.close()) || {}; } catch (e) { log("close_error", String(e)); }
  if (st.mic) st.mic.getTracks().forEach((t) => t.stop());
  if (st.session) {
    try {
      st.session.ended = await post("api/session/end", { session_id: st.session.session_id, duration_s: duration,
        reason: reason || "試験者が終了", close_confirmed: !!closeInfo.closed, final_usage: closeInfo.usage || null });
    } catch (e) { log("end_error", String(e.message || e)); st.session.ended = { error: String(e.message || e) }; }
    st.session.duration_s = duration;
    log("session_end", { reason: reason || "試験者が終了", duration_s: Math.round(duration), estimate: st.session.ended,
      vendor_close: closeInfo.closed === undefined ? "対象外" : closeInfo.closed ? "業者の終了を確認" : "業者の終了・使用量は未確認" });
    $("saveBtn").disabled = false;
  }
  st.conn = null;
  if (st.ac) { try { await st.ac.close(); } catch (e) { /* closed */ } }
  $("status").textContent = "終了";
  $("startBtn").disabled = false;
}

async function refuseAi() {
  // the tester plays a caller who refuses AI processing: the server records it and refuses further AI work
  const r = await post("api/consent", { session_id: st.session.session_id, kind: "ai_refused" });
  log("AIの拒否（模擬）", r.actions.map((a) => a.label));
  if (r.stop_ai) await stopAi("ai_refused", "AIの拒否（業者への接続を閉じた）");
}

// closing the tab: tell the server (keepalive) so the session does not wait for the watchdog
window.addEventListener("pagehide", () => {
  if (!st.session || st.session.ended || st.closing) return;
  const body = JSON.stringify({ session_id: st.session.session_id, reason: "画面を閉じた（pagehide）",
    duration_s: st.t0 ? (performance.now() - st.t0) / 1000 : 0 });
  try {
    fetch("api/session/end", { method: "POST", keepalive: true, body,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": st.csrf } });
  } catch (e) { /* the watchdog expires it */ }
});

async function save() {
  const judgments = {}, audio = {}, logic = {};
  for (const s of SCENARIOS) {
    judgments[s.id] = {}; audio[s.id] = {}; logic[s.id] = {};
    for (const [key] of JUDGE_ITEMS) judgments[s.id][key] = $(`j-${s.id}-${key}`).value || null;
    for (const [key] of JUDGE_AUDIO) audio[s.id][key] = judgments[s.id][key];
    for (const [key] of JUDGE_LOGIC) logic[s.id][key] = judgments[s.id][key];
  }
  const payload = {
    candidate: st.session.candidate, session_id: st.session.session_id, adapter: st.session.adapter,
    verified_connection: st.session.verified, round: Number($("round").value),
    condition: { output: $("output").value, echo_cancellation: $("echo").checked,
                 thresholds_db: { mic: Number($("micThr").value), ai: Number($("aiThr").value) }, user_agent: navigator.userAgent },
    duration_s: st.session.duration_s, ledger_estimate: st.session.ended || null,
    metrics: { latencies: st.latencies, interruptions: st.interruptions, interruption_stops: st.stops, backchannel_stop_flags: st.flags,
               note: "端末の音量で測った値。割り込みはすべて結果付きで残す（遅い停止・止まらず・未判定を含む）。相づちの停止は自動推定で、判定欄の記入が正" },
    comparison: { audio_only: { judgments: audio, note: "音声だけの比較（すべての候補）" },
                  business_logic: { logic_path: st.session.logic_path, judgments: logic,
                                    note: "業務処理を含む比較。サーバーが業務処理を受け取らなかった会話は未評価" } },
    judgments, notes: $("notes").value, transcripts: st.transcripts, tool_calls: st.toolCalls, events: st.events,
  };
  const r = await post("api/results", payload);
  $("saved").textContent = `保存しました：${r.saved}`;
  $("saveBtn").disabled = true;   // one file per conversation
}

function renderStatic() {
  $("cards").innerHTML = SCENARIOS.map((s, i) => `<label class="card"><input type="radio" name="sc" value="${s.id}" ${i === 0 ? "checked" : ""}>` +
    `<b>${s.id} ${s.title}</b><ol class="lines">${s.lines.map((l) => `<li>${l}</li>`).join("")}</ol>` +
    `<p>見ること：${s.check}</p></label>`).join("");
  document.querySelectorAll("input[name=sc]").forEach((el) => el.addEventListener("change", () => { st.scenario = el.value; log("台本を切り替え", el.value); }));
  const opts = (key) => key === "natural"
    ? `<option value="">—</option>${[1, 2, 3, 4, 5].map((n) => `<option>${n}</option>`).join("")}`
    : `<option value="">—</option><option value="yes">はい</option><option value="no">いいえ</option><option value="na">該当なし</option>`;
  $("judge").style.gridTemplateColumns = `70px repeat(${JUDGE_ITEMS.length}, minmax(96px, 1fr))`;
  $("judge").innerHTML = `<div></div>${JUDGE_AUDIO.map(() => "<div class='grp'>音声</div>").join("")}${JUDGE_LOGIC.map(() => "<div class='grp'>業務処理</div>").join("")}` +
    `<div></div>${JUDGE_ITEMS.map(([, l]) => `<div><b>${l}</b></div>`).join("")}` +
    SCENARIOS.map((s) => `<div><b>${s.id}</b></div>${JUDGE_ITEMS.map(([k]) => `<select id="j-${s.id}-${k}" aria-label="${s.id} ${k}">${opts(k)}</select>`).join("")}`).join("");
}

function envHelp(c) {
  if (!c.missing_env.length) return "";
  const items = c.missing_env.map((k) => `<li><code>${esc(k)}</code>：${esc(c.env_help[k] || "")}</li>`).join("");
  const unix = c.missing_env.map((k) => `export ${k}='（ここに値）'`).join("\n");
  const win = c.missing_env.map((k) => `$env:${k} = '（ここに値）'`).join("\n");
  return `<details open><summary>この候補に必要な設定（この端末だけで行う）</summary><ul>${items}</ul>` +
    `<p>mac・Linux（ターミナル）</p><pre>${esc(unix)}\npython3 -m prototype.browser_lab</pre>` +
    `<p>Windows（PowerShell）</p><pre>${esc(win)}\npython -m prototype.browser_lab</pre>` +
    "<p>鍵の値は、チャット・メール・ファイルに貼らず、この端末のターミナルにだけ入力します。設定したターミナルで試験のサーバーを起動し直します。</p></details>";
}

async function loadCandidates() {
  try { st.csrf = (await (await fetch("api/csrf")).json()).csrf || ""; } catch (e) { st.csrf = ""; }
  const r = await fetch("api/candidates");
  const listed = await r.json();
  const keep = $("candidate").value;
  st.candidates = listed.candidates;
  st.stage = listed.stage_label;
  $("candidate").innerHTML = st.candidates.map((c) => `<option value="${esc(c.id)}" ${c.ready ? "" : "disabled"}>` +
    `${esc(c.name)}${c.hold ? "（保留）" : c.ready ? "" : `（未設定：${esc(c.missing_env.join("・"))}）`}${c.verified ? "" : "［接続未確認］"}</option>`).join("");
  // a real candidate first when one is ready: the offline stand-in only beeps and is easy to start by mistake
  const first = st.candidates.find((c) => c.id === keep && c.ready) || st.candidates.find((c) => c.ready && c.id !== "fake")
    || st.candidates.find((c) => c.ready);
  if (first) $("candidate").value = first.id;
  const showNote = () => {
    const c = st.candidates.find((x) => x.id === $("candidate").value);
    if (!c) { $("candNote").innerHTML = ""; $("limit").textContent = ""; return; }
    const n = c.counts || {};
    $("candNote").innerHTML = (c.hold ? `<p><b>保留中：</b>${esc(c.hold)}</p>` : "") +
      `<p>${c.verified ? "" : "接続未確認の構成です。"}業務処理の経路：${esc(c.logic_path)}。${esc(c.note || "")}</p>` +
      `<p>管理画面の設定の反映：指示 ${esc(c.applies.instructions)}／声 ${esc(c.applies.voice)}</p>` +
      (c.hold ? "" : `<p>これまでの開始：${n.sessions}回${n.limit != null ? `（上限${n.limit}回）` : "（回数の上限なし）"}。接続の失敗：${n.mint_failures}/${n.mint_failure_limit}回（超えたら「費用の台帳」で解除）</p>`) +
      envHelp(c);
    $("limit").textContent = `1回の会話は${c.max_session_min}分で自動的に止まります（画面を閉じても、サーバーが止めます）。終わったら「終える」を押してください。` +
      `費用は、会話ごとに台帳へ見込みを記録します（最大時間×$${c.upper_usd_per_min}/分。GPT-Liveは裏方のモデルの分を足す）。` +
      "回数と費用の上限はありません。業者側の予算の設定（OpenAIの月の予算など）は残しておいてください。";
  };
  $("candidate").onchange = showNote;
  showNote();
}

$("startBtn").addEventListener("click", start);
$("stopBtn").addEventListener("click", () => stop());
$("saveBtn").addEventListener("click", save);
$("refuseBtn").addEventListener("click", () => refuseAi().catch((e) => log("エラー", String(e.message || e))));
renderStatic();
loadCandidates();
if (location.pathname.startsWith("/lab/")) {   // served by the admin app: link back to it
  const a = document.createElement("a");
  a.href = "../#/"; a.textContent = "← 管理画面へ";
  document.querySelector("main").prepend(a);
}
window.__lab = st;   // for the automated smoke test
