"""Candidate list and cost/quality comparison frame on the same premise as the cost table.

python3 -m prototype.cost.candidate_costs > docs/candidate-comparison-v1.md
python3 -m prototype.cost.candidate_costs --list > docs/candidates-v1.md

Same premise for every candidate: the cost table's call volumes, 3-minute calls, caller/AI speaking
shares, line plan 1 (NTT forwarding + Twilio number), audio bridged over Twilio Media Streams, the same
post-call summary and LINE notification. Only the AI part differs. Unconfirmed prices stay symbols.
Quality columns are empty until measured: nothing here is a measurement or a ranking.
"""
from __future__ import annotations

import json
import pathlib
import sys

from . import cost_model as cm

DATA = json.loads((pathlib.Path(__file__).resolve().parent / "candidates.json").read_text(encoding="utf-8"))
FX, TAX = cm.FX, cm.TAX


def common_ai_call() -> cm.Money:
    """Per AI-answered call, the same for every candidate: NTT forwarding, Twilio inbound, Media Streams,
    recording (each Twilio leg rounded up per call), and the post-call summary."""
    billed = cm.twilio_min(cm.DUR_MIN)
    p = cm.PRICES
    return (cm.forward() + cm.Money(usd=(p["twilio_inbound"]["value"] + p["twilio_media_streams"]["value"]
                                          + p["twilio_recording"]["value"]) * billed)
            + cm.summary("anthropic_haiku45"))


def ai_part(c: dict, cache: bool = True) -> cm.Money:
    """The candidate's own AI cost for one 3-minute call (known part in USD plus symbols)."""
    pr, dur = c["price"], cm.DUR_MIN
    terms = {k: 1 for k in pr.get("symbols", {})}
    kind = pr["kind"]
    if kind == "per_min":
        return cm.Money(usd=pr["usd_per_min"] * dur, terms=terms)
    if kind == "live_tokens":
        return cm.Money(usd=pr["audio_in_usd_per_min"] * dur + pr["audio_out_usd_per_min"] * dur * cm.AI_SPEECH_SHARE,
                        terms=terms)
    if kind == "inworld":
        return cm.Money(usd=pr["stt_usd_per_hour"] / 60 * dur + pr["tts_usd_per_m_chars"] * cm.TTS_CHARS_PER_CALL / 1e6,
                        terms=terms)
    if kind == "realtime_model":
        return cm.plan_a_model(pr["model_key"], dur, cache) + cm.input_transcription(dur)
    return cm.Money(terms=terms)  # symbol_only


def monthly_plan(c: dict) -> cm.Money:
    pr = c["price"]
    return cm.Money(usd=pr.get("plan_usd_per_month", 0.0), terms={k: 1 for k in pr.get("monthly_symbols", {})})


def monthly_total(c: dict, scn: dict, cache: bool = True) -> tuple[cm.Money, float]:
    per_call = common_ai_call() + ai_part(c, cache)
    ai = per_call.scale(scn["ai"])
    human = sum(cm.human_call().values(), cm.Money()).scale(scn["human"])
    fixed_items = cm.monthly_fixed(scn)
    storage = fixed_items.pop("録音の保存（Twilio、保存月数R）")
    fixed = sum(fixed_items.values(), cm.Money())
    total = ai + human + fixed + monthly_plan(c) + cm.Money(terms={"R×録音保存": 1})
    return total, storage.usd


# --- line plan 2 (under consideration, not adopted): measured from today's forwarding to the staff mobile ----
# Today every call to 0120 and 092 is forwarded by NTT to the staff member's mobile (user, 2026-10-07; set with
# 142 as far as they remember). Plan 2 re-points that forward to the AI number only while the system is ON.
# Calls answered by people never pass through the cloud, so plan 1's extra for human-answered calls does not apply.
PLAN2_TESTED = ("gpt-live-1", "gemini-3.8-live")
PLAN2_REFERENCE = ("gpt-realtime-2.1", "elevenagents", "cartesia-agents", "qwen-omni-realtime")
DELTA_F = "ΔF"   # per AI call: F_050 (forward to the AI number) minus F_mob (forward to the mobile); may be negative


def plan2_ai_call(c: dict, cache: bool = True) -> cm.Money:
    """One AI-answered call under plan 2: the existing forward is re-pointed, so instead of a new NTT leg (F)
    only the difference ΔF = F_050 - F_mob is added."""
    common = common_ai_call()
    common.terms.pop("F", None)
    return common + ai_part(c, cache) + cm.Money(terms={DELTA_F: 1})


def line_cost(ai_calls: float, recipients: int) -> tuple[float, dict, cm.Money]:
    """One short push per AI call and recipient (details by reply, which is free), plus resends."""
    messages = ai_calls * recipients * (1 + cm.RESEND_RATE)
    lp = cm.line_plan(messages)
    return messages, lp, cm.Money(jpy=(lp["fee_excl"] + lp["extra_excl"]) * (1 + TAX))


def existing_monthly(scn: dict) -> cm.Money:
    """What is already paid today and stays with plan 2: forwarding every call to the mobile, and the
    forwarding service's monthly fee (both depend on the contract)."""
    return cm.Money(terms={"F_mob": scn["ai"] + scn["human"], "B_fwd": 1})


def plan2_monthly(c: dict, scn: dict, cache: bool = True) -> tuple[cm.Money, float]:
    """What plan 2 adds or changes per month on top of today's cost. Returns the total and the recording
    storage per retention month in USD."""
    ai = plan2_ai_call(c, cache).scale(scn["ai"])
    _, _, line = line_cost(scn["ai"], scn["recipients"])
    storage_usd = cm.PRICES["twilio_recording_storage"]["value"] * scn["ai"] * cm.twilio_min(cm.DUR_MIN)
    fixed = (cm.Money(usd=cm.PRICES["twilio_number_050"]["value"]) + line + cm.Money(terms={"S_srv": 1})
             + monthly_plan(c) + cm.Money(terms={"R×録音保存": 1}))
    return ai + fixed, storage_usd


def plan2_terms(m: cm.Money, storage_usd: float) -> str:
    parts = []
    for sym, coef in m.terms.items():
        if not coef:
            continue
        if sym in (DELTA_F, "F_mob"):
            parts.append(f"{coef:,.0f}件×{sym}")
        elif sym == "R×録音保存":
            parts.append(f"R×{storage_usd * (1 + TAX) * FX:,.0f}円")
        else:
            parts.append(sym if coef == 1 else f"{coef:g}×{sym}")
    return " ＋ ".join(parts) if parts else "なし"


def render_plan2(by_id: dict) -> list[str]:
    out: list[str] = []
    w = out.append
    scns = cm.SCENARIOS
    w("## 8. 回線案2（検討中）：今の携帯への転送を基準にした月額\n")
    w("- **今の運用**：0120・092のどちらへの着信も、NTTの転送で担当者の携帯へ全件を転送している（142番で設定した可能性）。")
    w("- **回線案2**：システムをオンにしている間だけ、転送先を携帯からAIの番号（Twilioの050）に変える。オフの間は今のまま携帯へ転送する。"
      "人が受ける通話はクラウドを通らないので、回線案1の「人が受ける通話の追加分」はかからない。")
    w("- **採用は未確定**です。092の回線の種類、転送先の切り替えの方法（手動・遠隔の操作など）とその費用は、確認待ちです。")
    w("- 記号：F_mob＝携帯への転送の1通話あたりの料金、F_050＝AIの番号への転送の1通話あたりの料金、"
      "ΔF＝F_050−F_mob（転送先が変わる分の差。減る可能性もある）、B_fwd＝転送サービスの月額。どれも契約しだいで未確認。\n")
    w("**8-1. 今すでに払っている費用（回線案2でも続く）**\n")
    w("| 費目 | " + " | ".join(f"{k}（AI{v['ai']}件・人{v['human']}件）" for k, v in scns.items()) + " |")
    w("| --- | --- | --- | --- |")
    w("| 全件の携帯への転送料 | " + " | ".join(plan2_terms(existing_monthly(v), 0).split(" ＋ ")[0] for v in scns.values()) + " |")
    w("| 転送サービスの月額（ボイスワープ等） | B_fwd | B_fwd | B_fwd |")
    w("\n回線案2では、AIが受ける通話の分だけ、F_mob が F_050 に置き換わります（差は8-2の ΔF）。\n")
    w("**8-2. 回線案2で増える・変わる費用（月額、税込換算）**\n")
    w("含むもの：AIが受ける通話ごとの料金（AI＋Twilioの着信・音声の受け渡し・録音＋要約）、050番号、LINE（8-3）、"
      "業者のプランの月額（分かっているもの）。記号で残すもの：ΔF、サーバー S_srv、録音の保存 R、各候補の未確認の費用。\n")
    w("| 候補 | " + " | ".join(f"{k}（AI{v['ai']}件）" for k, v in scns.items()) + " | 記号で残す費用（少の場合） |")
    w("| --- | --- | --- | --- | --- |")
    for ids, label in ((PLAN2_TESTED, ""), (PLAN2_REFERENCE, "［参考］")):
        for cid in ids:
            c = by_id[cid]
            cells = [yen(plan2_monthly(c, scn)[0].jpy_total()) for scn in scns.values()]
            t_low, st = plan2_monthly(c, scns["少"])
            flag = "（AIの部分を含まない）" if not ai_part(c).usd else ""
            w(f"| {label}{c['name']}{flag} | " + " | ".join(cells) + f" | {plan2_terms(t_low, st)} |")
    w("\n8-2は、少・中・多の通知人数（" + "・".join(str(v["recipients"]) for v in scns.values()) + "人）で計算しています。\n")
    w("**8-3. LINEの費用（受け取る人数で決まる。1件につき短い通知を1通、詳細は返信で見る形）**\n")
    w("返信（Reply API）と手動のチャットは無料です。プッシュの通知は、受け取る人数分を数えます（グループは人数分）。"
      "無料プランは月200通までで、超えると送れません。\n")
    w("| 受け取る人 | " + " | ".join(f"{k}（AI{v['ai']}件）" for k, v in scns.items()) + " |")
    w("| --- | --- | --- | --- |")
    for n in (1, 2, 3, 5):
        cells = []
        for scn in scns.values():
            msgs, lp, money = line_cost(scn["ai"], n)
            cells.append(f"{msgs:,.0f}通・{lp['name']} {yen(money.jpy)}")
        w(f"| {n}人 | " + " | ".join(cells) + " |")
    w("\n**8-4. 回線案1と並べる（同じ候補、月額の追加費用、税込換算）**\n")
    w("| 候補 | 回線 | " + " | ".join(scns) + " | 違い |")
    w("| --- | --- | --- | --- | --- | --- |")
    for cid in PLAN2_TESTED:
        c = by_id[cid]
        p1 = [yen(monthly_total(c, scn)[0].jpy_total()) for scn in scns.values()]
        p2 = [yen(plan2_monthly(c, scn)[0].jpy_total()) for scn in scns.values()]
        w(f"| {c['name']} | 回線案1 | " + " | ".join(p1) + " | 人が受ける通話もクラウドを通る（Twilioの着信と、かけ直し）。"
          "NTTの転送料 F・追加番号 B_num・U_ntt が別に増える |")
        w(f"| {c['name']} | 回線案2 | " + " | ".join(p2) + " | 人が受ける通話は今のまま。AIの通話の転送料の差 ΔF だけが増減する |")
    w("\n- 回線案1の額は3章の前提（人が受ける通話は事務所の電話へかけ直すH1）。今の運用のように携帯へかけ直す場合の単価は未確認。")
    w("- どちらの案も、記号で残した費用（転送料、サーバー、録音の保存、未確認のAIの費用）を含みません。")
    return out


def yen(x: float) -> str:
    return f"{x:,.0f}円"


def per_call_terms(m: cm.Money) -> str:
    parts = []
    for sym, coef in m.terms.items():
        if not coef:
            continue
        parts.append(sym if coef == 1 else (f"{coef:,.0f}件×{sym}" if sym == "F" else f"{coef:g}×{sym}"))
    return " ＋ ".join(parts) if parts else "—"


def fmt_monthly_terms(m: cm.Money, storage_usd: float) -> str:
    return cm.fmt_terms(m.terms, storage_usd)


def candidates() -> list[dict]:
    return DATA["candidates"]


def render_frame() -> str:
    out: list[str] = []
    w = out.append
    w("# 候補の比較枠 v1（同じ前提での総費用と品質）\n")
    w(f"- 確認日：{DATA['checked']}。候補の出典は [候補一覧](./candidates-v1.md)、単価は `prototype/cost/candidates.json` と "
      "`prices.json`。`python3 -m prototype.cost.candidate_costs` で作り直せる。")
    w("- **業者を使う実測は0件。** 金額は既知の単価と使用量の仮定に基づく概算で、記号の費用を含まない。品質の欄は空欄（未実測）。")
    w("- **採用の決め方**：必要な会話条件を満たした候補の中から、コストと性能のバランスで選ぶ（利用者の決定）。"
      "配点や予算の上限は、利用者が決めない限り置かない。安くても必要な会話条件を満たさない候補は採用しない。")
    w("- 旧費用表（案A・案B）の約37円・約50円などは、旧構成と旧仮定による概算。この表の比較には、同じ前提で計算し直した値を使う。")
    w("- 最初の接続の試験は GPT-Live 1 と Gemini 3.8 Live の2つだけ（利用者の決定、2026-10-07）。"
      "ElevenLabs・Cartesia・GPT-Realtime-2.1 は保留で、Qwen は2つがよくなかった場合の次の候補。表には比較のため全候補を残す。\n")

    w("## 1. そろえた前提\n")
    w("| 項目 | 値 | 旧費用表との違い |")
    w("| --- | --- | --- |")
    w(f"| 件数・通知人数・為替・税 | 旧費用表の少・中・多と同じ（1ドル＝{FX:.0f}円、ドル建てに{TAX:.0%}） | なし |")
    w(f"| 通話時間・話す割合 | 3分、発信者{cm.CALLER_SPEECH_SHARE:.0%}・AI{cm.AI_SPEECH_SHARE:.0%} | なし |")
    w("| 回線 | 回線案1（NTTで常時転送、Twilioの050番号で着信。人が受ける通話はH1）。回線案2（検討中）は8章 | なし |")
    w("| 音声の受け渡し | Twilio Media Streams（$0.0044/分）を全候補に仮定 | 旧案Bは Relay。SIPや各社のTwilio連携を使う場合の差は、接続方式を決めた後に置き換える |")
    w("| 要約・通知・記録 | 要約はHaiku 4.5、LINE通知、Twilioの録音を全候補で同じに置く | なし |")
    w("| 端数 | Twilioの区間は1通話ごとに1分単位の切り上げ（平均＋0.5分）。各社の分単価は3分ちょうどで計算 | 各社の端数処理は未確認 |")
    w("| 業者のプラン | 分かっている月額だけ入れる（Cartesia Pro $5）。分からないものは記号 | 新規 |\n")

    w("## 2. 1通話あたり（AIが受ける通話・3分）\n")
    w("| 候補 | 種類 | AIの部分（既知の単価） | 共通の部分（電話・要約） | 合計の概算 | 記号で残す費用 | 料金の範囲・注意 |")
    w("| --- | --- | --- | --- | --- | --- | --- |")
    common = common_ai_call()
    for c in candidates():
        ai = ai_part(c)
        total = ai + common
        note = c["price"].get("note", "")
        note = "" if note == "「から」表記" else note
        status = {"confirmed": "単価は確認済み", "reference": "「から」表記（プランで変わる）",
                  "unconfirmed": "単価・換算が未確認"}[c["price"]["status"]]
        ai_txt = f"${ai.usd:.3f}" if ai.usd else "—"
        if c["price"]["kind"] == "realtime_model":
            nc = ai_part(c, cache=False)
            ai_txt += f"（キャッシュ不成立 ${nc.usd:.3f}）"
        total_txt = f"約{yen(total.jpy_total())}" + ("（AIの部分を含まない）" if not ai.usd else "")
        monthly_syms = "、".join(c["price"].get("monthly_symbols", {}))
        extra = f"。月額 {monthly_syms} は3章" if monthly_syms else ""
        w(f"| {c['name']} | {c['type']} | {ai_txt} | ${common.usd:.3f} | {total_txt} | "
          f"{per_call_terms(total)} | {status}{('。' + note) if note else ''}{extra} |")
    b = sum(cm.ai_call(cm.CONFIGS["B-Haiku"]).values(), cm.Money())
    w(f"| {DATA['baseline_relay']['name']} | 旧構成 | （Relay＋会話モデルの合計に含む） | （同左） | 約{yen(b.jpy_total())} | "
      f"{per_call_terms(b)} | {DATA['baseline_relay']['note']} |")
    w("\n- 記号の意味は [候補一覧](./candidates-v1.md) の各候補の「未確認」の欄にある。F はNTT転送区間の1通話あたりの料金。")
    w("- 合計の概算には記号の費用を含まない。記号が多い候補ほど、実際の額は上に振れうる。\n")

    w("## 3. 月額の追加費用（税込換算。少・中・多）\n")
    w("| 候補 | 少（AI60件・人90件） | 中（AI180件・人270件） | 多（AI480件・人720件） | 記号で残す費用（少の場合） |")
    w("| --- | --- | --- | --- | --- |")
    for c in candidates():
        cells = []
        for name, scn in cm.SCENARIOS.items():
            total, _ = monthly_total(c, scn)
            cells.append(yen(total.jpy_total()))
        t_low, st = monthly_total(c, cm.SCENARIOS["少"])
        flag = "（AIの部分を含まない）" if not ai_part(c).usd else ""
        w(f"| {c['name']}{flag} | " + " | ".join(cells) + f" | {fmt_monthly_terms(t_low, st)} |")
    w("\n- 人が受ける通話の追加分（回線案1）と、Twilioの番号・LINEの固定費は、全候補で同じ額が入っている。")
    w("- GPT-Realtime-2.1・miniは、キャッシュが成立する場合の額。成立しない場合は2章の括弧内の単価で増える。\n")

    w("## 4. 初期の開発費と保守の負担\n")
    w("| 候補 | 自社で作るもの（定性） | 初期の開発費 | 月々の保守 |")
    w("| --- | --- | --- | --- |")
    for c in candidates():
        w(f"| {c['name']} | {c['build']} | D_dev（未見積もり） | M_ops（未見積もり） |")
    w("\n- 初期の開発費と保守は、構成を絞った後に見積もる。0円とはみなさない。総保有費用で比べるときに加える。\n")

    w("## 5. 品質の欄（未実測）と、受付1件あたりの費用\n")
    w("**正常に一次受付を完了した1件あたりの費用 ＝ 月額の追加費用 ÷（AIが受けた件数 × 受付成功率）**")
    w("- 受付成功率：必要な受付情報（用件・折り返し先など）を、確認の規則どおりに取得・確認できた割合。"
      "その後に担当者が折り返すことは失敗に数えない。")
    w("- 1分が安くても、会話が長くなる候補や、人の補正が多い候補は、この値が上がる。\n")
    w("| 候補 | 受付成功率 | 自然さ | 応答の遅延（端末） | 割り込みで止まるまで（端末） | 相づちでの不要な停止 | 訂正の取りこぼし | 人の補正の多さ | 受付1件あたりの費用 |")
    w("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for c in candidates():
        w(f"| {c['name']} | 未実測 | 未実測 | 未実測 | 未実測 | 未実測 | 未実測 | 未実測 | 月額 ÷（件数 × 成功率） |")
    w("\n## 6. この枠の使い方\n")
    w("- まず、必要な会話条件（AIの発話中も聞き、相づち・訂正・停止の求めに対応する、自然な日本語の会話が続く、"
      "電話番号・日時の明瞭な復唱と確認、受付情報の正確さ）を満たすかを、共通の会話試験（C1〜C6）で確かめる。")
    w("- 満たした候補について、品質の欄と月額を同じ表に並べる。")
    w("- 他の候補より高く、測った品質も低い構成は、地域・運用などの別の利点がないかを確かめてから外す。")
    w("- 残った候補から「安価で十分な構成」と「費用は上がるが品質が上がる構成」を、実際の会話例と月額の差で示す。"
      "価格と性能の合計点だけで自動的に採用しない。")
    w("- 部分減速（番号だけを遅く読む）は、初期導入の必須条件から外した（利用者の決定）。比べる場合は参考の欄とする。\n")

    ex = DATA["existing_service"]
    w("## 7. 既存サービス（自社で作る案の比較対象）\n")
    plans = "、".join(f"{k} {v:,}円〜" for k, v in ex["plans_jpy_excl_tax"].items())
    w(f"- {ex['name']}：月払い・税別 {plans}。{ex['note']}（[出典]({ex['source']})）\n")
    out.extend(render_plan2({c["id"]: c for c in candidates()}))
    return "\n".join(out) + "\n"


def render_list() -> str:
    out: list[str] = []
    w = out.append
    w("# 候補一覧 v1（公式資料で確認したこと・未確認・除外の理由）\n")
    w(f"- 確認日：{DATA['checked']}。{DATA['note']}")
    w("- 業者APIの実行・試聴・通話による実測は行っていない。性能の順位や採用の決定ではない。")
    w("- 費用と品質は [候補の比較枠](./candidate-comparison-v1.md) で、同じ前提にそろえて比べる。\n")
    w("## 1. 一覧\n")
    w("| 候補 | 種類 | 日本語 | ブラウザーからの接続 | 料金（公式） | 位置づけ |")
    w("| --- | --- | --- | --- | --- | --- |")
    for c in candidates():
        pr = c["price"]
        if pr["kind"] == "per_min":
            price = f"${pr['usd_per_min']}/分" + ("（から）" if pr["status"] == "reference" else "")
        elif pr["kind"] == "live_tokens":
            price = f"音声入力 ${pr['audio_in_usd_per_min']}/分・出力 ${pr['audio_out_usd_per_min']}/分（目安）"
        elif pr["kind"] == "inworld":
            price = f"STT ${pr['stt_usd_per_hour']}/時間、TTS ${pr['tts_usd_per_m_chars']}/100万文字、LLM別"
        elif pr["kind"] == "realtime_model":
            price = "旧費用表と同じトークン単価"
        else:
            price = "単価は確認、換算は未確認"
        w(f"| {c['name']} | {c['type']} | {c['japanese']} | {c['browser']} | {price} | {c['wave']} |")
    w("\n## 2. 候補ごとの詳細\n")
    for c in candidates():
        w(f"### {c['name']}（{c['wave']}）\n")
        w(f"- モデル・構成：{c['model']}")
        w("- 公式で確認：" + "／".join(c["confirmed"]))
        w(f"- 日本語：{c['japanese']}")
        w(f"- 接続：ブラウザーは{c['browser']}。電話は{c['phone']}")
        w("- 未確認：" + "／".join(c["unconfirmed"]))
        w(f"- 自社で作るもの：{c['build']}")
        w(f"- 位置づけの理由：{c['reason']}")
        w("- 出典：" + "、".join(f"[{i + 1}]({u})" for i, u in enumerate(c["sources"])) + "\n")
    w("## 3. 除外・保留・部品として扱うもの\n")
    w("比較資料（2026-10-05）の判断を引き継いだもの。今回、この表の出典は再確認していない。\n")
    w("| 区分 | 対象 | 理由 |")
    w("| --- | --- | --- |")
    for e in DATA["excluded"]:
        w(f"| 除外 | {e['name']} | {e['reason']}（[出典]({e['source']})） |")
    for e in DATA["on_hold"]:
        w(f"| 保留 | {e['name']} | {e['reason']} |")
    for e in DATA["parts_only"]:
        w(f"| 部品として比較 | {e['name']} | {e['reason']} |")
    w("\n## 4. 初回の会話試験の構成（利用者の決定。採用の決定ではない）\n")
    w("| 構成 | 位置づけ | 比べる意味 |")
    w("| --- | --- | --- |")
    for c in candidates():
        if c["wave"].startswith(("初回", "保留", "基準", "次の候補")):
            w(f"| {c['name']} | {c['wave']} | {c['reason']} |")
    w("\n- 最初の接続の確認は GPT-Live 1 と Gemini 3.8 Live の2つだけで行う（2026-10-07）。保留の候補は、採用の候補からは外していない。")
    w("- GPT-Liveは、日本語の受付を最初に確かめる。業務処理を受け持つ裏方のモデル（delegation）も確かめる。")
    w("- Inworldは、接続の調査を優先して進める（ブラウザーからは中継が要り、提供段階がResearch previewのため、会話試験は2回目）。")
    w("- Grok・Deepgram・Retell・Vapiは、初回の結果と未確認の点の確認の後に比べる。")
    w("- 「同時に聞いて話す」「低遅延」などの機能名だけで順位を付けない。採用は共通の会話試験で決める。")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    print(render_list() if "--list" in sys.argv else render_frame(), end="")
