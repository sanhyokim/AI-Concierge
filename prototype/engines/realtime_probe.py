"""Plan A partial slow-down probe (OpenAI Realtime, voices marin / cedar).

For each script (S-01..S-03) the same text is rendered twice with the same voice:
  N  normal speed throughout
  D  the target segment (number / date / amount) slowed, then back to normal
using two methods:
  instr  one response; the instructions ask to slow down only the bracketed part.
         Segment boundaries are NOT known automatically: a label template is written
         and must be filled by hand before measuring (see README).
  split  three responses (pre / target / post) with audio.output.speed changed between
         turns (it can only change between turns). Boundaries are the response edges.

Requires OPENAI_API_KEY and the `websockets` package. Without them it exits before
contacting anything. Output: WAV (8 kHz, decoded from mu-law), transcripts, measurements.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import os
import pathlib
import sys
from array import array

from ..concierge.prompts import READ_ALOUD_NORMAL, READ_ALOUD_SEGMENT, READ_ALOUD_SLOW_TARGET
from ..concierge.readings import count_morae
from ..measure.audio import ulaw_decode, write_wav
from ..measure.rate import compare_versions, measure_version
from .realtime_runner import AUDIO_DELTA_TYPES, TRANSCRIPT_DELTA_TYPES, WebSocketTransport, session_update

SCENARIOS = pathlib.Path(__file__).resolve().parents[1] / "scenarios" / "first_round.json"
READER_INSTRUCTIONS = "あなたは与えられた文章を、そのまま日本語で読み上げる読み手です。文章以外は話しません。"


async def _speak(t: WebSocketTransport, prompt: str) -> tuple[bytes, str]:
    await t.send({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}})
    await t.send({"type": "response.create"})
    audio, transcript = bytearray(), ""
    while True:
        ev = await t.recv()
        et = ev.get("type", "")
        if et in AUDIO_DELTA_TYPES:
            audio += base64.b64decode(ev.get("delta", ""))
        elif et in TRANSCRIPT_DELTA_TYPES:
            transcript += ev.get("delta", "")
        elif et == "response.done":
            return bytes(audio), transcript
        elif et == "error":
            raise RuntimeError(ev.get("error"))


async def _session(model: str, key: str, voice: str, speed: float = 1.0) -> WebSocketTransport:
    t = await WebSocketTransport(f"wss://api.openai.com/v1/realtime?model={model}", key).connect()
    await t.send(session_update(voice, READER_INSTRUCTIONS, speed=speed, tools=[], transcription_model=None))
    return t


def _morae(script: dict) -> dict:
    return {k: count_morae(script[k]["reading"]) for k in ("pre", "target", "post")}


async def render_instr(model, key, voice, script, version, out: pathlib.Path) -> dict:
    t = await _session(model, key, voice)
    try:
        if version == "N":
            prompt = READ_ALOUD_NORMAL.format(text=script["pre"]["text"] + script["target"]["text"]
                                              + script["post"]["text"])
        else:
            prompt = READ_ALOUD_SLOW_TARGET.format(pre=script["pre"]["text"], target=script["target"]["text"],
                                                   post=script["post"]["text"])
        audio, transcript = await _speak(t, prompt)
    finally:
        await t.close()
    stem = out / f"A-{voice}-instr-{script['id']}-{version}"
    write_wav(f"{stem}.wav", ulaw_decode(audio), 8000)
    morae = _morae(script)
    labels = {"wav": f"{stem.name}.wav", "fill_in": "start_s/end_s by listening (e.g. Audacity labels)",
              "segments": [{"name": n, "start_s": None, "end_s": None, "morae": morae[n]}
                           for n in ("pre", "target", "post")]}
    pathlib.Path(f"{stem}.labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=2))
    return {"wav": f"{stem}.wav", "transcript": transcript, "needs_manual_labels": True}


async def render_split(model, key, voice, script, version, target_speed, out: pathlib.Path) -> dict:
    t = await _session(model, key, voice)
    pcm, segments, transcripts = [], [], []
    try:
        for name in ("pre", "target", "post"):
            speed = target_speed if (version == "D" and name == "target") else 1.0
            await t.send({"type": "session.update",
                          "session": {"type": "realtime", "audio": {"output": {"speed": speed}}}})
            audio, transcript = await _speak(t, READ_ALOUD_SEGMENT.format(text=script[name]["text"]))
            start_s = len(pcm) / 8000
            pcm.extend(ulaw_decode(audio))
            segments.append({"name": name, "start_s": start_s, "end_s": len(pcm) / 8000,
                             "morae": count_morae(script[name]["reading"])})
            transcripts.append(transcript)
    finally:
        await t.close()
    samples = array("h", pcm)
    stem = out / f"A-{voice}-split-{script['id']}-{version}"
    write_wav(f"{stem}.wav", samples, 8000)
    return {"wav": f"{stem}.wav", "transcripts": transcripts, "segments": segments,
            "measure": measure_version(samples, 8000, segments)}


async def main_async(args) -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; nothing was sent. See prototype/README.md.", file=sys.stderr)
        return 2
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("Install vendor dependencies first: pip install -r prototype/requirements-vendor.txt", file=sys.stderr)
        return 2
    data = json.loads(SCENARIOS.read_text())
    scripts = [s for s in data["slowdown"] if s["id"] in args.scripts]
    target_speed = data["slow_settings"]["openai_split_target_speed"]
    out = pathlib.Path(args.out or f"prototype/results/probe-a-{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}")
    out.mkdir(parents=True, exist_ok=True)
    report = {"plan": "A", "model": args.model, "measurement": "API direct, phone codec only (no network)",
              "runs": []}
    for voice in args.voices:
        for script in scripts:
            if "split" in args.methods:
                n = await render_split(args.model, key, voice, script, "N", target_speed, out)
                d = await render_split(args.model, key, voice, script, "D", target_speed, out)
                report["runs"].append({"voice": voice, "method": "split", "script": script["id"], "N": n, "D": d,
                                       "compare": compare_versions(n["measure"], d["measure"])})
            if "instr" in args.methods:
                n = await render_instr(args.model, key, voice, script, "N", out)
                d = await render_instr(args.model, key, voice, script, "D", out)
                report["runs"].append({"voice": voice, "method": "instr", "script": script["id"], "N": n, "D": d,
                                       "compare": "fill labels, then run prototype.measure.compare_labels"})
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"wrote {out}/report.json")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default="gpt-realtime-2.1")
    p.add_argument("--voices", nargs="+", default=["marin", "cedar"])
    p.add_argument("--scripts", nargs="+", default=["S-01", "S-02", "S-03"])
    p.add_argument("--methods", nargs="+", default=["split", "instr"], choices=["split", "instr"])
    p.add_argument("--out")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
