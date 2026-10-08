"""Consent heard in the caller's own words (common logic for every adapter).

- AI refusal (「AIは使わないでください」): stop sending to the AI, stop its audio and business processing.
- Recording refusal (「録音はしないでください」): stop and delete the recording; the AI conversation continues
  unless AI processing is refused too.
- A wish to talk to a person (「人と話したい」) is not a refusal by itself (spec 1-5, X-02). A refusal in the
  same utterance wins: 「人と話したいので、AIは使わないでください」 is an AI refusal.
- A negated refusal (「AIは嫌ではありません」「AIでも大丈夫です」) is not a refusal.
The utterance is split into clauses and each clause is judged on its own. Keyword rules for the prototype;
what the AI vendor heard before this point has already been sent and cannot be recalled.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_AI = ("AI", "エーアイ", "人工知能", "機械", "ロボット", "自動音声", "自動応答")
_RECORDING = ("録音",)
_REFUSE = ("使わないで", "使わない", "使うな", "使ってほしくない", "嫌", "いやです", "いやだ", "イヤ", "やめて", "止めて",
           "とめて", "話したくない", "お断り", "断り", "断る", "いらない", "要らない", "は結構", "しないで", "しないでほしい",
           "勘弁", "困ります", "やめてほしい")
_NOT_REFUSE = ("嫌ではな", "嫌じゃな", "嫌ではありません", "いやではな", "いやじゃな", "イヤじゃな", "構わな", "構いません",
               "かまわな", "かまいません", "大丈夫", "でもいい", "でも良い", "でいい", "で良い", "で結構", "でも結構",
               "問題な", "問題ありません", "OK", "オッケー", "いいですよ")
_HUMAN = ("人と話", "人間と話", "担当者と話", "担当の方と", "担当者に代", "人に代", "スタッフと話", "社員の方と", "人につな",
          "担当者につな")
_SPLIT = re.compile(r"[、。,.!?！？\n]|ので|から|けれども|けれど|けど|ですが|が、")


@dataclass(frozen=True)
class SpokenConsent:
    ai_refused: bool = False
    recording_refused: bool = False
    human_request: bool = False


def _clauses(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", text).replace(" ", "").replace("　", "")
    return [c for c in _SPLIT.split(t) if c]


def _refuses(clause: str, subjects: tuple[str, ...]) -> bool:
    if not any(s in clause for s in subjects):
        return False
    if any(n in clause for n in _NOT_REFUSE):
        return False
    return any(r in clause for r in _REFUSE)


def detect(text: str) -> SpokenConsent:
    clauses = _clauses(text)
    ai = any(_refuses(c, _AI) for c in clauses)
    rec = any(_refuses(c, _RECORDING) for c in clauses)
    human = not ai and any(h in c for c in clauses for h in _HUMAN)
    return SpokenConsent(ai_refused=ai, recording_refused=rec, human_request=human)
