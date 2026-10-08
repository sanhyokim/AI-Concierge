"""Measure an N/D pair from hand-filled label files (used for plan A 'instr' renders).

python3 -m prototype.measure.compare_labels N.labels.json D.labels.json
Each label file: {"wav": "...wav", "segments": [{"name", "start_s", "end_s", "morae"}, ...]}
"""
from __future__ import annotations

import json
import pathlib
import sys

from .audio import read_wav
from .rate import compare_versions, measure_version


def _measure(label_path: str) -> dict:
    label = json.loads(pathlib.Path(label_path).read_text(encoding="utf-8"))
    if any(s["start_s"] is None or s["end_s"] is None for s in label["segments"]):
        raise SystemExit(f"{label_path}: fill start_s/end_s for every segment first")
    wav = pathlib.Path(label_path).parent / label["wav"]
    samples, rate = read_wav(str(wav))
    return measure_version(samples, rate, label["segments"])


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    n, d = _measure(sys.argv[1]), _measure(sys.argv[2])
    print(json.dumps({"N": n, "D": d, "compare": compare_versions(n, d)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
