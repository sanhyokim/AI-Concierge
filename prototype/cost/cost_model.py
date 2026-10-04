"""Cost comparison for plan A / plan B under line plan 1 (NTT always-forward, cloud routing, H1).

Known unit prices are computed numerically; unconfirmed ones stay as symbols
(F, B_fwd, B_num, V_stt, V_tts, V_asr, S_srv, U_ntt, R) and are printed as such,
never replaced by zero. NTT hikari-denwa prices are a conditional reference only (the 092
contract type is unknown), so they appear in a separate column, not in the estimate.
No vendor measurement exists yet: every amount is an estimate from known unit prices and
usage assumptions. Usage assumptions are not the user's actual figures.

Billing units: Twilio rounds each call leg (Call SID) up to whole minutes; NTT bills in
3-minute units (per-call rounding assumed). The average rounding overshoot is added
assuming the fractional part is uniformly distributed (+0.5 min per Twilio leg, +1.5 min
for the NTT 3-minute unit). Token-billed items (OpenAI, Anthropic) are not rounded.

python3 -m prototype.cost.cost_model > docs/cost-comparison-v1.md
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from . import measurement_budget

PRICES = json.loads((pathlib.Path(__file__).parent / "prices.json").read_text())["items"]
FX = 150.0            # JPY per USD, assumption (not verified)
FX_ALT = (140.0, 160.0)
TAX = 0.10            # applied to every tax-exclusive item (conservative where vendor tax treatment is unknown)
DUR_MIN = 3.0          # actual call duration before rounding to billing units
RESEND_RATE = 0.10
TWILIO_ROUND_ALLOW_MIN = 0.5   # average overshoot of per-leg rounding up to whole minutes
NTT_UNIT_MIN = 3.0
NTT_ROUND_ALLOW_MIN = 1.5      # average overshoot of rounding up to 3-minute units (assumed per call)
SCENARIOS = {          # assumptions, not the user's actual volumes
    "少": {"ai": 60, "human": 90, "recipients": 2},
    "中": {"ai": 180, "human": 270, "recipients": 3},
    "多": {"ai": 480, "human": 720, "recipients": 5},
}
# Plan A token assumptions (per 3-minute AI call)
CALLER_SPEECH_SHARE, AI_SPEECH_SHARE = 0.40, 0.45
TURNS, TOOL_RESPONSES = 10, 4
INSTRUCTION_TOKENS = 1500
TOOL_ARG_TOKENS = 300
# Plan B dialogue-model token assumptions (per 3-minute AI call)
B_UNCACHED_IN, B_CACHE_READ, B_CACHE_WRITE, B_OUT = 1500, 24000, 3200, 760
TTS_CHARS_PER_CALL = 400
# Post-call summary (both plans)
SUM_IN, SUM_OUT = 3000, 500


@dataclass
class Money:
    """usd (tax-exclusive) + jpy (tax-inclusive) + symbolic terms {symbol: coefficient}."""
    usd: float = 0.0
    jpy: float = 0.0
    terms: dict = field(default_factory=dict)

    def __add__(self, other: "Money") -> "Money":
        terms = dict(self.terms)
        for k, v in other.terms.items():
            terms[k] = terms.get(k, 0) + v
        return Money(self.usd + other.usd, self.jpy + other.jpy, terms)

    def scale(self, n: float) -> "Money":
        return Money(self.usd * n, self.jpy * n, {k: v * n for k, v in self.terms.items()})

    def jpy_total(self, fx: float = FX) -> float:
        return self.jpy + self.usd * (1 + TAX) * fx


def per_m(price_per_million: float, tokens: float) -> float:
    return price_per_million * tokens / 1_000_000


def plan_a_tokens(dur_min: float = DUR_MIN) -> dict:
    tok = PRICES["openai_audio_tokens"]
    audio_in = dur_min * 60 * CALLER_SPEECH_SHARE * tok["input_per_s"]
    audio_out = dur_min * 60 * AI_SPEECH_SHARE * tok["output_per_s"]
    responses = TURNS + TOOL_RESPONSES
    cached_text = responses * INSTRUCTION_TOKENS
    cached_audio = responses * (audio_in + audio_out) / 2
    return {"audio_in": audio_in, "audio_out": audio_out, "text_in": INSTRUCTION_TOKENS,
            "cached_text": cached_text, "cached_audio": cached_audio, "text_out": TOOL_ARG_TOKENS,
            "responses": responses}


def plan_a_model(model_key: str, dur_min: float = DUR_MIN) -> Money:
    p, t = PRICES[model_key], plan_a_tokens(dur_min)
    usd = (per_m(p["audio_in"], t["audio_in"]) + per_m(p["text_in"], t["text_in"])
           + per_m(p["cached_text_in"], t["cached_text"]) + per_m(p["cached_audio_in"], t["cached_audio"])
           + per_m(p["audio_out"], t["audio_out"]) + per_m(p["text_out"], t["text_out"]))
    caller_min = dur_min * CALLER_SPEECH_SHARE
    return Money(usd=usd, terms={"V_asr": caller_min})


def plan_b_model(model_key: str) -> Money:
    p = PRICES[model_key]
    usd = (per_m(p["in"], B_UNCACHED_IN) + per_m(p["cache_read"], B_CACHE_READ)
           + per_m(p["cache_write_5m"], B_CACHE_WRITE) + per_m(p["out"], B_OUT))
    return Money(usd=usd)


def summary(model_key: str) -> Money:
    p = PRICES[model_key]
    return Money(usd=per_m(p["in"], SUM_IN) + per_m(p["out"], SUM_OUT))


def twilio_min(dur_min: float, round_allow: bool = True) -> float:
    """Billed minutes of one Twilio call leg."""
    return dur_min + (TWILIO_ROUND_ALLOW_MIN if round_allow else 0.0)


def ntt_units(dur_min: float, round_allow: bool = True) -> float:
    """Billed 3-minute units of one NTT forwarded leg."""
    return (dur_min + (NTT_ROUND_ALLOW_MIN if round_allow else 0.0)) / NTT_UNIT_MIN


def forward() -> Money:
    """NTT forwarded leg of one call: F is a per-call symbol (billing unit and rounding included)."""
    return Money(terms={"F": 1})


def hikari_reference(m: Money, from_2027_04: bool = False) -> float:
    """JPY to add if 092 is hikari-denwa office type (conditional reference, not part of the estimate)."""
    f = PRICES["ntt_forward_hikari_to_050"]
    per_call = ntt_units(DUR_MIN) * (f["value_from_2027_04"] if from_2027_04 else f["value"])
    return (m.terms.get("F", 0) * per_call + m.terms.get("B_fwd", 0) * PRICES["ntt_voicewarp_monthly"]["value"]
            + m.terms.get("B_num", 0) * PRICES["ntt_extra_number_monthly"]["value"])


CONFIGS = {
    "A-2.1":   {"plan": "A", "model": "openai_rt21", "summary": "anthropic_haiku45",
                "label": "案A gpt-realtime-2.1（要約 Haiku 4.5）"},
    "A-mini":  {"plan": "A", "model": "openai_rt21_mini", "summary": "anthropic_haiku45",
                "label": "案A gpt-realtime-2.1-mini（参考、要約 Haiku 4.5）"},
    "B-Haiku": {"plan": "B", "model": "anthropic_haiku45", "summary": "anthropic_haiku45",
                "label": "案B Relay＋Haiku 4.5（要約も同じ）"},
    "B-Sonnet": {"plan": "B", "model": "anthropic_sonnet55", "summary": "anthropic_sonnet55",
                 "label": "案B Relay＋Sonnet 5.5（要約も同じ）"},
}


def ai_call(cfg: dict, dur_min: float = DUR_MIN, round_allow: bool = True) -> dict[str, Money]:
    """Line items for one AI-answered call (one Twilio leg)."""
    tw = lambda key: PRICES[key]["value"]
    billed = twilio_min(dur_min, round_allow)
    items = {
        "NTT転送": forward(),
        "Twilio着信": Money(usd=tw("twilio_inbound") * billed),
        "録音（Twilio）": Money(usd=tw("twilio_recording") * billed),
        "要約": summary(cfg["summary"]),
    }
    if cfg["plan"] == "A":
        items["Media Streams"] = Money(usd=tw("twilio_media_streams") * billed)
        items["会話モデル（音声）"] = plan_a_model(cfg["model"], dur_min)
    else:
        items["ConversationRelay"] = Money(usd=tw("twilio_conversation_relay") * billed,
                                           terms={"V_stt": billed, "V_tts": TTS_CHARS_PER_CALL / 100})
        items["会話モデル（テキスト）"] = plan_b_model(cfg["model"])
    return items


def human_call(dur_min: float = DUR_MIN, round_allow: bool = True) -> dict[str, Money]:
    """Extra cost of one normal (human-answered) call under line plan 1 / H1. Same for A and B.
    Inbound and outbound are separate Call SIDs, each rounded up on its own."""
    billed = twilio_min(dur_min, round_allow)
    return {
        "NTT転送": forward(),
        "Twilio着信": Money(usd=PRICES["twilio_inbound"]["value"] * billed),
        "事務所へのかけ直し（固定電話あて）": Money(usd=PRICES["twilio_outbound_landline"]["value"] * billed),
    }


def line_plan(messages: float) -> dict:
    for plan in PRICES["line_plans"]["plans"]:
        if messages <= plan["included"]:
            return {"name": plan["name"], "fee_excl": plan["fee"], "extra_excl": 0}
    std = PRICES["line_plans"]["plans"][-1]
    return {"name": std["name"], "fee_excl": std["fee"], "extra_excl": (messages - std["included"]) * std["extra"]}


def monthly_fixed(scn: dict) -> dict[str, Money]:
    messages = scn["ai"] * scn["recipients"] * (1 + RESEND_RATE)
    lp = line_plan(messages)
    return {
        "Twilio 050番号（1本）": Money(usd=PRICES["twilio_number_050"]["value"]),
        "NTTの転送オプション（転送の設定1つ）": Money(terms={"B_fwd": 1}),
        "NTTの追加番号（事務所への経路H1）": Money(terms={"B_num": 1}),
        "番号ごとの料金（ユニバーサル等）": Money(terms={"U_ntt": 2}),
        f"LINE（{lp['name']}、{messages:.0f}通）": Money(jpy=(lp["fee_excl"] + lp["extra_excl"]) * (1 + TAX)),
        "サーバー": Money(terms={"S_srv": 1}),
        "録音の保存（Twilio、保存月数R）": Money(usd=PRICES["twilio_recording_storage"]["value"] * scn["ai"]
                                         * twilio_min(DUR_MIN), terms={}),  # multiplied by R below
    }


def scenario_total(cfg_key: str, scn: dict) -> tuple[Money, dict]:
    cfg = CONFIGS[cfg_key]
    ai = sum(ai_call(cfg).values(), Money()).scale(scn["ai"])
    human = sum(human_call().values(), Money()).scale(scn["human"])
    fixed_items = monthly_fixed(scn)
    storage = fixed_items.pop("録音の保存（Twilio、保存月数R）")
    fixed = sum(fixed_items.values(), Money())
    # storage grows with retention R (months, undecided): keep as symbol with its USD/month per R
    storage_term = Money(terms={"R×録音保存": 1})
    total = ai + human + fixed + storage_term
    return total, {"ai": ai, "human": human, "fixed": fixed, "storage_usd_per_R": storage.usd}


# --- rendering ------------------------------------------------------------------

def yen(x: float) -> str:
    return f"{x:,.0f}円"


def fmt_terms(terms: dict, storage_usd_per_r: float | None = None) -> str:
    parts = []
    for sym, coef in terms.items():
        if not coef:
            continue
        if sym == "R×録音保存" and storage_usd_per_r is not None:
            parts.append(f"R×{storage_usd_per_r * (1 + TAX) * FX:,.0f}円")
        elif sym == "F":
            parts.append("F" if coef == 1 else f"{coef:,.0f}件×F")
        elif sym == "V_tts":
            parts.append(f"{coef:,.0f}×V_tts（100文字あたり）")
        elif sym in ("V_stt", "V_asr"):
            parts.append(f"{coef:,.4g}分×{sym}")
        elif coef == 1:
            parts.append(sym)
        else:
            parts.append(f"{coef:g}×{sym}")
    return " ＋ ".join(parts) if parts else "なし"


ESTIMATE_NOTE = ("業者を使う実測は0件。金額は、既知の単価と使用量の仮定に基づく概算で、"
                 "未確認の費用（F、B_fwd、B_num、V_stt、V_tts、V_asr、U_ntt、S_srv、R）を含まない")


def render() -> str:
    f = PRICES["ntt_forward_hikari_to_050"]
    f_ref = ntt_units(DUR_MIN) * f["value"]
    f_ref_2027 = ntt_units(DUR_MIN) * f["value_from_2027_04"]
    out = []
    w = out.append
    w("# 費用比較表 v1（案A・案B）\n")
    w("- 確認日：2026年10月4日。単価の出典は `prototype/cost/prices.json`。この文書は "
      "`python3 -m prototype.cost.cost_model` で作り直せる。")
    w(f"- **{ESTIMATE_NOTE}。**")
    w("- **件数・通話時間・通知人数・トークン数はすべて仮定で、利用者の実績ではない。**")
    w("- **NTTひかり電話の料金は、092の契約種別が確認できるまで条件付きの参考値として扱い、概算には入れない。** "
      "ひかり電話オフィスタイプだった場合の額は、別の列（参考・条件付き）にだけ示す。")
    w("- 回線は、完成条件を満たす回線案1（NTTで常時転送し、クラウドで分岐。人への経路はH1）を前提にする。"
      "回線案2（代替運用）では、人が受ける通話の追加費用がかからない。")
    w(f"- 為替：1ドル＝{FX:.0f}円（仮置き、未確認）。税の扱いは3章のとおり。\n")

    w("## 1. 仮定\n")
    w("| 想定 | AIが受ける通話／月 | 人が受ける通話／月 | 平均通話時間 | 通知先の人数 | 訂正による再送 |")
    w("| --- | --- | --- | --- | --- | --- |")
    for name, sc in SCENARIOS.items():
        w(f"| {name} | {sc['ai']}件 | {sc['human']}件 | {DUR_MIN:g}分 | {sc['recipients']}人 | {RESEND_RATE:.0%} |")
    w(f"\n**通話時間と請求単位**")
    w(f"- 平均通話時間の{DUR_MIN:g}分は、請求単位に切り上げる前の実際の時間とする。")
    w(f"- 端数の切り上げによる上振れは、端数が一様に分かれると仮定した平均を加える。"
      f"Twilioは区間（Call SID）ごとに＋{TWILIO_ROUND_ALLOW_MIN:g}分（課金は{twilio_min(DUR_MIN):g}分）、"
      f"NTTの3分単位は＋{NTT_ROUND_ALLOW_MIN:g}分（課金は{ntt_units(DUR_MIN):g}単位）。")
    w("- 1通話あたりの振れ幅：Twilioは1区間あたり0〜＋1分、NTTは0〜＋1単位。端数がない場合を下限として4章に併記する。")
    w("- トークンで課金されるもの（OpenAI・Anthropic）は切り上げない。")
    t = plan_a_tokens()
    w("\n**案Aのトークン数**（1通話・3分あたり。要実測）")
    w(f"- 発信者が話す時間は通話の{CALLER_SPEECH_SHARE:.0%}：音声入力 {t['audio_in']:,.0f} トークン（100msあたり1トークン【公式】）")
    w(f"- AIが話す時間は通話の{AI_SPEECH_SHARE:.0%}：音声出力 {t['audio_out']:,.0f} トークン（50msあたり1トークン【公式】）")
    w(f"- 応答の回数 {t['responses']}回（ターン{TURNS}回と関数呼び出し{TOOL_RESPONSES}回）")
    w(f"  - 応答のたびに、指示文 {INSTRUCTION_TOKENS:,}トークンと会話の履歴を読み直す。これはキャッシュ扱いとし、テキスト {t['cached_text']:,.0f}・音声 {t['cached_audio']:,.0f} トークンになる。")
    w(f"  - 最初の指示文だけはキャッシュされない：テキスト入力 {INSTRUCTION_TOKENS:,}トークン")
    w(f"- 関数の引数などのテキスト出力：{TOOL_ARG_TOKENS}トークン")
    w("\n**案Bの会話モデルのトークン数**（1通話あたり。要実測）")
    w(f"- 入力 {B_UNCACHED_IN:,}、キャッシュ読み取り {B_CACHE_READ:,}、キャッシュ書き込み {B_CACHE_WRITE:,}、出力 {B_OUT:,}")
    w(f"- AIが話す文字数：{TTS_CHARS_PER_CALL}文字（V_ttsの計算に使う）")
    w(f"\n**要約**（両案とも、1通話あたり）：入力 {SUM_IN:,}トークン、出力 {SUM_OUT}トークン\n")

    w("## 2. 単価と確認状況\n")
    w("| 項目 | 単価 | 確認状況 |")
    w("| --- | --- | --- |")
    rows = [
        ("Twilio 050番号", "$4.75/月", "確認済み"), ("Twilio 着信", "$0.0100/分", "確認済み"),
        ("Twilio 発信（固定電話あて）", "$0.0746/分", "確認済み"), ("Media Streams", "$0.0044/分", "確認済み（日本の料金ページ）"),
        ("ConversationRelay", "$0.07/分", "確認済み（日本の料金ページ）"),
        ("Relayの音声認識・音声合成", "V_stt・V_tts", "**未確認**（「voice costs billed separately」とあるのみ）"),
        ("録音／録音の保存", "$0.0025/分 ／ $0.0005/分/月", "確認済み"),
        ("gpt-realtime-2.1（1Mトークン）", "音声入力$32、キャッシュ$0.40、音声出力$64、テキスト入力$4、テキスト出力$24", "確認済み"),
        ("gpt-realtime-2.1-mini（1Mトークン）", "音声入力$10、キャッシュ音声$0.30、キャッシュテキスト$0.06、音声出力$20、テキスト入力$0.60、テキスト出力$2.40", "確認済み"),
        ("案Aの入力文字起こし", "V_asr", "**未確認**"),
        ("Haiku 4.5（1Mトークン）", "入力$1、出力$5、キャッシュ読み取り$0.10、書き込み$1.25", "入出力は確認済み。キャッシュは倍率の規則による"),
        ("Sonnet 5.5（1Mトークン）", "入力$2、出力$10、キャッシュ読み取り$0.20、書き込み$2.50", "確認済み（書き込みは倍率の規則による）"),
        ("NTT転送区間 F（1通話あたり）", "F", "**未確認**（092の契約種別しだい）。参考：ひかり電話オフィスタイプなら050あて11.55円/3分（税込）、2027年4月から13.2円/3分"),
        ("NTTの転送オプション B_fwd／追加番号 B_num（月額）", "B_fwd・B_num", "**未確認**（同上）。参考：ひかり電話オフィスタイプならボイスワープ月550円、追加番号月110円（税込）"),
        ("番号ごとの料金（ユニバーサル等）", "U_ntt", "**未確認**"),
        ("LINE", "0円（200通）／5,000円（5,000通）／15,000円（30,000通、追加1通3円）、税別", "確認済み"),
        ("サーバー", "S_srv", "**未確認**（事業者が未決定）"),
    ]
    for r in rows:
        w(f"| {r[0]} | {r[1]} | {r[2]} |")

    w("\n## 3. 課金単位・端数処理・税の確認結果\n")
    w("| 事業者・項目 | 課金単位 | 通話ごとの端数処理 | 税 | 状態 |")
    w("| --- | --- | --- | --- | --- |")
    w("| NTT西日本 ひかり電話オフィスタイプ → 050（転送区間） | 3分（11.55円/3分。2027年4月から13.2円/3分） | 料金表は「円/3分」の表示だけで、端数処理の約款の条文は未確認。**安全側で、1通話ごとに3分単位の切り上げを仮定** | 税込（「特に記載のない限り税込」） | 契約種別が分かるまで**条件付きの参考値** |")
    w("| 同 転送区間の負担 | — | 転送元（契約者）の負担。転送先が話し中・無応答なら課金なし | — | ひかり電話のボイスワープの案内で確認。オフィスタイプでの適用は条件付き |")
    w("| 同 ボイスワープ／追加番号／工事費 | 月額・1回 | — | 税込 | 条件付きの参考値 |")
    w("| Twilio 着信・発信・Media Streams | 1分 | **通話（Call SID）ごとに1分単位で切り上げ**。H1の受け直しは、着信と発信の2本が別々に切り上げられる | 税抜。日本の顧客には、Twilio Japanが消費税10%を月ごとに請求 | 確認済み（端数はサポート記事。本文は直接取得できず、検索結果の抜粋で確認） |")
    w("| Twilio ConversationRelay・録音 | 1分 | 切り上げの対象一覧に載っていない。**未確認。安全側で同じ切り上げを仮定** | 同上 | 未確認 |")
    w("| Twilio 録音の保存 | 1分・1か月 | — | 同上 | 単価は確認済み |")
    w("| OpenAI・Anthropic | トークン | 切り上げなし（トークン数で課金） | **未確認**。安全側で10%を加える | 単価は確認済み |")
    w("| AWS Polly（試作だけ） | 文字 | — | **未確認**。安全側で10%を加える | 単価は確認済み |")
    w("| LINE公式アカウント | 月額・通数 | — | 税別の表示。10%を加える | 確認済み |")
    w("\n出典：[NTT西日本 ひかり電話オフィスタイプ 料金](https://business.ntt-west.co.jp/service/ipphone/office/price.html)、"
      "[ひかり電話 ボイスワープ](https://flets-w.com/opt/hikaridenwa/service/voicewarp/)、"
      "[Twilio 日本の音声料金](https://www.twilio.com/en-us/voice/pricing/jp)、"
      "[Twilio 通話時間の切り上げ](https://support.twilio.com/hc/en-us/articles/223132307)、"
      "[Twilio Japanの消費税](https://support.twilio.com/hc/en-us/articles/360033933914)（いずれも2026年10月4日に確認）\n")

    w("## 4. 1通話あたりの変動費（概算。税抜USDと、税込の円換算）\n")
    w(f"{ESTIMATE_NOTE}。\n")
    w("| 費目 | " + " | ".join(c["label"] for c in CONFIGS.values()) + " |")
    w("| --- | " + " | ".join("---" for _ in CONFIGS) + " |")
    keys = ["NTT転送", "Twilio着信", "Media Streams", "ConversationRelay", "会話モデル（音声）", "会話モデル（テキスト）",
            "要約", "録音（Twilio）"]
    calls = {k: ai_call(c) for k, c in CONFIGS.items()}
    for key in keys:
        cells = []
        for ck in CONFIGS:
            m = calls[ck].get(key)
            if m is None:
                cells.append("—")
            else:
                num = f"${m.usd:.4f}" if m.usd else ""
                var = fmt_terms(m.terms) if m.terms else ""
                cells.append(" ＋ ".join(x for x in (num, var) if x) or "—")
        w(f"| AI通話：{key} | " + " | ".join(cells) + " |")
    totals = {ck: sum(calls[ck].values(), Money()) for ck in CONFIGS}
    lows = {ck: sum(ai_call(c, round_allow=False).values(), Money()) for ck, c in CONFIGS.items()}
    w("| **AI通話の合計（既知の単価による概算。未確認の費用を除く）** | " + " | ".join(
        f"**${m.usd:.3f}（約{yen(m.jpy_total())}）**" for m in totals.values()) + " |")
    w("| 同（端数がない場合の下限） | " + " | ".join(
        f"${m.usd:.3f}（約{yen(m.jpy_total())}）" for m in lows.values()) + " |")
    w("| AI通話の未確認の費用（記号） | " + " | ".join(fmt_terms(m.terms) for m in totals.values()) + " |")
    h = sum(human_call().values(), Money())
    h_low = sum(human_call(round_allow=False).values(), Money())
    w(f"| **人が受ける通話の追加分**（案A・案Bで同じ） | " + " | ".join(
        f"${h.usd:.4f}（約{yen(h.jpy_total())}。下限{yen(h_low.jpy_total())}）＋ {fmt_terms(h.terms)}" for _ in CONFIGS) + " |")
    w(f"\n参考（条件付き）：092がひかり電話オフィスタイプなら、F＝約{f_ref:.1f}円/通話"
      f"（{ntt_units(DUR_MIN):g}単位×{f['value']}円。2027年4月以降は約{f_ref_2027:.1f}円。"
      f"端数がない場合の下限は{f['value']}円）。\n")

    w("## 5. 固定の月額\n")
    for name, scn in SCENARIOS.items():
        items = monthly_fixed(scn)
        storage = items.pop("録音の保存（Twilio、保存月数R）")
        parts = []
        for k, m in items.items():
            val = yen(m.jpy_total()) if (m.usd or m.jpy or not m.terms) else ""
            var = fmt_terms(m.terms) if m.terms else ""
            parts.append(f"{k}：{' ＋ '.join(x for x in (val, var) if x)}")
        parts.append(f"録音の保存（Twilio）：R×{yen(storage.usd * (1 + TAX) * FX)}（Rは保存月数。未決定。例：R＝12なら"
                     f"{yen(storage.usd * 12 * (1 + TAX) * FX)}）")
        w(f"**{name}**")
        for p in parts:
            w(f"- {p}")
        w("")
    w(f"参考（条件付き）：ひかり電話オフィスタイプなら、B_fwd＝ボイスワープ月{PRICES['ntt_voicewarp_monthly']['value']}円、"
      f"B_num＝追加番号月{PRICES['ntt_extra_number_monthly']['value']}円（税込）。\n")

    w("## 6. 月額の追加費用の合計（税込換算）\n")
    w(f"{ESTIMATE_NOTE}。未確認の費用を0円とみなした額ではない。\n")
    w("| 想定 | 構成 | 概算（既知の単価のみ。1ドル＝150円） | 140円／160円の場合 | 未確認の費用（記号） | 参考：092がひかり電話オフィスタイプの場合（条件付き。F・B_fwd・B_numを入れた額） |")
    w("| --- | --- | --- | --- | --- | --- |")
    for name, scn in SCENARIOS.items():
        for ck, cfg in CONFIGS.items():
            total, parts = scenario_total(ck, scn)
            base = total.jpy_total()
            alt = " ／ ".join(yen(total.jpy_total(fx)) for fx in FX_ALT)
            terms = fmt_terms(total.terms, parts["storage_usd_per_R"])
            w(f"| {name} | {cfg['label']} | {yen(base)} | {alt} | {terms} | {yen(base + hikari_reference(total))} |")
    w("\n**内訳の見方**")
    w("- 概算には、AIが受ける通話、人が受ける通話の追加分（Twilio分）、固定費のうち単価が確認できたもの（Twilioの番号・LINE）が入っている。")
    w("- 参考の列も、U_ntt・S_srv・R・V_stt・V_tts・V_asrは含まない。")
    w("- 人が受ける通話の追加分は、回線案1を選んだ場合にだけかかる（回線案2では0）。")
    for name, scn in SCENARIOS.items():
        h_total = sum(human_call().values(), Money()).scale(scn["human"])
        w(f"  - {name}：{yen(h_total.jpy_total())} ＋ {fmt_terms(h_total.terms)}")
    w("")

    w("## 7. 初期費用・試作費用\n")
    nb = PRICES
    w("| 費目 | 金額 | 備考 |")
    w("| --- | --- | --- |")
    w(f"| NTT：転送オプションの工事 | 未確認 | 参考（条件付き）：ひかり電話オフィスタイプのボイスワープは{yen(nb['ntt_voicewarp_setup']['value'])}（税込）。新設などと同時なら無料 |")
    w(f"| NTT：追加番号の工事 | 未確認 | 参考（条件付き）：同 {yen(nb['ntt_extra_number_setup']['value'])}（税込） |")
    bw, bd = nb["ntt_basic_work_no_dispatch"], nb["ntt_basic_work_dispatch"]
    w(f"| NTT：基本工事費 | 未確認 | 参考（条件付き）：同 無派遣工事{yen(bw['min'])}〜{yen(bw['max'])}、派遣工事{yen(bd['value'])}〜"
      f"（2027年4月から{yen(bd['value_from_2027_04'])}〜）。いずれも税込 |")
    w("| NTT：0120の着信先の変更 | 1,400円（税別） | 経路X1を選んだ場合だけ |")
    w("| Twilio：番号の取得・規制書類 | 初月の番号料（固定費に含む） | 規制書類の手数料は記載なし（未確認） |")
    est = measurement_budget.estimate()
    w(f"| 試作：案Aの計測（OpenAI） | 概算約${est['openai_usd']:.0f}（提案額・未承認：上限$15） | [実測計画](./measurement-plan-v1.md)のT1・T3 |")
    w(f"| 試作：案Bの一部（Polly直接） | 概算約${est['T2']['usd']:.2f}（提案額・未承認：上限$2） | T2。"
      "**Polly単体の費用で、案B全体（Relay・電話経路・会話モデル）の費用ではない** |")
    w("| 試作：電話経路（Twilio有料アカウント） | 未確認（今回は範囲外） | 無料トライアルではRelay・Stream・録音が使えない |")
    w("| 開発の作業費 | この表には含めない | — |")
    w("\n## 8. この表から言えること・言えないこと\n")
    w("- どの金額も、業者を使う実測がない段階の概算である。実測の後、使用量の仮定を置き換える。")
    w("- 1通話あたりで最も大きいのは、案Aでは音声出力のトークン、案BではRelayの$0.07/分。どちらも仮定のトークン数と時間による。")
    w("- 端数の切り上げで、Twilioの分の費用は1区間あたり最大1分ぶん増える。短い通話が多いほど、この影響が大きくなる。")
    w("- 案Bは、V_stt・V_tts（Relayの音声認識・音声合成が別料金かどうか）が確認できるまで、比較が確定しない。")
    w("- 回線案1では、人が受ける通話にも転送と発信の費用がかかる。件数が多いほど、この差が大きくなる。")
    w("- NTT側の費用（F・B_fwd・B_num・工事費）は、092の契約種別が分かるまで決まらない。")
    w("- 採用は、費用だけでは決めない。必須条件と実測の結果を先に見る（設計レビュー3-4）。")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    print(render(), end="")
