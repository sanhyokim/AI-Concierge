"""Japanese readings for phone numbers, dates and times, plus mora counting.

The readings serve two purposes:
- the text handed to TTS, so that digits are read one by one with pauses between groups;
- the mora count used by the speech-rate measurement (prototype/measure/rate.py).
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

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


# --- what the AI actually said in a read-back ---------------------------------
# The server hands the model the exact words to read back, but the model may still say something else.
# These parse the AI's own transcript of a read-back so that a different number or date is caught before
# the caller's 「はい」 can confirm it.

_SPOKEN_DIGIT_WORDS = sorted({
    "ゼロ": "0", "レイ": "0", "マル": "0", "イチ": "1", "ニー": "2", "ニイ": "2", "ニ": "2", "サン": "3",
    "ヨン": "4", "シ": "4", "ゴー": "5", "ゴ": "5", "ロク": "6", "ナナ": "7", "シチ": "7", "ハチ": "8",
    "キュウ": "9", "キュー": "9", "ク": "9",
    "〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7",
    "八": "8", "九": "9",
}.items(), key=lambda kv: -len(kv[0]))
_RUN_SEPARATORS = "、,・-‐ー― 　の"   # between digit groups; 「ー」 also lengthens ゴー/ニー and is skipped
_COUNTER_AFTER = "年月日時分円台件階番号歳"   # 「2026年」「15日」: a quantity, not part of a phone number


def spoken_digit_runs(text: str) -> list[str]:
    """Digit sequences said in the text: '080-5644', 'ゼロハチゼロ、ゴーロクヨンヨン', '〇八〇'.

    A run is a stretch made only of digits, katakana or kanji digit words and separators. Hiragana is not read
    as digits: 「2050に折り返し」 ends with the particle に, not 2 (2026-10-13 browser test). Runs of one or two
    digits are ignored (「一度」 starts with 一), as are numbers followed by a counter (「15日」).
    """
    t = unicodedata.normalize("NFKC", text)
    runs, cur, i = [], "", 0
    def close(next_char: str = "") -> None:
        nonlocal cur
        if len(cur) >= 3 and not (next_char and next_char in _COUNTER_AFTER):
            runs.append(cur)
        cur = ""
    while i < len(t):
        c = t[i]
        if c.isdigit():
            cur += c
            i += 1
            continue
        for word, d in _SPOKEN_DIGIT_WORDS:
            if t.startswith(word, i):
                cur += d
                i += len(word)
                break
        else:
            if cur and c in _RUN_SEPARATORS:
                i += 1
                continue
            close(c)
            i += 1
    close()
    return runs


def spoken_number_mismatch(text: str, number: str) -> str | None:
    """The digits the AI said, when they are not the number it was asked to read back; None when they match
    or when the text says no number. Saying the same number twice is not a mismatch."""
    said = "".join(spoken_digit_runs(text))
    want = digits_only(number)
    if not said or not want:
        return None
    if len(said) % len(want) == 0 and said == want * (len(said) // len(want)):
        return None
    return said


_KANJI_NUM = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _kanji_int(s: str) -> int | None:
    """'15', '十五', '二十一' -> int (up to 99)."""
    if s.isdigit():
        return int(s)
    if "十" in s:
        tens, _, ones = s.partition("十")
        t = _KANJI_NUM.get(tens, None) if tens else 1
        o = _KANJI_NUM.get(ones, None) if ones else 0
        return None if t is None or o is None else t * 10 + o
    return _KANJI_NUM.get(s) if len(s) == 1 else None


_NUM = r"(\d{1,2}|[〇一二三四五六七八九十]{1,3})"


def spoken_datetime_mismatch(text: str, value: str) -> str | None:
    """What the AI said, when its month/day, weekday or hour differs from the ISO value it was asked to read
    back ('2026-10-15' or '2026-10-15T15:00'); None when it matches or says no date."""
    try:
        when = dt.datetime.fromisoformat(value) if "T" in value or " " in value else None
        day = when.date() if when else dt.date.fromisoformat(value)
    except ValueError:
        return None
    t = unicodedata.normalize("NFKC", text)
    wrong = []
    for m in re.finditer(_NUM + "月" + _NUM + "日", t):
        mo, d = _kanji_int(m.group(1)), _kanji_int(m.group(2))
        if (mo, d) != (day.month, day.day):
            wrong.append(m.group(0))
    for m in re.finditer(r"([月火水木金土日])曜", t):
        if m.group(1) != WEEKDAY_KANJI[day.weekday()]:
            wrong.append(m.group(0) + "日")
    if when is not None:
        for m in re.finditer(r"(午前|午後)?" + _NUM + "時", t):
            h = _kanji_int(m.group(2))
            if h is None:
                continue
            if m.group(1) == "午後" and h < 12:
                h += 12
            elif m.group(1) == "午前" and h == 12:
                h = 0
            if h != when.hour and not (m.group(1) is None and h + 12 == when.hour):
                wrong.append(m.group(0))
    return "、".join(wrong) or None
