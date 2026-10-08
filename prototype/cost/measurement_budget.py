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

PRICES = json.loads((pathlib.Path(__file__).resolve().parent / "prices.json").read_text(encoding="utf-8"))["items"]
SCENARIOS = json.loads((pathlib.Path(__file__).resolve().parents[1] / "scenarios" / "first_round.json")
                       .read_text(encoding="utf-8"))

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


# --- browser conversation stage (measurement plan v2.2; a proposal, not an approved budget) ---------------
TESTED = ("gpt-live-1", "gemini-3.8-live")                        # user decision 2026-10-07
HELD = ("gpt-realtime-2.1", "elevenagents", "cartesia-agents")    # kept as candidates, no sessions planned
BROWSER_CONFIGS = TESTED + HELD
BROWSER_AVG_MIN = 3.0                       # expected length; the page and the server stop at MAX_SESSION_MIN
CHECKED = "2026-10-07"
FX_ASSUMED = 150.0
BILLING_DELAY = {   # why a prepaid balance is not a spending cap (official pages, read 2026-10-07)
    "openai_lab": ("集計の遅れの間の利用は、残高のマイナスとして次の購入から引かれる。前払いの残高を即時の停止に頼らない、"
                   "との記載", "https://help.openai.com/en/articles/8264644-setting-up-and-managing-prepaid-api-billing"),
    "google_lab": ("課金の集計に約10分の遅れがあり、その間は残高を超えて使われることがある（マイナスは次の購入から引かれる）。"
                   "残高0でHTTP 402", "https://ai.google.dev/gemini-api/docs/billing"),
}
# Plans and quotas from the vendors' official pages (read 2026-10-07). USD, tax excluded unless stated.
PLANS = {
    "openai_lab": {
        "vendor": "OpenAI（GPT-Live 1）", "plan": "プランなし（従量）。前払いクレジット（最低$5）",
        "quota": "なし（GPT-Liveに無料枠はない）", "free_condition": "無料枠なし",
        "overage": "従量。GPT-Live $0.05/分（秒単位）。裏方のモデル（delegation）は別料金（トークン単価）",
        "expiry": "前払いクレジットは1年で失効・返金なし。" + BILLING_DELAY["openai_lab"][0],
        "tax": "消費税10%（直接契約の日本の顧客）",
        "sources": ["https://developers.openai.com/api/docs/models/gpt-live-1", BILLING_DELAY["openai_lab"][1],
                    "https://help.openai.com/en/articles/10242647-the-japanese-consumption-tax-on-your-openai-invoices"]},
    "google_lab": {
        "vendor": "Google（Gemini 3.8 Live）", "plan": "無料枠、または有料枠（Cloud Billing＋前払い$5から）",
        "quota": "無料枠：入出力とも無料（回数・量の上限はAI Studioの画面だけに表示＝未確認）",
        "free_condition": "無料枠の入出力は、Googleの製品の改善に使われ、人が読むことがある。有料枠では使われない",
        "overage": "有料枠：音声入力$3・出力$12（1Mトークン）。履歴はターンごとに再計算されて課金（記号 Ctx_gem）",
        "expiry": "前払いは12か月で失効・返金なし。" + BILLING_DELAY["google_lab"][0],
        "tax": "消費税10%（日本の顧客。価格は税抜）",
        "sources": ["https://ai.google.dev/gemini-api/docs/pricing", "https://ai.google.dev/gemini-api/terms",
                    BILLING_DELAY["google_lab"][1], "https://ai.google.dev/gemini-api/docs/live-api/best-practices"]},
}
HELD_NOTE = {   # reference only: no sessions are planned until the user releases them
    "gpt-realtime-2.1": "保留（再開すると同じOpenAIの前払いから引かれる。$0.30/分を上限に留保）",
    "elevenagents": "保留。Free 15分／Starter $6/月（75分）。超過$0.08/分、LLMは別料金（L_11）",
    "cartesia-agents": "保留。Free $1／Pro $5/月。$0.06/分、LLMの単価は未確認（L_cart）",
}


def browser_stage_estimate() -> dict:
    """Connection check first, then the detailed comparison for the candidates that connected.

    Four things are kept apart: what is paid, the estimate of the known cost items (assumptions), costs that
    are not confirmed (symbols, never 0), and the in-app limits (which stop sessions but cap no vendor bill)."""
    from ..browser_lab import config as lc
    cand = {c["id"]: c for c in json.loads((pathlib.Path(__file__).resolve().parent / "candidates.json")
                                           .read_text(encoding="utf-8"))["candidates"]}
    from . import candidate_costs as cc
    live = lc.CANDIDATES["gpt-live-1"]
    model = lc.delegation_model(live, {})
    backend = {"typical": lc.backend_usd(model, lc.BACKEND_TYPICAL), "strict": lc.backend_usd(model, lc.BACKEND_STRICT)}
    stages = {}
    for stage in ("connection", "detailed"):
        rows = []
        for cid in BROWSER_CONFIGS:
            lab = lc.CANDIDATES[cid]
            n = lc.SESSIONS[stage][cid]
            exp_min, max_min = n * BROWSER_AVG_MIN, n * lc.MAX_SESSION_MIN
            ref_per_min = cc.ai_part(cand[cid]).usd / 3.0
            row = {"id": cid, "name": lab["name"], "held": cid in HELD, "sessions": n, "expected_min": exp_min,
                   "max_min": max_min, "ref_usd_per_min": round(ref_per_min, 4),
                   "upper_usd_per_min": lab["upper_usd_per_min"], "ledger_vendor": lab["vendor"],
                   "voice_expected_usd": round(exp_min * ref_per_min, 2),
                   "voice_max_usd": round(max_min * lab["upper_usd_per_min"], 2),
                   "backend_typical_usd": 0.0, "backend_strict_usd": 0.0,
                   "symbols": sorted(cc.ai_part(cand[cid]).terms)}
            if cid == "gpt-live-1":
                row["backend_typical_usd"] = round(n * backend["typical"], 3)
                row["backend_strict_usd"] = round(n * backend["strict"], 3)
            row["expected_usd"] = round(row["voice_expected_usd"] + row["backend_typical_usd"], 2)
            row["max_usd"] = round(n * lc.session_reserve_usd(lab, {}), 2)   # what the ledger reserves
            rows.append(row)
        limits = lc.LAB_LIMITS_BY_STAGE[stage]
        caps = {v: limits[v].max_cost_usd for v in limits}
        stages[stage] = {"rows": rows, "expected_usd": round(sum(r["expected_usd"] for r in rows), 2),
                         "max_usd": round(sum(r["max_usd"] for r in rows), 2), "runtime_caps_usd": caps,
                         "runtime_caps_total_usd": round(sum(caps.values()), 2),
                         "requests_caps": {v: limits[v].max_requests for v in limits}}
    cum = {v: sum(r["max_usd"] for st in stages.values() for r in st["rows"] if r["ledger_vendor"] == v)
           for v in ("openai_lab", "google_lab")}
    conn = {v: sum(r["max_usd"] for r in stages["connection"]["rows"] if r["ledger_vendor"] == v)
            for v in ("openai_lab", "google_lab")}
    payments = {
        "connection": [("OpenAI 前払い（残高がなければ。最低額）", 5.0),
                       ("Google 無料枠（架空のデータだけ）。有料枠を選ぶ場合は前払い$5", 0.0)],
        "detailed": [("OpenAI 追加の前払い（累計の最大が最初の$5を超えるため。残高しだい）", 5.0 if cum["openai_lab"] > 5 else 0.0),
                     ("Google 有料枠の前払い（無料枠を使わない場合）", 5.0)],
    }
    unconfirmed = [
        ("L_live", "GPT-Liveの裏方のモデルに渡る量", "入力は指示文（最大16,384トークン）と会話の履歴。渡る量が公式に書かれていないため、"
         "典型と厳しめの仮定を並べるだけで、上限は確定しない"),
        ("—", "GPT-Liveの日本語", "公式の言語の一覧がない。声の追加の表は英語・ポルトガル語だけ。最初の接続で聞いて確かめる"),
        ("Ctx_gem", "Gemini Liveの履歴の再計算", "会話が長いほど増える。接続の設定で履歴を短く保つ（contextWindowCompression）"),
        ("—", "Geminiの無料枠の上限", "回数・量の上限はAI Studioの画面だけに表示される"),
        ("—", "課金の集計の遅れ", "OpenAI：" + BILLING_DELAY["openai_lab"][0] + "。Google：" + BILLING_DELAY["google_lab"][0]),
        ("FX", "為替", f"1ドル＝{FX_ASSUMED:.0f}円の仮置き（140〜160円で変わる）"),
    ]
    in_app = {
        "sessions": {st: {cid: lc.SESSIONS[st][cid] for cid in BROWSER_CONFIGS} for st in lc.SESSIONS},
        "max_session_min": lc.MAX_SESSION_MIN, "grace_s": lc.SESSION_GRACE_S,
        "tool_calls": lc.MAX_TOOL_CALLS, "backend_responses": lc.MAX_BACKEND_RESPONSES,
        "response_creates": lc.MAX_RESPONSE_CREATES, "mint_failures": lc.MAX_MINT_FAILURES,
    }
    return {"stages": stages, "plans": PLANS, "held_note": HELD_NOTE, "payments": payments,
            "unconfirmed": unconfirmed, "in_app": in_app, "backend_per_session": backend, "backend_model": model,
            "backend_assumptions": {"typical": lc.BACKEND_TYPICAL, "strict": lc.BACKEND_STRICT},
            "cumulative_max_usd": {k: round(v, 2) for k, v in cum.items()},
            "connection_max_usd": {k: round(v, 2) for k, v in conn.items()},
            "max_session_min": lc.MAX_SESSION_MIN, "avg_session_min": BROWSER_AVG_MIN, "checked": CHECKED,
            "fx_jpy_per_usd": FX_ASSUMED, "tax": 0.10,
            "runtime_caps_usd": stages["detailed"]["runtime_caps_usd"],
            "runtime_caps_total_usd": stages["detailed"]["runtime_caps_total_usd"]}


def browser_budget_markdown() -> str:
    """5章 of docs/measurement-plan-v2.md: payments, the known-cost estimate, unconfirmed costs, in-app limits."""
    e = browser_stage_estimate()
    out = []
    w = out.append
    title = {"connection": "接続の予備試験", "detailed": "詳しい会話比較（接続できた候補だけ）"}
    w("**① 支払い額（税抜。前払い・プラン）**\n")
    w("| 段階 | 支払うもの | 額 |")
    w("| --- | --- | --- |")
    for stage in ("connection", "detailed"):
        for label, usd in e["payments"][stage]:
            w(f"| {title[stage]} | {label} | ${usd:g} |")
    w("")
    w("- 前払いの残高は、**費用の上限ではありません**。停止と集計の遅れで、残高を超えて使われることがあります"
      "（OpenAI：残高がマイナスになり、次の購入から引かれる。Google：集計に約10分の遅れ）。出典は下の固定費の表。")
    w(f"- 税：OpenAI・Googleは消費税10%。例：$5は税込で約{5 * 1.1 * e['fx_jpy_per_usd']:.0f}円（1ドル＝{e['fx_jpy_per_usd']:.0f}円の仮置き）。")
    w("- 無料枠は試験の条件（架空のデータだけ）でだけ使います。本番の料金には流用しません。\n")
    w(f"**② 既知の費目の試算（仮定。1回最大{e['max_session_min']:.0f}分、平均{e['avg_session_min']:.0f}分と仮定）**\n")
    b = e["backend_assumptions"]
    w(f"GPT-Liveの裏方のモデル（{e['backend_model']}）は、典型（{b['typical']['responses']}応答×入力{b['typical']['input_tokens']:,}"
      f"・出力{b['typical']['output_tokens']:,}トークン、1会話 ${e['backend_per_session']['typical']:.3f}）と、"
      f"厳しめ（{b['strict']['responses']}応答×入力{b['strict']['input_tokens']:,}・出力{b['strict']['output_tokens']:,}トークン、"
      f"1会話 ${e['backend_per_session']['strict']:.3f}）の2つの仮定で示します。どちらも保証ではありません。\n")
    for stage in ("connection", "detailed"):
        st = e["stages"][stage]
        w(f"*{title[stage]}*\n")
        w("| 候補 | 回数 | 最大時間 | 声の見込み（参考の分単価） | 裏方の見込み（典型） | 留保する最大額（声は上限の分単価、裏方は厳しめ） | 記号で残す費用 |")
        w("| --- | --- | --- | --- | --- | --- | --- |")
        for r in st["rows"]:
            if r["held"]:
                continue
            back = f"${r['backend_typical_usd']:.2f}" if r["id"] == "gpt-live-1" else "—"
            w(f"| {r['name']} | {r['sessions']} | {r['max_min']:.0f}分 | 約${r['voice_expected_usd']:.2f}（${r['ref_usd_per_min']}/分） | "
              f"{back} | ${r['max_usd']:.2f}（${r['upper_usd_per_min']}/分"
              f"{'＋裏方 $' + format(r['backend_strict_usd'], '.2f') if r['id'] == 'gpt-live-1' else ''}） | {'、'.join(r['symbols']) or '—'} |")
        tested = [r for r in st["rows"] if not r["held"]]
        w(f"| **合計** | {sum(r['sessions'] for r in tested)} | {sum(r['max_min'] for r in tested):.0f}分 | "
          f"約${sum(r['voice_expected_usd'] for r in tested):.2f} | ${sum(r['backend_typical_usd'] for r in tested):.2f} | "
          f"${sum(r['max_usd'] for r in tested):.2f} | ＋記号 |\n")
    w("保留の候補（回数0。参考）：" + "／".join(f"{next(r['name'] for r in e['stages']['connection']['rows'] if r['id'] == k)}：{v}"
                                       for k, v in e["held_note"].items()) + "。\n")
    w("**③ 未確認の費用（0円にしない。上限を確定できない理由）**\n")
    w("| 記号 | 項目 | 確定できない理由・抑え方 |")
    w("| --- | --- | --- |")
    for sym, item, why in e["unconfirmed"]:
        w(f"| {sym} | {item} | {why} |")
    w("")
    w("**④ アプリ内の制限（会話を止める仕組み。業者の課金の上限は保証しない）**\n")
    a = e["in_app"]
    w("| 制限 | 値 | 守り方 |")
    w("| --- | --- | --- |")
    for stage in ("connection", "detailed"):
        cnt = "、".join(f"{r['name'].split('（')[0]} {r['sessions']}回" for r in e["stages"][stage]["rows"])
        w(f"| 開始の回数（{title[stage]}） | {cnt} | サーバーのデータベースで数える（再起動・同時の開始を含む）。送る前に拒否する |")
    w(f"| 1回の長さ | 最大{a['max_session_min']:.0f}分 | 画面が止め、サーバーの見張りも{a['max_session_min']:.0f}分＋{a['grace_s']}秒で期限切れにする |")
    w(f"| 業務処理（ツール）の呼び出し | 1会話{a['tool_calls']}回 | 超えたらサーバーが停止を指示する |")
    w(f"| GPT-Liveの裏方の応答 | 1会話{a['backend_responses']}回 | 画面が数えてサーバーへ送る。超えたら停止 |")
    w(f"| 画面から送る response.create | 1会話{a['response_creates']}回 | 同上 |")
    w(f"| 接続情報の発行の失敗・業者が接続を拒否（4xx） | 候補×段階で{a['mint_failures']}回 | 開始の回数には数えない（業者の会話は作られていない）。"
      "超えたら、原因を確かめてから人が管理画面の「費用の台帳」で解除する |")
    for stage in ("connection", "detailed"):
        st = e["stages"][stage]
        caps = "、".join(f"{v} ${c:g}（{st['requests_caps'][v]}回）" for v, c in st["runtime_caps_usd"].items() if c > 0)
        w(f"| 台帳の上限（{title[stage]}、{'累計' if stage == 'detailed' else 'この段階'}） | {caps} | "
          f"開始の前に留保し、超えるなら始めない。{'`LAB_STAGE=detailed` で起動したときだけ' if stage == 'detailed' else '既定の段階'} |")
    w("")
    w("- 留保は、終了の後も**照合待ち**として残ります。業者の利用画面の額で照合したときに閉じます（推定では閉じない）。")
    w(f"- 留保の最大：接続の予備試験 OpenAI ${e['connection_max_usd']['openai_lab']:.2f}・Google ${e['connection_max_usd']['google_lab']:.2f}、"
      f"詳しい比較まで累計 OpenAI ${e['cumulative_max_usd']['openai_lab']:.2f}・Google ${e['cumulative_max_usd']['google_lab']:.2f}。"
      "裏方のモデルを高いもの（GPT_LIVE_DELEGATION_MODEL）に変えると、台帳の上限で開始できません（上限の見直しは判断が要ります）。\n")
    w("**固定費・利用枠（出典つき）**\n")
    w("| 業者 | 契約するプラン | 含まれる分数・クレジット | 無料枠の適用条件 | 超過料金 | 期限・集計の遅れ・税 | 出典 |")
    w("| --- | --- | --- | --- | --- | --- | --- |")
    for pl in e["plans"].values():
        src = " ".join(f"[{i + 1}]({u})" for i, u in enumerate(pl["sources"]))
        w(f"| {pl['vendor']} | {pl['plan']} | {pl['quota']} | {pl['free_condition']} | {pl['overage']} | {pl['expiry']}。{pl['tax']} | {src} |")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if "--browser-md" in sys.argv:
        print(browser_budget_markdown())
    else:
        print(json.dumps({"stage_2a_api_direct": estimate(), "stage_2b_phone": phone_stage_estimate(),
                          "browser_stage": browser_stage_estimate()}, ensure_ascii=False, indent=2))
