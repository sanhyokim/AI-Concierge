"use strict";
// Admin app (vanilla JS, no build step). All business rules live on the server; this page only shows and edits.
let view = document.getElementById("view");
// each screen gets a fresh element, so listeners from the previous screen never pile up
function resetView() { const fresh = view.cloneNode(false); view.replaceWith(fresh); view = fresh; }
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let CSRF = "";
let ME = "";

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = "/login"; throw new Error("ログインが必要です"); }
  const j = await r.json();
  if (!r.ok) { const err = new Error(j.error || `HTTP ${r.status}`); err.status = r.status; throw err; }
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
  dtmf_confirm: "番号の確認待ち（1か2）", dtmf_offer_caller_id: "回線の番号に折り返すか確認中", forwarded_to_staff: "担当者の携帯へ転送した", handed_to_normal: "普通受電へ渡した（旧版の記録）", ended: "終了" };
const OUTCOME_LABELS = { accepted: "受付済み", retry_same_key: "同じキーで再試行", channel_stopped: "経路を停止", failed_stopped: "止めて人が確認",
  unknown_after_24h: "24時間で結果不明", not_sent_unapproved: "送信せず（承認前）" };
const routeBadge = (route, label) => `<span class="badge ${route === "ai" ? "info" : route === "forward" || route === "normal" ? "good" : "warn"}">${esc(label)}</span>`;
const NO_INTAKE = (route) => route === "forward" || route === "normal";   // "normal": a record from before the schedule was removed

// --- alerts shown on every screen ---------------------------------------------------------------------------
async function refreshAlerts() {
  const s = await api("/api/status");
  const out = [];
  const p = s.notification_problems;
  const bad = (p.counts.failed_stopped || 0) + (p.counts.unknown || 0) + (p.counts.channel_stopped || 0);
  if (bad) out.push(`<div class="alert bad">通知の障害があります：失敗 ${p.counts.failed_stopped || 0}件・結果不明 ${p.counts.unknown || 0}件・停止中 ${p.counts.channel_stopped || 0}件。<a href="#/notify">通知の画面で確認</a></div>`);
  for (const c of p.stopped_channels) out.push(`<div class="alert bad">通知の経路「${esc(c.channel)}」が停止中：${esc(c.reason)}</div>`);
  const home = !(location.hash.split("/")[1] || "");   // the standing notes only on the home screen, not on every page
  if (home && s.faq_misses_open) out.push(`<div class="alert warn">FAQで答えられなかった質問が ${s.faq_misses_open}件あります。<a href="#/faq">FAQの画面で回答を書く</a></div>`);
  if (home && s.provisional) out.push(`<div class="alert info">FAQは暫定・架空の値です。本番の決定済みの設定ではありません。</div>`);
  if (home && s.retention_unset) out.push(`<div class="alert info">受付記録の保存期間が未設定です（<a href="#/mode">基本設定</a>で設定できます。自動削除は未実装）。</div>`);
  document.getElementById("alerts").innerHTML = out.join("");
}

// the heading of a screen: title, one line of explanation, and the screen's main buttons on the right
const head = (title, lead = "", actions = "") => `<div class="page-head"><div><h1>${title}</h1>${lead ? `<p>${lead}</p>` : ""}</div>${actions ? `<div class="actions">${actions}</div>` : ""}</div>`;

// --- router -----------------------------------------------------------------------------------------------
const routes = {};
// The tests in one place (user, 2026-10-26: 「テストはテストページ一か所でまとめてほしい」): one menu item, three tabs
const TEST_PAGES = ["test", "demo", "ledger"];
const testTabs = (on) => `<nav class="page-tabs" aria-label="試験の種類">${[["test", "声で話す", "AIと実際に話す"],
  ["demo", "文字で試す", "受付・要約・通知の流れ"], ["ledger", "費用", "試験の費用の記録"]].map(([k, l, d]) =>
  `<a href="#/${k}" class="${k === on ? "on" : ""}" ${k === on ? 'aria-current="page"' : ""}><b>${l}</b><span>${d}</span></a>`).join("")}</nav>`;
async function render() {
  const [, name = "", arg] = location.hash.split("/");
  const menu = name === "call" ? "calls" : TEST_PAGES.includes(name) ? "test" : name;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#/${menu}`));
  resetView();
  view.innerHTML = `<p class="muted">読み込み中…</p>`;
  try { await (routes[name] || routes[""])(arg ? decodeURIComponent(arg) : undefined); }
  catch (e) { view.innerHTML = `<div class="card error">表示できませんでした：${esc(e.message)}</div>`; }
  window.scrollTo(0, 0);
  refreshAlerts().catch(() => {});
}
window.addEventListener("hashchange", render);

// --- home -------------------------------------------------------------------------------------------------
routes[""] = async () => {
  const [s, c] = await Promise.all([api("/api/status"), api("/api/calls")]);
  view.innerHTML = `${head("ホーム", "AIの電話受付の設定と、最近の受付の一覧です。",
      `<a class="button primary" href="#/test">試験</a>`)}
  <div class="grid">
    <section class="card stat"><span class="label">AI受付のオン・オフ</span><span class="value">NTTの転送の切り替えで行います</span>
      <span class="sub">このアプリからは変えられません。</span>
      <details class="fold"><summary>切り替え方</summary>
        <ul class="sub"><li>オン：転送先をAIの番号にする。このアプリに届いた電話は、すべてAIが受けます。</li>
          <li>オフ：転送先を担当者の携帯に戻す。今まで通りで、このアプリには届きません。</li></ul>
        <p class="sub">092はボイスワープ（ひかり電話設定サイト）、0120はカスタマコントロールで切り替えます（契約の確認待ち）。</p></details></section>
    <section class="card stat"><span class="label">AIが止まったときの扱い</span><span class="value" id="homeFailure">${esc(s.ai_failure_label)}</span>
      <span class="sub">設定の版 v${s.config_version}（${esc(fmt(s.config_meta.created_at))}・${esc(s.config_meta.created_by || "")}）・<a href="#/mode">変える</a></span></section>
  </div>
  <section class="card"><div class="card-head"><h2>最近の受付</h2><a href="#/calls">すべて見る</a></div>${callsTable(c.calls.slice(0, 5))}</section>`;
};

// --- basic settings ---------------------------------------------------------------------------------------
routes.mode = async () => {
  const c = await api("/api/config");
  const cfg = c.config;
  view.innerHTML = `${head("基本設定", `保存すると新しい版になり、<b>次の着信から</b>使われます。今の版は v${c.version} です。AI受付のオン・オフは、NTTの転送で切り替えます（この画面にはありません）。`)}
    <section class="card">
    <label>AIが止まったとき（着信の時点でAIが使えないとき）の扱い
      <select id="failure">${Object.entries(c.labels.failure_actions).map(([k, l]) => `<option value="${k}" ${cfg.ai_failure_action === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
    <p class="sub" style="margin:-6px 0 14px">AI受付をオンにしている間にAIが止まると、電話を取りこぼすおそれがあるため、どちらかを決めておきます。</p>
    <label>受付記録の保存期間（日。空欄は未設定）<input id="retention" type="number" min="1" value="${cfg.retention_days ?? ""}"></label>
    <label>変更のメモ（任意）<input id="note" maxlength="200"></label>
    <div class="form-actions"><button class="primary" id="saveMode">保存</button></div></section>
    <section class="card"><h2>保存の履歴</h2><table class="stack"><thead><tr><th>版</th><th>日時</th><th>保存した人</th><th>メモ</th></tr></thead><tbody>
    ${c.history.map((h) => `<tr><td data-label="版">v${h.id}</td><td data-label="日時">${esc(fmt(h.created_at))}</td><td data-label="保存した人">${esc(h.created_by)}</td><td data-label="メモ">${esc(h.note)}</td></tr>`).join("")}</tbody></table></section>`;
  document.getElementById("saveMode").addEventListener("click", () => guarded(async () => {
    const rd = document.getElementById("retention").value;
    const r = await api("/api/config", { config: { ...cfg, ai_failure_action: document.getElementById("failure").value, retention_days: rd === "" ? null : Number(rd) },
      note: document.getElementById("note").value });
    toast(`v${r.version} を保存しました。次の着信から使われます`);
    render();
  }));
};

// --- FAQ --------------------------------------------------------------------------------------------------
routes.faq = async () => {
  const { faqs, misses } = await api("/api/faqs");
  const byCode = Object.fromEntries(faqs.map((f) => [f.code, f]));
  const examplesOf = (f) => String(f.examples || "").split("\n").filter((e) => e.trim());
  const faqOptions = `<option value="">足す先のFAQを選ぶ</option>` + faqs.map((f) => `<option value="${f.id}">${esc(f.code)}　${esc(f.question)}</option>`).join("");
  const waiting = misses.filter((m) => m.status === "open" || m.status === "ai_answered");
  const done = misses.filter((m) => !(m.status === "open" || m.status === "ai_answered"));
  // 2026-10-29 (user): 「答えられなかった質問はフィードバックで回答に追加できる流れが必要かもね」「FAQで回答できなかった内容を
  // いれて、管理者が回答を記入したら使えるようにするとか？」
  const missCard = (m) => {
    const chosen = m.status === "ai_answered" && byCode[m.faq_code];
    const when = `<p class="sub">${esc(fmt(m.at))}・<a href="#/call/${encodeURIComponent(m.call_id)}">この受付の記録</a>${m.ai_question && m.ai_question.replace(/[?？。、\s]/g, "") !== m.question.replace(/[?？。、\s]/g, "") ? `・AIの聞き取り「${esc(m.ai_question)}」` : ""}</p>`;
    const writeAnswer = `<div class="miss-way"><b>回答を書いて、新しいFAQにする</b>
        <label>質問 <input data-m="question" value="${esc(m.question)}"></label>
        <label>回答（AIはこの文だけで答えます）<textarea data-m="answer" rows="2" placeholder="例：はい、福岡市は対応しています。"></textarea></label>
        <div class="form-actions"><button class="primary small" data-miss-answer="${m.id}">新しいFAQにする</button></div></div>`;
    const addTo = `<div class="miss-way"><b>同じ意味のFAQがあるときは、似た質問として足す</b>
        <div class="row"><select data-m="faq" class="inline" aria-label="足す先のFAQ">${faqOptions}</select><button class="small" data-miss-add="${m.id}">このFAQに足す</button></div></div>`;
    return `<section class="card" data-miss="${m.id}">
      <div class="card-head"><div class="row"><b>「${esc(m.question)}」</b>${statusBadge(chosen ? "heard" : "awaiting_confirmation", m.status_label)}</div>
        <button class="small" data-miss-dismiss="${m.id}" title="この質問にはFAQで答えない（担当者が折り返しで答える）">FAQにしない</button></div>
      ${when}
      ${chosen ? `<div class="alert info">AIは、意味の近い <b>${esc(chosen.code)}「${esc(chosen.question)}」</b> の回答で答えました。<br>回答：${esc(chosen.answer)}
          <div class="form-actions"><button class="primary small" data-miss-ok="${m.id}">合っている（似た質問として足す）</button></div></div>
        <details class="fold"><summary>違う答えにしたいとき</summary>${writeAnswer}${addTo}</details>`
        : `${writeAnswer}${addTo}`}</section>`;
  };
  view.innerHTML = `${head("FAQ", "有効なFAQだけを、AIが答える範囲に使います。変更は<b>次の会話から</b>反映されます。すべて未承認の推奨案・試作です。")}
    <section class="card"><div class="card-head"><h2>答えられなかった質問（${waiting.length}件）</h2></div>
      <p class="sub">お客様の質問に、言い方の合うFAQがなかったものです。回答を書くか、同じ意味のFAQに「似た質問」として足すと、次の会話から、同じような聞き方にもAIが答えます。</p>
      ${waiting.length ? "" : `<p class="muted">いまはありません。</p>`}</section>
    <div class="stack-gap">${waiting.map(missCard).join("")}</div>
    ${done.length ? `<section class="card"><details><summary>反映したもの・FAQにしないもの（${done.length}件）</summary>
      <ul class="sub">${done.map((m) => `<li>${esc(fmt(m.handled_at || m.at))}「${esc(m.question)}」：${esc(m.status_label)}${m.faq_code ? `（${esc(m.faq_code)}）` : ""}</li>`).join("")}</ul></details></section>` : ""}
    <section class="card"><details><summary>＋ FAQを追加する</summary>
      <label>質問の例 <input id="newQ"></label><label>回答 <textarea id="newA" rows="3"></textarea></label>
      <label>似た質問（1行に1つ。お客様の言い方の例）<textarea id="newE" rows="2"></textarea></label>
      <label>探すための語（読点・カンマ区切り）<input id="newK" placeholder="例：駐車場,車,ある|止め|置け"></label>
      <p class="sub">「いくら|料金|値段」のように「|」でつないだ語は、どれかが質問に入っていないと、そのFAQは選ばれません（「無煙ロースター」とだけ言った日程の質問に、料金の答えが返らないようにするため）。</p>
      <div class="form-actions"><button class="primary" id="addFaq">追加</button></div></details></section>
    <div id="faqList" class="stack-gap">${faqs.map((f) => `<section class="card" data-id="${f.id}">
      <div class="card-head"><div class="row"><b>${esc(f.code)}</b><span class="badge warn">${esc(f.approval)}</span></div>
        <div class="row"><label class="inline"><input type="checkbox" data-toggle="${f.id}" ${f.enabled ? "checked" : ""}> 有効</label><button class="small" data-edit="${f.id}">編集</button></div></div>
      <div class="view-mode"><p><b>Q.</b> ${esc(f.question)}</p><p><b>A.</b> ${esc(f.answer)}</p>
        ${examplesOf(f).length ? `<p class="sub">似た質問：${examplesOf(f).map(esc).join("／")}</p>` : ""}<p class="sub">探す語：${esc(f.keywords)}</p></div>
      <div class="edit-mode hidden"><label>質問の例 <input data-f="question" value="${esc(f.question)}"></label>
        <label>回答 <textarea data-f="answer" rows="3">${esc(f.answer)}</textarea></label>
        <label>似た質問（1行に1つ）<textarea data-f="examples" rows="2">${esc(f.examples || "")}</textarea></label>
        <label>探す語 <input data-f="keywords" value="${esc(f.keywords)}"></label>
        <div class="form-actions"><button class="primary small" data-save="${f.id}">保存</button></div></div></section>`).join("")}</div>`;
  const byId = Object.fromEntries(faqs.map((f) => [String(f.id), f]));
  document.getElementById("addFaq").addEventListener("click", () => guarded(async () => {
    await api("/api/faqs", { question: document.getElementById("newQ").value, answer: document.getElementById("newA").value, examples: document.getElementById("newE").value, keywords: document.getElementById("newK").value, enabled: true });
    toast("追加しました。次の会話から使われます"); render();
  }));
  const miss = (id, body, msg) => guarded(async () => { await api(`/api/faq-misses/${id}`, body); toast(msg); render(); });
  view.addEventListener("click", (e) => {
    const t = e.target;
    const card = t.closest("[data-miss]");
    if (t.dataset.missAnswer) {
      const answer = card.querySelector("[data-m=answer]").value.trim();
      if (!answer) { toast("回答を入力してください"); return; }
      miss(t.dataset.missAnswer, { action: "answer", question: card.querySelector("[data-m=question]").value, answer }, "新しいFAQにしました。次の会話から使われます");
    }
    if (t.dataset.missAdd) {
      const faqId = Number(card.querySelector("[data-m=faq]").value);
      if (!faqId) { toast("足す先のFAQを選んでください"); return; }
      miss(t.dataset.missAdd, { action: "add_to", faq_id: faqId }, "似た質問として足しました。次の会話から使われます");
    }
    if (t.dataset.missOk) miss(t.dataset.missOk, { action: "add_to" }, "似た質問として足しました。次の会話から使われます");
    if (t.dataset.missDismiss) miss(t.dataset.missDismiss, { action: "dismiss" }, "FAQにしないものにしました");
    if (t.dataset.edit) { const c = t.closest("[data-id]"); c.querySelector(".view-mode").classList.add("hidden"); c.querySelector(".edit-mode").classList.remove("hidden"); }
    if (t.dataset.save) guarded(async () => {
      const c = t.closest("[data-id]"); const f = byId[t.dataset.save];
      const val = (k) => c.querySelector(`[data-f=${k}]`).value;
      await api(`/api/faqs/${f.id}`, { question: val("question"), answer: val("answer"), examples: val("examples"), keywords: val("keywords"), enabled: !!f.enabled });
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
  const offered = v.catalog.filter((x) => !x.hold);   // held candidates (user decision) are not offered here
  const voices = JSON.parse(JSON.stringify(v.voices));
  let active = v.active_voice;
  const draw = () => {
    document.getElementById("voiceRows").innerHTML = voices.map((x, i) => {
      const k = cat[x.candidate] || { status: "unknown", status_label: "候補にない", voices: [] };
      const fixed = k.voice_applies === false;   // the vendor's agent settings decide the voice
      return `<tr><td data-label="使う"><input type="radio" name="active" value="${esc(x.id)}" ${active === x.id ? "checked" : ""} ${fixed ? "disabled" : ""} aria-label="使う声"></td>
        <td data-label="表示名"><input data-i="${i}" data-k="label" value="${esc(x.label)}"></td>
        <td data-label="接続候補"><select data-i="${i}" data-k="candidate">${(k.hold ? [k, ...offered] : offered).map((o) => `<option value="${o.candidate}" ${o.candidate === x.candidate ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select></td>
        <td data-label="声"><input data-i="${i}" data-k="voice" value="${esc(x.voice)}" list="vl-${i}"><datalist id="vl-${i}">${k.voices.map((n) => `<option value="${esc(n)}">`).join("")}</datalist></td>
        <td data-label="状態">${statusBadge(k.status, k.status_label)}${k.hold ? ' <span class="badge">保留</span>' : ""}${fixed ? '<br><span class="sub">記録だけ（反映されない：業者のエージェント設定で固定）</span>' : ""}</td>
        <td data-label=""><button class="small danger" data-del="${i}">削除</button></td></tr>`;
    }).join("");
  };
  view.innerHTML = `${head("音声", "声を登録し、表示名（例：女性）を付けます。試験の「声で話す」では、AIを選んだ後に<b>この表示名で声を選べます</b>。「使う」は、「文字で試す」と「声で話す」で最初に選ばれる声です。")}
    <section class="card"><table class="stack"><thead><tr><th>使う</th><th>表示名</th><th>接続候補</th><th>声（名前・ID）</th><th>状態</th><th></th></tr></thead><tbody id="voiceRows"></tbody></table>
    <div class="form-actions"><button id="addVoice">＋ 声を追加</button><button class="primary" id="saveVoices">保存</button></div></section>
    <section class="card"><details><summary>接続候補と声の状態</summary><table class="stack"><thead><tr><th>候補</th><th>状態</th><th>声の候補</th><th>声の指定の方法</th><th>この画面の声の反映</th></tr></thead><tbody>
    ${offered.map((x) => `<tr><td data-label="候補">${esc(x.name)}</td><td data-label="状態">${statusBadge(x.status, x.status_label)}</td><td data-label="声の候補">${esc(x.voices.join("、") || "（業者の画面で選ぶ）")}</td><td data-label="指定の方法">${esc(x.how)}</td><td data-label="反映">${esc(x.apply_label)}</td></tr>`).join("")}
    </tbody></table><p class="sub">「利用可能」はオフラインの模擬だけです（音声の評価には使えません）。鍵の値は表示しません（設定の有無だけ）。保留中の候補（ElevenAgents・Cartesia・GPT-Realtime-2.1）は表示していません。</p></details></section>`;
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
  if (!calls.length) return `<p class="muted">まだ受付はありません。<a href="#/demo">試験の「文字で試す」</a>で作れます。</p>`;
  return `<table class="stack"><thead><tr><th>日時</th><th>種類</th><th>経路</th><th>店舗・お名前</th><th>折り返し先</th><th>用件</th><th>通知</th><th>対応</th></tr></thead><tbody>
  ${calls.map((c) => `<tr><td data-label="日時"><a href="#/call/${encodeURIComponent(c.id)}">${esc(fmt(c.started_at))}</a></td>
    <td data-label="種類">${esc(c.source_label)}${c.emergency ? ` <span class="badge bad">${c.emergency === "obvious" ? "緊急" : "要注意"}</span>` : ""}</td>
    <td data-label="経路">${routeBadge(c.route, c.route_label)}</td>
    <td data-label="店舗・お名前">${esc(c.shop.display)} ${esc(c.name.display)}</td>
    <td data-label="折り返し先">${esc(c.number.display)} ${c.number.display ? `<span class="sub">（${esc(c.number.status_label)}）</span>` : ""}</td>
    <td data-label="用件">${esc(c.request.display)}</td><td data-label="通知">${esc(notifySummary(c.notifications))}</td>
    <td data-label="対応">${esc(c.handled_status)}${c.handled_by ? `<br><span class="sub">担当 ${esc(c.handled_by)}</span>` : ""}</td></tr>`).join("")}</tbody></table>`;
}
routes.calls = async () => {
  const { calls } = await api("/api/calls");
  // 2026-10-29 (user): 「受付一覧に記録のエクスポート機能がほしい」
  view.innerHTML = `${head("受付履歴", "試験（文字で試す・声で話す）の記録です（会社の回線の着信はありません）。日時を押すと詳しく見られます。",
    `<a class="button" href="/export/calls.csv" download title="すべての受付を1行ずつ、店舗名・用件・会話などと一緒に保存します">一覧をファイルに保存（Excelで開けます）</a>`)}<section class="card">${callsTable(calls)}</section>`;
};

function eventText(e) {
  const d = e.data || {};
  switch (e.kind) {
    case "caller": return `お客様：${d.text}`;
    case "ai": return `AI：${d.text}`;
    case "tool": return `業務処理 ${d.name}(${JSON.stringify(d.args)}) → ${JSON.stringify(d.result)}`;
    case "route": return `経路：${d.label}（${d.reason}）`;
    case "ai_used": return `使ったAI：${d.name}・声 ${d.voice}${d.note ? `（${d.note}）` : ""}`;
    case "play": return `録音済みの案内 ${d.clip}：${d.text || ""}`;
    case "gather_dtmf": return "プッシュボタンの入力を待つ";
    case "say_ai": return d.clip ? `AIが話す案内 ${d.clip}：${d.intent}（AIの声で）` : `AIが話す（意図：${d.intent}）`;
    case "ai_send_stopped": return d.text || "AIへの音声送信を停止";
    case "transcript_deleted": return `拒否より前の文字起こしを削除（${d.count}件）`;
    case "ai_send_blocked": return `AIへ送らなかった（${d.reason}）`;
    case "ai_blocked": return `AIの業務処理を受け付けなかった（${d.tool}：${d.reason}）`;
    case "consent": return `同意の変更：${({ ai_refused: "AIの拒否", recording_refused: "録音の拒否", human_request: "人との会話の希望", human_request_answer: "短い受付への回答", ai_failure: "AIの障害" })[d.kind] || d.kind}（${({ speech: "発話から判定", operator: "操作", tester: "試験者" })[d.source] || d.source || ""}）`;
    case "recording_stopped": return "録音を停止（以降は録音しない）";
    case "recording_deleted": return d.why || "拒否より前の録音を削除";
    case "vendor_interrupted": return `業者の割り込みの通知（${d.source}${d.unplayed_ms != null ? `・残りの音 ${(d.unplayed_ms / 1000).toFixed(1)}秒` : ""}）：${(d.invalidated || []).length ? `返事の前に遮られた復唱 ${d.invalidated.join("・")} を確認済みにしない` : "無効にした復唱はない"}`;
    case "read_back_mismatch": return `復唱の言い間違い（${d.field === "callback_number" ? "折り返し先" : d.field === "preferred_datetime" ? "希望日時" : d.field}）：AIは「${d.said}」と言った。保存した値と違うので、「はい」でも確認済みにしない`;
    case "forward_to_staff": return d.text || "担当者の携帯へ転送";
    default: return `${e.kind} ${d && Object.keys(d).length ? JSON.stringify(d) : ""}`;
  }
}
// The whole record as plain text (2026-10-20, user: copying the event list by hand to report a test was tedious)
function callText(c) {
  const lines = [`受付 ${c.id}`, `種類：${c.source_label}`, `開始・終了：${fmt(c.started_at)} 〜 ${fmt(c.ended_at) || "通話中"} ${c.end_reason || ""}`,
    `着信先：${c.dialed}`, `回線の番号：${c.caller_id || "非通知・不明"}`, `経路：${c.route_label}（${c.route_reason}）`];
  if (c.ai) lines.push(`AI：${c.ai.name}`);
  lines.push(`この通話の設定：v${c.config_version}・声 ${c.voice.label || c.voice.voice}・有効だったFAQ ${c.faq_codes.join(", ")}`,
    `同意：録音 ${c.consent.recording === "allowed" ? "許可" : "拒否"}・AIでの処理 ${c.consent.ai_processing === "allowed" ? "許可" : "拒否"}`);
  if (c.emergency) lines.push(`緊急：${c.emergency}`);
  if (c.open_questions.length) lines.push(`確認事項：${c.open_questions.join("／")}`);
  if (c.fields && c.fields.length) {
    lines.push("", "■ 受付項目");
    for (const f of c.fields) lines.push(`${f.label}：${f.display || "未取得"}（${f.status_label}）`);
  }
  for (const sm of c.summaries || []) lines.push("", `■ 要約 版${sm.version}（${sm.reason}）`, sm.text);
  lines.push("", `■ 出来事の記録（${c.events.length}件）`);
  for (const e of c.events) lines.push(`${fmt(e.at).slice(11)}\t${eventText(e)}`);
  return lines.join("\r\n") + "\r\n";
}

function downloadText(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["\ufeff" + text], { type: "text/plain;charset=utf-8" }));
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

routes.call = async (id) => {
  const c = await api(`/api/calls/${encodeURIComponent(id)}`);
  const ended = !!c.ended_at;
  const canCorrect = ended && !NO_INTAKE(c.route);
  view.innerHTML = `${head(`受付 ${esc(fmt(c.started_at))}`, `${esc(c.source_label)}・<code>${esc(c.id)}</code>`,
      `<a class="button" href="#/calls">一覧へ</a><button id="saveText" title="受付項目・要約・出来事の記録を1つのファイルにします。開発の担当に送るときに使います">記録をテキストで保存</button>`)}
  <div class="grid">
    <section class="card"><h2>この通話</h2><dl class="kv"><dt>種類</dt><dd>${esc(c.source_label)}（デモ・架空のデータ）</dd><dt>開始・終了</dt><dd>${esc(fmt(c.started_at))} 〜 ${esc(fmt(c.ended_at) || "通話中")} ${esc(c.end_reason || "")}</dd>
    <dt>着信先</dt><dd>${esc(c.dialed)}</dd><dt>回線の番号</dt><dd>${esc(c.caller_id || "非通知・不明")}</dd>
    <dt>経路</dt><dd>${routeBadge(c.route, c.route_label)} ${esc(c.route_reason)}</dd>
    ${c.ai ? `<dt>AI</dt><dd>${esc(c.ai.name)}</dd>` : ""}
    <dt>この通話の設定</dt><dd>v${c.config_version}・声 ${esc(c.voice.label || c.voice.voice)}・有効だったFAQ ${esc(c.faq_codes.join(", "))}</dd>
    <dt>同意</dt><dd>録音 ${c.consent.recording === "allowed" ? "許可" : "拒否"}・AIでの処理 ${c.consent.ai_processing === "allowed" ? "許可" : "拒否"}</dd>
    ${c.emergency ? `<dt>緊急</dt><dd><span class="badge bad">${esc(c.emergency)}</span></dd>` : ""}
    ${c.open_questions.length ? `<dt>確認事項</dt><dd>${c.open_questions.map(esc).join("／")}</dd>` : ""}</dl></section>
    <section class="card"><h2>対応</h2><dl class="kv"><dt>対応状態</dt><dd><select id="handled" class="inline" aria-label="対応状態">${["未対応", "対応中", "対応済み", "対応不要"].map((s) => `<option ${c.handled_status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
      ${c.handled_status === "対応中" && c.handled_by && c.handled_by !== ME ? `<button class="small" id="takeover">担当を引き継ぐ</button>` : ""}</dd>
    <dt>担当</dt><dd id="handledBy">${c.handled_by ? `${esc(c.handled_by)}${c.handled_by === ME ? "（自分）" : ""}<span class="sub">（${esc(fmt(c.handled_at))}）</span>` : '<span class="muted">なし（「対応中」にした人が担当になります）</span>'}</dd></dl></section>
  </div>
  ${NO_INTAKE(c.route) ? `<section class="card"><h2>担当者へ回した通話</h2><p>AIの会話は始めていません。受付項目・要約・通知はありません（デモでは電話はかけません）。</p></section>` : `
  <section class="card"><h2>受付項目</h2>
    <p class="sub">「本人確認済み」は、復唱の後にお客様がはっきり「はい」と答えた値だけです。担当者の補正は「担当者が補正」と表示し、本人確認済みにはしません。${canCorrect ? "" : "補正は通話の終了後にできます。"}</p>
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
  <section class="card"><h2>要約（固定の規則で作成）</h2>
    ${c.summaries.length ? c.summaries.map((s) => `<h3>版${s.version}（${esc(s.reason)}・${esc(fmt(s.created_at))}）</h3><pre class="body">${esc(s.text)}</pre>`).join("") : `<p class="muted">通話の終了後に作ります。</p>`}
    <p class="sub">会話モデルの要約ではありません。本番の要約の品質を示すものではありません。</p></section>
  <section class="card"><div class="card-head"><h2>通知（シミュレーション）</h2><div class="row"><button class="small" id="previewBtn">プレビュー</button>
      <button class="small" id="process" title="再試行待ちも今すぐ送ります（外部へは送りません）">送信シミュレーションを実行</button>
      <button class="small" id="renotify" title="同じ通知が二重に作られないことを確かめます">通知イベントを再投入</button></div></div>
    <div id="preview"></div>
    ${notificationsTable(c.notifications)}</section>`}
  <section class="card"><details><summary>出来事の記録（${c.events.length}件）</summary>
    <table class="stack"><tbody>${c.events.map((e) => `<tr><td data-label="時刻">${esc(fmt(e.at).slice(11))}</td><td data-label="内容">${esc(eventText(e))}</td></tr>`).join("")}</tbody></table></details></section>`;
  // the screen sends the version it read; if someone else changed the state meanwhile, the server refuses (409)
  const setHandled = async (status, done) => {
    try {
      await api(`/api/calls/${encodeURIComponent(id)}/status`, { status, version: c.handled_version });
      toast(done);
    } catch (e) {
      if (e.status !== 409) { toast(`エラー：${e.message}`); return; }
      await render();
      toast(e.message);
      return;
    }
    await render();
  };
  document.getElementById("handled").addEventListener("change", (e) => setHandled(e.target.value, "対応状態を保存しました"));
  document.getElementById("takeover")?.addEventListener("click", () => setHandled("対応中", "担当を引き継ぎました"));
  // an ASCII file name: Chromium saved a Japanese one as "download"
  document.getElementById("saveText").addEventListener("click", () => downloadText(`call-${c.id}.txt`, callText(c)));
  if (NO_INTAKE(c.route)) return;
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
  view.innerHTML = `${head("通知", "通知はすべて<b>シミュレーション</b>で、外部へは送りません。LINEは、送信先の登録・接続設定・送信の承認が済むまで送りません（公式のMessaging APIの候補構成。個人LINEの非公式な操作は使いません）。",
      `<button id="processAll" title="再試行待ちも今すぐ送ります（外部へは送りません）">送信シミュレーションを実行</button>`)}
    <section class="card"><h2>宛先</h2><table class="stack"><thead><tr><th>有効</th><th>名前</th><th>経路</th><th>シミュレーションの結果</th><th></th></tr></thead><tbody>
    ${t.targets.map((x) => `<tr data-tid="${x.id}"><td data-label="有効"><input type="checkbox" data-k="enabled" ${x.enabled ? "checked" : ""} aria-label="有効"></td>
      <td data-label="名前"><input data-k="name" value="${esc(x.name)}"></td><td data-label="経路"><select data-k="channel">${opts(t.channels, x.channel)}</select></td>
      <td data-label="結果"><select data-k="simulate">${opts(t.simulate, x.simulate)}</select></td>
      <td data-label=""><button class="small primary" data-savet="${x.id}">保存</button></td></tr>`).join("")}
    <tr data-tid="new"><td data-label="有効"><input type="checkbox" data-k="enabled" checked aria-label="有効"></td><td data-label="名前"><input data-k="name" placeholder="新しい宛先（架空）"></td>
      <td data-label="経路"><select data-k="channel">${opts(t.channels, "simulation")}</select></td><td data-label="結果"><select data-k="simulate">${opts(t.simulate, "success")}</select></td>
      <td data-label=""><button class="small" data-savet="new">追加</button></td></tr></tbody></table>
    ${t.problems.stopped_channels.map((c) => `<p class="error">経路「${esc(c.channel)}」が停止中：${esc(c.reason)} <button class="small" data-resume="${esc(c.channel)}">人が確認して再開</button></p>`).join("")}
    <p class="sub">同じ受付の同じ版は、宛先ごとに1件しか作りません。</p></section>
  <section class="card"><h2>送信内容のプレビュー</h2>
    <div class="row"><select id="pvCall" aria-label="受付を選ぶ" style="flex:1 1 260px;width:auto">${withSummary.map((c) => `<option value="${esc(c.id)}">${esc(fmt(c.started_at))} ${esc(c.shop.display)} ${esc(c.request.display)}</option>`).join("") || "<option value=''>（要約のある受付がありません）</option>"}</select>
    <button id="pvBtn">プレビュー</button></div><div id="pvOut"></div></section>
  <section class="card"><h2>最近の通知</h2><table class="stack"><thead><tr><th>受付</th><th>宛先</th><th>版</th><th>状態</th><th>試行</th><th>最後の結果</th></tr></thead><tbody>
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
  view.innerHTML = `${head("試験")}${testTabs("demo")}
  <p class="sub">架空の着信を文字で入れて、受付・要約・通知の流れを確かめます。AIの代わりに<b>決まった規則で動く試験用の応答</b>を使うので、会話のAIの性能は分かりません（それは「声で話す」で見ます）。業務処理は本番と同じです。</p>
  <div class="grid">
    <section class="card"><h2>1. 架空の着信を入れる</h2>
      <label>着信先 <select id="dialed"><option>0120-77-3408</option><option>092-504-2185</option><option>不明</option></select></label>
      <label>回線の番号（架空）<input id="callerId" value="090-1111-2222"></label>
      <label class="check" style="margin:-4px 0 12px"><input type="checkbox" id="withheld"> 非通知</label>
      <label>通話の日時（日本時間。「明後日」などの日付の確認に使う）<input type="datetime-local" id="at" value="${now}"></label>
      <label class="check" style="margin-bottom:14px"><input type="checkbox" id="aiDown"> AIが止まっている状態にする（止まったときの扱いを確かめる）</label>
      <div class="form-actions"><button class="primary" id="startCall">着信を入れる</button></div></section>
    <section class="card"><h2>2. この通話</h2><div id="decision"><p class="muted">AI受付がオンのときに、この試作に届いた電話として扱います。</p></div></section>
  </div>
  <div class="grid hidden" id="callArea">
    <section class="card"><h2>3. 会話（お客様役として入力）</h2>
      <div class="chat" id="chat"></div>
      <div class="row" style="margin-top:8px"><input id="say" placeholder="お客様の発話を入力" aria-label="お客様の発話"><button class="primary" id="sayBtn">送る</button></div>
      <div class="quick" id="quick">${QUICK.map((q) => `<button data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
      <div class="row"><button class="small" id="runScript">例の会話を流す（番号・日時の訂正あり）</button><button class="small" id="interruptBtn" title="AIの発話（復唱など）がお客様に遮られた、と業者が知らせた想定。端末の音量ではなく、業者の通知だけを根拠にする">業者の割り込みの通知（模擬）</button><button class="small danger" id="endCall">通話を終える</button></div>
      <div id="dtmfArea" class="hidden"><h3>プッシュボタン（AIなしの経路）</h3><div class="row"><input id="digits" inputmode="numeric" placeholder="番号（#まで）"><button id="dtmfSend">#で確定</button></div>
        <div class="row" style="margin-top:6px"><button data-key="1">1（はい）</button><button data-key="2">2（入力し直す）</button><button id="dtmfTimeout">入力なし</button></div></div>
    </section>
    <section class="card"><h2>4. 受付項目と同意の状態</h2><div id="fields"></div></section>
  </div>
  <section class="card hidden" id="resultArea"><h2>5. 要約と通知</h2><div id="result"></div></section>`;
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
    <p class="sub">着信 ${esc(fmt(c.started_at))}${c.ai ? `・AI ${esc(c.ai.name)}` : ""}・設定 v${c.config_version}・声 ${esc(c.voice.label || c.voice.voice)}・FAQ ${esc(c.faq_codes.join(", "))}</p>
    <p class="sub">この通話は、開始時の設定のまま進みます。管理画面で設定を変えても、次の着信から反映されます。</p>
    ${NO_INTAKE(c.route) ? `<p><b>AIが止まっているため、担当者の携帯へ転送します。AIの会話は始めません</b>（デモでは電話はかけません）。発話を送っても、AIへは送りません。</p>` : ""}
    <p><a href="#/call/${encodeURIComponent(c.id)}">受付の詳細を開く</a></p>`;
  document.getElementById("fields").innerHTML = `<table class="stack" id="fieldTable"><thead><tr><th>項目</th><th>値</th><th>状態</th></tr></thead><tbody>
    ${c.fields.map((f) => `<tr data-name="${f.name}"><td data-label="項目">${esc(f.label)}</td><td data-label="値">${esc(f.display) || '<span class="muted">—</span>'}</td><td data-label="状態">${statusBadge(f.status, f.status_label)}</td></tr>`).join("")}</tbody></table>
    <dl class="kv" style="margin-top:8px"><dt>状態</dt><dd id="callState">${esc(STATE_LABELS[c.state] || c.state)}</dd><dt>録音</dt><dd>${c.consent.recording === "allowed" ? "許可" : "拒否（停止済み）"}</dd>
    <dt>AIでの処理</dt><dd id="aiConsent">${c.consent.ai_processing === "allowed" ? "許可" : "拒否"}</dd><dt>AIへの送信</dt><dd id="aiSend">${c.ai_allowed ? "送信中（許可）" : "送らない"}</dd>
    ${c.emergency ? `<dt>緊急</dt><dd><span class="badge bad">${esc(c.emergency)}</span></dd>` : ""}${c.open_questions.length ? `<dt>確認事項</dt><dd>${c.open_questions.map(esc).join("／")}</dd>` : ""}</dl>`;
  document.getElementById("dtmfArea").classList.toggle("hidden", !(c.state || "").startsWith("dtmf"));
  if (c.ended_at && !NO_INTAKE(c.route)) showResult(c);
}
async function startDemo() {
  const r = await api("/api/demo/start", { dialed: document.getElementById("dialed").value, caller_id: document.getElementById("callerId").value,
    withheld: document.getElementById("withheld").checked, at: document.getElementById("at").value, ai_available: !document.getElementById("aiDown").checked });
  document.getElementById("callArea").classList.remove("hidden");
  document.getElementById("resultArea").classList.add("hidden");
  document.getElementById("chat").innerHTML = "";
  for (const e of r.call.events) {
    if (e.kind === "play") chat("sys", `録音済みの案内 ${e.data.clip}：${e.data.text}`);
    if (e.kind === "say_ai" && e.data.clip) chat("sys", `AIが話す案内 ${e.data.clip}：${e.data.intent}`);
  }
  for (const line of r.say) chat("ai", line);
  if (r.call.route === "dtmf") chat("sys", "AIが使えないため、録音の案内とプッシュボタンで受け付けます");
  drawCall(r.call);
}
async function sendSay(text) {
  if (!demo.call || !text) return;
  document.getElementById("say").value = "";
  chat("caller", text);
  const r = await api("/api/demo/say", { call_id: demo.call.id, text });
  if (!r.result.forwarded) chat("blocked", `AIへは送っていません（${({ ai_not_used_for_this_call: "担当者へ転送した通話", ai_refused: "AIでの処理を拒否済み", call_ended: "通話は終了", ai_failure: "AIの障害" })[r.result.reason] || r.result.reason}）。内容も保存していません。`);
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

// --- test: talking with the AI (the lab page, shown inside this app) ---------------------------------------
routes.test = async () => {
  view.innerHTML = `${head("試験")}${testTabs("test")}
    <iframe id="labFrame" class="lab-frame" src="/lab/?embed=1" allow="microphone; autoplay" title="声で話す試験"></iframe>`;
};
window.addEventListener("message", (e) => {   // the lab page tells its height, so the page scrolls as one
  const f = document.getElementById("labFrame");
  if (f && e.origin === location.origin && e.source === f.contentWindow && e.data && e.data.labHeight) {
    f.style.height = `${Math.ceil(e.data.labHeight) + 4}px`;
  }
});

// --- ledger -----------------------------------------------------------------------------------------------
const usd = (v) => `$${Number(v || 0).toFixed(4)}`;
routes.ledger = async () => {
  const l = await api("/api/ledger");
  const rows = Object.entries(l.ledgers).filter(([, x]) => x.cap_usd !== 0 || x.requests || x.open_reservations);
  view.innerHTML = `${head("試験")}${testTabs("ledger")}
    <p class="sub">声で話す試験の費用の見込みと実際の額です。${esc(l.note)}</p>
    <section class="card"><table class="stack"><thead><tr><th>台帳</th><th>会話の数</th><th>合計（推定・照合済み）</th><th>うち照合待ちの留保</th><th>照合待ち</th><th>上限</th></tr></thead><tbody>
    ${rows.map(([v, x]) => `<tr><td data-label="台帳">${esc(v)}</td><td data-label="会話の数">${x.requests}</td><td data-label="合計">${usd(x.est_cost_usd)}</td><td data-label="留保">${usd(x.held_usd)}</td><td data-label="照合待ち">${x.open_reservations}件</td><td data-label="上限">${x.cap_usd == null ? "なし" : `$${x.cap_usd}`}</td></tr>`).join("")}
    </tbody></table></section>
    <section class="card"><h2>照合待ちの留保</h2>
    <p class="sub">業者の利用画面で、その会話の実際の額を確かめてから入力します。照合すると、台帳の合計がその額になり、留保が閉じます。同じ留保を別の額で照合し直すことはできません。</p>
    ${rows.some(([, x]) => x.open.length) ? `<table class="stack"><thead><tr><th>台帳</th><th>留保</th><th>会話</th><th>留保した額</th><th>利用画面の額（USD）</th><th>メモ</th><th></th></tr></thead><tbody>
    ${rows.flatMap(([v, x]) => x.open.map((r) => `<tr><td data-label="台帳">${esc(v)}</td><td data-label="留保"><code>${esc(r.rid)}</code><br><span class="sub">${esc(fmt(r.ts))}</span></td><td data-label="会話">${esc(r.operation)} ${esc(r.run_id)}</td><td data-label="留保した額">${usd(r.est_cost_usd)}</td>
      <td data-label="利用画面の額"><input inputmode="decimal" data-amount="${esc(r.rid)}" aria-label="利用画面の額"></td><td data-label="メモ"><input data-note="${esc(r.rid)}" placeholder="例：10/09 利用画面" aria-label="メモ"></td>
      <td data-label=""><button class="small primary" data-reconcile="${esc(r.rid)}" data-vendor="${esc(v)}">照合する</button></td></tr>`)).join("")}</tbody></table>` : `<p class="muted">照合待ちの留保はありません。</p>`}</section>
    <section class="card"><h2>試験の会話（サーバーの記録）</h2>
    <p class="sub">これまでの会話：${Object.entries(l.counts || {}).filter(([, n]) => n.sessions || n.mint_failures).map(([c, n]) => `${esc(c)} ${n.sessions}回`).join("、") || "0回"}（回数の上限なし）</p>
    ${l.sessions.length ? `<table class="stack"><thead><tr><th>開始</th><th>候補</th><th>状態</th><th>止めた理由</th><th>業者の終了</th><th>業務処理・裏方の応答</th></tr></thead><tbody>
    ${l.sessions.map((x) => `<tr><td data-label="開始">${esc(fmt(x.started_at))}</td><td data-label="候補">${esc(x.candidate)}</td><td data-label="状態">${esc(x.status_label || x.status)}<br><span class="sub">${esc(x.end_reason || "")}</span>${x.why_counted ? `<br><span class="sub">${esc(x.why_counted)}</span>` : ""}${x.releasable ? `<div class="row" style="margin-top:4px"><input data-relnote="${esc(x.id)}" placeholder="例：10/08 OpenAIの利用画面で0件・$0を確認" aria-label="確認した内容"><button class="small" data-release="${esc(x.id)}">費用から外す</button></div>` : ""}</td><td data-label="止めた理由">${esc(x.stop_reason || "—")}</td><td data-label="業者の終了">${x.close_confirmed ? "確認" : "未確認"}</td><td data-label="回数">${x.tool_calls}・${x.backend_responses}</td></tr>`).join("")}</tbody></table>
    <p class="sub">「費用から外す」は、開始に失敗し、業者の会話が作られなかった記録だけに出ます。業者の利用画面で利用が0件であることを確かめてから、確かめた内容を入力して押してください（留保を0円で閉じます。操作履歴に残ります）。</p>` : `<p class="muted">まだ試験の会話はありません。</p>`}</section>`;
  view.addEventListener("click", (e) => {
    const t = e.target;
    if (t.dataset.release) guarded(async () => {
      await api("/api/lab/release", { session_id: t.dataset.release, note: view.querySelector(`[data-relnote="${t.dataset.release}"]`).value });
      toast("費用から外しました（留保を0円で閉じました）"); render();
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
  view.innerHTML = `${head("操作履歴", "誰が、いつ、何を変えたかの記録です。")}<section class="card"><table class="stack"><thead><tr><th>日時</th><th>人</th><th>操作</th><th>対象</th><th>内容</th></tr></thead><tbody>
  ${audit.map((a) => `<tr><td data-label="日時">${esc(fmt(a.at))}</td><td data-label="人">${esc(a.user)}</td><td data-label="操作">${esc(a.action)}</td><td data-label="対象">${esc(a.target)}</td><td data-label="内容">${esc(a.detail)}</td></tr>`).join("")}</tbody></table></section>`;
};

// --- start ------------------------------------------------------------------------------------------------
document.getElementById("logoutBtn").addEventListener("click", () => guarded(async () => { await api("/api/logout", {}); location.href = "/login"; }));
(async () => {
  const me = await api("/api/me");
  CSRF = me.csrf; ME = me.user;
  document.getElementById("who").textContent = me.user;
  render();
})();
