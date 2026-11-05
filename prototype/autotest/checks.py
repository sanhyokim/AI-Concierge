"""What is checked after each caller-role conversation: the pattern's own expectations, then the rules every
conversation keeps (no words the AI must not use, no English, no raw dates, the same question not asked again and
again). Each check gives a line a person can read: 「番号が確認済み：○」."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

from ..concierge.confirmation import Status
from ..concierge.readings import WEEKDAY_KANJI, digits_only, split_wish
from ..tenants import current as current_tenant

# The words the AI must not use (prompts.VOICE_RULES 「使わない言葉」, user decisions 2026-10-13 to 10-30)
NG_WORDS = ("もしもし", "なるほど", "了解", "わかりました", "分かりました", "すみません", "よろしかったでしょうか",
            "になります", "少々確認", "確認しましょう", "さようでございますか", "承り", "承っ", "承ります", "うけたまわ")
FIELD_NAMES = {"callback_number": "折り返しの番号", "preferred_datetime": "希望日時", "request": "ご用件",
               "shop_name": "店舗名", "caller_name": "お名前"}


def _n(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _questions(ai_lines: list[str]) -> dict[str, int]:
    """Each question sentence the AI said, and how many times."""
    out: dict[str, int] = {}
    for line in ai_lines:
        for s in re.split(r"(?<=[。？?])", _n(line)):
            s = s.strip()
            if s.endswith(("か。", "か?", "か？")) and len(s) > 6:
                out[s] = out.get(s, 0) + 1
    return out


def evaluate(pattern: dict, call: dict, transcript: list[tuple[str, str]], tools: list[dict],
             ai_stopped: bool, started: dt.datetime) -> list[dict]:
    exp = pattern.get("expect", {})
    fields = {f["name"]: f for f in call.get("fields", [])}
    ai_lines = [t for r, t in transcript if r == "ai"]
    ai_text = _n("\n".join(ai_lines))
    called = [t["name"] for t in tools]
    out = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    def value(key: str) -> str:
        f = fields.get(key)
        return (f.get("value") or "") if f else ""

    for key in exp.get("confirmed", []):
        f = fields.get(key) or {}
        check(f"{FIELD_NAMES.get(key, key)}が本人の「はい」で確認済み", f.get("status") == Status.CONFIRMED.value,
              f"状態：{f.get('status_label') or f.get('status') or '空'}")
    if "number" in exp:
        got = digits_only(value("callback_number"))
        check("折り返しの番号が正しい", got == exp["number"], f"保存：{got or '（なし）'}／正しい番号：{exp['number']}")
    for group in exp.get("request_has", []):
        req = _n(value("request"))
        check(f"ご用件に「{'・'.join(group)}」", any(_n(w) in req for w in group), f"ご用件：{value('request') or '（なし）'}")
    if "datetime" in exp:
        want, raw = exp["datetime"], value("preferred_datetime")
        if want.get("none"):
            check("希望日時なしで進んだ", not raw or raw in ("なし", "希望なし"), f"保存：{raw or '（なし）'}")
        else:
            parts = split_wish(raw)
            if not parts:
                check("希望日時が日付として保存された", False, f"保存：{raw or '（なし）'}")
            else:
                day, when, period = parts
                if "weekday" in want:
                    wd = WEEKDAY_KANJI[day.weekday()]
                    check(f"希望日時の曜日（{want['weekday']}）", wd == want["weekday"], f"保存：{raw}（{wd}曜日）")
                if "days_after" in want:
                    n = (day - started.date()).days
                    check(f"希望日時が通話の{want['days_after']}日後", n == want["days_after"], f"保存：{raw}（{n}日後）")
                if "time" in want:
                    got = when.strftime("%H:%M") if when else (period or "")
                    check(f"希望の時刻（{want['time']}）", got == want["time"], f"保存：{raw}")
                if "part" in want:
                    ok = bool(period and want["part"] in period) or (when is not None and want["part"] == "午後" and when.hour >= 12)
                    check(f"希望の時間帯（{want['part']}）", ok, f"保存：{raw}")
    ups = {u.key: u for u in current_tenant().follow_ups}
    for key, should in exp.get("asked", {}).items():
        up = ups.get(key)
        asked = bool(up and any(up.asked_in(line) for line in ai_lines))
        label = up.label if up else key
        check(f"「{label}」を{'伺った' if should else '伺わなかった'}", asked == bool(should))
    for name in exp.get("tools", []):
        check(f"業務処理 {name} を使った", name in called)
    for name in exp.get("no_tools", []):
        check(f"業務処理 {name} を使わなかった", name not in called)
    if exp.get("emergency"):
        check("緊急として扱った", bool(call.get("emergency")) or "flag_emergency" in called)
    if "ai_stopped" in exp:
        check("お客様の求めでAIが止まった" if exp["ai_stopped"] else "AIが止まらなかった", ai_stopped == bool(exp["ai_stopped"]))
    if exp.get("ended"):
        check("最後のあいさつをして電話を切った（end_call）", "end_call" in called)
    if exp.get("ai_says_any"):
        words = exp["ai_says_any"]
        check(f"AIが「{'・'.join(words)}」のどれかを言った", any(_n(w) in ai_text for w in words))
    # --- every conversation ----------------------------------------------------------------------------
    ng = sorted({w for w in NG_WORDS if w in ai_text})
    check("使わない言葉を言っていない", not ng, "、".join(ng))
    english = sorted(set(re.findall(r"[A-Za-z]{4,}", ai_text)))
    check("英語を話していない", not english, "、".join(english[:5]))
    raw_dates = re.findall(r"\d{4}-\d{2}-\d{2}|T\d{2}:\d{2}", ai_text)
    check("日付をそのままの形（2026-11-10など）で読んでいない", not raw_dates, "、".join(raw_dates[:3]))
    limit = 1 if exp.get("ai_not_repeat_question") else 2
    again = [f"{q}（{n}回）" for q, n in _questions(ai_lines).items() if n > limit]
    check("同じ質問を何度も繰り返していない" if limit == 2 else "同じ質問をもう一度していない", not again, "、".join(again[:3]))
    return out
