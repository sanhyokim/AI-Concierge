"""Japanese readings for phone numbers, dates and times, plus mora counting.

The readings serve two purposes:
- the text handed to TTS, so that digits are read one by one with pauses between groups;
- the mora count used by the speech-rate measurement (prototype/measure/rate.py).
"""
from __future__ import annotations

import datetime as dt
import re

DIGIT_KANA = {
    "0": "ゼロ", "1": "イチ", "2": "ニー", "3": "サン", "4": "ヨン",
    "5": "ゴー", "6": "ロク", "7": "ナナ", "8": "ハチ", "9": "キュウ",
}

# Small kana merge with the preceding kana into one mora. Small tsu (っ/ッ) is
# deliberately absent: it is a mora of its own.
_SMALL_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ")
_MOBILE_PREFIXES = ("050", "060", "070", "080", "090")


def digits_only(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def is_valid_jp_number(number: str) -> bool:
    """Format check only (10 or 11 digits starting with 0); says nothing about existence."""
    d = digits_only(number)
    if not d.startswith("0"):
        return False
    if len(d) == 11:
        return d.startswith(_MOBILE_PREFIXES) or d.startswith("0800")
    return len(d) == 10


def group_phone_number(number: str) -> list[str]:
    """Split a number into spoken groups.

    Hyphens in the input are respected as given. Without hyphens a heuristic is
    used; local area codes vary in length, so callers should pass hyphenated
    numbers whenever the grouping matters.
    """
    if "-" in number:
        return [g for g in (digits_only(p) for p in number.split("-")) if g]
    d = digits_only(number)
    if len(d) == 11 and (d.startswith(_MOBILE_PREFIXES) or d.startswith("0800")):
        return [d[:3], d[3:7], d[7:]] if not d.startswith("0800") else [d[:4], d[4:7], d[7:]]
    if len(d) == 10 and d.startswith("0120"):
        return [d[:4], d[4:6], d[6:]]
    if len(d) == 10 and d[:2] in ("03", "06"):
        return [d[:2], d[2:6], d[6:]]
    if len(d) == 10:
        return [d[:3], d[3:6], d[6:]]
    return [d] if d else []


def phone_reading(number: str) -> str:
    """'092-123-4567' -> 'ゼロキュウニー、イチニーサン、ヨンゴーロクナナ'."""
    return "、".join("".join(DIGIT_KANA[c] for c in group) for group in group_phone_number(number))


def count_morae(kana: str) -> int:
    """Count morae in a kana string. Non-kana characters (punctuation) are ignored."""
    count = 0
    for ch in kana:
        if ch in _SMALL_KANA:
            continue
        if "ぁ" <= ch <= "ゖ" or "ァ" <= ch <= "ヺ" or ch == "ー":
            count += 1
    return count


# --- dates and times -------------------------------------------------------

_MONTH = {1: "いちがつ", 2: "にがつ", 3: "さんがつ", 4: "しがつ", 5: "ごがつ", 6: "ろくがつ",
          7: "しちがつ", 8: "はちがつ", 9: "くがつ", 10: "じゅうがつ", 11: "じゅういちがつ",
          12: "じゅうにがつ"}
_DAY_SPECIAL = {1: "ついたち", 2: "ふつか", 3: "みっか", 4: "よっか", 5: "いつか", 6: "むいか",
                7: "なのか", 8: "ようか", 9: "ここのか", 10: "とおか", 14: "じゅうよっか",
                20: "はつか", 24: "にじゅうよっか"}
_ONES_NICHI = ["", "いち", "に", "さん", "よん", "ご", "ろく", "しち", "はち", "く"]
_TENS = {1: "じゅう", 2: "にじゅう", 3: "さんじゅう", 4: "よんじゅう", 5: "ごじゅう"}
_HOUR = {1: "いちじ", 2: "にじ", 3: "さんじ", 4: "よじ", 5: "ごじ", 6: "ろくじ", 7: "しちじ",
         8: "はちじ", 9: "くじ", 10: "じゅうじ", 11: "じゅういちじ", 12: "じゅうにじ"}
_MINUTE_ONES = {1: "いっぷん", 2: "にふん", 3: "さんぷん", 4: "よんぷん", 5: "ごふん",
                6: "ろっぷん", 7: "ななふん", 8: "はっぷん", 9: "きゅうふん"}
_MINUTE_TENS = {1: "じゅっぷん", 2: "にじゅっぷん", 3: "さんじゅっぷん", 4: "よんじゅっぷん",
                5: "ごじゅっぷん"}
WEEKDAY_KANJI = ["月", "火", "水", "木", "金", "土", "日"]
_WEEKDAY_KANA = ["げつようび", "かようび", "すいようび", "もくようび", "きんようび", "どようび",
                 "にちようび"]


def _day_reading(day: int) -> str:
    if day in _DAY_SPECIAL:
        return _DAY_SPECIAL[day]
    tens, ones = divmod(day, 10)
    return _TENS.get(tens, "") + _ONES_NICHI[ones] + "にち"


def _minute_reading(minute: int) -> str:
    if minute == 0:
        return ""
    tens, ones = divmod(minute, 10)
    if ones == 0:
        return _MINUTE_TENS[tens]
    return _TENS.get(tens, "") + _MINUTE_ONES[ones]


def datetime_phrase(when: dt.datetime) -> tuple[str, str]:
    """Return (display, kana reading), e.g. ('10月6日、火曜日の、午後2時', 'じゅうがつむいか、…')."""
    ampm, ampm_kana = ("午前", "ごぜん") if when.hour < 12 else ("午後", "ごご")
    hour12 = when.hour % 12 or 12
    minute_display = f"{when.minute}分" if when.minute else ""
    display = (f"{when.month}月{when.day}日、{WEEKDAY_KANJI[when.weekday()]}曜日の、"
               f"{ampm}{hour12}時{minute_display}")
    reading = (f"{_MONTH[when.month]}{_day_reading(when.day)}、{_WEEKDAY_KANA[when.weekday()]}の、"
               f"{ampm_kana}{_HOUR[hour12]}{_minute_reading(when.minute)}")
    return display, reading


def date_phrase(day: dt.date) -> tuple[str, str]:
    display = f"{day.month}月{day.day}日、{WEEKDAY_KANJI[day.weekday()]}曜日"
    reading = f"{_MONTH[day.month]}{_day_reading(day.day)}、{_WEEKDAY_KANA[day.weekday()]}"
    return display, reading


# --- relative day expressions -----------------------------------------------

_LATE_NIGHT_END_HOUR = 5  # 0:00-4:59 "明日" may mean the coming morning or the day after


def resolve_relative_day(expression: str, call_time: dt.datetime) -> tuple[list[dt.date], bool]:
    """Resolve a relative day against the call time (JST).

    Returns (candidates, ambiguous). When ambiguous is True the agent must ask
    the caller to confirm; it must not pick a candidate by itself.
    """
    today = call_time.date()
    expr = expression.replace(" ", "")
    if expr.startswith(("今日", "本日", "きょう")):
        return [today], False
    if expr.startswith(("明明後日", "しあさって")):
        return [today + dt.timedelta(days=3)], False
    if expr.startswith("明後日") or expr.startswith("あさって"):
        return [today + dt.timedelta(days=2)], False
    if expr.startswith("明日") or expr.startswith("あした"):
        tomorrow = today + dt.timedelta(days=1)
        if call_time.hour < _LATE_NIGHT_END_HOUR:
            return [today, tomorrow], True
        return [tomorrow], False
    m = re.match(r"(来週|今週)の?([月火水木金土日])", expr)
    if m:
        target = WEEKDAY_KANJI.index(m.group(2))
        monday = today - dt.timedelta(days=today.weekday())
        if m.group(1) == "来週":
            monday += dt.timedelta(days=7)
        day = monday + dt.timedelta(days=target)
        # On a weekend "来週の月曜" may mean the coming Monday or the one a week later (weeks counted from
        # Sunday or from Monday). Both candidates are in the future; the earlier one is the calendar meaning.
        ambiguous = m.group(1) == "来週" and today.weekday() >= 5
        candidates = [day] if not ambiguous else [day, day + dt.timedelta(days=7)]
        return candidates, ambiguous
    return [], True
