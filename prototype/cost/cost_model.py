"""Cost comparison for plan A / plan B under line plan 1 (NTT always-forward, cloud routing, H1).

Confirmed unit prices are computed numerically; unconfirmed ones stay as symbols
(F, V_stt, V_tts, V_asr, S_srv, U_ntt, R) and are printed as variable terms,
never replaced by zero. Usage assumptions are not the user's actual figures.

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
DUR_MIN = 3.0
RESEND_RATE = 0.10
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


def forward(dur_min: float) -> Money:
    return Money(terms={"F": dur_min})


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


def ai_call(cfg: dict, dur_min: float = DUR_MIN) -> dict[str, Money]:
    """Line items for one AI-answered call."""
    tw = lambda key: PRICES[key]["value"]
    items = {
        "NTT転送": forward(dur_min),
        "Twilio着信": Money(usd=tw("twilio_inbound") * dur_min),
        "録音（Twilio）": Money(usd=tw("twilio_recording") * dur_min),
        "要約": summary(cfg["summary"]),
    }
    if cfg["plan"] == "A":
        items["Media Streams"] = Money(usd=tw("twilio_media_streams") * dur_min)
        items["会話モデル（音声）"] = plan_a_model(cfg["model"], dur_min)
    else:
        items["ConversationRelay"] = Money(usd=tw("twilio_conversation_relay") * dur_min,
                                           terms={"V_stt": dur_min, "V_tts": TTS_CHARS_PER_CALL / 100})
        items["会話モデル（テキスト）"] = plan_b_model(cfg["model"])
    return items


def human_call(dur_min: float = DUR_MIN) -> dict[str, Money]:
    """Extra cost of one normal (human-answered) call under line plan 1 / H1. Same for A and B."""
    return {
        "NTT転送": forward(dur_min),
        "Twilio着信": Money(usd=PRICES["twilio_inbound"]["value"] * dur_min),
        "事務所へのかけ直し（固定電話あて）": Money(usd=PRICES["twilio_outbound_landline"]["value"] * dur_min),
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
        "ボイスワープ（ひかり電話オフィスタイプの場合）": Money(jpy=PRICES["ntt_voicewarp_monthly"]["value"]),
        "追加番号（事務所への経路H1）": Money(jpy=PRICES["ntt_extra_number_monthly"]["value"]),
        "番号ごとの料金（ユニバーサル等）": Money(terms={"U_ntt": 2}),
        f"LINE（{lp['name']}、{messages:.0f}通）": Money(jpy=(lp["fee_excl"] + lp["extra_excl"]) * (1 + TAX)),
        "サーバー": Money(terms={"S_srv": 1}),
        "録音の保存（Twilio、保存月数R）": Money(usd=PRICES["twilio_recording_storage"]["value"] * scn["ai"] * DUR_MIN,
                                         terms={}),  # multiplied by R below
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
            parts.append(f"{coef:,.0f}分×F")
        elif sym == "V_tts":
            parts.append(f"{coef:,.0f}×V_tts（100文字あたり）")
        elif sym in ("V_stt", "V_asr"):
            parts.append(f"{coef:,.4g}分×{sym}")
        elif coef == 1:
            parts.append(sym)
        else:
            parts.append(f"{coef:g}×{sym}")
    return " ＋ ".join(parts) if parts else "なし"


def render() -> str:
    hikari_f = PRICES["ntt_forward_hikari_to_050"]["value"]
    out = []
    w = out.append
    w("# 費用比較表 v1（案A・案B）\n")
    w("- 確認日：2026年10月4日。単価の出典は `prototype/cost/prices.json`。この文書は "
      "`python3 -m prototype.cost.cost_model` で作り直せる。")
    w("- **件数・通話時間・通知人数・トークン数はすべて仮定で、利用者の実績ではない。**")
    w("- 回線は、完成条件を満たす回線案1（NTTで常時転送し、クラウドで分岐。人への経路はH1）を前提にする。"
      "回線案2（代替運用）では、人が受ける通話の追加費用（3章の表）がかからない。")
    w(f"- 為替：1ドル＝{FX:.0f}円（仮置き、未確認）。ドル建ての額は税抜に{TAX:.0%}を加えてから円に換算する"
      "（Twilioは請求されることを確認済み。OpenAI・Anthropic・AWSは税の扱いが未確認なので、安全側として加えている）。"
      "NTTは税込の表示、LINEは税別の表示に10%を加えている。")
    w("- **未確認の単価は0円にせず、記号で残す**：F（NTT転送の円/分）、V_stt・V_tts（Relayの音声認識・音声合成が別料金になる場合）、"
      "V_asr（案Aの入力文字起こし）、S_srv（サーバー）、U_ntt（番号ごとの料金）、R（録音の保存月数。未決定）。\n")

    w("## 1. 仮定\n")
    w("| 想定 | AIが受ける通話／月 | 人が受ける通話／月 | 平均通話時間 | 通知先の人数 | 訂正による再送 |")
    w("| --- | --- | --- | --- | --- | --- |")
    for name, s in SCENARIOS.items():
        w(f"| {name} | {s['ai']}件 | {s['human']}件 | {DUR_MIN:g}分 | {s['recipients']}人 | {RESEND_RATE:.0%} |")
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
        ("NTT転送 F（ひかり電話→050）", "11.55円/3分＝3.85円/分（税込）。2027年4月から13.2円/3分", "092がひかり電話の場合だけ確認済み。**それ以外は未確認**"),
        ("ボイスワープ／追加番号", "月550円／月110円（税込）", "ひかり電話オフィスタイプの場合だけ確認済み"),
        ("番号ごとの料金（ユニバーサル等）", "U_ntt", "**未確認**"),
        ("LINE", "0円（200通）／5,000円（5,000通）／15,000円（30,000通、追加1通3円）、税別", "確認済み"),
        ("サーバー", "S_srv", "**未確認**（事業者が未決定）"),
    ]
    for r in rows:
        w(f"| {r[0]} | {r[1]} | {r[2]} |")

    w("\n## 3. 1通話あたりの変動費（税抜USDと、税込の円換算）\n")
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
    w("| **AI通話の合計（確定分）** | " + " | ".join(
        f"**${m.usd:.3f}（{yen(m.jpy_total())}）**" for m in totals.values()) + " |")
    w("| AI通話の合計（変数項） | " + " | ".join(fmt_terms(m.terms) for m in totals.values()) + " |")
    h = sum(human_call().values(), Money())
    w(f"| **人が受ける通話の追加分**（案A・案Bで同じ） | " + " | ".join(
        f"${h.usd:.4f}（{yen(h.jpy_total())}）＋ {fmt_terms(h.terms)}" for _ in CONFIGS) + " |")
    w(f"\n参考：F＝ひかり電話の値（{hikari_f}円/分）を入れると、1通話あたり {yen(hikari_f * DUR_MIN)} が加わる"
      f"（2027年4月以降は {yen(13.2 / 3 * DUR_MIN)}）。\n")

    w("## 4. 固定の月額\n")
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

    w("## 5. 月額の追加費用の合計（税込換算）\n")
    w("確定分と変数項を分けて示す。変数項を0円とみなした額ではない。\n")
    w("| 想定 | 構成 | 確定分（1ドル＝150円） | 140円／160円の場合 | 変数項 | 参考：F＝ひかり電話の値を入れた確定分 |")
    w("| --- | --- | --- | --- | --- | --- |")
    for name, scn in SCENARIOS.items():
        for ck, cfg in CONFIGS.items():
            total, parts = scenario_total(ck, scn)
            base = total.jpy_total()
            alt = " ／ ".join(yen(total.jpy_total(fx)) for fx in FX_ALT)
            f_minutes = total.terms.get("F", 0)
            terms = fmt_terms(total.terms, parts["storage_usd_per_R"])
            w(f"| {name} | {cfg['label']} | {yen(base)} | {alt} | {terms} | {yen(base + f_minutes * hikari_f)} |")
    w("\n**内訳の見方**")
    w("- 確定分には、AIが受ける通話、人が受ける通話の追加分、固定費（番号・ボイスワープ・追加番号・LINE）の確定部分が入っている。")
    w("- 人が受ける通話の追加分は、回線案1を選んだ場合にだけかかる（回線案2では0）。")
    for name, scn in SCENARIOS.items():
        h_total = sum(human_call().values(), Money()).scale(scn["human"])
        w(f"  - {name}：{yen(h_total.jpy_total())} ＋ {fmt_terms(h_total.terms)}")
    w("")

    w("## 6. 初期費用・試作費用\n")
    nb = PRICES
    w("| 費目 | 金額 | 備考 |")
    w("| --- | --- | --- |")
    w(f"| NTT：ボイスワープの工事 | {yen(nb['ntt_voicewarp_setup']['value'])}（税込） | ひかり電話オフィスタイプの場合。新設と同時なら無料 |")
    w(f"| NTT：追加番号の工事 | {yen(nb['ntt_extra_number_setup']['value'])}（税込） | 同上 |")
    w(f"| NTT：基本工事費 | {yen(nb['ntt_basic_work']['value'])}（税込） | 公式サイトから申し込んだ場合。それ以外は3,300円 |")
    w("| NTT：0120の着信先の変更 | 1,400円（税別） | 経路X1を選んだ場合だけ |")
    w("| Twilio：番号の取得・規制書類 | 初月の番号料（固定費に含む） | 規制書類の手数料は記載なし（未確認） |")
    est = measurement_budget.estimate()
    w(f"| 試作：案Aの計測（OpenAI） | 見込み約${est['openai_usd']:.0f}（実行側の上限$15） | [実測計画](./measurement-plan-v1.md)のT1・T3 |")
    w(f"| 試作：案Bの一部（Polly直接） | 見込み約${est['T2']['usd']:.2f}（実行側の上限$2） | T2。"
      "**Polly単体の費用で、案B全体（Relay・電話経路・会話モデル）の費用ではない** |")
    w("| 試作：電話経路（Twilio有料アカウント） | 変数（今回は範囲外） | 無料トライアルではRelay・Stream・録音が使えない |")
    w("| 開発の作業費 | この表には含めない | — |")
    w("\n## 7. この表から言えること・言えないこと\n")
    w("- 1通話あたりで最も大きいのは、案Aでは音声出力のトークン、案BではRelayの$0.07/分。どちらも仮定のトークン数と時間による。")
    w("- 案Bは、V_stt・V_tts（Relayの音声認識・音声合成が別料金かどうか）が確認できるまで、比較が確定しない。")
    w("- 回線案1では、人が受ける通話にも転送と発信の費用がかかる。件数が多いほど、この差が大きくなる。")
    w("- 採用は、費用だけでは決めない。必須条件と実測の結果を先に見る（設計レビュー3-4）。")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    print(render(), end="")
