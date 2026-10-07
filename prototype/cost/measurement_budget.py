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


# --- browser conversation stage (measurement plan v2.1; a proposal, not an approved budget) ---------------
BROWSER_CONFIGS = ("gpt-live-1", "gemini-3.8-live", "elevenagents", "cartesia-agents", "gpt-realtime-2.1")
BROWSER_AVG_MIN = 3.0                       # expected length; the page hangs up at MAX_SESSION_MIN
LIST_USD_PER_MIN = {"elevenagents": 0.08, "cartesia-agents": 0.06}   # list price per minute (plan quota counts these)
CHECKED = "2026-10-07"
# Plans and quotas from the vendors' official pages (read 2026-10-07). USD, tax excluded unless stated.
PLANS = {
    "openai_lab": {
        "vendor": "OpenAI（GPT-Live 1・GPT-Realtime-2.1）", "plan": "プランなし（従量）。前払いクレジット",
        "prepay_min_usd": 5.0, "plan_usd": 0.0, "quota": "なし（GPT-Liveに無料枠はない）",
        "free_condition": "無料枠なし", "overage": "従量（GPT-Live $0.05/分・秒単位。バックエンドのモデルとツールは別料金）",
        "expiry": "前払いクレジットは1年で失効・返金なし。自動の追加購入を切れば、残高が上限になる",
        "tax": "消費税10%（直接契約の日本の顧客）",
        "sources": ["https://developers.openai.com/api/docs/models/gpt-live-1",
                    "https://help.openai.com/en/articles/8264644-setting-up-and-managing-prepaid-api-billing",
                    "https://help.openai.com/en/articles/10242647-the-japanese-consumption-tax-on-your-openai-invoices"]},
    "google_lab": {
        "vendor": "Google（Gemini 3.8 Live）", "plan": "無料枠、または有料枠（Cloud Billing＋前払い）",
        "prepay_min_usd": 5.0, "plan_usd": 0.0, "quota": "無料枠：入出力とも無料（回数・量の上限はAI Studioの画面だけに表示＝未確認）",
        "free_condition": "無料枠の入出力は、Googleの製品の改善に使われ、人が読むことがある。有料枠では使われない",
        "overage": "有料枠：音声入力$3・出力$12（1Mトークン）。履歴はターンごとに再計算されて課金（記号 Ctx_gem）",
        "expiry": "前払いは12か月で失効・返金なし。残高0でHTTP 402（残高が上限になる）",
        "tax": "消費税10%（日本の顧客。価格は税抜）",
        "sources": ["https://ai.google.dev/gemini-api/docs/pricing", "https://ai.google.dev/gemini-api/terms",
                    "https://ai.google.dev/gemini-api/docs/billing",
                    "https://ai.google.dev/gemini-api/docs/live-api/best-practices"]},
    "elevenlabs_lab": {
        "vendor": "ElevenLabs（ElevenAgents）", "plan": "Free $0（15分）／Starter $6/月（75分）／Creator $22/月（275分）",
        "prepay_min_usd": 0.0, "plan_usd": 0.0, "plan_options": [("Free", 0.0, 15), ("Starter", 6.0, 75), ("Creator", 22.0, 275)],
        "quota": "プランの分数（Free 15分、Starter 75分）",
        "free_condition": "Freeは非商用・表示義務。学習への利用は既定でオン（設定で外せる）。Freeでclient toolsが使えるかは未確認",
        "overage": "追加の分は全プラン$0.08/分（従量のクレジットが必要）。10秒を超える無音は95%引き。LLMは別料金（記号 L_11）",
        "expiry": "月ごとのプラン。Starterの初月割引（期限付き）は恒久の価格に使わない",
        "tax": "未確認（法令で必要な場合は課税、リバースチャージの場合は課税しない、との記載）",
        "sources": ["https://elevenlabs.io/pricing/agents",
                    "https://elevenlabs.io/docs/eleven-agents/customization/llm",
                    "https://elevenlabs.io/docs/help-center/legal/is-my-data-used-to-improve-eleven-labs-ai-models"]},
    "cartesia_lab": {
        "vendor": "Cartesia（Managed Agents）", "plan": "Free $0（エージェント$1分≈17分）／Pro $5/月（$5分≈83分）",
        "prepay_min_usd": 0.0, "plan_usd": 0.0, "plan_options": [("Free", 0.0, 1.0), ("Pro", 5.0, 5.0)],
        "quota": "エージェントの利用額（Free $1、Pro $5。$0.06/分で換算）",
        "free_condition": "Freeは商用不可。Freeで$1を超えて使えるかは未確認（超過の説明は有料プラン向け）",
        "overage": "有料プランは同じ単価$0.06/分。LLMは未確認（記号 L_cart。無料期間は2026-10-01まで、との記載と食い違い）",
        "expiry": "月ごとのプラン", "tax": "未確認（規約に日本の消費税の記載なし）",
        "sources": ["https://cartesia.ai/pricing", "https://docs.cartesia.ai/agents/models",
                    "https://docs.cartesia.ai/agents/client-tools"]},
}


def browser_stage_estimate() -> dict:
    """Two stages: connection check first, then the detailed comparison for candidates that connected."""
    from ..browser_lab.config import CANDIDATES, LAB_LIMITS_BY_STAGE, MAX_SESSION_MIN, SESSIONS
    cand = {c["id"]: c for c in json.loads((pathlib.Path(__file__).resolve().parent / "candidates.json")
                                           .read_text())["candidates"]}
    from . import candidate_costs as cc
    stages = {}
    for stage in ("connection", "detailed"):
        rows = []
        for cid in BROWSER_CONFIGS:
            lab = CANDIDATES[cid]
            sessions = SESSIONS[stage][cid]
            exp_min, max_min = sessions * BROWSER_AVG_MIN, sessions * MAX_SESSION_MIN
            ai = cc.ai_part(cand[cid])
            ref_per_min = ai.usd / 3.0
            row = {"id": cid, "name": lab["name"], "sessions": sessions, "expected_min": exp_min, "max_min": max_min,
                   "ref_usd_per_min": round(ref_per_min, 4), "upper_usd_per_min": lab["upper_usd_per_min"],
                   "expected_usd": round(exp_min * ref_per_min, 2), "max_usd": round(max_min * lab["upper_usd_per_min"], 2),
                   "symbols": sorted(ai.terms), "ledger_vendor": lab["vendor"]}
            if cid == "gpt-realtime-2.1":
                row["expected_usd_no_cache"] = round(exp_min * cc.ai_part(cand[cid], cache=False).usd / 3.0, 2)
            rows.append(row)
        caps = {v: LAB_LIMITS_BY_STAGE[stage][v].max_cost_usd for v in LAB_LIMITS_BY_STAGE[stage]}
        stages[stage] = {"rows": rows, "expected_usd": round(sum(r["expected_usd"] for r in rows), 2),
                         "max_usd": round(sum(r["max_usd"] for r in rows), 2), "runtime_caps_usd": caps,
                         "runtime_caps_total_usd": round(sum(caps.values()), 2)}
    # minutes / amounts that fall inside a plan's quota (not extra cash; never counted twice)
    used = {cid: {st: SESSIONS[st][cid] * MAX_SESSION_MIN for st in SESSIONS} for cid in BROWSER_CONFIGS}
    quota = {
        "elevenagents": {"connection": "Free（15分）の範囲：最大8分",
                         "detailed": f"累計で最大{used['elevenagents']['connection'] + used['elevenagents']['detailed']:.0f}分"
                                     "→ Starter（75分、$6/月）の範囲"},
        "cartesia-agents": {"connection": f"Free（$1）の範囲：最大${used['cartesia-agents']['connection'] * 0.06:.2f}",
                            "detailed": f"累計で最大${(used['cartesia-agents']['connection'] + used['cartesia-agents']['detailed']) * 0.06:.2f}"
                                        "→ Pro（$5/月）の範囲"},
    }
    openai_conn = sum(r["max_usd"] for r in stages["connection"]["rows"] if r["ledger_vendor"] == "openai_lab")
    openai_det = sum(r["max_usd"] for r in stages["detailed"]["rows"] if r["ledger_vendor"] == "openai_lab")
    google_det = sum(r["max_usd"] for r in stages["detailed"]["rows"] if r["ledger_vendor"] == "google_lab")
    totals = {
        "connection": {"prepaid_usd": {"OpenAI前払い（残高がなければ）": 5.0},
                       "plans_usd": {"ElevenLabs Free": 0.0, "Cartesia Free": 0.0, "Gemini 無料枠": 0.0},
                       "consumption_max_usd": {"OpenAI（前払いから引かれる）": round(openai_conn, 2)},
                       "inside_quota": {"ElevenLabs": quota["elevenagents"]["connection"],
                                        "Cartesia": quota["cartesia-agents"]["connection"],
                                        "Gemini": "無料枠（データの扱いの条件あり）"}},
        "detailed": {"prepaid_usd": {"OpenAI前払いの追加": 10.0, "Gemini 有料枠の前払い（無料枠を使わない場合）": 5.0},
                     "plans_usd": {"ElevenLabs Starter（1か月）": 6.0, "Cartesia Pro（1か月）": 5.0},
                     "consumption_max_usd": {"OpenAI（前払いから引かれる）": round(openai_det, 2),
                                             "Gemini（有料枠の場合、前払いから引かれる）": round(google_det, 2)},
                     "inside_quota": {"ElevenLabs": quota["elevenagents"]["detailed"],
                                      "Cartesia": quota["cartesia-agents"]["detailed"]}},
    }
    return {"stages": stages, "plans": PLANS, "quota": quota, "totals": totals, "max_session_min": MAX_SESSION_MIN,
            "avg_session_min": BROWSER_AVG_MIN, "checked": CHECKED, "fx_jpy_per_usd": FX_ASSUMED, "tax": 0.10,
            # kept for older callers: the whole browser stage (connection + detailed)
            "rows": stages["connection"]["rows"] + stages["detailed"]["rows"],
            "expected_usd": round(stages["connection"]["expected_usd"] + stages["detailed"]["expected_usd"], 2),
            "max_usd": round(stages["connection"]["max_usd"] + stages["detailed"]["max_usd"], 2),
            "runtime_caps_usd": stages["detailed"]["runtime_caps_usd"],
            "runtime_caps_total_usd": stages["detailed"]["runtime_caps_total_usd"]}


FX_ASSUMED = 150.0


def browser_budget_markdown() -> str:
    """The six tables of the implementation instruction v1, 5章 (for docs/measurement-plan-v2.md)."""
    e = browser_stage_estimate()
    out = []
    w = out.append
    stage_title = {"connection": "接続の予備試験", "detailed": "詳しい会話比較（接続できた候補だけ）"}
    for stage in ("connection", "detailed"):
        st = e["stages"][stage]
        w(f"**{stage_title[stage]}**（1回最大{e['max_session_min']:.0f}分で自動終了、平均{e['avg_session_min']:.0f}分と仮定）\n")
        w("| 候補 | 回数 | 最大時間 | 見込み額（参考の分単価） | 最大額（最大時間×上限の分単価） | 記号で残す費用 |")
        w("| --- | --- | --- | --- | --- | --- |")
        for r in st["rows"]:
            exp = f"約${r['expected_usd']:.2f}（${r['ref_usd_per_min']}/分）"
            if "expected_usd_no_cache" in r:
                exp += f"／キャッシュなし約${r['expected_usd_no_cache']:.2f}"
            w(f"| {r['name']} | {r['sessions']} | {r['max_min']:.0f}分 | {exp} | ${r['max_usd']:.2f}（${r['upper_usd_per_min']}/分） | "
              f"{'、'.join(r['symbols']) or '—'} |")
        w(f"| **合計** | {sum(r['sessions'] for r in st['rows'])} | {sum(r['max_min'] for r in st['rows']):.0f}分 | "
          f"約${st['expected_usd']:.2f} | ${st['max_usd']:.2f} | ＋記号 |\n")
        caps = "、".join(f"{v} ${c:g}" for v, c in st["runtime_caps_usd"].items())
        w(f"- 実行側の上限（台帳。{'累計' if stage == 'detailed' else 'この段階'}）：{caps}（合計 ${st['runtime_caps_total_usd']:g}）。"
          f"{'`LAB_STAGE=detailed` で起動したときだけ使える' if stage == 'detailed' else '既定の段階（`LAB_STAGE=connection`）'}。\n")
    w("**固定費・利用枠**\n")
    w("| 業者 | 契約するプラン | 含まれる分数・クレジット | 無料枠の適用条件 | 超過料金 | 期限・税 |")
    w("| --- | --- | --- | --- | --- | --- |")
    for p in e["plans"].values():
        w(f"| {p['vendor']} | {p['plan']} | {p['quota']} | {p['free_condition']} | {p['overage']} | {p['expiry']}。{p['tax']} |")
    w("")
    w("**別料金（上の分単価に含まれないもの）**\n")
    w("| 記号 | 内容 | 範囲・抑え方 |")
    w("| --- | --- | --- |")
    w("| L_live | GPT-Liveのバックエンドのモデル（delegation）とツール | 単価はモデルで変わる。1応答の出力上限1,200トークン、ツールは1発話3回まで |")
    w("| L_11 | ElevenAgentsのLLM | プロバイダーの単価でクレジットから引かれる。エージェントの設定で安いモデルを選ぶ |")
    w("| L_cart | Cartesia Managed AgentsのLLM | 単価はAPI（要キー）でしか見られない＝未確認 |")
    w("| Ctx_gem | Gemini Liveの履歴の再計算 | 接続の設定で履歴を短く保つ（contextWindowCompression） |")
    w("| サーバー | 試験のサーバー | 利用者のPCで動かすので$0。外部に置く場合は別途（今回は提案しない） |\n")
    w("**総額（前払い・プランと、消費の見込みを分ける。利用枠の中の分は二重に数えない）**\n")
    w("| 段階 | 前払い・プラン（税抜） | 消費の最大（前払いから引かれる。税抜） | プランの枠の中（追加の支払いなし） |")
    w("| --- | --- | --- | --- |")
    for stage in ("connection", "detailed"):
        t = e["totals"][stage]
        pre = "、".join(f"{k} ${v:g}" for k, v in {**t["prepaid_usd"], **t["plans_usd"]}.items())
        con = "、".join(f"{k} ${v:.2f}" for k, v in t["consumption_max_usd"].items())
        inside = "、".join(f"{k}：{v}" for k, v in t["inside_quota"].items())
        w(f"| {stage_title[stage]} | {pre} | {con}（＋記号） | {inside} |")
    conn_cash = sum(e["totals"]["connection"]["prepaid_usd"].values())
    det_cash = sum(e["totals"]["detailed"]["prepaid_usd"].values()) + sum(e["totals"]["detailed"]["plans_usd"].values())
    w("")
    w(f"- 支払う額の目安（税抜）：接続の予備試験は **${conn_cash:g}**（OpenAIに残高がなければ前払い。消費は前払いの中）。"
      f"詳しい会話比較まで進むと、追加で **最大${det_cash:g}**（OpenAIの追加の前払い$10、Geminiの有料枠$5を使う場合、"
      "ElevenLabs Starter $6、Cartesia Pro $5）。")
    w(f"- 税：OpenAI・Googleは消費税10%。ElevenLabs・Cartesiaは未確認なので、安全側に10%を加えて見る。"
      f"為替は1ドル＝{e['fx_jpy_per_usd']:.0f}円の仮置き（140〜160円で変わる）。例：$5は税込で約{5 * 1.1 * e['fx_jpy_per_usd']:.0f}円。")
    w("- 前払いのクレジットは、自動の追加購入を切っておけば、残高が業者側の上限になる（OpenAI、Gemini有料枠）。"
      "台帳の上限は、こちら側の見積もりで止める仕組みで、業者の課金の上限を保証しない。")
    w("- 無料枠は試験の条件だけで使う。本番の料金には流用しない。\n")
    w("**未確定項目（上限を確定できない理由と、外して実行できる案）**\n")
    w("| 項目 | 確定できない理由 | 外して実行できる案 |")
    w("| --- | --- | --- |")
    w("| GPT-Liveのバックエンドの費用（L_live） | モデルの単価と、会話で呼ばれる回数による | 接続の予備試験の2回だけにし、OpenAIのプロジェクトの予算と前払いの残高で止める。業務処理の比較は基準のGPT-Realtime-2.1で代える |")
    w("| ElevenLabsのLLM（L_11）、Freeでのclient tools | LLMの単価は選ぶモデルによる。Freeでの機能の制限は公式に明記がない | Freeの15分の範囲で接続だけ確かめる。業務処理が使えなければ、音声だけの比較にして「業務処理は未評価」と記録する |")
    w("| CartesiaのLLM（L_cart）、Freeの超過 | 単価がAPIでしか見られない。Freeで$1を超えられるかが不明 | Freeの$1の範囲で接続だけ確かめる。超えた場合の請求が不明なうちは、Proへ進まない |")
    w("| Geminiの履歴の課金（Ctx_gem）、無料枠の上限 | 会話の長さで増える。無料枠の上限は画面でしか見られない | 無料枠（架空のデータだけ）で接続を確かめる。有料枠は前払い$5の残高で止まる |")
    w("| ElevenLabs・Cartesiaの消費税 | 公式の記載が日本向けに明確でない | 10%を加えて見る |")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if "--browser-md" in sys.argv:
        print(browser_budget_markdown())
    else:
        print(json.dumps({"stage_2a_api_direct": estimate(), "stage_2b_phone": phone_stage_estimate(),
                          "browser_stage": browser_stage_estimate()}, ensure_ascii=False, indent=2))
