"""Expected vendor cost of the minimal measurement plan (docs/measurement-plan-v1.md).

python3 -m prototype.cost.measurement_budget

Counts follow the probe scripts' defaults; per-request sizes are assumptions written next to
each figure (pessimistic where unsure). Prices come from prices.json (USD, tax excluded).
T1 already prices every prompt token as uncached; T3 is shown with and without prompt caching,
and includes input transcription (gpt-4o-transcribe) at the pessimistic per-turn bound.
The run-side limits in engines/budget.py are set to about twice these counts (one full rerun).
"""
from __future__ import annotations

import json
import pathlib

from ..engines.budget import transcription_upper_bound

PRICES = json.loads((pathlib.Path(__file__).resolve().parent / "prices.json").read_text())["items"]
SCENARIOS = json.loads((pathlib.Path(__file__).resolve().parents[1] / "scenarios" / "first_round.json").read_text())

VOICES_A, VOICES_B, SCRIPTS = 2, 2, len(SCENARIOS["slowdown"])
REPEAT_T1, REPEAT_T3, D_RATES_T2 = 2, 3, 2

# assumptions per request
RENDER_AUDIO_S = 11.0          # one read-aloud of an S script (N about 10 s, D about 12 s)
PROMPT_TEXT_TOKENS = 1_000     # instructions + read-aloud prompt, uncached (pessimistic)
TEXT_OUT_TOKENS = 100          # transcript text per render
DIALOG_RESPONSES = 5           # AI responses per dialogue, including the ones after tool calls
DIALOG_AI_AUDIO_S = 25.0       # AI audio per dialogue
DIALOG_TEXT_IN = 2_000         # instructions + tools, first response uncached
DIALOG_AUDIO_IN_TOKENS = 2_000  # audio history re-read across the responses (treated as uncached)
DIALOG_TEXT_OUT = 300
DIALOG_CALLER_AUDIO_S = 15.0   # caller speech per dialogue sent to input transcription (upper side)
SSML_CHARS = 170               # S script with <speak>/<mark>/<prosody>; upper bound if tags are not billed


def _m(price: float, n: float) -> float:
    return price * n / 1_000_000


def estimate() -> dict:
    p = PRICES["openai_rt21"]
    out_per_s = PRICES["openai_audio_tokens"]["output_per_s"]
    t1_renders = VOICES_A * SCRIPTS * 2 * 2 * REPEAT_T1           # voices x scripts x N/D x methods x repeat
    t1_responses = t1_renders // 2 * 3 + t1_renders // 2          # split: 3 responses, instr: 1
    t1 = (t1_renders * _m(p["audio_out"], RENDER_AUDIO_S * out_per_s)
          + t1_responses * _m(p["text_in"], PROMPT_TEXT_TOKENS) + t1_renders * _m(p["text_out"], TEXT_OUT_TOKENS))
    dialogs = len(SCENARIOS["dialog"])
    t3_runs = dialogs * VOICES_A * REPEAT_T3
    rest = (_m(p["audio_in"], DIALOG_AUDIO_IN_TOKENS) + _m(p["audio_out"], DIALOG_AI_AUDIO_S * out_per_s)
            + _m(p["text_out"], DIALOG_TEXT_OUT))
    per_dialog = (_m(p["text_in"], DIALOG_TEXT_IN) + _m(p["cached_text_in"], DIALOG_TEXT_IN * (DIALOG_RESPONSES - 1))
                  + rest)
    per_dialog_no_cache = _m(p["text_in"], DIALOG_TEXT_IN * DIALOG_RESPONSES) + rest
    transcription = transcription_upper_bound(DIALOG_CALLER_AUDIO_S)
    t3 = t3_runs * (per_dialog + transcription)
    t3_no_cache = t3_runs * (per_dialog_no_cache + transcription)
    t2_renders = VOICES_B * SCRIPTS * (1 + D_RATES_T2)
    t2_requests = t2_renders * 2                                   # audio + speech marks
    t2 = _m(PRICES["polly_neural"]["value"], t2_requests * SSML_CHARS)
    caller_turns = sum(len(s["turns"]) for s in SCENARIOS["dialog"])
    t3_transcriptions = caller_turns * VOICES_A * REPEAT_T3
    caller_chars = sum(len(t["text"]) for s in SCENARIOS["dialog"] for t in s["turns"])
    caller = _m(PRICES["polly_standard"]["value"], caller_chars)
    return {
        "T1": {"vendor": "openai", "renders": t1_renders, "requests": t1_responses, "audio_s": t1_renders * RENDER_AUDIO_S,
               "usd": round(t1, 3)},
        "T2": {"vendor": "aws_polly", "renders": t2_renders, "requests": t2_requests,
               "audio_s": t2_renders * RENDER_AUDIO_S, "usd": round(t2, 3)},
        "T3": {"vendor": "openai", "runs": t3_runs, "requests": t3_runs * DIALOG_RESPONSES + t3_transcriptions,
               "transcriptions": t3_transcriptions, "audio_s": t3_runs * DIALOG_AI_AUDIO_S,
               "usd": round(t3, 3), "usd_no_cache": round(t3_no_cache, 3),
               "usd_per_dialog": round(per_dialog + transcription, 4),
               "usd_per_dialog_no_cache": round(per_dialog_no_cache + transcription, 4),
               "transcription_usd_per_dialog": round(transcription, 5)},
        "T3_caller_audio": {"vendor": "aws_polly", "requests": caller_turns, "characters": caller_chars,
                            "usd": round(caller, 4)},
        "openai_usd": round(t1 + t3, 2), "openai_usd_no_cache": round(t1 + t3_no_cache, 2),
        "aws_usd": round(t2 + caller, 3), "total_usd": round(t1 + t3 + t2 + caller, 2),
        "total_usd_no_cache": round(t1 + t3_no_cache + t2 + caller, 2),
    }


# --- stage 2b: final candidates over the real telephone path (needs a paid Twilio account; separate decision)
PHONE_CANDIDATES = ("A-2.1", "B-Haiku")   # example: one final candidate per plan
PHONE_CALL_MIN = 2.0                       # actual length of one test call
PHONE_CALLS = {                            # per candidate, from the sample sizes in spec 4-3
    "対話（7シナリオ × 3回）": 21,
    "応答の遅延 Q-01（200ターン ÷ 1通話8ターン）": 25,
    "割り込み Q-02〜Q-04（150回 ÷ 1通話5回）": 30,
    "部分減速 Q-07・Q-08（S台本3件 × N/D × 4回）": 24,
}


def phone_stage_estimate() -> dict:
    """Receiving-side cost of stage 2b per final candidate: Twilio legs with per-minute rounding, the AI,
    transcription and summary. The calling side (V_call) depends on how test calls are placed."""
    from . import cost_model as cm  # imported here: cost_model imports this module
    calls = sum(PHONE_CALLS.values())
    out = {"calls_per_candidate": calls, "call_min": PHONE_CALL_MIN,
           "billed_min_per_leg": cm.twilio_min(PHONE_CALL_MIN), "calls": PHONE_CALLS, "candidates": {}}
    for key in PHONE_CANDIDATES:
        cfg = cm.CONFIGS[key]
        per = {flag: {k: m for k, m in cm.ai_call(cfg, PHONE_CALL_MIN, cache=flag).items() if k != "NTT転送"}
               for flag in (True, False)}  # the test number is called directly: no NTT forwarding
        terms: dict = {}
        for m in per[True].values():
            for sym, coef in m.terms.items():
                terms[sym] = terms.get(sym, 0) + coef * calls
        out["candidates"][key] = {
            "label": cfg["label"],
            "usd": round(calls * sum(m.usd for m in per[True].values()), 2),
            "usd_no_cache": round(calls * sum(m.usd for m in per[False].values()), 2),
            "symbols": {k: round(v, 1) for k, v in terms.items()},
            "caller_side": f"{calls}件 × {cm.twilio_min(PHONE_CALL_MIN):g}分 × V_call",
        }
    out["numbers_usd_per_month"] = round(2 * PRICES["twilio_number_050"]["value"], 2)  # test number + caller number
    return out


if __name__ == "__main__":
    print(json.dumps({"stage_2a": estimate(), "stage_2b_phone": phone_stage_estimate()}, ensure_ascii=False, indent=2))
