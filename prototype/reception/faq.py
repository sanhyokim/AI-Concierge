"""FAQ lookup over the FAQ list frozen into a call's snapshot, and the instruction block built from it."""
from __future__ import annotations

import datetime as dt

from ..concierge.readings import WEEKDAY_KANJI
from .settings import FALLBACK_ANSWER

DAYS_AHEAD = 14
_NAMED = {0: "今日", 1: "明日", 2: "明後日"}
_WEEK = {0: "今週", 1: "来週", 2: "再来週"}


def _norm(text: str) -> str:
    return text.replace(" ", "").replace("　", "").replace("？", "?")


def faq_lookup(faqs: list[dict], question: str) -> dict:
    """Best keyword match among the enabled FAQ of this call. No match -> FAQ-10 and an open question."""
    q = _norm(question)
    best, best_score = None, 0
    for f in faqs:
        score = sum(1 for k in (f.get("keywords") or "").split(",") if k and k in q)
        if score > best_score:
            best, best_score = f, score
    if best is None:
        return {"ok": True, "found": False, "answer": FALLBACK_ANSWER,
                "note": "FAQにないため、担当者への確認事項として記録しました"}
    return {"ok": True, "found": True, "faq": best["code"], "answer": best["answer"]}


def faq_block(faqs: list[dict]) -> str:
    lines = ["【FAQ（この通話の開始時に有効だったもの。未承認の推奨案を含む試作）】"]
    lines += [f"- {f['question']} → {f['answer']}" for f in faqs]
    lines.append(f"- FAQにないこと → 「{FALLBACK_ANSWER}」と答え、lookup_faq で確認事項として記録する")
    return "\n".join(lines)


TOOL_RULES = """\
- 料金・条件の質問は lookup_faq を呼び、返ってきた answer の範囲だけで答えます。
- 火や煙が出ているなど明白な危険は flag_emergency(level="obvious")、曖昧なら "ambiguous"、判断できなければ "undetermined" を呼び、
  返ってきた指示に従います。安全とは断定せず、訪問や時刻を約束しません。
"""


def date_block(call_time: dt.datetime, days: int = DAYS_AHEAD) -> str:
    """Today's date and a look-up table of the next days, so the model reads 「明後日」「来週の火曜」 off the table
    instead of computing weekdays itself (2026-10-08: the AI did not know what day 「明後日」 was)."""
    today = call_time.date()
    monday = today - dt.timedelta(days=today.weekday())
    w = WEEKDAY_KANJI
    lines = [f"【今日】{today.year}年{today.month}月{today.day}日（{w[today.weekday()]}） "
             f"{call_time.hour}時{call_time.minute:02d}分（日本時間。この通話の開始時）",
             "【日付の早見表】"]
    d2 = today + dt.timedelta(days=2)
    for i in range(days):
        d = today + dt.timedelta(days=i)
        names = [_NAMED[i]] if i in _NAMED else []
        week = (d - monday).days // 7
        if week in _WEEK:
            names.append(f"{_WEEK[week]}の{w[d.weekday()]}曜")
        lines.append(f"- {d.month}/{d.day}（{w[d.weekday()]}）" + ("：" + "・".join(names) if names else ""))
    lines += [
        "- 「明日」「明後日」「来週の◯曜」などは、上の表で具体的な日付と曜日に直します。曜日を自分で計算しません。",
        f"  聞かれたら「明後日は{d2.month}月{d2.day}日、{w[d2.weekday()]}曜日です」のように、表の日付と曜日で答えます。",
        f"- 希望日時の save_field は、具体的な日付で保存します（例 {d2.isoformat()}、時刻があれば {d2.isoformat()}T14:00）。"
        "「明後日」のまま保存しません。",
        "- 復唱は request_readback が返す読み方（日付と曜日）で行い、合っているかを確認します。",
        "- 深夜0時〜5時の「明日」、土日の「来週の◯曜」は、どの日か人によって違うので、日付で確かめます。",
        "- 祝日かどうかは分からないため、断定しません。表より先の日は、日付と曜日をお客様に確かめます。",
    ]
    return "\n".join(lines)
