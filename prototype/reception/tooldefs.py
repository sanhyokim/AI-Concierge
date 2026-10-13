"""Tool schemas every voice adapter gets: the existing intake tools plus FAQ lookup, emergency flagging and the
end of the call."""
from __future__ import annotations

from ..concierge.prompts import TOOLS

RECEPTION_TOOLS = TOOLS + [
    {
        "type": "function",
        "name": "save_fields",
        "description": ("聞き取った値を、まとめて1回で保存する。聞いていない項目は空文字にする。"
                        "店名・お名前・用件・希望日時（具体的な日付。例 2026-10-15）・折り返しの電話番号。確認済みにはならない。"),
        "parameters": {"type": "object",
                       "properties": {"shop_name": {"type": "string"}, "caller_name": {"type": "string"},
                                      "request": {"type": "string"}, "preferred_datetime": {"type": "string"},
                                      "callback_number": {"type": "string"}},
                       "required": []},
    },
    {
        "type": "function",
        "name": "lookup_faq",
        "description": "料金・条件・対応範囲などを聞かれたときに呼ぶ。返ってきた answer の範囲だけで答える。",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
    },
    {
        "type": "function",
        "name": "flag_emergency",
        "description": ("火や煙などの危険の訴えがあったときに呼ぶ。obvious＝明白な現在の危険、ambiguous＝曖昧、"
                        "undetermined＝判断できない。返ってきた指示（案内の音声、次に伺うこと）に従う。"),
        "parameters": {"type": "object",
                       "properties": {"level": {"type": "string", "enum": ["obvious", "ambiguous", "undetermined"]},
                                      "reason": {"type": "string"}},
                       "required": ["level"]},
    },
    {
        "type": "function",
        "name": "end_call",
        "description": ("最後のあいさつ（「お電話ありがとうございました。失礼いたします。」）を言い終えた後に呼ぶ。"
                        "これ以上は話さない。お客様が先に切るのを少し待ってから、こちらで電話を切る。"),
        # one property: an object without properties may be refused by some vendors' schema checks
        "parameters": {"type": "object",
                       "properties": {"reason": {"type": "string",
                                                 "description": "終える理由（例：受付が終わった、お客様が終えたいと言った）"}},
                       "required": []},
    },
]
