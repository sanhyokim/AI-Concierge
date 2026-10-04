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
from .realtime_runner import RealtimeRunner, WebSocketTransport

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
    out = pathlib.Path(args.out or f"prototype/results/dialog-{args.scenario}-{args.voice}-"
                                  f"{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}")
    out.mkdir(parents=True, exist_ok=True)
    transport = await WebSocketTransport(f"wss://api.openai.com/v1/realtime?model={args.model}", key).connect()
    try:
        runner = RealtimeRunner(transport, args.voice)
        result = await runner.run(scenario["turns"], lambda turn: load_caller_audio(audio_dir / turn["audio"]))
        for i, pb in enumerate(runner.playbacks, 1):
            write_wav(str(out / f"ai_{i:02d}.wav"), ulaw_decode(bytes(pb.audio)), 8000)
    finally:
        await transport.close()
    result.update({"scenario": scenario["id"], "checks_for_reviewer": scenario["checks"], "model": args.model})
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"wrote {out}/result.json")
    return 0


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
