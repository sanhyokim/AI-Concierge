"use strict";
// Admin app (vanilla JS, no build step). All business rules live on the server; this page only shows and edits.
let view = document.getElementById("view");
// each screen gets a fresh element, so listeners from the previous screen never pile up
function resetView() { const fresh = view.cloneNode(false); view.replaceWith(fresh); view = fresh; }
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let CSRF = "";

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = "/login"; throw new Error("ログインが必要です"); }
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 3200);
}
async function guarded(fn) { try { await fn(); } catch (e) { toast(`エラー：${e.message}`); } }
const fmt = (iso) => (iso ? iso.replace("T", " ").replace(/\+09:00$/, "") : "");
const statusBadge = (st, label) => {
  const cls = { confirmed_by_caller: "good", corrected_by_staff: "info", empty: "", heard: "warn", awaiting_confirmation: "warn",
    corrected_unconfirmed: "warn", rejected_by_caller: "bad", accepted: "good", pending: "info", retrying: "warn",
    failed_stopped: "bad", channel_stopped: "bad", unknown: "bad", superseded: "", discarded: "", resent: "",
    blocked_unapproved: "warn", available: "good", unverified: "warn", no_key: "bad" }[st] ?? "";
  return `<span class="badge ${cls}">${esc(label || st)}</span>`;
};
const STATE_LABELS = { ai_conversation: "AIと会話中", human_request_pending: "人との会話の希望を確認中", dtmf_entry: "プッシュボタンで番号の入力待ち",
  dtmf_confirm: "番号の確認待ち（1か2）", dtmf_offer_caller_id: "回線の番号に折り返すか確認中", handed_to_normal: "普通受電へ渡した", ended: "終了" };
const OUTCOME_LABELS = { accepted: "受付済み", retry_same_key: "同じキーで再試行", channel_stopped: "経路を停止", failed_stopped: "止めて人が確認",
  unknown_after_24h: "24時間で結果不明", not_sent_unapproved: "送信せず（承認前）" };
const routeBadge = (route, label) => `<span class="badge ${route === "ai" ? "info" : route === "normal" ? "good" : "warn"}">${esc(label)}</span>`;

// --- alerts shown on every screen ---------------------------------------------------------------------------
async function refreshAlerts() {
  const s = await api("/api/status");
  const out = [];
  const p = s.notification_problems;
  const bad = (p.counts.failed_stopped || 0) + (p.counts.unknown || 0) + (p.counts.channel_stopped || 0);
  if (bad) out.push(`<div class="alert bad">通知の障害があります：失敗 ${p.counts.failed_stopped || 0}件・結果不明 ${p.counts.unknown || 0}件・停止中 ${p.counts.channel_stopped || 0}件。<a href="#/notify">通知の画面で確認</a></div>`);
  for (const c of p.stopped_channels) out.push(`<div class="alert bad">通知の経路「${esc(c.channel)}」が停止中：${esc(c.reason)}</div>`);
  if (s.manual_mode_warning) out.push(`<div class="alert info">受電設定を手動で「${esc(s.mode_label)}」に固定しています（スケジュールは使っていません）。</div>`);
  if (s.provisional) out.push(`<div class="alert info">営業曜日・時間・FAQは暫定・架空の値です。本番の決定済みの設定ではありません。</div>`);
  if (s.retention_unset) out.push(`<div class="alert info">受付記録の保存期間が未設定です（受電設定の画面で設定できます。自動削除は未実装）。</div>`);
  document.getElementById("alerts").innerHTML = out.join("");
}

// --- router -----------------------------------------------------------------------------------------------
const routes = {};
async function render() {
  const [, name = "", arg] = location.hash.split("/");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#/${name === "call" ? "calls" : name}`));
  resetView();
  view.innerHTML = `<p class="muted">読み込み中…</p>`;
  try { await (routes[name] || routes[""])(arg ? decodeURIComponent(arg) : undefined); }
  catch (e) { view.innerHTML = `<div class="card error">表示できませんでした：${esc(e.message)}</div>`; }
  refreshAlerts().catch(() => {});
}
window.addEventListener("hashchange", render);

// --- home -------------------------------------------------------------------------------------------------
routes[""] = async () => {
  const [s, c] = await Promise.all([api("/api/status"), api("/api/calls")]);
  const d = s.decision_now;
  view.innerHTML = `<div class="grid">
    <section class="card"><h2>現在の受電設定</h2><div class="big" id="homeMode">${esc(s.mode_label)}</div>
      <p class="sub">設定の版 v${s.config_version}（${esc(fmt(s.config_meta.created_at))}・${esc(s.config_meta.created_by || "")}）</p>
      <a href="#/mode"><button>受電設定を変える</button></a></section>
    <section class="card"><h2>今この時刻の判定</h2><div class="big">${routeBadge(d.route, d.route_label)}</div>
      <p class="sub">${esc(d.reason)}<br>${esc(fmt(d.at_jst))}（日本時間）</p>
      <p class="sub">実際の着信には反映されません（会社回線は未接続）。</p></section>
    <section class="card"><h2>架空の着信で試す</h2><p class="sub">保存した設定と日時で経路を判定し、受付・要約・通知のテストまで行います。</p>
      <a href="#/demo"><button class="primary">着信テストへ</button></a></section>
  </div>
  <section class="card" style="margin-top:12px"><h2>最近の受付</h2>${callsTable(c.calls.slice(0, 5))}<a href="#/calls">すべて見る</a></section>`;
};

// --- reception mode ---------------------------------------------------------------------------------------
const MODE_HELP = {
  always_ai: "時間帯に関係なく、AIがご用件を伺い、担当者が折り返します。スケジュールは使いません。",
  always_normal: "時間帯に関係なく、最初から担当者の電話へ回します。AIは使いません。スケジュールは使いません。",
  schedule: "スケジュールの画面で決めた曜日・時間帯と特別日、時間外の扱いで、日本時間の着信日時から判定します。",
};
routes.mode = async () => {
  const c = await api("/api/config");
  const cfg = c.config;
  view.innerHTML = `<section class="card"><h1>受電設定</h1>
    <p class="sub">保存すると新しい版になり、<b>次の着信から</b>使われます。通話中の着信には影響しません。今の版：v${c.version}</p>
    <div class="mode-options" id="modes">${Object.entries(c.labels.modes).map(([k, l]) => `
      <label class="mode-option ${cfg.mode === k ? "selected" : ""}"><input type="radio" name="mode" value="${k}" ${cfg.mode === k ? "checked" : ""}>
      <div><b>${esc(l)}</b><p>${esc(MODE_HELP[k])}</p></div></label>`).join("")}</div>
    <label>AIが使えないとき（着信の時点）の動作
      <select id="failure">${Object.entries(c.labels.failure_actions).map(([k, l]) => `<option value="${k}" ${cfg.ai_failure_action === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
    <label>受付記録の保存期間（日。空欄は未設定）<input id="retention" type="number" min="1" value="${cfg.retention_days ?? ""}"></label>
    <label>変更のメモ（任意）<input id="note" maxlength="200"></label>
    <div class="row"><button class="primary" id="saveMode">保存</button></div></section>
    <section class="card" style="margin-top:12px"><h2>保存の履歴</h2><table class="stack"><thead><tr><th>版</th><th>日時</th><th>保存した人</th><th>メモ</th></tr></thead><tbody>
    ${c.history.map((h) => `<tr><td data-label="版">v${h.id}</td><td data-label="日時">${esc(fmt(h.created_at))}</td><td data-label="保存した人">${esc(h.created_by)}</td><td data-label="メモ">${esc(h.note)}</td></tr>`).join("")}</tbody></table></section>`;
  view.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", () => {
    view.querySelectorAll(".mode-option").forEach((o) => o.classList.toggle("selected", o.querySelector("input").checked));
  }));
  document.getElementById("saveMode").addEventListener("click", () => guarded(async () => {
    const mode = view.querySelector("input[name=mode]:checked").value;
    const rd = document.getElementById("retention").value;
    const r = await api("/api/config", { config: { ...cfg, mode, ai_failure_action: document.getElementById("failure").value, retention_days: rd === "" ? null : Number(rd) },
      note: document.getElementById("note").value });
    toast(`v${r.version} を保存しました。次の着信から使われます`);
    render();
  }));
};

// --- schedule ---------------------------------------------------------------------------------------------
routes.schedule = async () => {
  const c = await api("/api/config");
  const cfg = c.config, L = c.labels;
  const weekly = JSON.parse(JSON.stringify(cfg.schedule.weekly));
  const specials = JSON.parse(JSON.stringify(cfg.schedule.special_days));
  const routeOpts = (sel) => Object.entries({ normal: L.routes.normal, ai: L.routes.ai }).map(([k, l]) => `<option value="${k}" ${sel === k ? "selected" : ""}>${esc(l)}</option>`).join("");
  const draw = () => {
    document.getElementById("days").innerHTML = Object.entries(L.weekdays).map(([d, label]) => `
      <div class="day"><div class="row"><b>${esc(label)}曜日</b><button class="small" data-add="${d}">＋時間帯</button>
      ${weekly[d].length ? "" : `<span class="muted">時間帯なし（終日「時間外の扱い」）</span>`}</div>
      ${weekly[d].map((b, i) => `<div class="band"><input aria-label="${label} 開始" data-d="${d}" data-i="${i}" data-k="start" value="${esc(b.start)}" inputmode="numeric" placeholder="09:00">
        <span>〜</span><input aria-label="${label} 終了" data-d="${d}" data-i="${i}" data-k="end" value="${esc(b.end)}" inputmode="numeric" placeholder="17:00">
        <select aria-label="${label} モード" data-d="${d}" data-i="${i}" data-k="mode">${routeOpts(b.mode)}</select>
        <button class="small danger" data-del="${d}" data-i="${i}">削除</button></div>`).join("")}</div>`).join("");
    document.getElementById("specials").innerHTML = specials.map((s, i) => `<div class="band">
      <input type="date" aria-label="特別日" data-s="${i}" data-k="date" value="${esc(s.date)}"><span></span>
      <input aria-label="メモ" data-s="${i}" data-k="note" value="${esc(s.note)}" placeholder="メモ（例：臨時休業）">
      <select aria-label="特別日のモード" data-s="${i}" data-k="mode">${routeOpts(s.mode)}</select>
      <button class="small danger" data-sdel="${i}">削除</button></div>`).join("") || `<p class="muted">特別日はありません。</p>`;
  };
  view.innerHTML = `<div class="grid">
    <section class="card"><h1>スケジュール</h1>
      <p class="sub">受電設定が「スケジュール運用」のときだけ使います。日本時間で判定し、時間帯は「開始以上・終了未満」です（9:00〜17:00なら、16:59:59までが対象、17:00から時間外）。日をまたぐときは2つに分けます。終わりは24:00まで。</p>
      <p class="sub"><span class="badge warn">暫定・架空</span> 営業曜日・祝日・時間の本番の設定は未確定です。</p>
      <div id="days"></div>
      <label>時間帯の外（時間外）の扱い <select id="outside">${routeOpts(cfg.schedule.outside)}</select></label>
      <h3>特別日（その日は終日この扱い。時間帯より優先）</h3><div id="specials"></div>
      <button class="small" id="addSpecial">＋特別日</button>
      <div class="row" style="margin-top:10px"><button class="primary" id="saveSchedule">保存</button><span class="sub">保存すると、次の着信から使われます。</span></div>
    </section>
    <section class="card"><h2>指定した日時でどうなるか</h2>
      <p class="sub">保存済みの設定（v${c.version}）で、サーバーの判定をそのまま使います。</p>
      <label>日時（日本時間）<input type="datetime-local" step="1" id="checkAt"></label>
      <label class="inline"><input type="checkbox" id="checkAi" checked> AIが使える状態</label>
      <button id="checkBtn">判定する</button>
      <div id="checkOut" class="card" style="margin-top:8px"><span class="muted">日時を入れて「判定する」</span></div>
      <h3>FAQ-04（受付時間の案内）の文</h3><p class="sub">保存済みの設定から自動で作ります。</p><pre class="body">${esc(c.hours_answer)}</pre>
    </section></div>`;
  draw();
  const now = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 19);
  document.getElementById("checkAt").value = now;
  view.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset.d) weekly[t.dataset.d][Number(t.dataset.i)][t.dataset.k] = t.value;
    if (t.dataset.s) specials[Number(t.dataset.s)][t.dataset.k] = t.value;
  });
  view.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.d) weekly[t.dataset.d][Number(t.dataset.i)][t.dataset.k] = t.value;
    if (t.dataset.s) specials[Number(t.dataset.s)][t.dataset.k] = t.value;
  });
  view.addEventListener("click", (e) => {
    const t = e.target;
    if (t.dataset.add) { weekly[t.dataset.add].push({ start: "09:00", end: "17:00", mode: "normal" }); draw(); }
    if (t.dataset.del) { weekly[t.dataset.del].splice(Number(t.dataset.i), 1); draw(); }
    if (t.dataset.sdel !== undefined) { specials.splice(Number(t.dataset.sdel), 1); draw(); }
  });
  document.getElementById("addSpecial").addEventListener("click", () => { specials.push({ date: now.slice(0, 10), mode: "ai", note: "" }); draw(); });
  document.getElementById("saveSchedule").addEventListener("click", () => guarded(async () => {
    const r = await api("/api/config", { config: { ...cfg, schedule: { weekly, outside: document.getElementById("outside").value, special_days: specials } }, note: "スケジュールの変更" });
    toast(`v${r.version} を保存しました`);
    render();
  }));
  document.getElementById("checkBtn").addEventListener("click", () => guarded(async () => {
    const at = document.getElementById("checkAt").value;
    const d = await api(`/api/route/check?at=${encodeURIComponent(at)}&ai_available=${document.getElementById("checkAi").checked ? 1 : 0}`);
    document.getElementById("checkOut").innerHTML = `<div class="big" id="checkRoute">${routeBadge(d.route, d.route_label)}</div><p>${esc(d.reason)}</p><p class="sub">${esc(fmt(d.at_jst))}・設定 v${d.config_version}・規則 ${esc(d.rule)}</p>`;
  }));
};

// --- FAQ --------------------------------------------------------------------------------------------------
routes.faq = async () => {
  const { faqs } = await api("/api/faqs");
  view.innerHTML = `<section class="card"><h1>FAQ</h1>
    <p class="sub">有効なFAQだけを、AIが答える範囲に使います。変更・有効／無効は<b>次の会話から</b>反映されます（通話中の会話は、開始時のFAQのまま）。すべて未承認の推奨案・試作です。</p>
    <details><summary>FAQを追加する</summary>
      <label>質問の例 <input id="newQ"></label><label>回答 <textarea id="newA" rows="3"></textarea></label>
      <label>探すための語（読点・カンマ区切り）<input id="newK" placeholder="例：駐車場,車"></label>
      <button class="primary" id="addFaq">追加</button></details></section>
    <div id="faqList">${faqs.map((f) => `<section class="card" style="margin-top:10px" data-id="${f.id}">
      <div class="row"><b>${esc(f.code)}</b><span class="badge warn">${esc(f.approval)}</span>${f.generated ? `<span class="badge info">スケジュールから自動</span>` : ""}
        <label class="inline"><input type="checkbox" data-toggle="${f.id}" ${f.enabled ? "checked" : ""}> 有効</label></div>
      <div class="view-mode"><p><b>Q</b> ${esc(f.question)}</p><p><b>A</b> ${esc(f.answer)}</p><p class="sub">探す語：${esc(f.keywords)}</p>
        <button class="small" data-edit="${f.id}">編集</button></div>
      <div class="edit-mode hidden"><label>質問の例 <input data-f="question" value="${esc(f.question)}"></label>
        <label>回答 <textarea data-f="answer" rows="3" ${f.generated ? "disabled" : ""}>${esc(f.answer)}</textarea></label>
        ${f.generated ? `<p class="sub">回答はスケジュールの設定から作ります。スケジュールの画面で変えてください。</p>` : ""}
        <label>探す語 <input data-f="keywords" value="${esc(f.keywords)}"></label>
        <button class="primary small" data-save="${f.id}">保存</button></div></section>`).join("")}</div>`;
  const byId = Object.fromEntries(faqs.map((f) => [String(f.id), f]));
  document.getElementById("addFaq").addEventListener("click", () => guarded(async () => {
    await api("/api/faqs", { question: document.getElementById("newQ").value, answer: document.getElementById("newA").value, keywords: document.getElementById("newK").value, enabled: true });
    toast("追加しました。次の会話から使われます"); render();
  }));
  view.addEventListener("click", (e) => {
    const t = e.target;
    if (t.dataset.edit) { const card = t.closest("[data-id]"); card.querySelector(".view-mode").classList.add("hidden"); card.querySelector(".edit-mode").classList.remove("hidden"); }
    if (t.dataset.save) guarded(async () => {
      const card = t.closest("[data-id]"); const f = byId[t.dataset.save];
      const val = (k) => card.querySelector(`[data-f=${k}]`).value;
      await api(`/api/faqs/${f.id}`, { question: val("question"), answer: val("answer"), keywords: val("keywords"), enabled: !!f.enabled });
      toast("保存しました。次の会話から使われます"); render();
    });
  });
  view.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.toggle) guarded(async () => {
      const f = byId[t.dataset.toggle];
      await api(`/api/faqs/${f.id}`, { question: f.question, answer: f.answer, keywords: f.keywords, enabled: t.checked });
      f.enabled = t.checked ? 1 : 0;
      toast(`${f.code} を${t.checked ? "有効" : "無効"}にしました（次の会話から）`);
    });
  });
};

// --- voices -----------------------------------------------------------------------------------------------
routes.voice = async () => {
  const [v, c] = await Promise.all([api("/api/voices"), api("/api/config")]);
  const cat = Object.fromEntries(v.catalog.map((x) => [x.candidate, x]));
  const voices = JSON.parse(JSON.stringify(v.voices));
  let active = v.active_voice;
  const draw = () => {
    document.getElementById("voiceRows").innerHTML = voices.map((x, i) => {
      const k = cat[x.candidate] || { status: "unknown", status_label: "候補にない", voices: [] };
      const fixed = k.voice_applies === false;   // the vendor's agent settings decide the voice
      return `<tr><td data-label="使う"><input type="radio" name="active" value="${esc(x.id)}" ${active === x.id ? "checked" : ""} ${fixed ? "disabled" : ""} aria-label="使う声"></td>
        <td data-label="表示名"><input data-i="${i}" data-k="label" value="${esc(x.label)}"></td>
        <td data-label="接続候補"><select data-i="${i}" data-k="candidate">${v.catalog.map((o) => `<option value="${o.candidate}" ${o.candidate === x.candidate ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select></td>
        <td data-label="声"><input data-i="${i}" data-k="voice" value="${esc(x.voice)}" list="vl-${i}"><datalist id="vl-${i}">${k.voices.map((n) => `<option value="${esc(n)}">`).join("")}</datalist></td>
        <td data-label="状態">${statusBadge(k.status, k.status_label)}${k.hold ? ' <span class="badge">保留</span>' : ""}${fixed ? '<br><span class="sub">記録だけ（反映されない：業者のエージェント設定で固定）</span>' : ""}</td>
        <td data-label=""><button class="small danger" data-del="${i}">削除</button></td></tr>`;
    }).join("");
  };
  view.innerHTML = `<section class="card"><h1>音声設定</h1>
    <p class="sub">複数の声を登録し、使う声を1つ選びます。<b>次の会話から</b>使われます。実際の声は、どれも業者に接続して確かめていません（「接続未確認」）。</p>
    <table class="stack"><thead><tr><th>使う</th><th>表示名</th><th>接続候補</th><th>声（名前・ID）</th><th>状態</th><th></th></tr></thead><tbody id="voiceRows"></tbody></table>
    <div class="row" style="margin-top:8px"><button class="small" id="addVoice">＋声を追加</button><button class="primary" id="saveVoices">保存</button></div></section>
    <section class="card" style="margin-top:12px"><h2>接続候補と声の状態</h2><table class="stack"><thead><tr><th>候補</th><th>状態</th><th>声の候補</th><th>声の指定の方法</th><th>この画面の声の反映</th></tr></thead><tbody>
    ${v.catalog.map((x) => `<tr><td data-label="候補">${esc(x.name)}${x.hold ? ' <span class="badge">保留</span>' : ""}</td><td data-label="状態">${statusBadge(x.status, x.status_label)}</td><td data-label="声の候補">${esc(x.voices.join("、") || "（業者の画面で選ぶ）")}</td><td data-label="指定の方法">${esc(x.how)}</td><td data-label="反映">${esc(x.apply_label)}</td></tr>`).join("")}
    </tbody></table><p class="sub">「利用可能」はオフラインの模擬だけです（音声の評価には使えません）。鍵の値は表示しません（設定の有無だけ）。</p></section>`;
  draw();
  view.addEventListener("input", (e) => { const t = e.target; if (t.dataset.i !== undefined && t.dataset.k) voices[Number(t.dataset.i)][t.dataset.k] = t.value; });
  view.addEventListener("change", (e) => {
    const t = e.target;
    if (t.name === "active") active = t.value;
    if (t.dataset.k === "candidate") { voices[Number(t.dataset.i)].candidate = t.value; draw(); }
  });
  view.addEventListener("click", (e) => { const t = e.target; if (t.dataset.del !== undefined) { voices.splice(Number(t.dataset.del), 1); draw(); } });
  document.getElementById("addVoice").addEventListener("click", () => {
    const n = voices.length + 1; let id = `v${n}`; while (voices.some((x) => x.id === id)) id += "x";
    voices.push({ id, candidate: "gpt-live-1", voice: "", label: "" }); draw();
  });
  document.getElementById("saveVoices").addEventListener("click", () => guarded(async () => {
    const r = await api("/api/config", { config: { ...c.config, voices, active_voice: active }, note: "声の変更" });
    toast(`v${r.version} を保存しました。次の会話から使われます`); render();
  }));
};

// --- calls ------------------------------------------------------------------------------------------------
function notifySummary(n) {
  const parts = Object.entries(n || {}).map(([k, v]) => `${({ accepted: "受付済み", pending: "送信待ち", retrying: "再試行待ち", failed_stopped: "失敗", unknown: "結果不明", channel_stopped: "停止中", superseded: "統合", blocked_unapproved: "未送信", discarded: "破棄", resent: "再送済み" })[k] || k} ${v}`);
  return parts.join("・") || "—";
}
function callsTable(calls) {
  if (!calls.length) return `<p class="muted">まだ受付はありません。<a href="#/demo">着信テスト</a>で作れます。</p>`;
  return `<table class="stack"><thead><tr><th>日時</th><th>種類</th><th>経路</th><th>店舗・お名前</th><th>折り返し先</th><th>用件</th><th>通知</th><th>対応</th></tr></thead><tbody>
  ${calls.map((c) => `<tr><td data-label="日時"><a href="#/call/${encodeURIComponent(c.id)}">${esc(fmt(c.started_at))}</a></td>
    <td data-label="種類">${esc(c.source_label)}${c.emergency ? ` <span class="badge bad">${c.emergency === "obvious" ? "緊急" : "要注意"}</span>` : ""}</td>
    <td data-label="経路">${routeBadge(c.route, c.route_label)}</td>
    <td data-label="店舗・お名前">${esc(c.shop.display)} ${esc(c.name.display)}</td>
    <td data-label="折り返し先">${esc(c.number.display)} ${c.number.display ? `<span class="sub">（${esc(c.number.status_label)}）</span>` : ""}</td>
    <td data-label="用件">${esc(c.request.display)}</td><td data-label="通知">${esc(notifySummary(c.notifications))}</td>
    <td data-label="対応">${esc(c.handled_status)}</td></tr>`).join("")}</tbody></table>`;
}
routes.calls = async () => {
  const { calls } = await api("/api/calls");
  view.innerHTML = `<section class="card"><h1>受付履歴</h1><p class="sub">架空の着信テストと、ブラウザー会話試験の記録です（会社回線の着信はありません）。</p>${callsTable(calls)}</section>`;
};

function eventText(e) {
  const d = e.data || {};
  switch (e.kind) {
    case "caller": return `お客様：${d.text}`;
    case "ai": return `AI：${d.text}`;
    case "tool": return `業務処理 ${d.name}(${JSON.stringify(d.args)}) → ${JSON.stringify(d.result)}`;
    case "route": return `経路：${d.label}（${d.reason}）`;
    case "play": return `録音済みの案内 ${d.clip}：${d.text || ""}`;
    case "gather_dtmf": return "プッシュボタンの入力を待つ";
    case "say_ai": return `AIが話す（意図：${d.intent}）`;
    case "ai_send_stopped": return d.text || "AIへの音声送信を停止";
    case "transcript_deleted": return `拒否より前の文字起こしを削除（${d.count}件）`;
    case "ai_send_blocked": return `AIへ送らなかった（${d.reason}）`;
    case "ai_blocked": return `AIの業務処理を受け付けなかった（${d.tool}：${d.reason}）`;
    case "consent": return `同意の変更：${({ ai_refused: "AIの拒否", recording_refused: "録音の拒否", human_request: "人との会話の希望", human_request_answer: "短い受付への回答", ai_failure: "AIの障害" })[d.kind] || d.kind}（${({ speech: "発話から判定", operator: "操作", tester: "試験者" })[d.source] || d.source || ""}）`;
    case "recording_stopped": return "録音を停止（以降は録音しない）";
    case "recording_deleted": return d.why || "拒否より前の録音を削除";
    case "vendor_interrupted": return `業者の割り込みの通知（${d.source}）：${(d.invalidated || []).length ? `返事の前に遮られた復唱 ${d.invalidated.join("・")} を確認済みにしない` : "無効にした復唱はない"}`;
    default: return `${e.kind} ${d && Object.keys(d).length ? JSON.stringify(d) : ""}`;
  }
}
routes.call = async (id) => {
  const c = await api(`/api/calls/${encodeURIComponent(id)}`);
  const ended = !!c.ended_at;
  const canCorrect = ended && c.route !== "normal";
  view.innerHTML = `<section class="card"><h1>受付 ${esc(c.id)}</h1>
    <dl class="kv"><dt>種類</dt><dd>${esc(c.source_label)}（デモ・架空のデータ）</dd><dt>開始・終了</dt><dd>${esc(fmt(c.started_at))} 〜 ${esc(fmt(c.ended_at) || "通話中")} ${esc(c.end_reason || "")}</dd>
    <dt>着信先</dt><dd>${esc(c.dialed)}</dd><dt>回線の番号</dt><dd>${esc(c.caller_id || "非通知・不明")}</dd>
    <dt>経路</dt><dd>${routeBadge(c.route, c.route_label)} ${esc(c.route_reason)}</dd>
    <dt>この通話の設定</dt><dd>v${c.config_version}（${esc(c.snapshot_mode)}）・声 ${esc(c.voice.label || c.voice.voice)}・有効だったFAQ ${esc(c.faq_codes.join(", "))}</dd>
    <dt>同意</dt><dd>録音 ${c.consent.recording === "allowed" ? "許可" : "拒否"}・AIでの処理 ${c.consent.ai_processing === "allowed" ? "許可" : "拒否"}</dd>
    ${c.emergency ? `<dt>緊急</dt><dd><span class="badge bad">${esc(c.emergency)}</span></dd>` : ""}
    ${c.open_questions.length ? `<dt>確認事項</dt><dd>${c.open_questions.map(esc).join("／")}</dd>` : ""}
    <dt>対応状態</dt><dd><select id="handled" class="inline">${["未対応", "対応中", "対応済み", "対応不要"].map((s) => `<option ${c.handled_status === s ? "selected" : ""}>${s}</option>`).join("")}</select></dd></dl></section>
  ${c.route === "normal" ? `<section class="card" style="margin-top:12px"><h2>普通受電へ渡した通話</h2><p>AIの会話は始めていません。受付項目・要約・通知はありません（デモでは電話機は鳴らしません）。</p></section>` : `
  <section class="card" style="margin-top:12px"><h2>受付項目</h2>
    <p class="sub">「本人確認済み」は、復唱の後にお客様が明確に肯定した値だけです。担当者の補正は「担当者が補正」と表示し、本人確認済みにはしません。${canCorrect ? "" : "補正は通話の終了後にできます。"}</p>
    <table class="stack"><thead><tr><th>項目</th><th>値</th><th>状態</th><th>取得元</th><th></th></tr></thead><tbody>
    ${c.fields.map((f) => `<tr data-field="${f.name}"><td data-label="項目">${esc(f.label)}${f.storable ? "" : ` <span class="badge">保存しない設定</span>`}</td>
      <td data-label="値">${esc(f.display) || '<span class="muted">未取得</span>'}</td><td data-label="状態">${statusBadge(f.status, f.status_label)}</td>
      <td data-label="取得元">${esc({ speech: "発話", dtmf: "プッシュボタン", line: "回線の番号", staff: "担当者" }[f.source] || "")}</td>
      <td data-label="">${canCorrect && f.storable ? `<button class="small" data-fix="${f.name}">補正</button>` : ""}</td></tr>
      <tr class="hidden" data-fixrow="${f.name}"><td colspan="5"><div class="row"><input aria-label="補正後の値" data-v placeholder="補正後の値" value="${esc(f.value || "")}"><input aria-label="補正の理由" data-r placeholder="理由（必須）"><button class="primary small" data-fixsave="${f.name}">補正を保存</button></div></td></tr>`).join("")}
    </tbody></table>
    ${c.corrections.length ? `<h3>補正の記録（前と後）</h3><table class="stack"><thead><tr><th>日時</th><th>項目</th><th>前</th><th>後</th><th>理由</th><th>補正した人</th></tr></thead><tbody>
      ${c.corrections.map((x) => `<tr><td data-label="日時">${esc(fmt(x.at))}</td><td data-label="項目">${esc(x.field)}</td><td data-label="前">${esc(x.before_value || "（なし）")}（${esc(x.before_status)}）</td><td data-label="後">${esc(x.after_value)}（担当者が補正）</td><td data-label="理由">${esc(x.reason)}</td><td data-label="補正した人">${esc(x.by_user)}</td></tr>`).join("")}</tbody></table>` : ""}
  </section>
  <section class="card" style="margin-top:12px"><h2>要約（固定の規則で作成）</h2>
    ${c.summaries.length ? c.summaries.map((s) => `<h3>版${s.version}（${esc(s.reason)}・${esc(fmt(s.created_at))}）</h3><pre class="body">${esc(s.text)}</pre>`).join("") : `<p class="muted">通話の終了後に作ります。</p>`}
    <p class="sub">会話モデルの要約ではありません。本番の要約の品質を示すものではありません。</p></section>
  <section class="card" style="margin-top:12px"><h2>通知</h2>
    <div class="row"><button id="process">送信シミュレーションを実行（再試行待ちも今すぐ）</button><button id="renotify">通知イベントを再投入（重複しないことの確認）</button><button id="previewBtn">プレビュー</button></div>
    <div id="preview"></div>
    ${notificationsTable(c.notifications)}</section>`}
  <section class="card" style="margin-top:12px"><details><summary>出来事の記録（${c.events.length}件）</summary>
    <table class="stack"><tbody>${c.events.map((e) => `<tr><td data-label="時刻">${esc(fmt(e.at).slice(11))}</td><td data-label="内容">${esc(eventText(e))}</td></tr>`).join("")}</tbody></table></details></section>`;
  document.getElementById("handled").addEventListener("change", (e) => guarded(async () => { await api(`/api/calls/${encodeURIComponent(id)}/status`, { status: e.target.value }); toast("対応状態を保存しました"); }));
  if (c.route === "normal") return;
  view.addEventListener("click", (e) => {
    const t = e.target;
    if (t.dataset.fix) view.querySelector(`[data-fixrow="${t.dataset.fix}"]`).classList.toggle("hidden");
    if (t.dataset.fixsave) guarded(async () => {
      const row = view.querySelector(`[data-fixrow="${t.dataset.fixsave}"]`);
      await api(`/api/calls/${encodeURIComponent(id)}/correct`, { field: t.dataset.fixsave, value: row.querySelector("[data-v]").value, reason: row.querySelector("[data-r]").value });
      toast("補正しました。要約と通知（訂正の版）を作り直しました"); render();
    });
    if (t.dataset.resend || t.dataset.discard) guarded(async () => {
      const nid = t.dataset.resend || t.dataset.discard;
      await api(`/api/notifications/${nid}/${t.dataset.resend ? "resend" : "discard"}`, {}); toast("記録しました"); render();
    });
  });
  document.getElementById("process").addEventListener("click", () => guarded(async () => { const r = await api("/api/outbox/process", { force_due: true }); toast(`送信 ${r.sent}・受付済み ${r.accepted}・再試行待ち ${r.retrying}・停止 ${r.stopped}・未送信 ${r.not_sent}`); render(); }));
  document.getElementById("renotify").addEventListener("click", () => guarded(async () => { const r = await api(`/api/calls/${encodeURIComponent(id)}/renotify`, {}); toast(`再投入：新しく作った通知 ${r.created}件（既存 ${r.ids.length - r.created}件はそのまま）`); render(); }));
  document.getElementById("previewBtn").addEventListener("click", () => guarded(async () => {
    const p = await api(`/api/calls/${encodeURIComponent(id)}/preview`);
    document.getElementById("preview").innerHTML = `<p class="sub">${esc(p.note)}</p>` + p.previews.map((x) => `<h3>${esc(x.target)} <span class="badge">${esc(x.channel_label)}</span></h3><pre class="body">${esc(x.body)}</pre>`).join("");
  }));
};
function notificationsTable(rows) {
  if (!rows.length) return `<p class="muted">通知はまだありません。</p>`;
  return `<table class="stack"><thead><tr><th>宛先</th><th>版</th><th>状態</th><th>試行</th><th>再送キー</th><th>最後の結果</th><th></th></tr></thead><tbody>
  ${rows.map((n) => `<tr><td data-label="宛先">${esc(n.target_name)}</td><td data-label="版">${n.version === 0 ? "即時（緊急）" : `版${n.version}`}</td>
    <td data-label="状態">${statusBadge(n.status, n.status_label)}</td><td data-label="試行">${n.attempts}回</td>
    <td data-label="再送キー"><code>${esc(n.retry_key.slice(0, 8))}…</code></td><td data-label="最後の結果">${esc(n.last_error || OUTCOME_LABELS[(n.attempts_log.at(-1) || {}).outcome] || "")}</td>
    <td data-label="">${["failed_stopped", "unknown", "channel_stopped"].includes(n.status) ? `<button class="small" data-resend="${n.id}">再送</button>` : ""}${["failed_stopped", "unknown", "channel_stopped", "blocked_unapproved"].includes(n.status) ? `<button class="small danger" data-discard="${n.id}">破棄</button>` : ""}</td></tr>
    <tr><td colspan="7"><details><summary>本文と試行の記録</summary><pre class="body">${esc(n.body)}</pre>
      ${n.attempts_log.map((a) => `<div class="sub">${esc(fmt(a.at))} ${esc(OUTCOME_LABELS[a.outcome] || a.outcome)} ${a.http_status ?? ""} キー ${esc(a.retry_key.slice(0, 8))}… ${esc(a.detail || "")}</div>`).join("")}
      ${n.line_request_preview ? `<p class="sub">LINEの要求の組み立て（送信しない）：</p><pre class="body">${esc(JSON.stringify(n.line_request_preview, null, 1))}</pre>` : ""}</details></td></tr>`).join("")}</tbody></table>`;
}

// --- notification settings --------------------------------------------------------------------------------
routes.notify = async () => {
  const [t, n, calls] = await Promise.all([api("/api/targets"), api("/api/notifications"), api("/api/calls")]);
  const opts = (obj, sel) => Object.entries(obj).map(([k, l]) => `<option value="${k}" ${k === sel ? "selected" : ""}>${esc(l)}</option>`).join("");
  const withSummary = calls.calls.filter((c) => c.summary_version);
  view.innerHTML = `<section class="card"><h1>通知設定</h1>
    <p class="sub">通知はすべて<b>シミュレーション</b>で、外部へは送りません。LINEは、送信先の登録・接続設定・送信の承認が済むまで送りません（公式のMessaging APIの候補構成。個人LINEの非公式な操作は使いません）。同じ受付の同じ版は、宛先ごとに1件しか作りません。</p>
    <table class="stack"><thead><tr><th>有効</th><th>名前</th><th>経路</th><th>シミュレーションの結果</th><th></th></tr></thead><tbody>
    ${t.targets.map((x) => `<tr data-tid="${x.id}"><td data-label="有効"><input type="checkbox" data-k="enabled" ${x.enabled ? "checked" : ""} aria-label="有効"></td>
      <td data-label="名前"><input data-k="name" value="${esc(x.name)}"></td><td data-label="経路"><select data-k="channel">${opts(t.channels, x.channel)}</select></td>
      <td data-label="結果"><select data-k="simulate">${opts(t.simulate, x.simulate)}</select></td>
      <td data-label=""><button class="small primary" data-savet="${x.id}">保存</button></td></tr>`).join("")}
    <tr data-tid="new"><td data-label="有効"><input type="checkbox" data-k="enabled" checked aria-label="有効"></td><td data-label="名前"><input data-k="name" placeholder="新しい宛先（架空）"></td>
      <td data-label="経路"><select data-k="channel">${opts(t.channels, "simulation")}</select></td><td data-label="結果"><select data-k="simulate">${opts(t.simulate, "success")}</select></td>
      <td data-label=""><button class="small" data-savet="new">追加</button></td></tr></tbody></table>
    ${t.problems.stopped_channels.map((c) => `<p class="error">経路「${esc(c.channel)}」が停止中：${esc(c.reason)} <button class="small" data-resume="${esc(c.channel)}">人が確認して再開</button></p>`).join("")}
    <div class="row"><button id="processAll">送信シミュレーションを実行（再試行待ちも今すぐ）</button></div></section>
  <section class="card" style="margin-top:12px"><h2>送信内容のプレビュー</h2>
    <label>受付を選ぶ <select id="pvCall">${withSummary.map((c) => `<option value="${esc(c.id)}">${esc(fmt(c.started_at))} ${esc(c.shop.display)} ${esc(c.request.display)}</option>`).join("") || "<option value=''>（要約のある受付がありません）</option>"}</select></label>
    <button id="pvBtn">プレビュー</button><div id="pvOut"></div></section>
  <section class="card" style="margin-top:12px"><h2>最近の通知</h2><table class="stack"><thead><tr><th>受付</th><th>宛先</th><th>版</th><th>状態</th><th>試行</th><th>最後の結果</th></tr></thead><tbody>
    ${n.notifications.map((x) => `<tr><td data-label="受付"><a href="#/call/${encodeURIComponent(x.call_id)}">${esc(x.call_id)}</a></td><td data-label="宛先">${esc(x.target_name)}</td><td data-label="版">${x.version}</td><td data-label="状態">${statusBadge(x.status, x.status_label)}</td><td data-label="試行">${x.attempts}</td><td data-label="最後の結果">${esc(x.last_error || "")}</td></tr>`).join("")}</tbody></table></section>`;
  view.addEventListener("click", (e) => {
    const b = e.target;
    if (b.dataset.savet) guarded(async () => {
      const row = view.querySelector(`[data-tid="${b.dataset.savet}"]`);
      const v = (k) => row.querySelector(`[data-k=${k}]`);
      const body = { name: v("name").value, channel: v("channel").value, simulate: v("simulate").value, enabled: v("enabled").checked };
      await api(b.dataset.savet === "new" ? "/api/targets" : `/api/targets/${b.dataset.savet}`, body);
      toast("保存しました。次に作る通知から使われます"); render();
    });
    if (b.dataset.resume) guarded(async () => { await api(`/api/channels/${b.dataset.resume}/resume`, {}); toast("再開しました"); render(); });
  });
  document.getElementById("processAll").addEventListener("click", () => guarded(async () => { const r = await api("/api/outbox/process", { force_due: true }); toast(`送信 ${r.sent}・受付済み ${r.accepted}・再試行待ち ${r.retrying}・停止 ${r.stopped}・未送信 ${r.not_sent}`); render(); }));
  document.getElementById("pvBtn").addEventListener("click", () => guarded(async () => {
    const id = document.getElementById("pvCall").value; if (!id) return;
    const p = await api(`/api/calls/${encodeURIComponent(id)}/preview`);
    document.getElementById("pvOut").innerHTML = `<p class="sub">${esc(p.note)}（要約 版${p.summary_version}）</p>` + p.previews.map((x) => `<h3>${esc(x.target)} <span class="badge">${esc(x.channel_label)}</span> <span class="badge">${esc(x.simulate_label)}</span></h3><pre class="body">${esc(x.body)}</pre>`).join("");
  }));
};

// --- demo call --------------------------------------------------------------------------------------------
const QUICK = ["点検は無料ですか？", "駐車場はありますか？", "人と話したいです", "はい、お願いします", "AIとは話したくないです",
  "人と話したいので、AIは使わないでください", "AIは嫌ではありません", "録音はしないでください", "焦げ臭いです", "火が出ています"];
const demo = { call: null };
routes.demo = async () => {
  const now = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 16);
  view.innerHTML = `<div class="demo-banner">着信テスト（デモ）：会社回線には未接続です。AIの代わりに、<b>固定の規則で動く試験用の応答</b>を使います。業務処理（経路の判定・FAQ・確認・保存・要約・通知）は本番と同じ処理です。実際の会話モデルの性能は示しません。</div>
  <div class="grid" style="margin-top:12px">
    <section class="card"><h2>1. 架空の着信を入れる</h2>
      <label>着信先 <select id="dialed"><option>0120-77-3408</option><option>092-504-2185</option><option>不明</option></select></label>
      <label>回線の番号（架空）<input id="callerId" value="090-1111-2222"></label>
      <label class="inline"><input type="checkbox" id="withheld"> 非通知</label>
      <label>着信の日時（日本時間）<input type="datetime-local" id="at" value="${now}"></label>
      <label class="inline"><input type="checkbox" id="aiDown"> AIが使えない状態（障害時の動作を確認）</label>
      <button class="primary" id="startCall">着信を入れる</button></section>
    <section class="card"><h2>2. 経路の判定</h2><div id="decision"><p class="muted">着信を入れると、保存済みの設定とこの日時で判定します。</p></div></section>
  </div>
  <div class="grid hidden" id="callArea" style="margin-top:12px">
    <section class="card"><h2>3. 会話（お客様役として入力）</h2>
      <div class="chat" id="chat"></div>
      <div class="row" style="margin-top:8px"><input id="say" placeholder="お客様の発話を入力" aria-label="お客様の発話"><button class="primary" id="sayBtn">送る</button></div>
      <div class="quick" id="quick">${QUICK.map((q) => `<button data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
      <div class="row"><button id="runScript">例の会話を流す（番号・日時の訂正あり）</button><button id="interruptBtn" title="AIの発話（復唱など）がお客様に遮られた、と業者が知らせた想定。端末の音量ではなく、業者の通知だけを根拠にする">業者の割り込みの通知（模擬）</button><button class="danger" id="endCall">通話を終える</button></div>
      <div id="dtmfArea" class="hidden"><h3>プッシュボタン（AIなしの経路）</h3><div class="row"><input id="digits" inputmode="numeric" placeholder="番号（#まで）"><button id="dtmfSend">#で確定</button></div>
        <div class="row" style="margin-top:6px"><button data-key="1">1（はい）</button><button data-key="2">2（入力し直す）</button><button id="dtmfTimeout">入力なし</button></div></div>
    </section>
    <section class="card"><h2>4. 受付項目と同意の状態</h2><div id="fields"></div></section>
  </div>
  <section class="card hidden" id="resultArea" style="margin-top:12px"><h2>5. 要約と通知</h2><div id="result"></div></section>`;
  document.getElementById("startCall").addEventListener("click", () => guarded(startDemo));
  document.getElementById("sayBtn").addEventListener("click", () => guarded(() => sendSay(document.getElementById("say").value)));
  document.getElementById("say").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) guarded(() => sendSay(e.target.value)); });
  document.getElementById("quick").addEventListener("click", (e) => { if (e.target.dataset.q) guarded(() => sendSay(e.target.dataset.q)); });
  document.getElementById("runScript").addEventListener("click", () => guarded(async () => {
    const { script } = await api("/api/demo/script");
    for (const line of script) { await sendSay(line); await new Promise((r) => setTimeout(r, 250)); }
  }));
  document.getElementById("endCall").addEventListener("click", () => guarded(endDemo));
  document.getElementById("interruptBtn").addEventListener("click", () => guarded(async () => {
    if (!demo.call) return;
    const r = await api("/api/demo/interrupted", { call_id: demo.call.id });
    chat("sys", r.invalidated.length ? `業者の割り込みの通知（模擬）：返事の前に遮られた復唱（${r.invalidated.join("・")}）は、「はい」と言われても確認済みにしません。AIが復唱し直します`
      : "業者の割り込みの通知（模擬）：返事を待っている復唱はないため、無効にしたものはありません");
    drawCall(r.call);
  }));
  document.getElementById("dtmfSend").addEventListener("click", () => guarded(() => sendDtmf(document.getElementById("digits").value)));
  document.getElementById("dtmfTimeout").addEventListener("click", () => guarded(() => sendDtmf(null)));
  document.getElementById("dtmfArea").addEventListener("click", (e) => { if (e.target.dataset.key) guarded(() => sendDtmf(e.target.dataset.key)); });
};
function chat(cls, text) {
  const el = document.getElementById("chat");
  const div = document.createElement("div");
  div.className = `msg ${cls}`; div.textContent = text;
  el.appendChild(div); el.scrollTop = el.scrollHeight;
}
function showActions(actions) {
  for (const a of actions || []) chat("sys", `${a.label}${a.args.length ? "：" + a.args.join(" ") : ""}`);
}
function drawCall(c) {
  demo.call = c;
  const d = document.getElementById("decision");
  d.innerHTML = `<div class="big" id="demoRoute">${routeBadge(c.route, c.route_label)}</div><p>${esc(c.route_reason)}</p>
    <p class="sub">着信 ${esc(fmt(c.started_at))}・設定 v${c.config_version}（${esc(c.snapshot_mode)}）・声 ${esc(c.voice.label || c.voice.voice)}・FAQ ${esc(c.faq_codes.join(", "))}</p>
    <p class="sub">この通話は、開始時の設定のまま進みます。管理画面で設定を変えても、次の着信から反映されます。</p>
    ${c.route === "normal" ? `<p><b>普通受電へ渡す判定です。AIの会話は始めません</b>（デモでは会社の電話機は鳴らしません）。発話を送っても、AIへは送りません。</p>` : ""}
    <p><a href="#/call/${encodeURIComponent(c.id)}">受付の詳細を開く</a></p>`;
  document.getElementById("fields").innerHTML = `<table class="stack" id="fieldTable"><thead><tr><th>項目</th><th>値</th><th>状態</th></tr></thead><tbody>
    ${c.fields.map((f) => `<tr data-name="${f.name}"><td data-label="項目">${esc(f.label)}</td><td data-label="値">${esc(f.display) || '<span class="muted">—</span>'}</td><td data-label="状態">${statusBadge(f.status, f.status_label)}</td></tr>`).join("")}</tbody></table>
    <dl class="kv" style="margin-top:8px"><dt>状態</dt><dd id="callState">${esc(STATE_LABELS[c.state] || c.state)}</dd><dt>録音</dt><dd>${c.consent.recording === "allowed" ? "許可" : "拒否（停止済み）"}</dd>
    <dt>AIでの処理</dt><dd id="aiConsent">${c.consent.ai_processing === "allowed" ? "許可" : "拒否"}</dd><dt>AIへの送信</dt><dd id="aiSend">${c.ai_allowed ? "送信中（許可）" : "送らない"}</dd>
    ${c.emergency ? `<dt>緊急</dt><dd><span class="badge bad">${esc(c.emergency)}</span></dd>` : ""}${c.open_questions.length ? `<dt>確認事項</dt><dd>${c.open_questions.map(esc).join("／")}</dd>` : ""}</dl>`;
  document.getElementById("dtmfArea").classList.toggle("hidden", !(c.state || "").startsWith("dtmf"));
  if (c.ended_at && c.route !== "normal") showResult(c);
}
async function startDemo() {
  const r = await api("/api/demo/start", { dialed: document.getElementById("dialed").value, caller_id: document.getElementById("callerId").value,
    withheld: document.getElementById("withheld").checked, at: document.getElementById("at").value, ai_available: !document.getElementById("aiDown").checked });
  document.getElementById("callArea").classList.remove("hidden");
  document.getElementById("resultArea").classList.add("hidden");
  document.getElementById("chat").innerHTML = "";
  for (const e of r.call.events) if (e.kind === "play") chat("sys", `録音済みの案内 ${e.data.clip}：${e.data.text}`);
  for (const line of r.say) chat("ai", line);
  if (r.call.route === "dtmf") chat("sys", "AIが使えないため、録音の案内とプッシュボタンで受け付けます");
  drawCall(r.call);
}
async function sendSay(text) {
  if (!demo.call || !text) return;
  document.getElementById("say").value = "";
  chat("caller", text);
  const r = await api("/api/demo/say", { call_id: demo.call.id, text });
  if (!r.result.forwarded) chat("blocked", `AIへは送っていません（${({ ai_not_used_for_this_call: "普通受電の通話", ai_refused: "AIでの処理を拒否済み", call_ended: "通話は終了", ai_failure: "AIの障害" })[r.result.reason] || r.result.reason}）。内容も保存していません。`);
  for (const line of r.result.say || []) chat("ai", line);
  const prev = demo.call;
  const newEvents = r.call.events.filter((e) => e.id > Math.max(0, ...prev.events.map((x) => x.id)));
  for (const e of newEvents) {
    if (e.kind === "tool" && e.data.name === "confirm_field" && !e.data.result.ok) chat("sys", `サーバーが確認を拒否：${e.data.result.reason}`);
    if (["consent", "ai_send_stopped", "transcript_deleted", "recording_stopped", "recording_deleted", "play", "gather_dtmf"].includes(e.kind)) chat("sys", eventText(e));
  }
  drawCall(r.call);
}
async function sendDtmf(digits) {
  const r = await api("/api/demo/dtmf", { call_id: demo.call.id, digits });
  chat("caller", `（プッシュボタン：${digits ?? "入力なし"}）`);
  showActions(r.actions);
  drawCall(r.call);
}
async function endDemo() {
  if (!demo.call) return;
  const r = await api("/api/demo/end", { call_id: demo.call.id });
  drawCall(r.call);
  showResult(r.call);
}
async function showResult(c) {
  const area = document.getElementById("resultArea");
  area.classList.remove("hidden");
  const s = c.summaries.at(-1);
  const p = await api(`/api/calls/${encodeURIComponent(c.id)}/preview`);
  document.getElementById("result").innerHTML = `<h3>要約（版${s ? s.version : "—"}・固定の規則）</h3><pre class="body" id="summaryText">${esc(s ? s.text : "")}</pre>
    <h3>宛先ごとの通知の結果（シミュレーション）</h3>${notificationsTable(c.notifications)}
    <h3>宛先ごとのプレビュー</h3>${p.previews.map((x) => `<p class="sub">${esc(x.target)}（${esc(x.channel_label)}）</p><pre class="body">${esc(x.body)}</pre>`).join("")}
    <p><a href="#/call/${encodeURIComponent(c.id)}">受付の詳細（補正・再送・再投入）を開く</a></p>`;
}

// --- ledger -----------------------------------------------------------------------------------------------
const usd = (v) => `$${Number(v || 0).toFixed(4)}`;
routes.ledger = async () => {
  const l = await api("/api/ledger");
  const rows = Object.entries(l.ledgers).filter(([, x]) => x.cap_usd > 0 || x.requests || x.open_reservations);
  view.innerHTML = `<section class="card"><h1>費用の台帳（ブラウザー会話試験）</h1>
    <p class="sub">${esc(l.note)} 段階：${esc(l.stage)}。</p>
    <table class="stack"><thead><tr><th>台帳</th><th>会話の数</th><th>合計（推定・照合済み）</th><th>うち照合待ちの留保</th><th>照合待ち</th><th>上限（この段階）</th></tr></thead><tbody>
    ${rows.map(([v, x]) => `<tr><td data-label="台帳">${esc(v)}</td><td data-label="会話の数">${x.requests}</td><td data-label="合計">${usd(x.est_cost_usd)}</td><td data-label="留保">${usd(x.held_usd)}</td><td data-label="照合待ち">${x.open_reservations}件</td><td data-label="上限">$${x.cap_usd}</td></tr>`).join("")}
    </tbody></table></section>
    <section class="card" style="margin-top:12px"><h2>照合待ちの留保</h2>
    <p class="sub">業者の利用画面で、その会話の実際の額を確かめてから入力します。照合すると、台帳の合計がその額になり、留保が閉じます。同じ留保を別の額で照合し直すことはできません。</p>
    ${rows.some(([, x]) => x.open.length) ? `<table class="stack"><thead><tr><th>台帳</th><th>留保</th><th>会話</th><th>留保した額</th><th>利用画面の額（USD）</th><th>メモ</th><th></th></tr></thead><tbody>
    ${rows.flatMap(([v, x]) => x.open.map((r) => `<tr><td data-label="台帳">${esc(v)}</td><td data-label="留保"><code>${esc(r.rid)}</code><br><span class="sub">${esc(fmt(r.ts))}</span></td><td data-label="会話">${esc(r.operation)} ${esc(r.run_id)}</td><td data-label="留保した額">${usd(r.est_cost_usd)}</td>
      <td data-label="利用画面の額"><input inputmode="decimal" data-amount="${esc(r.rid)}" aria-label="利用画面の額"></td><td data-label="メモ"><input data-note="${esc(r.rid)}" placeholder="例：10/09 利用画面" aria-label="メモ"></td>
      <td data-label=""><button class="small primary" data-reconcile="${esc(r.rid)}" data-vendor="${esc(v)}">照合する</button></td></tr>`)).join("")}</tbody></table>` : `<p class="muted">照合待ちの留保はありません。</p>`}</section>
    <section class="card" style="margin-top:12px"><h2>試験の会話（サーバーの記録）</h2>
    <p class="sub">この段階の開始の回数：${Object.entries(l.counts || {}).map(([c, n]) => `${esc(c)} ${n.sessions}/${n.limit}回（失敗 ${n.mint_failures}/${n.mint_failure_limit}回${n.mint_failures ? ` <button class="small" data-resetfail="${esc(c)}">失敗の回数を解除</button>` : ""}）`).join("、")}</p>
    ${l.sessions.length ? `<table class="stack"><thead><tr><th>開始</th><th>候補</th><th>段階</th><th>状態</th><th>止めた理由</th><th>業者の終了</th><th>業務処理・裏方の応答</th></tr></thead><tbody>
    ${l.sessions.map((x) => `<tr><td data-label="開始">${esc(fmt(x.started_at))}</td><td data-label="候補">${esc(x.candidate)}</td><td data-label="段階">${esc(x.stage)}</td><td data-label="状態">${esc(x.status_label || x.status)}<br><span class="sub">${esc(x.end_reason || "")}</span>${x.releasable ? `<div class="row" style="margin-top:4px"><input data-relnote="${esc(x.id)}" placeholder="例：10/08 OpenAIの利用画面で0件・$0を確認" aria-label="確認した内容"><button class="small" data-release="${esc(x.id)}">回数から外す</button></div>` : ""}</td><td data-label="止めた理由">${esc(x.stop_reason || "—")}</td><td data-label="業者の終了">${x.close_confirmed ? "確認" : "未確認"}</td><td data-label="回数">${x.tool_calls}・${x.backend_responses}</td></tr>`).join("")}</tbody></table>
    <p class="sub">「回数から外す」は、開始に失敗し、業者の会話が作られなかった記録だけに出ます。業者の利用画面で利用が0件であることを確かめてから、確かめた内容を入力して押してください（操作履歴に残ります）。</p>` : `<p class="muted">まだ試験の会話はありません。</p>`}</section>`;
  view.addEventListener("click", (e) => {
    const t = e.target;
    if (t.dataset.release) guarded(async () => {
      await api("/api/lab/release", { session_id: t.dataset.release, note: view.querySelector(`[data-relnote="${t.dataset.release}"]`).value });
      toast("回数から外しました（留保も0円で閉じました）"); render();
    });
    if (t.dataset.resetfail) guarded(async () => {
      const r = await api("/api/lab/reset_failures", { candidate: t.dataset.resetfail });
      toast(`失敗の回数を解除しました（${r.cleared}件）`); render();
    });
    if (!t.dataset.reconcile) return;
    guarded(async () => {
      const rid = t.dataset.reconcile;
      const r = await api("/api/ledger/reconcile", { vendor: t.dataset.vendor, rid,
        actual_usd: view.querySelector(`[data-amount="${rid}"]`).value, note: view.querySelector(`[data-note="${rid}"]`).value });
      toast(r.result === "adjusted" ? "照合しました（留保を閉じました）" : "同じ額で照合済みでした"); render();
    });
  });
};

// --- audit ------------------------------------------------------------------------------------------------
routes.audit = async () => {
  const { audit } = await api("/api/audit");
  view.innerHTML = `<section class="card"><h1>操作履歴</h1><table class="stack"><thead><tr><th>日時</th><th>人</th><th>操作</th><th>対象</th><th>内容</th></tr></thead><tbody>
  ${audit.map((a) => `<tr><td data-label="日時">${esc(fmt(a.at))}</td><td data-label="人">${esc(a.user)}</td><td data-label="操作">${esc(a.action)}</td><td data-label="対象">${esc(a.target)}</td><td data-label="内容">${esc(a.detail)}</td></tr>`).join("")}</tbody></table></section>`;
};

// --- start ------------------------------------------------------------------------------------------------
document.getElementById("logoutBtn").addEventListener("click", () => guarded(async () => { await api("/api/logout", {}); location.href = "/login"; }));
(async () => {
  const me = await api("/api/me");
  CSRF = me.csrf;
  document.getElementById("who").textContent = me.user;
  render();
})();
