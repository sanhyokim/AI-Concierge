"""FAQ lookup over the FAQ list frozen into a call's snapshot, and the instruction block built from it."""
from __future__ import annotations

from .settings import FALLBACK_ANSWER


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
