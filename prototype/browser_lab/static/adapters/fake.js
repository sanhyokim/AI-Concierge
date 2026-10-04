// Offline stand-in: "speaks" with a tone, stops when the microphone picks up speech, answers after a pause.
// It exercises the page, the measurement and the tool path only. It says nothing about any vendor.
export async function connect(ctx) {
  const ac = ctx.audioContext;
  const gain = ac.createGain();
  gain.gain.value = 0;
  const osc = ac.createOscillator();
  osc.frequency.value = 330;
  osc.connect(gain);
  ctx.attachOutputNode(gain);
  osc.start();
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
    if (ev === "start" && speaking) setTimeout(() => { if (speaking) { stopSpeaking(); ctx.log("fake_interrupted"); } }, 120);
    if (ev === "end") {
      turn += 1;
      const result = await ctx.toolCall("save_field", { field: "request", value: `模擬の用件 ${turn}` });
      ctx.log("fake_tool_result", result);
      setTimeout(() => { if (!closed) say(1500, `（模擬応答 ${turn}）`); }, 500);
    }
  });
  return {
    async close() { closed = true; off(); clearTimeout(timer); try { osc.stop(); } catch (e) { /* stopped */ } },
  };
}
