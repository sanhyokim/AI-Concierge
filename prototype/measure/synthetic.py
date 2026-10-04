"""Synthetic 'speech' with known timing, used only to validate the measurement tool.

Each mora is a harmonic tone burst; short gaps (< pause threshold) imitate
consonant closures, longer silences are pauses. The ground truth is computed
from the generation plan, so the measurement error can be stated exactly.
This is not a voice and says nothing about any TTS candidate.
"""
from __future__ import annotations

import math
import random
from array import array

from .audio import to_phone_band

RATE = 8000


def _burst(duration_s: float, amp: float, rng: random.Random) -> list[float]:
    n = int(duration_s * RATE)
    ramp = max(1, int(0.005 * RATE))
    f0 = rng.uniform(120, 180)
    out = []
    for i in range(n):
        env = min(1.0, i / ramp, (n - 1 - i) / ramp)
        t = i / RATE
        s = (math.sin(2 * math.pi * f0 * t) + 0.6 * math.sin(4 * math.pi * f0 * t)
             + 0.3 * math.sin(6 * math.pi * f0 * t))
        out.append(amp * env * s / 1.9)
    return out


def build(segments: list[dict], *, seed: int = 0, lead_s: float = 0.3, tail_s: float = 0.3,
          boundary_pause_s: float = 0.25) -> tuple[array, list[dict], dict]:
    """segments: [{"name", "morae", "mora_s", "gap_s", "pauses": [(after_mora_index, pause_s), ...]}].

    Returns (8 kHz mu-law round-tripped samples, segment bounds for the measurement, truth).
    """
    rng = random.Random(seed)
    # Durations are jittered and offset from the 10 ms analysis grid so that frame
    # quantisation errors show up in the validation instead of cancelling out.
    signal: list[float] = [0.0] * int((lead_s + rng.uniform(0.0, 0.0099)) * RATE)
    bounds, truth = [], {}
    for k, seg in enumerate(segments):
        if k:
            signal += [0.0] * int(boundary_pause_s * rng.uniform(0.9, 1.1) * RATE)
        start_s = len(signal) / RATE
        pauses = dict(seg.get("pauses", []))
        speaking_n = pause_n = 0
        first_voiced = None
        for m in range(seg["morae"]):
            if first_voiced is None:
                first_voiced = len(signal) / RATE
            burst = _burst(seg["mora_s"] * rng.uniform(0.85, 1.15), rng.uniform(0.15, 0.8) * 32767 * 0.5, rng)
            signal += burst
            speaking_n += len(burst)
            if m == seg["morae"] - 1:
                break
            if m in pauses:
                n = int(pauses[m] * rng.uniform(0.9, 1.1) * RATE)
                signal += [0.0] * n
                pause_n += n
            else:
                n = int(seg["gap_s"] * rng.uniform(0.7, 1.3) * RATE)
                signal += [0.0] * n
                speaking_n += n
        speaking_s, pause_total = speaking_n / RATE, pause_n / RATE
        end_s = len(signal) / RATE
        bounds.append({"name": seg["name"], "start_s": start_s, "end_s": end_s, "morae": seg["morae"]})
        truth[seg["name"]] = {
            "span_s": round(end_s - first_voiced, 3),
            "pause_total_s": round(pause_total, 3),
            "pause_count": len(pauses),
            "articulation_rate": round(seg["morae"] / speaking_s, 3),
        }
    signal += [0.0] * int(tail_s * RATE)
    noisy = [s + rng.gauss(0, 30) for s in signal]  # about -60 dBFS noise floor
    pcm = array("h", (max(-32768, min(32767, int(v))) for v in noisy))
    phone, _ = to_phone_band(pcm, RATE)
    # The boundary pause belongs to neither segment; extend bounds to cover it so marks behave like Polly's.
    for i in range(len(bounds) - 1):
        bounds[i + 1]["start_s"] = bounds[i]["end_s"]
    bounds[-1]["end_s"] = len(phone) / RATE
    return phone, bounds, truth
