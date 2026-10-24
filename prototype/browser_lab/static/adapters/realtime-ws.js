// OpenAI-compatible Realtime API over WebSocket, for the single-model candidates that call the reception tools
// themselves: OpenAI GPT-Realtime-2.1 and xAI Grok Voice 3.0 (user request 2026-10-23). The local server mints a
// short-lived secret; the browser passes it as a WebSocket subprotocol (credentials.protocols). Settings the
// vendor does not take with the secret (xAI) are sent here as session.update. Mic and model audio: 24 kHz PCM.
// As with Gemini, the caller's short sounds while the AI talks (back-channel 「はい」) are held back (backchannel.js).
// xAI's force_message speaks a given text as it is: the read-backs and the closing words made by the server are
// then said word for word (credentials.force_message). Written from the public docs (2026-10-23); the
// connection has not been confirmed with a real account.
import { createPlayer, fromBase64, startCapture, toBase64 } from "../pcm.js";
import { createBackchannelGate } from "../backchannel.js";

const RATE = 24000;
const TRANSCRIPT_WAIT_MS = 1500;   // how long a tool call waits for the caller's words that came just before it
const OPENING_WAIT_MS = 3000;      // the model has this long to start the opening before it is asked another way
const MIC_WAIT_MS = 8000;          // the mic is sent once the opening is heard, or after this long at the latest
const FORCE_WAIT_MS = 2500;        // the vendor must start a response for a forced text within this, or the model is asked
const FORCE_SOUND_MS = 6000;       // once it has started one, the voice must come within this
const TAIL_S = 1.5;                // under this much of the AI's voice left, a short 「はい」 is an answer

// The words a tool result asks to be read as they are (the server makes them: prompts.ASK_FOR, request_summary)
export function wordsToRead(name, result) {
  if (!result || typeof result !== "object") return "";
  if (result.read_back_now && result.read_back_now.say) return result.read_back_now.say;
  if ((name === "request_summary" || name === "request_readback") && result.say) return result.say;
  return "";
}

export async function connect(ctx) {
  const c = ctx.credentials;
  const ac = ctx.audioContext;
  const out = ac.createGain();
  ctx.attachOutputNode(out);
  const player = createPlayer(ac, out, RATE);
  let capture = null, ready = false, closing = false, holding = false, micStarted = false;
  let aiItem = null, aiQueuedMs = 0, generationDone = true, responded = false, openingAudio = false;
  let transcriptWait = null;
  let force = !!c.force_message;                   // turned off for the session if the vendor refuses it once
  let forced = null;                               // { text, timer } while a forced text has not sounded yet
  let aiText = "", aiReported = false;             // the AI's words of the current response (from deltas)
  let lastForced = "";                             // the last forced text (already reported as the AI's words)
  // The caller's words per item. xAI sends several transcripts for one item, before and after the end of speech
  // (2026-10-24: only the first piece, 「焼肉」, reached the server): the words not yet reported are kept and
  // reported when they matter (a tool call, the AI's next words); a later, longer transcript adds the rest.
  const userHeard = new Map();                     // item_id → words heard and not yet reported
  const userSent = new Map();                      // item_id → words already reported
  let heardSinceSpeech = false;                    // some of the caller's words reported since they began to speak
  // per response: tool outputs sent, calls still running, and whether the response itself is over
  let inflight = 0, outputs = [], responseOver = false, hangup = false;

  const ws = new WebSocket(c.ws_url, c.protocols);
  const send = (obj) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));
  const readyOn = c.session_update ? "session.updated" : "session.created";

  ws.onopen = () => {
    if (c.session_update) send({ type: "session.update", session: c.session_update });
    setTimeout(() => { if (!ready && !closing) ctx.vendorClosed(`10秒たっても ${readyOn} が届かない`); }, 10000);
  };
  ws.onerror = () => ctx.log("vendor_error", "websocket error");
  ws.onclose = (e) => {
    ctx.log("vendor_closed", { code: e.code, reason: e.reason });
    if (!closing) ctx.vendorClosed(`code ${e.code}${e.reason ? "：" + e.reason : ""}`);
  };

  const waitForTranscript = () => {
    let done;
    const p = new Promise((r) => { done = r; });
    const timer = setTimeout(() => done(null), TRANSCRIPT_WAIT_MS);
    transcriptWait = { promise: p, resolve: (v) => { clearTimeout(timer); done(v); } };
  };
  function heardUser(item, text) {   // a transcript for an item: the whole so far (cumulative) or a new piece
    if (!text) return;
    const sent = userSent.get(item) || "", sofar = sent + (userHeard.get(item) || "");
    let whole;
    if (text.startsWith(sofar)) whole = text;                    // the whole so far, grown
    else if (sofar.includes(text)) return;                       // heard already
    else if (sofar && text.startsWith(sent) && text.slice(0, 2) === sofar.slice(0, 2)) whole = text;   // corrected
    else whole = sofar + text;                                   // the next piece
    const rest = whole.slice(sent.length);
    if (rest) userHeard.set(item, rest); else userHeard.delete(item);
  }
  async function flushUser() {   // report the words not reported yet
    let last = null;
    for (const [item, text] of [...userHeard]) {
      userHeard.delete(item);
      if (!text.trim()) continue;
      userSent.set(item, (userSent.get(item) || "") + text);
      heardSinceSpeech = true;
      last = await ctx.onTranscript("user", text);
      if (transcriptWait) { transcriptWait.resolve(last); transcriptWait = null; }
    }
    return last;
  }
  const reportAi = async (text) => {
    const words = (text || "").replace(/\[[a-z _-]+\]\s*/gi, "").trim();
    if (!words || aiReported) return;
    aiReported = true;
    if (lastForced && same(words, lastForced)) { lastForced = ""; return; }   // the forced text, already reported
    await flushUser();   // the caller's words come first
    ctx.onTranscript("ai", words);
  };
  const same = (a, b) => a.replace(/[\s、。]/g, "") === b.replace(/[\s、。]/g, "");

  // a text read as it is (xAI force_message, in the shape of the xAI docs: an assistant item with output_text);
  // the vendor answers it with a response of its own, so no response.create after it. The opening is not
  // interruptible (the caller's sounds are dropped until it has played); the read-backs are.
  function speak(text, isOpening) {
    send({ type: "conversation.item.create", item: { type: "force_message", role: "assistant",
           interruptible: !isOpening, content: [{ type: "output_text", text }] } });
    forced = { text, isOpening, timer: setTimeout(() => forceFailed("声が届かない"), FORCE_WAIT_MS) };
  }
  function forceFailed(why) {
    if (!forced) return;
    clearTimeout(forced.timer);
    const wasOpening = forced.isOpening;
    forced = null;
    force = false;
    ctx.log("注意", `決まった文をそのまま読ませる方法（force_message）が使えなかった（${why}）。ここからはAIに読ませます`);
    if (wasOpening) askForOpening(); else send({ type: "response.create" });
  }

  function afterTools() {   // every call of the response answered: one response.create (or a forced text)
    if (!responseOver || inflight > 0 || !outputs.length) return;
    const done = outputs; outputs = [];
    if (hangup) return;   // end_call: no new response
    const words = force ? done.map((o) => wordsToRead(o.name, o.result)).filter(Boolean).join("") : "";
    if (words) speak(words); else send({ type: "response.create" });
  }

  ws.onmessage = async (msg) => {
    const text = typeof msg.data === "string" ? msg.data : await msg.data.text();
    let ev;
    try { ev = JSON.parse(text); } catch (e) { return; }
    const t = ev.type || "";
    if (!/audio\.delta$|transcript\.delta$|transcription\.(delta|updated)$/.test(t)) ctx.log("vendor_event", { type: t });
    if (t === readyOn) {
      if (ready) return;
      ready = true;
      opening();
      setTimeout(startMic, MIC_WAIT_MS);
    } else if (t === "response.created") {
      responded = true; generationDone = false; aiText = ""; aiReported = false;
      outputs = []; inflight = 0; responseOver = false; hangup = false;
      if (forced) {   // the vendor took the forced text: give its voice time (a slow start is not a refusal)
        clearTimeout(forced.timer);
        forced.timer = setTimeout(() => forceFailed("声が届かない"), FORCE_SOUND_MS);
      }
      await flushUser();
    } else if (t === "response.output_audio.delta" || t === "response.audio.delta") {
      if (holding || !ev.delta) return;
      if (forced) {   // the forced text is sounding: these are the AI's words (the server checks the read-back)
        clearTimeout(forced.timer);
        lastForced = forced.text;
        const said = forced.text;
        flushUser().then(() => ctx.onTranscript("ai", said));
        forced = null;
      }
      if (ev.item_id !== aiItem) { aiItem = ev.item_id; aiQueuedMs = 0; }
      const buf = fromBase64(ev.delta);
      aiQueuedMs += (buf.byteLength / 2) / RATE * 1000;
      player.enqueue(buf);
      if (!openingAudio) { openingAudio = true; startMic(); }
    } else if (t === "response.output_audio_transcript.delta" || t === "response.audio_transcript.delta") {
      aiText += ev.delta || "";
    } else if (t === "response.output_audio_transcript.done" || t === "response.audio_transcript.done") {
      reportAi(ev.transcript || aiText);
    } else if (t === "response.done") {
      generationDone = true;
      reportAi(aiText);
      responseOver = true;
      afterTools();
    } else if (t === "input_audio_buffer.speech_started") {
      heardSinceSpeech = false;
      const unplayedMs = player.pendingSeconds() * 1000;
      if (unplayedMs > 0) {
        player.clear();
        if (aiItem && c.truncate !== false) {
          send({ type: "conversation.item.truncate", item_id: aiItem, content_index: 0,
                 audio_end_ms: Math.max(0, Math.round(aiQueuedMs - unplayedMs)) });
        }
        ctx.vendorInterrupted(`${c.vendor || "realtime"}.input_audio_buffer.speech_started`, unplayedMs);
      }
    } else if (t === "input_audio_buffer.speech_stopped") {
      waitForTranscript();
    } else if (t === "conversation.item.input_audio_transcription.completed") {
      heardUser(ev.item_id || "u", ev.transcript || "");
      if (c.vendor === "xai") ctx.log("文字起こし", { item: ev.item_id, text: ev.transcript || "" });
      if (transcriptWait) await flushUser();   // the speech is over: a tool call may be waiting for these words
    } else if (t === "conversation.item.input_audio_transcription.updated") {
      heardUser(ev.item_id || "u", ev.transcript || "");
    } else if (t === "response.function_call_arguments.done") {
      inflight++;
      // the caller's 「はい」 can arrive after the model's tool call: let the server see it first
      // words already heard go at once; only when none has come since the speech ended, wait a little for them
      let heard = await flushUser();
      if (!heard && transcriptWait && !heardSinceSpeech) heard = await transcriptWait.promise;
      if (heard && heard.stop_ai) { inflight--; return; }
      let args = {};
      try { args = JSON.parse(ev.arguments || "{}"); } catch (e) { /* keep empty */ }
      const result = await ctx.toolCall(ev.name, args);
      send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: ev.call_id,
             output: JSON.stringify(result) } });
      outputs.push({ name: ev.name, result });
      if (result && result.hangup) hangup = true;
      inflight--;
      afterTools();
    } else if (t === "error") {
      ctx.log("vendor_error", ev.error || ev);
      if (forced) forceFailed(((ev.error && ev.error.message) || "エラー").slice(0, 200));
    }
  };

  function opening() {
    // the opening (A-01) in the model's own voice (user decision 2026-10-20), then it waits for the caller
    if (force && c.greeting) { speak(c.greeting, true); return; }   // xAI: the exact words of A-01
    askForOpening();
  }

  function askForOpening() {   // the model is asked to say the opening (the words come with the request)
    if (!c.opening_request) { startMic(); return; }
    send({ type: "conversation.item.create",
           item: { type: "message", role: "user", content: [{ type: "input_text", text: c.opening_request }] } });
    send({ type: "response.create" });
    setTimeout(() => {
      if (responded || closing) return;
      ctx.log("冒頭の案内", "AIが話し始めないので、別の頼み方でもう一度頼みました");
      send({ type: "response.create", response: { instructions: c.opening_request } });
    }, OPENING_WAIT_MS);
  }

  async function startMic() {
    if (micStarted || closing) return;
    micStarted = true;
    const gate = createBackchannelGate({
      send: (buf) => send({ type: "input_audio_buffer.append", audio: toBase64(buf) }),
      aiTalking: () => (generationDone ? player.pendingSeconds() > TAIL_S : player.pendingSeconds() > 0.05),
      thresholdDb: () => (ctx.micThresholdDb ? ctx.micThresholdDb() : -42),
      sampleRate: RATE,
      onBackchannel: (d) => ctx.log("相づち", `AIの話し中の短い声（${d.ms} ms）は、AIに渡さなかった（話を止めない）`),
      onRelease: (d) => ctx.log("話し中の割り込み", d.why === "long" ? `AIの話し中に${d.ms} ms以上話したので、AIに渡した`
        : "AIが話し終えたので、お客様の声をそのままAIに渡した"),
    });
    capture = await startCapture(ac, ctx.micStream, RATE, (buf) => gate.push(buf));
    if (closing) capture.stop();
  }

  return {
    mute() { out.gain.value = 0; player.clear(); },
    // after end_call the closing words are queued; anything said after it is a repeat (2026-10-20, Gemini)
    holdNewSpeech(on) { holding = !!on; ctx.log("vendor_event", { type: on ? "hold_after_goodbye" : "resume_speech" }); },
    async close() { closing = true; if (capture) capture.stop(); player.clear(); ws.close(); },
  };
}
