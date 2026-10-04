"""Run a plan A dialogue scenario (C-01, I-02, I-03, R-03, X-02, ...) against OpenAI Realtime.

Caller audio: one 16-bit mono WAV per caller turn (8 kHz or 16 kHz), named as in
scenarios/first_round.json ("audio" field), placed in --caller-audio-dir. Record them
with a person reading the script, or synthesise them with a TTS voice that is NOT one
of the candidates.

Measurement point: API side of this client (no telephone network). Requires
OPENAI_API_KEY and `websockets`; without them it exits before contacting anything.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import pathlib
import sys

from ..measure.audio import (frame_db, read_wav, to_phone_band, ulaw_decode, ulaw_encode, voiced_threshold_db,
                             write_wav)
from .budget import (LIMITS, BudgetExceeded, UsageLedger, async_with_retries, openai_upper_bound,
                     transcription_upper_bound)
from .realtime_runner import TURN_AUDIO_S, TURN_RESPONSES, RealtimeRunner, WebSocketTransport

SCENARIOS = pathlib.Path(__file__).resolve().parents[1] / "scenarios" / "first_round.json"


def load_caller_audio(path: pathlib.Path) -> tuple[bytes, float]:
    samples, rate = read_wav(str(path))
    phone, _ = to_phone_band(samples, rate)
    levels = frame_db(phone, 8000, 10)
    thr = voiced_threshold_db(levels)
    onset_ms = next((i * 10.0 for i, lv in enumerate(levels) if lv >= thr), 0.0)
    return ulaw_encode(phone), onset_ms


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
    scenario = next(s for s in json.loads(SCENARIOS.read_text())["dialog"] if s["id"] == args.scenario)
    audio_dir = pathlib.Path(args.caller_audio_dir)
    missing = [t["audio"] for t in scenario["turns"] if not (audio_dir / t["audio"]).exists()]
    if missing:
        print(f"missing caller audio: {missing}", file=sys.stderr)
        return 2
    run_id = f"dialog-{args.scenario}-{args.voice}-{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}"
    out = pathlib.Path(args.out or f"prototype/results/{run_id}")
    out.mkdir(parents=True, exist_ok=True)
    limits = LIMITS["openai"]
    ledger = UsageLedger("openai", limits)
    model_key = "openai_rt21_mini" if args.model.endswith("-mini") else "openai_rt21"
    longest_turn_s = max(len(load_caller_audio(audio_dir / t["audio"])[0]) / 8000 for t in scenario["turns"])
    try:  # stop before connecting when even the longest turn (replies + input transcription) would not fit
        ledger.check(TURN_RESPONSES * openai_upper_bound(TURN_AUDIO_S, model_key)
                     + transcription_upper_bound(longest_turn_s), audio_seconds=TURN_AUDIO_S)
    except BudgetExceeded as exc:
        print(f"{exc}; nothing was sent.", file=sys.stderr)
        return 3
    url = f"wss://api.openai.com/v1/realtime?model={args.model}"
    transport = await async_with_retries(lambda: WebSocketTransport(url, key).connect(),
                                         max_retries=limits.max_retries)
    runner = RealtimeRunner(transport, args.voice, ledger=ledger, run_id=run_id, model_key=model_key)
    timed_out, budget_stop = False, None
    try:
        result = await asyncio.wait_for(
            runner.run(scenario["turns"], lambda turn: load_caller_audio(audio_dir / turn["audio"])),
            timeout=limits.dialog_timeout_s)
    except asyncio.TimeoutError:
        timed_out, result = True, runner.result()
    except BudgetExceeded as exc:
        budget_stop, result = str(exc), runner.result()
    finally:
        await transport.close()
        for i, pb in enumerate(runner.playbacks, 1):
            write_wav(str(out / f"ai_{i:02d}.wav"), ulaw_decode(bytes(pb.audio)), 8000)
    result.update({"scenario": scenario["id"], "checks_for_reviewer": scenario["checks"], "model": args.model,
                   "path": "openai_api_direct", "timed_out": timed_out, "stopped_by_budget": budget_stop,
                   "ledger_totals": ledger.totals()})
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"wrote {out}/result.json")
    return 3 if budget_stop else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scenario", required=True)
    p.add_argument("--voice", default="marin", choices=["marin", "cedar"])
    p.add_argument("--model", default="gpt-realtime-2.1")
    p.add_argument("--caller-audio-dir", default="prototype/scenarios/caller_audio")
    p.add_argument("--out")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
