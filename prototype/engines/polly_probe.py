"""Plan B partial slow-down probe with Amazon Polly called DIRECTLY (not via ConversationRelay).

Voices: Kazuha (neural) and Takumi (neural), i.e. Relay voice values Kazuha-Neural / Takumi-Neural.
For each script the same SSML text is rendered as N (no prosody) and D (target wrapped in
<prosody rate>), with <mark> tags around the target; speech marks give the segment bounds.
Audio is requested as 8 kHz PCM and passed through G.711 mu-law to imitate the phone codec.

Relay may behave differently (SSML handling, streaming); results here do not prove Relay support.

Credentials are read from dedicated variables so that unrelated AWS settings are never used:
  CONCIERGE_POLLY_ACCESS_KEY_ID, CONCIERGE_POLLY_SECRET_ACCESS_KEY, CONCIERGE_POLLY_REGION
Without them (or without boto3) the script exits before contacting anything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

from ..concierge.readings import count_morae
from ..concierge.ssml import segment_ssml
from ..measure.audio import pcm16_from_bytes, to_phone_band, write_wav
from ..measure.rate import compare_versions, measure_version
from .budget import LIMITS, UsageLedger, polly_cost, with_retries

SCENARIOS = pathlib.Path(__file__).resolve().parents[1] / "scenarios" / "first_round.json"
VOICES = {"Kazuha-Neural": ("Kazuha", "neural"), "Takumi-Neural": ("Takumi", "neural")}
PATH = "polly_direct"
PATH_NOTE = "Polly API called directly; not via ConversationRelay and not over a telephone network"


def _client():
    ak = os.environ.get("CONCIERGE_POLLY_ACCESS_KEY_ID")
    sk = os.environ.get("CONCIERGE_POLLY_SECRET_ACCESS_KEY")
    region = os.environ.get("CONCIERGE_POLLY_REGION", "ap-northeast-1")
    if not (ak and sk):
        print("CONCIERGE_POLLY_ACCESS_KEY_ID / CONCIERGE_POLLY_SECRET_ACCESS_KEY are not set; nothing was sent. "
              "See prototype/README.md.", file=sys.stderr)
        return None
    try:
        import boto3
    except ImportError:
        print("Install vendor dependencies first: pip install -r prototype/requirements-vendor.txt", file=sys.stderr)
        return None
    from botocore.config import Config
    lim = LIMITS["aws_polly"]
    cfg = Config(connect_timeout=10, read_timeout=lim.request_timeout_s,
                 retries={"max_attempts": 1 + lim.max_retries, "mode": "standard"})
    return boto3.client("polly", region_name=region, aws_access_key_id=ak, aws_secret_access_key=sk, config=cfg)


def _synthesize(client, ledger: UsageLedger, run_id: str, op: str, engine: str, **kwargs) -> bytes:
    """One Polly request: the estimate is reserved before sending and settled from the response.
    If the request fails after it may have been sent, the reservation stays held."""
    chars = len(kwargs["Text"])
    rid = ledger.reserve(op, PATH, polly_cost(chars, engine), run_id)
    resp = with_retries(lambda: client.synthesize_speech(Engine=engine, **kwargs), max_retries=0)  # botocore retries
    billed = int(resp.get("RequestCharacters", chars))
    data = resp["AudioStream"].read()
    audio_s = len(data) / 2 / 8000 if kwargs.get("OutputFormat") == "pcm" else 0.0
    ledger.record(op, PATH, polly_cost(billed, engine), run_id, audio_seconds=audio_s, reservation=rid,
                  characters=billed, reported_by_api="RequestCharacters" in resp)
    ledger.close(rid)
    return data


def render(client, relay_voice: str, script: dict, rate: str | None, out: pathlib.Path, version: str,
           ledger: UsageLedger, run_id: str) -> dict:
    voice_id, engine = VOICES[relay_voice]
    ssml = segment_ssml(script["pre"]["text"], script["target"]["text"], script["post"]["text"], rate)
    common = dict(Text=ssml, TextType="ssml", VoiceId=voice_id, LanguageCode="ja-JP")
    op = f"{script['id']}:{version}:{rate or 'normal'}"
    audio = _synthesize(client, ledger, run_id, f"audio:{op}", engine, OutputFormat="pcm", SampleRate="8000",
                        **common)
    marks_raw = _synthesize(client, ledger, run_id, f"marks:{op}", engine, OutputFormat="json",
                            SpeechMarkTypes=["ssml"], **common)
    marks = {m["value"]: m["time"] / 1000 for m in (json.loads(line) for line in marks_raw.decode().splitlines() if line)}
    samples, _ = to_phone_band(pcm16_from_bytes(audio), 8000)
    end_s = len(samples) / 8000
    segments = [
        {"name": "pre", "start_s": 0.0, "end_s": marks["target_start"], "morae": count_morae(script["pre"]["reading"])},
        {"name": "target", "start_s": marks["target_start"], "end_s": marks["target_end"],
         "morae": count_morae(script["target"]["reading"])},
        {"name": "post", "start_s": marks["target_end"], "end_s": end_s, "morae": count_morae(script["post"]["reading"])},
    ]
    stem = out / f"B-{relay_voice}-{script['id']}-{version}-{(rate or 'normal').replace('%', 'pct')}"
    write_wav(f"{stem}.wav", samples, 8000)
    return {"wav": f"{stem}.wav", "path": PATH, "ssml": ssml, "marks": marks, "segments": segments,
            "measure": measure_version(samples, 8000, segments)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--voices", nargs="+", default=list(VOICES))
    p.add_argument("--scripts", nargs="+", default=["S-01", "S-02", "S-03"])
    p.add_argument("--rates", nargs="+", default=["80%", "70%"], help="prosody rates for D versions")
    p.add_argument("--out")
    args = p.parse_args()
    client = _client()
    if client is None:
        return 2
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    run_id = f"probe-b-{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}"
    out = pathlib.Path(args.out or f"prototype/results/{run_id}")
    out.mkdir(parents=True, exist_ok=True)
    ledger = UsageLedger("aws_polly", LIMITS["aws_polly"])
    report = {"plan": "B", "path": PATH, "path_note": PATH_NOTE, "rates": args.rates, "runs": []}
    try:
        for voice in args.voices:
            for script in (s for s in data["slowdown"] if s["id"] in args.scripts):
                n = render(client, voice, script, None, out, "N", ledger, run_id)
                for rate in args.rates:
                    d = render(client, voice, script, rate, out, "D", ledger, run_id)
                    report["runs"].append({"voice": voice, "script": script["id"], "rate": rate, "N": n, "D": d,
                                           "compare": compare_versions(n["measure"], d["measure"])})
    finally:
        report["ledger_totals"] = ledger.totals()
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {out}/report.json; ledger totals: {report['ledger_totals']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
