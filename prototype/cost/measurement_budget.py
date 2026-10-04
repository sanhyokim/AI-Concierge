"""Expected vendor cost of the minimal measurement plan (docs/measurement-plan-v1.md).

python3 -m prototype.cost.measurement_budget

Counts follow the probe scripts' defaults; per-request sizes are assumptions written next to
each figure (pessimistic where unsure). Prices come from prices.json (USD, tax excluded).
The run-side limits in engines/budget.py are set to about twice these counts (one full rerun).
"""
from __future__ import annotations

import json
import pathlib

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
    per_dialog = (_m(p["text_in"], DIALOG_TEXT_IN) + _m(p["cached_text_in"], DIALOG_TEXT_IN * (DIALOG_RESPONSES - 1))
                  + _m(p["audio_in"], DIALOG_AUDIO_IN_TOKENS) + _m(p["audio_out"], DIALOG_AI_AUDIO_S * out_per_s)
                  + _m(p["text_out"], DIALOG_TEXT_OUT))
    t3 = t3_runs * per_dialog
    t2_renders = VOICES_B * SCRIPTS * (1 + D_RATES_T2)
    t2_requests = t2_renders * 2                                   # audio + speech marks
    t2 = _m(PRICES["polly_neural"]["value"], t2_requests * SSML_CHARS)
    caller_turns = sum(len(s["turns"]) for s in SCENARIOS["dialog"])
    caller_chars = sum(len(t["text"]) for s in SCENARIOS["dialog"] for t in s["turns"])
    caller = _m(PRICES["polly_standard"]["value"], caller_chars)
    return {
        "T1": {"vendor": "openai", "renders": t1_renders, "requests": t1_responses, "audio_s": t1_renders * RENDER_AUDIO_S,
               "usd": round(t1, 3)},
        "T2": {"vendor": "aws_polly", "renders": t2_renders, "requests": t2_requests,
               "audio_s": t2_renders * RENDER_AUDIO_S, "usd": round(t2, 3)},
        "T3": {"vendor": "openai", "runs": t3_runs, "requests": t3_runs * DIALOG_RESPONSES,
               "audio_s": t3_runs * DIALOG_AI_AUDIO_S, "usd": round(t3, 3), "usd_per_dialog": round(per_dialog, 4)},
        "T3_caller_audio": {"vendor": "aws_polly", "requests": caller_turns, "characters": caller_chars,
                            "usd": round(caller, 4)},
        "openai_usd": round(t1 + t3, 2), "aws_usd": round(t2 + caller, 3), "total_usd": round(t1 + t3 + t2 + caller, 2),
    }


if __name__ == "__main__":
    print(json.dumps(estimate(), ensure_ascii=False, indent=2))
