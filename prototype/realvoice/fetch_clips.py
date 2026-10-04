"""Fetch the public-domain clips, cut excerpts and render spectrogram windows for labelling.

python3 -m prototype.realvoice.fetch_clips
Downloads go to prototype/realvoice/work/ (not committed). The sha256 of each downloaded
MP3 is written to clips.lock.json so a later run can confirm it used the same audio.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

from ..measure.audio import read_wav, to_phone_band, write_wav

HERE = pathlib.Path(__file__).resolve().parent
WORK = HERE / "work"
WINDOW_S = 2.5


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def fetch(clip: dict) -> pathlib.Path:
    mp3 = WORK / clip["file"]
    if not mp3.exists():
        url = f"https://archive.org/download/{clip['archive_id']}/{clip['file']}"
        _run(["curl", "-sSL", "--max-time", "300", "-o", str(mp3), url])
    return mp3


def main() -> int:
    WORK.mkdir(exist_ok=True)
    cfg = json.loads((HERE / "clips.json").read_text())
    lock = {}
    for clip in cfg["clips"]:
        mp3 = fetch(clip)
        lock[clip["id"]] = {"file": clip["file"], "sha256": hashlib.sha256(mp3.read_bytes()).hexdigest()}
        wav16 = WORK / f"{clip['id']}-16k.wav"
        _run(["ffmpeg", "-y", "-ss", str(clip["offset_s"]), "-t", str(clip["duration_s"]), "-i", str(mp3),
              "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(wav16)])
        samples, rate = read_wav(str(wav16))
        phone, prate = to_phone_band(samples, rate)
        write_wav(str(WORK / f"{clip['id']}-phone8k.wav"), phone, prate)
        n = int(clip["duration_s"] / WINDOW_S + 0.999)
        for k in range(n):
            t0 = k * WINDOW_S
            png = WORK / f"{clip['id']}-w{k:02d}-{t0:05.2f}s.png"
            _run(["ffmpeg", "-y", "-ss", str(t0), "-t", str(WINDOW_S), "-i", str(wav16), "-lavfi",
                  "showspectrumpic=s=1500x300:legend=1:scale=log:fscale=lin:stop=5000", str(png)])
    (HERE / "clips.lock.json").write_text(json.dumps(lock, ensure_ascii=False, indent=2))
    print(f"wrote clips and windows to {WORK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
