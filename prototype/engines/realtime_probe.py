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
from .budget import (LIMITS, BudgetExceeded, UsageLedger, async_with_retries, openai_cost, openai_response_bound,
                     openai_usage_breakdown)
from .realtime_runner import AUDIO_DELTA_TYPES, TRANSCRIPT_DELTA_TYPES, WebSocketTransport, session_update

SCENARIOS = pathlib.Path(__file__).resolve().parents[1] / "scenarios" / "first_round.json"
PATH = "openai_api_direct"
MODEL_KEYS = {"gpt-realtime-2.1": "openai_rt21", "gpt-realtime-2.1-mini": "openai_rt21_mini"}
READER_INSTRUCTIONS = "あなたは与えられた文章を、そのまま日本語で読み上げる読み手です。文章以外は話しません。"


class Ctx:
    """Shared run context: credentials, model, ledger (spend limits) and run id."""

    def __init__(self, model: str, key: str, ledger: UsageLedger, run_id: str) -> None:
        self.model, self.key, self.ledger, self.run_id = model, key, ledger, run_id
        self.model_key = MODEL_KEYS.get(model, "openai_rt21")


async def _speak(ctx: Ctx, t: WebSocketTransport, prompt: str, op: str) -> tuple[bytes, str]:
    """One response. A pessimistic estimate is reserved (after the limit check) before sending; usage from
    response.done settles it. Without usage (timeout, error, missing field) the reservation stays held."""
    reserved = openai_response_bound(ctx.model_key)  # session max_output_tokens + all input uncached
    rid = ctx.ledger.reserve(op, PATH, reserved, ctx.run_id, audio_seconds=60)
    await t.send({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}})
    await t.send({"type": "response.create"})
    audio, transcript = bytearray(), ""

    async def collect() -> dict:
        nonlocal audio, transcript
        while True:
            ev = await t.recv()
            et = ev.get("type", "")
            if et in AUDIO_DELTA_TYPES:
                audio += base64.b64decode(ev.get("delta", ""))
            elif et in TRANSCRIPT_DELTA_TYPES:
                transcript += ev.get("delta", "")
            elif et == "response.done":
                return ev.get("response", {}).get("usage") or {}
            elif et == "error":
                raise RuntimeError(ev.get("error"))

    usage: dict = {}
    try:
        usage = await asyncio.wait_for(collect(), timeout=ctx.ledger.limits.request_timeout_s)
    finally:
        if usage:  # otherwise the reservation stays open and keeps counting at the estimate
            cost = openai_cost(usage, ctx.model_key)
            ctx.ledger.record(op, PATH, cost, ctx.run_id, audio_seconds=len(audio) / 8000, reservation=rid,
                              tokens=openai_usage_breakdown(usage), usage=usage)
            ctx.ledger.close(rid)
            if cost > reserved:  # the bound did not hold: stop the run instead of continuing
                raise BudgetExceeded(f"{op}: actual ${cost:.4f} exceeded the reservation ${reserved:.4f}")
    return bytes(audio), transcript


async def _session(ctx: Ctx, voice: str, speed: float = 1.0) -> WebSocketTransport:
    url = f"wss://api.openai.com/v1/realtime?model={ctx.model}"
    t = await async_with_retries(lambda: WebSocketTransport(url, ctx.key).connect(),
                                 max_retries=ctx.ledger.limits.max_retries)
    await t.send(session_update(voice, READER_INSTRUCTIONS, speed=speed, tools=[], transcription_model=None))
    return t


def _morae(script: dict) -> dict:
    return {k: count_morae(script[k]["reading"]) for k in ("pre", "target", "post")}


async def render_instr(ctx: Ctx, voice, script, version, out: pathlib.Path, rep: int = 1) -> dict:
    t = await _session(ctx, voice)
    try:
        if version == "N":
            prompt = READ_ALOUD_NORMAL.format(text=script["pre"]["text"] + script["target"]["text"]
                                              + script["post"]["text"])
        else:
            prompt = READ_ALOUD_SLOW_TARGET.format(pre=script["pre"]["text"], target=script["target"]["text"],
                                                   post=script["post"]["text"])
        audio, transcript = await _speak(ctx, t, prompt, f"instr:{script['id']}:{version}")
    finally:
        await t.close()
    stem = out / f"A-{voice}-instr-{script['id']}-{version}-r{rep}"
    write_wav(f"{stem}.wav", ulaw_decode(audio), 8000)
    morae = _morae(script)
    labels = {"wav": f"{stem.name}.wav", "fill_in": "start_s/end_s by listening (e.g. Audacity labels)",
              "segments": [{"name": n, "start_s": None, "end_s": None, "morae": morae[n]}
                           for n in ("pre", "target", "post")]}
    pathlib.Path(f"{stem}.labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=2))
    return {"wav": f"{stem}.wav", "path": PATH, "transcript": transcript, "needs_manual_labels": True}


async def render_split(ctx: Ctx, voice, script, version, target_speed, out: pathlib.Path, rep: int = 1) -> dict:
    t = await _session(ctx, voice)
    pcm, segments, transcripts = [], [], []
    try:
        for name in ("pre", "target", "post"):
            speed = target_speed if (version == "D" and name == "target") else 1.0
            await t.send({"type": "session.update",
                          "session": {"type": "realtime", "audio": {"output": {"speed": speed}}}})
            audio, transcript = await _speak(ctx, t, READ_ALOUD_SEGMENT.format(text=script[name]["text"]),
                                             f"split:{script['id']}:{version}:{name}")
            start_s = len(pcm) / 8000
            pcm.extend(ulaw_decode(audio))
            segments.append({"name": name, "start_s": start_s, "end_s": len(pcm) / 8000,
                             "morae": count_morae(script[name]["reading"])})
            transcripts.append(transcript)
    finally:
        await t.close()
    samples = array("h", pcm)
    stem = out / f"A-{voice}-split-{script['id']}-{version}-r{rep}"
    write_wav(f"{stem}.wav", samples, 8000)
    return {"wav": f"{stem}.wav", "path": PATH, "transcripts": transcripts, "segments": segments,
            "measure": measure_version(samples, 8000, segments)}


def repeat_spread(runs: list[dict]) -> dict:
    """Natural variation between repeated N renders of the same script and voice (split method only).

    For each later repetition, articulation-rate ratio N(rep k) / N(rep 1) per segment. The spread of
    the post ratios is the basis for reviewing the Q-08 return band (0.95-1.10, proposal)."""
    first: dict = {}
    pairs = []
    for r in sorted((r for r in runs if r.get("method") == "split"), key=lambda r: r["rep"]):
        key = (r["voice"], r["script"])
        segs = r["N"]["measure"]["segments"]
        if key not in first:
            first[key] = segs
            continue
        base = first[key]
        ratios = {name: (round(segs[name]["articulation_rate"] / base[name]["articulation_rate"], 3)
                         if base[name]["articulation_rate"] else None) for name in ("pre", "target", "post")}
        pairs.append({"voice": key[0], "script": key[1], "rep": r["rep"], "ratio_vs_rep1": ratios})
    post = [p["ratio_vs_rep1"]["post"] for p in pairs if p["ratio_vs_rep1"]["post"] is not None]
    return {"pairs": pairs, "post_min": min(post) if post else None, "post_max": max(post) if post else None,
            "note": "N vs N of the same script; if this spread is near the Q-08 band, the band needs review"}


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
    run_id = f"probe-a-{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}"
    out = pathlib.Path(args.out or f"prototype/results/{run_id}")
    out.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(args.model, key, UsageLedger("openai", LIMITS["openai"]), run_id)
    report = {"plan": "A", "model": args.model, "path": PATH,
              "measurement": "API direct, phone codec only (no telephone network)", "runs": []}
    try:
        for rep in range(1, args.repeat + 1):
            for voice in args.voices:
                for script in scripts:
                    if "split" in args.methods:
                        n = await render_split(ctx, voice, script, "N", target_speed, out, rep)
                        d = await render_split(ctx, voice, script, "D", target_speed, out, rep)
                        report["runs"].append({"voice": voice, "method": "split", "script": script["id"], "rep": rep,
                                               "N": n, "D": d,
                                               "compare": compare_versions(n["measure"], d["measure"])})
                    if "instr" in args.methods:
                        n = await render_instr(ctx, voice, script, "N", out, rep)
                        d = await render_instr(ctx, voice, script, "D", out, rep)
                        report["runs"].append({"voice": voice, "method": "instr", "script": script["id"], "rep": rep,
                                               "N": n, "D": d,
                                               "compare": "fill labels, then run prototype.measure.compare_labels"})
    finally:
        report["n_repeat_spread"] = repeat_spread(report["runs"])
        report["ledger_totals"] = ctx.ledger.totals()
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"wrote {out}/report.json; ledger totals: {report['ledger_totals']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default="gpt-realtime-2.1")
    p.add_argument("--voices", nargs="+", default=["marin", "cedar"])
    p.add_argument("--scripts", nargs="+", default=["S-01", "S-02", "S-03"])
    p.add_argument("--methods", nargs="+", default=["split", "instr"], choices=["split", "instr"])
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--out")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
