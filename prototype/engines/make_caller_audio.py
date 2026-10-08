"""Synthesise the caller turns of the dialogue scenarios with a NON-candidate voice (Polly Mizuki, standard).

python3 -m prototype.engines.make_caller_audio [--scenarios C-01 R-03 ...]

Writes one 8 kHz WAV per caller turn into prototype/scenarios/caller_audio/ (not committed),
named as in first_round.json. Existing files are kept, so a rerun sends nothing new.
Mizuki is not one of the four candidates; it only plays the caller. Every request goes through
the same ledger and limits as polly_probe (vendor aws_polly, path polly_direct).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

from ..measure.audio import pcm16_from_bytes, write_wav
from .budget import LIMITS, UsageLedger
from .polly_probe import _client, _synthesize

SCENARIOS = pathlib.Path(__file__).resolve().parents[1] / "scenarios" / "first_round.json"
OUT = SCENARIOS.parent / "caller_audio"
CALLER_VOICE, CALLER_ENGINE = "Mizuki", "standard"


def make(client, scenarios: list[dict], out: pathlib.Path, ledger: UsageLedger, run_id: str) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for sc in scenarios:
        for turn in sc["turns"]:
            path = out / turn["audio"]
            if path.exists():
                continue
            audio = _synthesize(client, ledger, run_id, f"caller_audio:{sc['id']}:{turn['audio']}", CALLER_ENGINE,
                                Text=turn["text"], TextType="text", VoiceId=CALLER_VOICE, LanguageCode="ja-JP",
                                OutputFormat="pcm", SampleRate="8000")
            write_wav(str(path), pcm16_from_bytes(audio), 8000)
            written.append(str(path))
    return written


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scenarios", nargs="+", help="scenario ids (default: all dialogue scenarios)")
    args = p.parse_args()
    client = _client()
    if client is None:
        return 2
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))["dialog"]
    chosen = [s for s in data if not args.scenarios or s["id"] in args.scenarios]
    run_id = "caller-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ledger = UsageLedger("aws_polly", LIMITS["aws_polly"])
    written = make(client, chosen, OUT, ledger, run_id)
    print(f"wrote {len(written)} files to {OUT}; ledger totals {ledger.totals()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
