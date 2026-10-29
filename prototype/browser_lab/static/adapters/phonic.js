// Phonic-1 over Phonic's own speech-to-speech WebSocket (wss://api.phonic.ai/v1/sts/ws), user request 2026-10-29
// (「すぐにテストしたい」). One model listens, thinks, speaks and calls the reception tools itself, like Grok. The local
// server mints a session token (credentials.ws_url carries it); the page sends the "config" message, then the mic as
// 20 ms "audio_chunk"s of 24 kHz PCM. The opening (A-01) is Phonic's welcome message, said as it is. The read-backs
// and the closing made by the server are said word for word with Phonic's "say" message (credentials.say).
// As with Gemini and Grok, the caller's short sounds while the AI talks (back-channel 「はい」) are held back.
// Written from the public docs (2026-10-29); the connection has not been confirmed with a real account.
import { createPlayer, fromBase64, startCapture, toBase64 } from "../pcm.js";
import { createBackchannelGate } from "../backchannel.js";
import { wordsToRead } from "./realtime-ws.js";

const RATE = 24000;
const CHUNK_BYTES = RATE / 50 * 2;  // 20 ms of 16-bit PCM (Phonic: at most 40 ms per chunk, about 20 ms suggested)
const READY_WAIT_MS = 10000;       // ready_to_start_conversation must come within this after the config
const TRANSCRIPT_WAIT_MS = 2000;   // Phonic may call a tool while the caller still talks: wait this long for the words
const MIC_WAIT_MS = 8000;          // the mic is sent once the opening is heard, or after this long at the latest
const SAY_WAIT_MS = 4000;          // the voice of a "say" must start within this, or the model is asked to read it
const TAIL_S = 1.5;                // under this much of the AI's voice left, a short 「はい」 is an answer

// What the model is told when the page says the words itself (it must not say them a second time)
export const SAID_BY_SYSTEM = "この文は、システムがいま、そのまま読み上げています。あなたは同じ文を言わず、何も話さずに、お客様のお返事を待ってください。";

export async function connect(ctx) {
  const c = ctx.credentials;
  const ac = ctx.audioContext;
  const out = ac.createGain();
  ctx.attachOutputNode(out);
  const player = createPlayer(ac, out, RATE);
  let ws = null, capture = null, ready = false, closing = false, holding = false, micStarted = false;
  let triedMinimal = false, openingAudio = false;
  let aiText = "", aiReported = false, generationDone = true;
  let say = !!c.say;                // turned off for the session if Phonic does not say a text once
  let forced = null;                // { text, timer } while a said text has not sounded yet
  let lastForced = "";              // the last said text (already reported as the AI's words)
  let awaitingText = false;         // the caller began to speak and their words have not come yet
  let transcriptWait = null;

  const send = (obj) => ws && ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));
  const same = (a, b) => a.replace(/[\s、。]/g, "") === b.replace(/[\s、。]/g, "");

  function open(config) {
    ws = new WebSocket(c.ws_url);
    const mine = ws;
    ws.onopen = () => {
      send(config);
      setTimeout(() => {
        if (!ready && !closing && ws === mine) ctx.vendorClosed("10秒たっても ready_to_start_conversation が届かない");
      }, READY_WAIT_MS);
    };
    ws.onerror = () => ctx.log("vendor_error", "websocket error");
    ws.onclose = (e) => {
      if (ws !== mine) return;   // the first connection, closed after its config was refused
      ctx.log("vendor_closed", { code: e.code, reason: e.reason });
      if (!closing) ctx.vendorClosed(`code ${e.code}${e.reason ? "：" + e.reason : ""}`);
    };
    ws.onmessage = onMessage;
  }

  // Phonic refused the config before the call began: try once more with only what the reception needs
  function retryMinimal(why) {
    if (triedMinimal || ready || !c.config_minimal) return false;
    triedMinimal = true;
    ctx.log("注意", `設定の一部が断られたので、最小の設定でつなぎ直します（${why}）`);
    const old = ws;
    ws = null;
    try { old.close(); } catch (e) { /* closed */ }
    open(c.config_minimal);
    return true;
  }

  const waitForTranscript = () => {
    let done;
    const p = new Promise((r) => { done = r; });
    const timer = setTimeout(() => done(null), TRANSCRIPT_WAIT_MS);
    transcriptWait = { promise: p, resolve: (v) => { clearTimeout(timer); done(v); } };
  };
  const reportAi = (text) => {
    const words = (text || "").trim();
    if (!words || aiReported) return;
    aiReported = true;
    if (lastForced && same(words, lastForced)) { lastForced = ""; return; }   // the said text, already reported
    ctx.onTranscript("ai", words);
  };

  // a text said as it is; if no voice comes, the model is asked to read it (and "say" is not used again)
  function speak(text) {
    send({ type: "say", text, interruptible: true });
    forced = { text, timer: setTimeout(() => sayFailed("声が届かない"), SAY_WAIT_MS) };
  }
  function sayFailed(why) {
    if (!forced) return;
    clearTimeout(forced.timer);
    const text = forced.text;
    forced = null;
    say = false;
    ctx.log("注意", `決まった文をそのまま読ませる方法（say）が使えなかった（${why}）。ここからはAIに読ませます`);
    send({ type: "generate_reply", system_message: `次の文を、一字一句そのまま読み上げてください。「${text}」` });
  }

  async function onMessage(msg) {
    const text = typeof msg.data === "string" ? msg.data : await msg.data.text();
    let ev;
    try { ev = JSON.parse(text); } catch (e) { return; }
    const t = ev.type || "";
    if (t !== "audio_chunk") ctx.log("vendor_event", t === "error" || t === "warning" ? ev : { type: t });
    if (t === "ready_to_start_conversation") {
      if (ready) return;
      ready = true;
      setTimeout(startMic, MIC_WAIT_MS);
    } else if (t === "conversation_created") {
      ctx.log("Phonicの会話ID", ev.conversation_id || "");
    } else if (t === "assistant_started_speaking") {
      generationDone = false; aiText = ""; aiReported = false;
    } else if (t === "audio_chunk") {
      if (holding || !ev.audio) return;
      if (forced) {   // the said text is sounding: these are the AI's words (the server checks the read-back)
        clearTimeout(forced.timer);
        lastForced = forced.text;
        ctx.onTranscript("ai", forced.text);
        forced = null;
      }
      aiText += ev.text || "";
      player.enqueue(fromBase64(ev.audio));
      if (!openingAudio) { openingAudio = true; startMic(); }
    } else if (t === "assistant_finished_speaking") {
      generationDone = true;
      reportAi(aiText);
    } else if (t === "interrupted_response") {
      generationDone = true;
      reportAi(ev.text || aiText);   // only the part said before the caller cut in
    } else if (t === "user_started_speaking") {
      awaitingText = true;
      const unplayedMs = player.pendingSeconds() * 1000;
      if (unplayedMs > 0) {
        player.clear();
        ctx.vendorInterrupted("phonic.user_started_speaking", unplayedMs);
      }
    } else if (t === "user_finished_speaking") {
      if (awaitingText && !transcriptWait) waitForTranscript();
    } else if (t === "input_text") {
      awaitingText = false;
      const heard = ev.text ? await ctx.onTranscript("user", ev.text) : null;
      if (transcriptWait) { transcriptWait.resolve(heard); transcriptWait = null; }
    } else if (t === "input_cancelled") {
      awaitingText = false;
      if (transcriptWait) { transcriptWait.resolve(null); transcriptWait = null; }
    } else if (t === "tool_call") {
      // the caller's 「はい」 must reach the server before the tool that it confirms
      if (awaitingText) {
        if (!transcriptWait) waitForTranscript();
        const heard = await transcriptWait.promise;
        if (heard && heard.stop_ai) return;
      }
      const result = await ctx.toolCall(ev.tool_name, ev.parameters || {});
      const words = say ? wordsToRead(ev.tool_name, result) : "";
      send({ type: "tool_call_output", tool_call_id: ev.tool_call_id,
             output: words ? { ...result, said_by_system: SAID_BY_SYSTEM } : result });
      if (words) speak(words);
    } else if (t === "assistant_ended_conversation") {
      ctx.log("Phonicが会話を終えた", "");
    } else if (t === "error") {
      const why = ((ev.error && ev.error.message) || "エラー").slice(0, 300) +
        (ev.param_errors ? "：" + JSON.stringify(ev.param_errors).slice(0, 300) : "");
      if (!ready && retryMinimal(why)) return;
      if (forced) sayFailed(why);
    }
  }

  async function startMic() {
    if (micStarted || closing) return;
    micStarted = true;
    const gate = createBackchannelGate({
      send: (buf) => {
        for (let i = 0; i < buf.byteLength; i += CHUNK_BYTES) {
          send({ type: "audio_chunk", audio: toBase64(buf.slice(i, Math.min(i + CHUNK_BYTES, buf.byteLength))) });
        }
      },
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

  open(c.config);

  return {
    mute() { out.gain.value = 0; player.clear(); },
    // after end_call the closing words are queued; anything said after it is a repeat (2026-10-20, Gemini)
    holdNewSpeech(on) { holding = !!on; ctx.log("vendor_event", { type: on ? "hold_after_goodbye" : "resume_speech" }); },
    async close() { closing = true; if (forced) clearTimeout(forced.timer); if (capture) capture.stop(); player.clear(); if (ws) ws.close(); },
  };
}
