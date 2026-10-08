// Offline stand-in: "speaks" with a tone, stops when the microphone picks up speech, answers after a pause.
// It exercises the page, the measurement and the tool path only. It says nothing about any vendor.
// What it "hears" at each end of caller speech comes from the server's script (?fake_script=refusal|recording),
// so spoken refusals can be checked without speech recognition. A barge-in is reported like a vendor's
// interruption notice.
export async function connect(ctx) {
  const ac = ctx.audioContext;
  const gain = ac.createGain();
  gain.gain.value = 0;
  const osc = ac.createOscillator();
  osc.frequency.value = 330;
  osc.connect(gain);
  ctx.attachOutputNode(gain);
  osc.start();
  const script = (ctx.credentials && ctx.credentials.script) || [];
  let speaking = false, timer = null, closed = false, turn = 0;

  function say(ms, text) {
    speaking = true;
    gain.gain.setTargetAtTime(0.3, ac.currentTime, 0.01);
    ctx.onTranscript("ai", text);
    clearTimeout(timer);
    timer = setTimeout(stopSpeaking, ms);
  }
  function stopSpeaking() {
    speaking = false;
    gain.gain.setTargetAtTime(0, ac.currentTime, 0.01);
  }
  say(2500, "お電話ありがとうございます。株式会社野田のAI受付です。（模擬）");

  // react to the caller through the page's own voice-activity detection
  const off = ctx.onUserSpeech(async (ev) => {
    if (closed) return;
    if (ev === "start" && speaking) {
      setTimeout(() => {
        if (!speaking || closed) return;
        stopSpeaking();
        ctx.log("fake_interrupted");
        ctx.vendorInterrupted("fake.interrupted");
      }, 120);
    }
    if (ev === "end") {
      turn += 1;
      const heard = script[(turn - 1) % Math.max(1, script.length)];
      if (heard) {
        const r = await ctx.onTranscript("user", heard);
        if (closed || (r && r.stop_ai)) return;   // AI refused: nothing more is processed or said
      }
      const result = turn === 2
        ? await ctx.toolCall("lookup_faq", { question: "点検は無料ですか" })
        : await ctx.toolCall("save_field", { field: "request", value: `模擬の用件 ${turn}` });
      ctx.log("fake_tool_result", result);
      setTimeout(() => { if (!closed) say(1500, `（模擬応答 ${turn}）`); }, 500);
    }
  });
  return {
    mute() { gain.gain.value = 0; speaking = false; },
    async close() { closed = true; off(); clearTimeout(timer); try { osc.stop(); } catch (e) { /* stopped */ } },
  };
}
