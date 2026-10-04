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

SCENARIOS = pathlib.Path(__file__).resolve().parents[1] / "scenarios" / "first_round.json"
VOICES = {"Kazuha-Neural": ("Kazuha", "neural"), "Takumi-Neural": ("Takumi", "neural")}


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
    return boto3.client("polly", region_name=region, aws_access_key_id=ak, aws_secret_access_key=sk)


def render(client, relay_voice: str, script: dict, rate: str | None, out: pathlib.Path, version: str) -> dict:
    voice_id, engine = VOICES[relay_voice]
    ssml = segment_ssml(script["pre"]["text"], script["target"]["text"], script["post"]["text"], rate)
    common = dict(Text=ssml, TextType="ssml", VoiceId=voice_id, Engine=engine, LanguageCode="ja-JP")
    audio = client.synthesize_speech(OutputFormat="pcm", SampleRate="8000", **common)["AudioStream"].read()
    marks_raw = client.synthesize_speech(OutputFormat="json", SpeechMarkTypes=["ssml"], **common)["AudioStream"].read()
    marks = {m["value"]: m["time"] / 1000 for m in (json.loads(line) for line in marks_raw.decode().splitlines() if line)}
    samples, _ = to_phone_band(pcm16_from_bytes(audio), 8000)
    end_s = len(samples) / 8000
    segments = [
        {"name": "pre", "start_s": 0.0, "end_s": marks["target_start"], "morae": count_morae(script["pre"]["reading"])},
        {"name": "target", "start_s": marks["target_start"], "end_s": marks["target_end"],
         "morae": count_morae(script["target"]["reading"])},
        {"name": "post", "start_s": marks["target_end"], "end_s": end_s, "morae": count_morae(script["post"]["reading"])},
    ]
    stem = out / f"B-{relay_voice}-{script['id']}-{version}"
    write_wav(f"{stem}.wav", samples, 8000)
    return {"wav": f"{stem}.wav", "ssml": ssml, "marks": marks, "segments": segments,
            "measure": measure_version(samples, 8000, segments)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--voices", nargs="+", default=list(VOICES))
    p.add_argument("--scripts", nargs="+", default=["S-01", "S-02", "S-03"])
    p.add_argument("--rate", help="prosody rate for D, e.g. 80%% (default from scenarios)")
    p.add_argument("--out")
    args = p.parse_args()
    client = _client()
    if client is None:
        return 2
    data = json.loads(SCENARIOS.read_text())
    rate = args.rate or data["slow_settings"]["polly_prosody_rate"]
    out = pathlib.Path(args.out or f"prototype/results/probe-b-{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}")
    out.mkdir(parents=True, exist_ok=True)
    report = {"plan": "B", "path": "polly_direct_not_via_conversationrelay", "rate": rate, "runs": []}
    for voice in args.voices:
        for script in (s for s in data["slowdown"] if s["id"] in args.scripts):
            n = render(client, voice, script, None, out, "N")
            d = render(client, voice, script, rate, out, "D")
            report["runs"].append({"voice": voice, "script": script["id"], "N": n, "D": d,
                                   "compare": compare_versions(n["measure"], d["measure"])})
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"wrote {out}/report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
