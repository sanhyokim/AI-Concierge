"""Compare the speech-rate tool with AI-provisional labels on public human recordings.

python3 -m prototype.realvoice.compare [--labels ai-provisional|human-verified]   (run fetch_clips first)

The labels are a provisional reference made by an AI looking at spectrograms; they are
not human-verified ground truth. Results check the measurement method only and are not
used to choose a candidate voice. Path: public_human_recording (phone codec simulated).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

from ..measure.audio import read_wav
from ..measure.rate import DEFAULT_MIN_PAUSE_MS, EDGE_BLIP_MS, FRAME_MS, measure_segment, voiced_mask

HERE = pathlib.Path(__file__).resolve().parent
WORK, LABELS = HERE / "work", HERE / "labels"
RESULTS = HERE.parent / "results"
SWEEP_MS = (0, 10, 20, 30, 40, 50)
WINDOW_S = 2.5  # spectrogram window used for labelling (fetch_clips.WINDOW_S)


def read_labels(path: pathlib.Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        start, end, label = line.split("\t")[:3]
        rows.append({"start": float(start), "end": float(end), "label": label.strip()})
    return rows


def label_pauses(speech: list[dict], min_pause_s: float) -> list[tuple[float, float]]:
    return [(a["end"], b["start"]) for a, b in zip(speech, speech[1:]) if b["start"] - a["end"] >= min_pause_s]


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def compare_pauses(tool: list, ref: list) -> dict:
    matched, used = [], set()
    for r in ref:
        best = max(range(len(tool)), key=lambda i: overlap(r, tool[i]), default=None)
        if best is not None and best not in used and overlap(r, tool[best]) >= 0.5 * (r[1] - r[0]):
            used.add(best)
            matched.append({"ref": r, "tool": tool[best], "start_err_ms": round((tool[best][0] - r[0]) * 1000),
                            "end_err_ms": round((tool[best][1] - r[1]) * 1000)})
    return {"ref_count": len(ref), "tool_count": len(tool), "matched": len(matched),
            "missed": [r for r in ref if not any(m["ref"] == r for m in matched)],
            "extra": [tool[i] for i in range(len(tool)) if i not in used], "pairs": matched}


def isolated_short_runs(mask: list[bool], min_pause_frames: int, max_frames: int) -> list[tuple[float, float]]:
    """Voiced groups (runs with no pause inside) whose extent is at most max_frames, with at least
    min_pause_frames of silence on both sides: the only material the edge rule could ever remove."""
    runs, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            if runs and i - runs[-1][1] - 1 < min_pause_frames:
                runs[-1] = (runs[-1][0], j)
            else:
                runs.append((i, j))
            i = j + 1
        else:
            i += 1
    out = []
    for k, (s, e) in enumerate(runs):
        before = s - (runs[k - 1][1] + 1) if k else s
        after = (runs[k + 1][0] - e - 1) if k + 1 < len(runs) else n - e - 1
        if e - s + 1 <= max_frames and before >= min_pause_frames and after >= min_pause_frames:
            out.append((s * FRAME_MS / 1000, (e + 1) * FRAME_MS / 1000))
    return out


def classify(interval: tuple[float, float], labels: list[dict]) -> str:
    mid = (interval[0] + interval[1]) / 2
    for lab in labels:
        if lab["start"] - 0.03 <= mid <= lab["end"] + 0.03:
            return lab["label"]
    return "unlabelled"


def sweep_label_edges(mask: list[bool], speech: list[dict], pauses: list[tuple[float, float]]) -> dict:
    """Shift a segment start around each labelled phrase onset and record what the blip rule removes.

    Caveat: the AI-provisional labels sit late by a window-position-dependent offset (see
    label_offset), so "spill kept" here mostly reflects that offset, not the rule.

    start = onset - d (d ms of the previous phrase's tail inside the segment, then the pause)
      -> removing it is correct spill-over handling while d <= EDGE_BLIP_MS.
    start = onset + d (segment starts inside the phrase)
      -> nothing should be removed; a removal inside the phrase would cut real speech.
    """
    stats = {"spill_removed": 0, "spill_kept": 0, "inside_removed_real_speech": 0, "inside_ok": 0,
             "spill_kept_by_shift_ms": {}, "cases": []}
    for k in range(1, len(speech)):
        prev, cur = speech[k - 1], speech[k]
        if cur["start"] - prev["end"] < DEFAULT_MIN_PAUSE_MS / 1000:
            continue
        seg_end = cur["end"]
        for d in SWEEP_MS:
            for side in ("spill", "inside"):
                if side == "spill" and d == 0:
                    continue
                start = prev["end"] - d / 1000 if side == "spill" else cur["start"] + d / 1000
                details: dict = {}
                measure_segment(mask, "x", start, seg_end, 1, details=details)
                removed = details.get("removed", [])
                span = details.get("span")
                if side == "spill":
                    began_at_phrase = span is not None and span[0] >= cur["start"] - 0.06
                    key = "spill_removed" if began_at_phrase else "spill_kept"
                    stats[key] += 1
                    if not began_at_phrase:
                        stats["spill_kept_by_shift_ms"][d] = stats["spill_kept_by_shift_ms"].get(d, 0) + 1
                else:
                    bad = [r for r in removed if r[0] >= cur["start"] - 0.03 and r[1] <= cur["end"] + 0.03]
                    stats["inside_removed_real_speech" if bad else "inside_ok"] += 1
                stats["cases"].append({"onset": cur["start"], "side": side, "shift_ms": d,
                                       "removed": removed, "span": span})
    return stats


def _first_phrase(mask: list[bool], start: float, end: float) -> tuple[float, float] | None:
    """Voiced extent from start up to the first silence of at least the minimum pause."""
    details: dict = {}
    measure_segment(mask, "x", start, end, 1, details=details)
    span = details.get("span")
    if span is None:
        return None
    first_pause = details["pauses"][0] if details["pauses"] else None
    return (span[0], first_pause[0] if first_pause else span[1])


def sweep_tool_edges(mask: list[bool], labels: list[dict], pauses: list[tuple[float, float]],
                     span: tuple[float, float]) -> dict:
    """Place segment bounds relative to the tool's own pause edges, so label offsets do not matter.

    For every detected pause (a, b) and shift d:
      spill  start = a - d  (the segment begins with d ms of the previous phrase's tail)
             end   = b + d  (the segment ends with d ms of the next phrase's head)
             -> the rule should drop the fragment while d <= EDGE_BLIP_MS; beyond that it is kept
                by design, and the measured span grows by d plus the pause.
      inside start = b + d  (the segment starts inside the next phrase)
             end   = a - d  (the segment ends inside the previous phrase)
             -> nothing should be dropped; a drop here cuts real speech.
    The other bound is the clip's voiced span, so each segment crosses later pauses as a real
    target segment can.
    """
    stats = {"spill": {}, "inside": {}, "spill_kept_reasons": {}, "inside_removed": [], "cases": 0}
    blip_s = EDGE_BLIP_MS / 1000
    for a, b in pauses:
        for d in SWEEP_MS:
            ds = d / 1000
            for side, edge, start, end in (("spill", "start", a - ds, span[1]), ("spill", "end", span[0], b + ds),
                                           ("inside", "start", b + ds, span[1]), ("inside", "end", span[0], a - ds)):
                if side == "spill" and d == 0:
                    continue
                details: dict = {}
                measure_segment(mask, "x", start, end, 1, details=details)
                removed = details.get("removed", [])
                stats["cases"] += 1
                row = stats[side].setdefault(str(d), {"removed": 0, "kept": 0})
                if side == "spill":
                    # removed if the measured span no longer reaches into the neighbouring phrase
                    got = details.get("span")
                    dropped = got is not None and (got[0] >= b - 1e-9 if edge == "start" else got[1] <= a + 1e-9)
                    row["removed" if dropped else "kept"] += 1
                    if not dropped and d <= EDGE_BLIP_MS:
                        tail = (a - ds, a) if edge == "start" else (b, b + ds)
                        runs = isolated_runs_in(mask, *tail)
                        reason = "fragmented" if len(runs) > 1 else "other"
                        stats["spill_kept_reasons"][reason] = stats["spill_kept_reasons"].get(reason, 0) + 1
                else:
                    row["removed" if removed else "kept"] += 1
                    for r in removed:
                        stats["inside_removed"].append({"edge": edge, "pause": (a, b), "shift_ms": d, "interval": r,
                                                        "label": classify(r, labels),
                                                        "phrase": _first_phrase(mask, b, span[1]) if edge == "start"
                                                        else None})
    stats["expected"] = {"spill_removed_if_shift_ms_at_most": EDGE_BLIP_MS, "inside_removed": 0,
                         "blip_s": blip_s}
    return stats


def isolated_runs_in(mask: list[bool], start: float, end: float) -> list[tuple[float, float]]:
    """Voiced runs inside [start, end) (no isolation condition; used to explain a kept spill)."""
    frame_s = FRAME_MS / 1000
    lo, hi = int(round(start / frame_s)), min(len(mask), int(round(end / frame_s)))
    runs, cur = [], None
    for i in range(lo, hi):
        if mask[i] and cur is None:
            cur = i
        elif not mask[i] and cur is not None:
            runs.append((cur * frame_s, i * frame_s))
            cur = None
    if cur is not None:
        runs.append((cur * frame_s, hi * frame_s))
    return runs


def label_offset(pairs: list[dict]) -> dict:
    """Boundary error (tool - label) against the boundary's position inside its labelling window.

    A slope that depends on the position inside the 2.5 s image window cannot come from the tool,
    which never sees the windows; it points to a scale error in reading times off the images.
    """
    pts = []
    for p in pairs:
        for k, t in ((0, p["ref"][0]), (1, p["ref"][1])):
            err = p["start_err_ms"] if k == 0 else p["end_err_ms"]
            pts.append((t % WINDOW_S, err))
    n = len(pts)
    if n < 3:
        return {"n": n}
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    syy = sum((y - my) ** 2 for _, y in pts)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    slope = sxy / sxx if sxx else 0.0
    errs = sorted(abs(y) for _, y in pts)
    return {"n": n, "mean_err_ms": round(my, 1), "median_abs_err_ms": errs[n // 2], "max_abs_err_ms": errs[-1],
            "slope_ms_per_s_of_window_position": round(slope, 1), "intercept_ms": round(my - slope * mx, 1),
            "corr": round(sxy / (sxx * syy) ** 0.5, 2) if sxx and syy else None}


LABEL_KINDS = {"ai-provisional": "ai-provisional (not human-verified ground truth)",
               "human-verified": "human-verified (labelled and checked by a person)"}


def run_clip(clip_id: str, kind: str = "ai-provisional") -> dict:
    samples, rate = read_wav(str(WORK / f"{clip_id}-phone8k.wav"))
    mask, thr = voiced_mask(samples, rate)
    labels = read_labels(LABELS / f"{clip_id}.{kind}.txt")
    speech = [lab for lab in labels if lab["label"] == "speech"]
    ref_pauses = label_pauses(speech, DEFAULT_MIN_PAUSE_MS / 1000)
    span_ref = (speech[0]["start"], speech[-1]["end"])
    details: dict = {}
    m = measure_segment(mask, "whole", 0.0, len(samples) / rate, 1, details=details)
    tool_pauses = [p for p in details["pauses"]]
    # speaking time within the labelled span: labels = sum of speech intervals; tool = span - pauses
    ref_speaking = sum(s["end"] - s["start"] for s in speech)
    tool_span = details["span"]
    tool_speaking = (tool_span[1] - tool_span[0]) - sum(b - a for a, b in tool_pauses)
    min_pause_frames = int(round(DEFAULT_MIN_PAUSE_MS / FRAME_MS))
    short = isolated_short_runs(mask, min_pause_frames, int(round(EDGE_BLIP_MS / FRAME_MS)))
    return {
        "clip": clip_id, "threshold_db": round(thr, 1),
        "span": {"ref": span_ref, "tool": tool_span},
        "removed_at_clip_edges": details["removed"],
        "pauses": compare_pauses(tool_pauses, ref_pauses),
        "speaking_time_s": {"ref": round(ref_speaking, 3), "tool": round(tool_speaking, 3),
                            "rel_diff": round((tool_speaking - ref_speaking) / ref_speaking, 4)},
        "isolated_short_runs": [{"interval": r, "label": classify(r, labels)} for r in short],
        "sweep_tool_edges": sweep_tool_edges(mask, labels, tool_pauses, tool_span),
        "sweep_label_edges": sweep_label_edges(mask, speech, ref_pauses),
        "measure_whole": {"pause_count": m.pause_count, "pause_total_s": m.pause_total_s, "span_s": m.span_s},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", choices=sorted(LABEL_KINDS), default="ai-provisional")
    kind = ap.parse_args().labels
    cfg = json.loads((HERE / "clips.json").read_text())
    missing = [c["id"] for c in cfg["clips"] if not (WORK / f"{c['id']}-phone8k.wav").exists()]
    if missing:
        print(f"run `python3 -m prototype.realvoice.fetch_clips` first (missing: {missing})", file=sys.stderr)
        return 2
    no_labels = [c["id"] for c in cfg["clips"] if not (LABELS / f"{c['id']}.{kind}.txt").exists()]
    if no_labels:
        print(f"no {kind} labels for {no_labels}", file=sys.stderr)
        return 2
    now = dt.datetime.now(dt.timezone.utc)
    clips = [run_clip(c["id"], kind) for c in cfg["clips"]]
    for c in clips:
        c["label_offset"] = label_offset(c["pauses"]["pairs"])
    result = {"run_at_utc": now.isoformat(timespec="seconds"), "path": cfg["path"], "path_note": cfg["path_note"],
              "labels": LABEL_KINDS[kind],
              "use": "measurement-method preliminary check only; not for choosing a candidate voice",
              "frame_ms": FRAME_MS, "min_pause_ms": DEFAULT_MIN_PAUSE_MS, "edge_blip_ms": EDGE_BLIP_MS,
              "label_offset_pooled": label_offset([p for c in clips for p in c["pauses"]["pairs"]]),
              "clips": clips}
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"realvoice-{now:%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    for c in clips:
        p, s, te, lo = c["pauses"], c["speaking_time_s"], c["sweep_tool_edges"], c["label_offset"]
        print(f"{c['clip']}: pauses ref {p['ref_count']} / tool {p['tool_count']} / matched {p['matched']}; "
              f"speaking time rel diff {s['rel_diff']:+.1%}; label offset mean {lo.get('mean_err_ms')} ms "
              f"slope {lo.get('slope_ms_per_s_of_window_position')} ms/s; edge removals {c['removed_at_clip_edges']}; "
              f"isolated short runs {[(r['interval'], r['label']) for r in c['isolated_short_runs']]}")
        print(f"  tool-edge sweep spill {te['spill']} kept reasons {te['spill_kept_reasons']}")
        print(f"  tool-edge sweep inside {te['inside']} removed {[(r['interval'], r['label'], r['shift_ms']) for r in te['inside_removed']]}")
    print(f"pooled label offset {result['label_offset_pooled']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
