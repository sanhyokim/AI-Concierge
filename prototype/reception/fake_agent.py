"""Rule-based stand-in for the conversation model (a test double), used by the demo call and the tests.

It only decides WHICH tool to call, from simple text patterns, the way a model would through function
calling, and it speaks only what the tools return. It produces no business result itself: saving, read-back,
the confirmation guard, FAQ answers, emergency handling and refusal all run in ReceptionService, the same
code the vendor adapters use. It is deliberately naive in one place - it proposes confirm_field whenever a
reply starts with 「はい」 - so the server-side guard is visible when the caller says 「はい、違います」.
It shows nothing about how a real model converses in Japanese.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

from ..concierge.confirmation import Reply, classify_reply
from ..concierge.prompts import CLOSING, GREETING
from ..concierge.readings import digits_only, is_valid_jp_number, resolve_relative_day

ORDER = ("request", "preferred_datetime", "shop_name", "caller_name", "callback_number")   # user decision 2026-10-23
LABEL = {"request": "ご用件", "shop_name": "店舗名", "caller_name": "お名前", "callback_number": "折り返し先のお電話番号",
         "preferred_datetime": "ご希望の日時"}
ASK = {"request": "どのようなご用件でしょうか。", "shop_name": "恐れ入りますが、店舗名をお伺いしてもよろしいでしょうか。",
       "caller_name": "恐れ入りますが、お名前をお伺いしてもよろしいでしょうか。",
       "callback_number": "担当の者から折り返しお電話いたしますので、ご連絡先のお電話番号をお伺いしてもよろしいでしょうか。",
       "preferred_datetime": "ご希望の日時はございますか。"}
CONFIRM = ("callback_number", "preferred_datetime")
_RELATIVE = re.compile(r"明明後日|しあさって|明後日|あさって|明日|あした|(?:来週|今週)の?[月火水木金土日]")
_NO_WISH = ("特に", "とくに", "いつでも", "おまかせ", "お任せ", "希望はない", "希望はありません", "ないです", "ありません")
_PHONE = re.compile(r"0\d{1,4}-\d{1,4}-\d{3,4}|0\d{9,10}")
_REQUEST_WORDS = ("清掃", "点検", "修理", "見積", "交換", "取り付け", "相談")
_POLITE_TAIL = re.compile(r"(をお願いしたいのですが|をお願いしたいです|をお願いします|をお願いできますか|お願いしたいです|"
                          r"お願いします|をしてほしいです|してほしいです|をしたいです|です|。)+$")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


class RuleAgent:
    def __init__(self, service, call_id: str) -> None:
        self.svc, self.call_id = service, call_id
        self.awaiting: str | None = None       # field read back, waiting for the caller's reply
        self.human_pending = False
        self.emergency_asked = False
        self.skip: set[str] = set()            # fields the settings say not to store, or a date the caller has no wish for
        self.asked: str | None = None          # the field asked last
        self.done = False
        self.unclear_day: list = []          # candidate dates of an ambiguous 「来週の火曜」 or late-night 「明日」
        self.line_offered = False            # asked once whether to call back on the line's number

    # --- entry points ----------------------------------------------------------------------------------
    def greeting(self) -> list[str]:
        self.svc.ai_utterance(self.call_id, GREETING)
        return [GREETING]

    def hear(self, text: str) -> dict:
        r = self.svc.caller_utterance(self.call_id, text)   # spoken refusals are judged here, in the common logic
        if not r["forward"]:
            return {"forwarded": False, "reason": r["reason"], "stop_ai": r.get("stop_ai", False),
                    "actions": r.get("actions", []), "say": []}
        if r.get("recording_stopped"):
            say = ["承知いたしました。ここからの録音を止め、これまでの録音も削除いたします。ご用件の受付は、このままAIが続けます。"
                   "AIでの音声の処理もご希望でなければ、お申し付けください。"]   # A-02 (spec 4-2)
            for line in say:
                self.svc.ai_utterance(self.call_id, line)
            return {"forwarded": True, "recording_stopped": True, "actions": r.get("actions", []), "say": say}
        say = self._reply(_norm(text))
        for line in say:
            self.svc.ai_utterance(self.call_id, line)
        return {"forwarded": True, "say": say}

    # --- helpers ---------------------------------------------------------------------------------------
    def _tool(self, name: str, **args) -> dict:
        return self.svc.tool(self.call_id, name, args)

    def _values(self) -> dict:
        return self.svc.fields(self.call_id)

    def _next_question(self) -> list[str]:
        have = self._values()
        for f in ORDER:
            if f not in self.skip and not (have.get(f) or {}).get("value"):
                if f == "callback_number" and not self.line_offered:   # the line's number first (spec 2)
                    self.line_offered = True
                    res = self._tool("offer_line_number")
                    if res.get("read_back_now"):
                        self.awaiting = f
                        return [res["read_back_now"]["say"]]
                self.asked = f
                return [ASK[f]]
        self.done = True
        return [CLOSING]

    def _readback(self, field: str) -> list[str]:
        res = self._tool("request_readback", field=field)
        if not res.get("ok"):
            return [ASK[field]]
        self.awaiting = field
        return [self._readback_line(field, res)]

    @staticmethod
    def _readback_line(field: str, res: dict) -> str:
        return f"{LABEL[field]}を復唱いたします。{res['read_this_clearly']}。{res['then_ask']}"

    def _date(self, text: str, current: str | None) -> str | None:
        md = re.search(r"(\d{1,2})月(\d{1,2})日", text)
        d_only = re.search(r"(\d{1,2})日", text)
        tm = re.search(r"(午前|午後)?(\d{1,2})時(半|(\d{1,2})分)?", text)
        base = self.svc.snapshot(self.call_id)["decision"]["at_jst"]
        start = dt.datetime.fromisoformat(base)
        rel = None if md else _RELATIVE.search(text)
        if rel:   # 明日・明後日・来週の火曜 … resolved against the call time, as the instructions tell the model
            days, ambiguous = resolve_relative_day(rel.group(0), start)
            if ambiguous or not days:
                self.unclear_day = days   # asked back with the candidate dates, never picked
                return None
            md_day = days[0]
        elif not md and not (d_only and current):
            return None
        cur = dt.datetime.fromisoformat(current) if current and len(current) > 10 else None
        if rel:
            year, month, day = md_day.year, md_day.month, md_day.day
        else:
            month = int(md.group(1)) if md else (int(current[5:7]) if current else start.month)
            day = int(md.group(2)) if md else int(d_only.group(1))
            year = start.year + (1 if month < start.month - 1 else 0)
        if tm:
            hour = int(tm.group(2)) + (12 if tm.group(1) == "午後" and int(tm.group(2)) < 12 else 0)
            minute = 30 if tm.group(3) == "半" else int(tm.group(4) or 0)
        elif cur:
            hour, minute = cur.hour, cur.minute
        else:
            try:
                return dt.date(year, month, day).isoformat()
            except ValueError:
                return None
        try:
            return dt.datetime(year, month, day, hour, minute).isoformat(timespec="minutes")
        except ValueError:
            return None

    def _extract(self, text: str) -> dict[str, str]:
        have = self._values()
        found: dict[str, str] = {}
        phone = _PHONE.search(text)
        if phone and is_valid_jp_number(digits_only(phone.group(0))):
            found["callback_number"] = phone.group(0)
        date = self._date(text, (have.get("preferred_datetime") or {}).get("value"))
        if date:
            found["preferred_datetime"] = date
        shop = re.search(r"([^\s、。「」]{2,20}?店)(?:の|です|と申します|$)", text)
        if shop and not shop.group(1).startswith(("うちの", "お店")):
            found["shop_name"] = shop.group(1)
            name = re.search(r"店の([^\s、。]{1,8}?)(?:です|と申します)", text)
            if name:
                found["caller_name"] = name.group(1)
        name = re.search(r"名前は([^\s、。]{1,8}?)(?:です|と申します)", text)
        if name:
            found["caller_name"] = name.group(1)
        for sentence in re.split(r"[。！？!?]", text):
            if any(w in sentence for w in _REQUEST_WORDS) and "無料" not in sentence and "いくら" not in sentence:
                part = sentence
                if "店の" in part:   # 「〇〇店の田中です、△△の清掃を…」: keep what follows the introduction
                    part = re.sub(r"^.*?店の[^\s、。]{1,8}?(です|と申します)、?", "", part)
                part = re.sub(r"^(すみません|あと|それと|それから)、?", "", part)
                req = _POLITE_TAIL.sub("", part).strip("、 ")
                current = (have.get("request") or {}).get("value")
                if req.endswith("も") and current:   # 「ダクトの点検も」 adds to the request
                    req = f"{current}、{req[:-1]}"
                if req:
                    found["request"] = req
                break
        return found

    # --- the reply -------------------------------------------------------------------------------------
    def _reply(self, text: str) -> list[str]:
        # AI and recording refusals were already handled by ReceptionService.caller_utterance.
        if self.human_pending:
            reply = classify_reply(text)
            if reply != Reply.UNCLEAR:
                self.human_pending = False
                accepted = reply == Reply.AFFIRMATIVE
                self.svc.consent_event(self.call_id, "human_request_answer", accepted)
                return ["ありがとうございます。短くお伺いいたします。"] + self._next_question() if accepted else []
            return ["恐れ入ります。AIで短くお伺いしてもよろしいでしょうか。"]
        if any(w in text for w in ("人と話", "担当者と話", "人間と", "担当の方と", "人に代わ")):
            self.svc.consent_event(self.call_id, "human_request")
            self.human_pending = True
            return ["申し訳ございません。この電話から担当の者へおつなぎすることはできかねますが、担当の者から折り返しご連絡いたします。"
                    "折り返しのため、AIでご用件とご連絡先を短くお伺いしてもよろしいでしょうか。"]   # A-03
        if any(w in text for w in ("火が出", "燃えて", "炎が", "火事")):
            res = self._tool("flag_emergency", level="obvious", reason="火・炎の訴え")
            return [f"（録音済みの緊急の案内 {res.get('play')} を流しました）", ASK["callback_number"]]
        if self.emergency_asked:
            self.emergency_asked = False
            if any(w in text for w in ("炎", "焦げ", "いつもと違う")):
                res = self._tool("flag_emergency", level="obvious", reason="選択で異常")
                return [f"（録音済みの緊急の案内 {res.get('play')} を流しました）", ASK["callback_number"]]
            if not any(w in text for w in ("排気", "調理", "いつもの")):
                res = self._tool("flag_emergency", level="undetermined", reason="選択が曖昧")
                return [f"（録音済みの案内 {res.get('play')} を流しました）"] + self._next_question()
        if any(w in text for w in ("焦げ臭", "煙が多", "煙がすごい", "変な煙")):
            self._tool("flag_emergency", level="ambiguous", reason="煙・におい")
            self.emergency_asked = True
            return ["調理中の煙がうまく排気されない、というご相談でしょうか。それとも、炎や、いつもと違う煙・焦げ臭さがありますか。"]

        if self.asked == "preferred_datetime" and any(w in text for w in _NO_WISH):   # no wish: not asked again
            self.skip.add("preferred_datetime")
        found = self._extract(text)
        out: list[str] = []
        if self.awaiting:
            field = self.awaiting
            current = (self._values().get(field) or {}).get("value")
            if classify_reply(text) == Reply.AFFIRMATIVE or text.startswith(("はい", "ええ", "うん")):
                res = self._tool("confirm_field", field=field, value=current or "")
                if res.get("ok"):
                    self.awaiting = None
                    out.append("ありがとうございます。")
                    return out + self._after_save(found, exclude=field)
                if "interrupted" in str(res.get("reason", "")):   # the vendor said the read-back was cut off
                    return ["失礼いたしました。途中で切れてしまいましたので、もう一度復唱いたします。"] + self._readback(field)
            if field in found:
                self.awaiting = None
                return self._save_all(found, out)
            if classify_reply(text) == Reply.NEGATIVE:
                self.awaiting = None
                return [f"失礼いたしました。恐れ入りますが、正しい{LABEL[field]}をお伺いしてもよろしいでしょうか。"]
            return ["恐れ入ります。こちらでお間違いないでしょうか。"]
        if found:
            return self._save_all(found, out)
        if "preferred_datetime" in self.skip and self.asked == "preferred_datetime":
            return ["承知いたしました。"] + self._next_question()
        if any(m in text for m in ("ですか", "ますか", "?", "？", "いくら", "何時")):
            res = self._tool("lookup_faq", question=text)
            return [res.get("answer", "")] + self._next_question()
        if self.unclear_day:
            days, self.unclear_day = self.unclear_day, []
            return ["恐れ入ります。" + "と、".join(f"{d.month}月{d.day}日（{'月火水木金土日'[d.weekday()]}）" for d in days)
                    + "の、どちらでしょうか。"]
        return ["恐れ入ります。もう一度お聞かせいただけますでしょうか。"]

    def _save_all(self, found: dict, out: list[str]) -> list[str]:
        res = self._tool("save_fields", **found)   # one call; a number or a date comes back ready to read
        self.skip.update(res.get("not_stored_by_setting", []))
        now = res.get("read_back_now")
        if now:
            self.awaiting = now["field"]
            return out + ["承知いたしました。", self._readback_line(now["field"], now)]
        return out + self._after_save(found)

    def _after_save(self, found: dict, exclude: str | None = None) -> list[str]:
        values = self._values()
        for field in CONFIRM:   # also one heard together with the item just confirmed (read_back_later)
            if field != exclude and field not in self.skip:
                st = (values.get(field) or {}).get("status")
                if st in ("heard", "corrected_unconfirmed"):
                    return ["承知いたしました。"] + self._readback(field)
        return (["承知いたしました。"] if found else []) + self._next_question()
