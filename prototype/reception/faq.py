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
    """Best keyword match among the enabled FAQ of this call. No match -> FAQ-10 and an open question.

    A keyword written as 「いくら|料金|値段」 is a required group: one of its words must be in the question, or that
    FAQ is not chosen. It keeps a question that only names the subject (「無煙ロースターの清掃に明後日来られますか」)
    from getting a price answer (2026-10-13, browser test C1).
    """
    q = _norm(question)
    best, best_score = None, 0
    for f in faqs:
        words = [k.strip() for k in (f.get("keywords") or "").replace("、", ",").split(",") if k.strip()]
        groups = [[w for w in k.split("|") if w] for k in words if "|" in k]
        if any(not any(w in q for w in g) for g in groups):
            continue
        score = sum(1 for k in words if "|" not in k and k in q) + len(groups)
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
【業務処理（ツール）】
- 業務処理を呼ぶ前に、お客様への返事を先に短く話します（「かしこまりました。」など）。業務処理のあいだ黙り込まないでください。
- 電話番号と希望日時は、聞いたらすぐ save_field で保存します。結果の read_back_now.say を、一字一句そのまま読んで復唱します。
  保存する前に、自分で番号や日時を復唱しません。数字を自分で読み替えません（0を「マル」と読む、「〇」と書く、など）。
- 店名・お名前・用件は、まとまったところで save_fields を1回だけ呼んで、まとめて保存します（結果に read_back_now があれば、それも読みます）。
  訂正された値は、save_field（1項目）で保存し直します。
- 復唱した値にお客様が「はい」とはっきり答えたら、サーバーが確認済みにします。confirm_field は呼ばなくてもかまいません。
  もう一つ復唱が残っていれば（read_back_later）、その後で request_readback を呼び、返ってきた say を読みます。
- 料金・条件・対応範囲の質問だけ lookup_faq を呼び、返ってきた answer の範囲だけで答えます。
  日程の空きや、来てもらえるかは FAQ ではないので呼ばず、「担当の者から折り返しお電話して、日程をご相談いたします。」と伝えます。
- 受付が終わったら、【終わり方（終話）】のとおりに話し、最後のあいさつの後で end_call を呼びます。
- 火や煙が出ているなど明白な危険は flag_emergency(level="obvious")、曖昧なら "ambiguous"、判断できなければ "undetermined" を呼び、
  返ってきた指示に従います。安全とは断定せず、訪問や時刻を約束しません。
"""


# --- the short prompts (2026-10-14): a short conversation prompt for the voice model, the steps elsewhere -------
# Gemini 3.8 Live talks and calls the tools itself: short rules here, the rest is in the tool descriptions and in
# what the tools return (read_back_now.say, the FAQ answer).
DIRECT_TOOL_RULES = """\
【業務処理（ツール）】
- 保存の前の返事は「かしこまりました。」だけにし、保存のことは口に出しません。結果の後に返事を繰り返しません。
- 結果の do_not_ask_again の項目は伺い直さず、still_to_ask の項目だけを伺います。
- 「明後日」などは下の表で日付に直して保存します。候補の日付を並べて話しません。
- 電話番号と日時は、聞いたらすぐ save_field で保存し、返ってきた read_back_now.say を一字一句そのまま読みます。
- 店舗名・お名前・ご用件は、そろったところで save_fields を1回呼んで保存します。訂正された値は save_field で保存し直します。
- 復唱にお客様が「はい」と答えたら、確認は済んでいます。復唱が残っていれば（read_back_later）、request_readback を呼び、
  返ってきた say を読みます。
- 料金・条件・対応地域を聞かれたら、答える前に必ず lookup_faq を呼び、返ってきた answer の範囲だけで答えます。
  日程の空きや訪問の可否は呼ばずに、「担当の者から折り返しお電話して、日程をご相談いたします。」とお伝えします。
- 火や煙など危険の訴えがあったら flag_emergency を呼び、返ってきた指示に従います。
"""

# GPT-Live: the voice model only decides when to hand work to its backend (delegation policy of OpenAI's guide).
DELEGATION_POLICY = """\
【裏方への頼み方】
裏方ができること：聞き取った内容の保存と、電話番号・日時の復唱の文づくり。料金・条件・対応地域の確認。
日付と曜日の確認。回線の番号の確認。危険の訴えの記録。通話の終了。
裏方に頼むとき：
- お客様が店舗名・お名前・ご用件・電話番号・日時をおっしゃったとき、訂正されたとき
- 電話番号・日時の復唱に、お客様が「はい」と答えたとき（次の復唱があれば、裏方が文を返します）
- 料金・条件・対応地域や、日付・曜日を聞かれたとき
- 火や煙など、危険の訴えがあったとき
- 最後のあいさつを言い終えたとき
裏方に頼まないとき：短い聞き返しや、相づちだけのとき。
- 頼むときは、先に「かしこまりました。」などと短く返事をします。結果を推測して話しません。
- 裏方の結果の後は、さきほどの返事を繰り返さず、続きから話します。
- 裏方の結果の「そのまま読む文：」は、一字一句変えずに読みます。
- 「お伝えすること：」は、内容を変えずに、丁寧な言葉でお伝えします。
- 「次にすること：」は声に出さず、そのとおりに進めます。
"""

# GPT-Live's backend (delegation) model: the procedures, the tools, the dates and the FAQ.
BACKEND_RULES = """\
【あなたの役割】あなたは、株式会社野田（飲食店の排煙ダクト・厨房換気設備の清掃と保守）の電話受付AIの裏方です。
お客様と話すのは声のAIです。声のAIから頼まれたことを業務処理（ツール）で行い、結果を短く返します。
【返し方】次の3つの形のどれかで、短く返します。ほかの説明は付けません。
- 「そのまま読む文：」：業務処理の結果に say があるとき（read_back_now.say など）、その文を一字一句変えずに続けます。
  数字や日付を読み替えません（0を「マル」と読む、「〇」と書く、などをしません）。
- 「お伝えすること：」：お客様への答え（FAQの答えなど）。内容を変えずに「〜でございます」「〜いたします」の丁寧な言い方に整えます。
- 「次にすること：」：声のAIへの指示で、お客様には聞かせません。例：「次にすること：ご希望の日時を伺ってください。」
  店舗名・お名前・ご用件・折り返しのお電話番号・ご希望の日時を伺い、確認も済んだら、request_summary を呼び、
  返ってきた say を「そのまま読む文：」で返します（まとめと締めの言葉は、どのAIでも同じ文にする。2026-10-22）。
【業務処理（ツール）】
- 電話番号と希望日時は、受け取ったらすぐ save_field で保存し、read_back_now.say を返します。
- 店舗名・お名前・ご用件は save_fields でまとめて保存します（結果に read_back_now があれば、その say を返します）。
  訂正された値は、save_field（1項目）で保存し直します。
- 復唱にお客様が「はい」と答えたと伝えられたら、サーバーが確認済みにしています。confirm_field は呼ばなくてかまいません。
  まだ復唱していない値があれば（read_back_later）、request_readback を呼び、返ってきた say を返します。
- 料金・条件・対応範囲の質問は lookup_faq を呼び、answer の範囲だけで返します。
  日程の空きや訪問の可否は FAQ ではないので呼ばず、「担当の者から折り返しお電話して、日程をご相談いたします。」と返します。
- 火や煙が出ているなど明白な危険は flag_emergency(level="obvious")、曖昧なら "ambiguous"、判断できなければ "undetermined" を呼び、
  返ってきた指示を返します。安全とは断定せず、訪問や時刻を約束しません。
- 最後のあいさつを言い終えたと伝えられたら、end_call を呼びます。
"""


def voice_line_hint(has_line_number: bool) -> str:
    """GPT-Live's voice model: whether the line delivered a number; the backend reads it (no digits here)."""
    if has_line_number:
        return ("【回線の番号】この電話には、回線の番号が届いています。折り返し先を伺うときは、まず裏方に回線の番号の確認を頼み、"
                "返ってきた文をそのまま読みます。お客様が先に番号をおっしゃったときは、その番号を裏方に伝えます。")
    return "【回線の番号】この電話は非通知です。折り返し先のお電話番号は、必ず口頭で伺います。"


def backend_line_block(has_line_number: bool) -> str:
    if has_line_number:
        return ("【回線の番号】この電話には、回線の番号が届いています。\n"
                "- 折り返し先の確認を頼まれたら、offer_line_number を呼び、read_back_now.say を返します。\n"
                "- お客様が別の番号をおっしゃったら、save_field で保存し、read_back_now.say を返します。")
    return "【回線の番号】この電話は非通知か、回線の番号が届いていません。offer_line_number は呼びません。"


def line_block(has_line_number: bool) -> str:
    """Whether the line delivered the caller's number. The digits themselves are not in the instructions: the
    tool returns the exact words, so the AI cannot read them its own way (user request 2026-10-14)."""
    if has_line_number:
        return ("【回線の番号】この電話には、回線の番号（お客様がおかけの電話番号）が届いています。\n"
                "- 折り返し先を伺うときは、まず offer_line_number を呼び、返ってきた read_back_now.say をそのまま読みます。\n"
                "- お客様が「はい」とおっしゃれば、その番号で承ります（サーバーが確認済みにします）。\n"
                "- 別の番号をおっしゃったら、save_field で保存し、返ってきた say で復唱します。\n"
                "- お客様が先に番号をおっしゃったときは、offer_line_number を呼ばずに、その番号を保存して復唱します。")
    return ("【回線の番号】この電話は非通知か、回線の番号が届いていません。折り返し先のお電話番号は、必ず口頭で伺います。"
            "offer_line_number は呼びません。")


def today_line(call_time: dt.datetime) -> str:
    today = call_time.date()
    return (f"【今日】{today.year}年{today.month}月{today.day}日（{WEEKDAY_KANJI[today.weekday()]}） "
            f"{call_time.hour}時{call_time.minute:02d}分（日本時間。この通話の開始時）")


def date_block(call_time: dt.datetime, days: int = DAYS_AHEAD, brief: bool = False) -> str:
    """Today's date and a look-up table of the next days, so the model reads 「明後日」「来週の火曜」 off the table
    instead of computing weekdays itself (2026-10-08: the AI did not know what day 「明後日」 was).
    brief: the table with three rules (the short prompt, 2026-10-14)."""
    today = call_time.date()
    monday = today - dt.timedelta(days=today.weekday())
    w = WEEKDAY_KANJI
    lines = [today_line(call_time), "【日付の早見表】"]
    d2 = today + dt.timedelta(days=2)
    for i in range(days):
        d = today + dt.timedelta(days=i)
        names = [_NAMED[i]] if i in _NAMED else []
        week = (d - monday).days // 7
        if week in _WEEK:
            names.append(f"{_WEEK[week]}の{w[d.weekday()]}曜")
        lines.append(f"- {d.month}/{d.day}（{w[d.weekday()]}）" + ("：" + "・".join(names) if names else ""))
    if brief:
        return "\n".join(lines + [
            "- 「明日」「明後日」「来週の◯曜」は、この表で日付と曜日に直します。曜日を自分で計算しません。",
            f"- save_field には、{d2.isoformat()}（時刻があれば {d2.isoformat()}T14:00）のような具体的な日付で保存します。",
            "- 深夜0時〜5時の「明日」、土日の「来週の◯曜」、表より先の日は、日付と曜日をお客様に確かめます。"])
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
