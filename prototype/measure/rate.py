"""Speech-rate measurement that separates articulation rate from pauses.

For each segment (pre / target / post):
- span          first to last voiced frame inside the segment bounds
- pauses        silent runs of at least ``min_pause_ms`` inside the span
- articulation  morae / (span - pause total)   [morae per second]
- speech rate   morae / span                    [morae per second, pauses included]

Comparing the normal version (N) with the slowed version (D) of the same text:
- articulation_ratio = D.target.articulation / N.target.articulation
- pause_increase_s   = D.target.pause_total - N.target.pause_total
- return_ratio       = D.post.articulation / N.post.articulation
Pauses alone never make a slow-down pass (eval scenarios v1.1, section S). The return ratio must
fall inside a band: below it the voice stayed slow, above it the voice rushed to catch up.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .audio import frame_db, voiced_threshold_db

FRAME_MS = 5  # 10 ms frames exceeded the pause-total tolerance in validation (max 26 ms)
DEFAULT_MIN_PAUSE_MS = 150
PASS_ARTICULATION_RATIO = 0.85   # proposal, not agreed
PASS_RETURN_RATIO = 0.95         # proposal, not agreed (spec Q-08)
PASS_RETURN_RATIO_MAX = 1.10     # proposal, not agreed (spec Q-08): faster than this after the slow part fails


@dataclass
class SegmentMeasure:
    name: str
    morae: int
    span_s: float
    pause_total_s: float
    pause_count: int
    articulation_rate: float
    speech_rate: float


def voiced_mask(samples, rate, threshold_db=None) -> tuple[list[bool], float]:
    levels = frame_db(samples, rate, FRAME_MS)
    thr = voiced_threshold_db(levels) if threshold_db is None else threshold_db
    return [lv >= thr for lv in levels], thr


EDGE_BLIP_MS = 30  # edge fragment this short (total extent), cut off by a pause = spill-over from the neighbour


def _voiced_runs(mask: list[bool], lo: int, hi: int) -> list[list[int]]:
    runs, cur = [], None
    for i in range(lo, hi):
        if mask[i]:
            if cur is None:
                cur = [i, i]
            else:
                cur[1] = i
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    return runs


def measure_segment(mask: list[bool], name: str, start_s: float, end_s: float, morae: int,
                    min_pause_ms: int = DEFAULT_MIN_PAUSE_MS, details: dict | None = None) -> SegmentMeasure:
    """details, when given, receives the removed edge blips, the pause intervals and the span (seconds)."""
    frame_s = FRAME_MS / 1000
    lo, hi = int(round(start_s / frame_s)), min(len(mask), int(round(end_s / frame_s)))
    min_run = max(1, int(round(min_pause_ms / FRAME_MS)))
    blip = max(1, int(round(EDGE_BLIP_MS / FRAME_MS)))
    # Segment bounds (speech marks, labels) rarely fall exactly between sounds; drop a short
    # fragment at either edge when a pause separates it from the rest of the segment. A phrase
    # tail often breaks into several tiny runs (real recordings, v1 check), so the fragment is a
    # group of runs with no pause inside, judged by its total extent.
    groups: list[list[list[int]]] = []
    for r in _voiced_runs(mask, lo, hi):
        if groups and r[0] - groups[-1][-1][1] - 1 < min_run:
            groups[-1].append(r)
        else:
            groups.append([r])
    extent = lambda g: g[-1][1] - g[0][0] + 1
    removed = []
    while len(groups) > 1 and extent(groups[0]) <= blip:
        g = groups.pop(0)
        removed.append((g[0][0], g[-1][1]))
    while len(groups) > 1 and extent(groups[-1]) <= blip:
        g = groups.pop()
        removed.append((g[0][0], g[-1][1]))
    runs = [r for g in groups for r in g]
    if details is not None:
        details["removed"] = [(r[0] * frame_s, (r[1] + 1) * frame_s) for r in removed]
        details["pauses"], details["span"] = [], None
    if not runs:
        return SegmentMeasure(name, morae, 0.0, 0.0, 0, 0.0, 0.0)
    first, last = runs[0][0], runs[-1][1]
    span_frames = last - first + 1
    pauses, run = [], 0
    for i in range(first, last + 1):
        if not mask[i]:
            run += 1
        else:
            if run >= min_run:
                pauses.append(run)
                if details is not None:
                    details["pauses"].append(((i - run) * frame_s, i * frame_s))
            run = 0
    if details is not None:
        details["span"] = (first * frame_s, (last + 1) * frame_s)
    pause_frames = sum(pauses)
    span_s = span_frames * frame_s
    speaking_s = (span_frames - pause_frames) * frame_s
    return SegmentMeasure(name, morae, round(span_s, 3), round(pause_frames * frame_s, 3), len(pauses),
                          round(morae / speaking_s, 3) if speaking_s else 0.0,
                          round(morae / span_s, 3) if span_s else 0.0)


def measure_version(samples, rate, segments: list[dict], min_pause_ms: int = DEFAULT_MIN_PAUSE_MS,
                    threshold_db: float | None = None) -> dict:
    """segments: [{"name": "pre"|"target"|"post", "start_s": ..., "end_s": ..., "morae": ...}]."""
    mask, thr = voiced_mask(samples, rate, threshold_db)
    out = {"threshold_db": round(thr, 1), "min_pause_ms": min_pause_ms, "segments": {}}
    for seg in segments:
        m = measure_segment(mask, seg["name"], seg["start_s"], seg["end_s"], seg["morae"], min_pause_ms)
        out["segments"][seg["name"]] = asdict(m)
    return out


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 3) if b else None


def compare_versions(normal: dict, slowed: dict) -> dict:
    n, d = normal["segments"], slowed["segments"]
    art_ratio = _ratio(d["target"]["articulation_rate"], n["target"]["articulation_rate"])
    ret_ratio = _ratio(d["post"]["articulation_rate"], n["post"]["articulation_rate"]) if "post" in n else None
    pause_inc = round(d["target"]["pause_total_s"] - n["target"]["pause_total_s"], 3)
    speech_ratio = _ratio(d["target"]["speech_rate"], n["target"]["speech_rate"])
    return {
        "articulation_ratio": art_ratio,
        "speech_rate_ratio": speech_ratio,
        "pause_increase_s": pause_inc,
        "return_ratio": ret_ratio,
        "articulation_slowed": art_ratio is not None and art_ratio <= PASS_ARTICULATION_RATIO,
        "returned_to_normal": ret_ratio is not None and PASS_RETURN_RATIO <= ret_ratio <= PASS_RETURN_RATIO_MAX,
        "post_too_fast": ret_ratio is not None and ret_ratio > PASS_RETURN_RATIO_MAX,
        "pause_only_slowdown": (art_ratio is not None and art_ratio > PASS_ARTICULATION_RATIO
                                and speech_ratio is not None and speech_ratio <= PASS_ARTICULATION_RATIO),
        "note": "listening evaluation is still required; these numbers alone do not pass the scenario",
    }
