"""Offline measurements that need no vendor account.

1. Scenario checks: confirmation (R-03, I-02, guard) and call-flow (X-01..X-06) cases from
   scenarios/first_round.json, run against the deterministic modules.
2. Measurement-tool validation: synthetic signals with known articulation rate and pauses
   (passed through G.711 mu-law), measured with measure/rate.py, compared with the truth.
   This validates the tool only; it measures no voice.
3. SSML checks for the S scripts (N and D versions).

Writes prototype/results/offline-<UTC timestamp>.json and .md.
Usage: python3 -m prototype.run_offline
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import platform
import statistics
import sys
import xml.etree.ElementTree as ET

from .concierge.call_flow import CallFlow
from .concierge.confirmation import FieldState
from .concierge.readings import count_morae
from .concierge.ssml import segment_ssml
from .measure.rate import compare_versions, measure_version
from .measure.synthetic import RATE, build

HERE = pathlib.Path(__file__).resolve().parent
TOL_ARTICULATION_REL = 0.05   # proposal for tool accuracy
TOL_PAUSE_TOTAL_S = 0.020


def _action_key(action: tuple) -> str:
    return ":".join(str(a) for a in action if not isinstance(a, dict))


def run_confirmation(case: dict) -> list[dict]:
    f = FieldState(case["field"])
    results, accepted = [], {}
    for ev in case["events"]:
        if ev["type"] == "hear":
            f.hear(ev["value"])
        elif ev["type"] == "read_back":
            f.read_back(ev["value"])
        elif ev["type"] == "answer":
            f.answer(ev["text"])
        elif ev["type"] == "model_confirm":
            accepted[ev["n"]] = f.confirm(ev["value"], ev["last_caller_utterance"])[0]
        for exp in case["expected_states"]:
            if exp["after"] == ev["n"]:
                ok = f.value == exp["value"] and f.status.value == exp["status"]
                if "accepted" in exp:
                    ok = ok and accepted.get(ev["n"]) == exp["accepted"]
                results.append({"check": f"after event {ev['n']}: {exp['status']}", "ok": ok,
                                "actual": {"value": f.value, "status": f.status.value,
                                           "accepted": accepted.get(ev["n"])}})
    return results


def run_call_flow(case: dict) -> list[dict]:
    init = case.get("init", {})
    flow = CallFlow(caller_id=init.get("caller_id"), voicemail_enabled=init.get("voicemail_enabled", False))
    actions: list[tuple] = []
    refusal_index = None
    for ev in case["events"]:
        if ev["call"] in ("on_ai_refused",) or (ev["call"] == "on_human_request_answer" and ev.get("args") == [False]):
            refusal_index = len(actions)
        actions += getattr(flow, ev["call"])(*ev.get("args", []))
    keys = [_action_key(a) for a in actions]
    exp, results = case["expect"], []
    for want in exp.get("actions_include", []):
        results.append({"check": f"includes {want}", "ok": any(k == want or k.startswith(want + ":") for k in keys)})
    for bad in exp.get("actions_exclude", []):
        results.append({"check": f"excludes {bad}", "ok": not any(k.startswith(bad) for k in keys)})
    for key, value in exp.get("consent", {}).items():
        results.append({"check": f"consent.{key} == {value}", "ok": getattr(flow.consent, key) == value})
    for attr in ("state", "intake_mode"):
        if attr in exp:
            results.append({"check": f"{attr} == {exp[attr]}", "ok": getattr(flow, attr) == exp[attr]})
    for want, n in exp.get("count", {}).items():
        results.append({"check": f"count {want} == {n}", "ok": keys.count(want) == n})
    if "voicemail_allowed" in exp:
        results.append({"check": f"voicemail allowed == {exp['voicemail_allowed']}",
                        "ok": flow.can_use_voicemail() == exp["voicemail_allowed"]})
    for bad in exp.get("after_refusal_no", []):
        after = keys[refusal_index:] if refusal_index is not None else []
        results.append({"check": f"no {bad} after refusal", "ok": not any(k.startswith(bad) for k in after)})
    return results


def _segments(target_mora_s, target_pause_s, post_mora_s=0.12):
    return [
        {"name": "pre", "morae": 16, "mora_s": 0.12, "gap_s": 0.01},
        {"name": "target", "morae": 18, "mora_s": target_mora_s, "gap_s": 0.01,
         "pauses": [(5, target_pause_s), (11, target_pause_s)]},
        {"name": "post", "morae": 16, "mora_s": post_mora_s, "gap_s": 0.01},
    ]


VALIDATION_CASES = [
    # name, D-version segments, expected verdicts
    ("articulation_slowed", _segments(0.16, 0.2), {"articulation_slowed": True, "returned_to_normal": True,
                                                    "pause_only_slowdown": False}),
    ("pauses_only", _segments(0.12, 0.6), {"articulation_slowed": False, "pause_only_slowdown": True}),
    ("slowed_with_longer_pauses", _segments(0.16, 0.35), {"articulation_slowed": True, "returned_to_normal": True}),
    ("no_return_to_normal", _segments(0.16, 0.2, post_mora_s=0.16), {"articulation_slowed": True,
                                                                     "returned_to_normal": False}),
]
SEEDS = range(1, 6)


def validate_measurement() -> dict:
    errors_art, errors_pause, count_mismatch, verdicts = [], [], 0, []
    for name, d_segs, expected in VALIDATION_CASES:
        for seed in SEEDS:
            n_samples, n_bounds, n_truth = build(_segments(0.12, 0.2), seed=seed)
            d_samples, d_bounds, d_truth = build(d_segs, seed=seed + 100)
            n_meas = measure_version(n_samples, RATE, n_bounds)
            d_meas = measure_version(d_samples, RATE, d_bounds)
            for meas, truth in ((n_meas, n_truth), (d_meas, d_truth)):
                for seg, t in truth.items():
                    m = meas["segments"][seg]
                    errors_art.append(abs(m["articulation_rate"] - t["articulation_rate"]) / t["articulation_rate"])
                    errors_pause.append(abs(m["pause_total_s"] - t["pause_total_s"]))
                    count_mismatch += m["pause_count"] != t["pause_count"]
            cmp = compare_versions(n_meas, d_meas)
            ok = all(cmp[k] == v for k, v in expected.items())
            verdicts.append({"case": name, "seed": seed, "ok": ok,
                             "articulation_ratio": cmp["articulation_ratio"], "return_ratio": cmp["return_ratio"],
                             "pause_increase_s": cmp["pause_increase_s"]})
    return {
        "segments_measured": len(errors_art),
        "articulation_rel_error": {"mean": round(statistics.mean(errors_art), 4), "max": round(max(errors_art), 4)},
        "pause_total_abs_error_s": {"mean": round(statistics.mean(errors_pause), 4),
                                    "max": round(max(errors_pause), 4)},
        "pause_count_mismatches": count_mismatch,
        "within_tolerance": max(errors_art) <= TOL_ARTICULATION_REL and max(errors_pause) <= TOL_PAUSE_TOTAL_S
        and count_mismatch == 0,
        "verdict_cases": verdicts,
        "verdicts_ok": sum(v["ok"] for v in verdicts),
        "verdicts_total": len(verdicts),
        "tolerance": {"articulation_rel": TOL_ARTICULATION_REL, "pause_total_s": TOL_PAUSE_TOTAL_S},
    }


def check_ssml(scripts: list[dict]) -> list[dict]:
    out = []
    for s in scripts:
        for version, rate in (("N", None), ("D", "80%")):
            ssml = segment_ssml(s["pre"]["text"], s["target"]["text"], s["post"]["text"], rate)
            root = ET.fromstring(ssml)
            marks = [m.get("name") for m in root.iter("mark")]
            prosody = root.find("prosody")
            ok = marks == ["target_start", "target_end"] and ((prosody is None) == (rate is None))
            out.append({"script": s["id"], "version": version, "ok": ok,
                        "morae": {k: count_morae(s[k]["reading"]) for k in ("pre", "target", "post")}})
    return out


def main() -> int:
    data = json.loads((HERE / "scenarios" / "first_round.json").read_text(encoding="utf-8"))
    scenario_results = []
    for case in data["offline"]:
        runner = run_confirmation if case["kind"] == "confirmation" else run_call_flow
        checks = runner(case)
        scenario_results.append({"id": case["id"], "kind": case["kind"], "checks": checks,
                                 "ok": all(c["ok"] for c in checks)})
    validation = validate_measurement()
    ssml = check_ssml(data["slowdown"])
    now = dt.datetime.now(dt.timezone.utc)
    result = {
        "run_at_utc": now.isoformat(timespec="seconds"),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "vendors_contacted": "none"},
        "scenario_checks": scenario_results,
        "measurement_tool_validation": validation,
        "ssml_checks": ssml,
    }
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stem = out_dir / f"offline-{now:%Y%m%dT%H%M%SZ}"
    stem.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    n_checks = sum(len(s["checks"]) for s in scenario_results)
    n_ok = sum(c["ok"] for s in scenario_results for c in s["checks"])
    lines = [f"# Offline results {result['run_at_utc']}", "",
             f"- Environment: Python {result['environment']['python']}; vendors contacted: none",
             f"- Scenario checks: {n_ok}/{n_checks} passed across {len(scenario_results)} cases",
             "", "| case | checks passed |", "| --- | --- |"]
    lines += [f"| {s['id']} | {sum(c['ok'] for c in s['checks'])}/{len(s['checks'])} |" for s in scenario_results]
    v = validation
    lines += ["", "## Measurement tool validation (synthetic signals, not voices)", "",
              f"- Segments measured: {v['segments_measured']}",
              f"- Articulation rate relative error: mean {v['articulation_rel_error']['mean']}, "
              f"max {v['articulation_rel_error']['max']} (tolerance {TOL_ARTICULATION_REL})",
              f"- Pause total absolute error: mean {v['pause_total_abs_error_s']['mean']} s, "
              f"max {v['pause_total_abs_error_s']['max']} s (tolerance {TOL_PAUSE_TOTAL_S} s)",
              f"- Pause count mismatches: {v['pause_count_mismatches']}",
              f"- Verdict cases correct: {v['verdicts_ok']}/{v['verdicts_total']}",
              "", f"## SSML checks: {sum(c['ok'] for c in ssml)}/{len(ssml)} passed"]
    stem.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    all_ok = n_ok == n_checks and v["within_tolerance"] and v["verdicts_ok"] == v["verdicts_total"] \
        and all(c["ok"] for c in ssml)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
