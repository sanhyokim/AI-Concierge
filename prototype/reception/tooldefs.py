"""Tool schemas every voice adapter gets: the existing intake tools plus FAQ lookup and emergency flagging."""
from __future__ import annotations

from ..concierge.prompts import TOOLS

RECEPTION_TOOLS = TOOLS + [
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
]
