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
from ..concierge.readings import digits_only, is_valid_jp_number

ORDER = ("request", "shop_name", "caller_name", "callback_number", "preferred_datetime")
LABEL = {"request": "ご用件", "shop_name": "店舗名", "caller_name": "お名前", "callback_number": "折り返し先のお電話番号",
         "preferred_datetime": "ご希望の日時"}
ASK = {"request": "ご用件を伺えますか。", "shop_name": "店舗名を伺えますか。", "caller_name": "お名前を伺えますか。",
       "callback_number": "担当者から折り返すお電話番号を伺えますか。",
       "preferred_datetime": "ご希望の日時があれば伺えますか。"}
CONFIRM = ("callback_number", "preferred_datetime")
GREETING = "お電話ありがとうございます。株式会社野田のAI受付です。ご用件をお伺いします。"
CLOSING = "ありがとうございました。内容を担当者に伝え、担当者から折り返しご連絡します。失礼いたします。"
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
        self.skip: set[str] = set()            # fields the settings say not to store
        self.done = False

    # --- entry points ----------------------------------------------------------------------------------
    def greeting(self) -> list[str]:
        self.svc.ai_utterance(self.call_id, GREETING)
        return [GREETING]

    def hear(self, text: str) -> dict:
        r = self.svc.caller_utterance(self.call_id, text)
        if not r["forward"]:
            return {"forwarded": False, "reason": r["reason"], "say": []}
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
                return [ASK[f]]
        self.done = True
        return [CLOSING]

    def _readback(self, field: str) -> list[str]:
        res = self._tool("request_readback", field=field)
        if not res.get("ok"):
            return [ASK[field]]
        self.awaiting = field
        return [f"{LABEL[field]}を復唱します。{res['read_this_clearly']}。{res['then_ask']}"]

    def _date(self, text: str, current: str | None) -> str | None:
        md = re.search(r"(\d{1,2})月(\d{1,2})日", text)
        d_only = re.search(r"(\d{1,2})日", text)
        tm = re.search(r"(午前|午後)?(\d{1,2})時(半|(\d{1,2})分)?", text)
        if not md and not (d_only and current):
            return None
        base = self.svc.snapshot(self.call_id)["decision"]["at_jst"]
        start = dt.datetime.fromisoformat(base)
        cur = dt.datetime.fromisoformat(current) if current and len(current) > 10 else None
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
        if "録音" in text and any(w in text for w in ("止め", "しないで", "やめ", "嫌", "いや")):
            self.svc.consent_event(self.call_id, "recording_refused")
            return ["承知しました。ここからの録音を止め、これまでの録音も削除します。ご用件の受付は、このままAIが続けます。"]
        if any(w in text for w in ("AI", "機械")) and any(w in text for w in ("嫌", "話したくない", "やめ", "いらない", "断")):
            self.svc.consent_event(self.call_id, "ai_refused")
            return []          # recorded clips and the keypad take over; the AI says nothing more
        if self.human_pending:
            reply = classify_reply(text)
            if reply != Reply.UNCLEAR:
                self.human_pending = False
                accepted = reply == Reply.AFFIRMATIVE
                self.svc.consent_event(self.call_id, "human_request_answer", accepted)
                return ["ありがとうございます。短く伺います。"] + self._next_question() if accepted else []
            return ["恐れ入ります。AIで短く伺ってもよろしいですか。"]
        if any(w in text for w in ("人と話", "担当者と話", "人間と", "担当の方と", "人に代わ")):
            self.svc.consent_event(self.call_id, "human_request")
            self.human_pending = True
            return ["申し訳ありません。この電話から担当者へおつなぎすることはできませんが、担当者から折り返しご連絡します。"
                    "折り返しのため、AIで短くご用件とご連絡先を伺ってもよろしいですか。"]
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
            if field in found:
                self.awaiting = None
                return self._save_all(found, out)
            if classify_reply(text) == Reply.NEGATIVE:
                self.awaiting = None
                return [f"失礼しました。正しい{LABEL[field]}を伺えますか。"]
            return ["恐れ入ります。こちらでお間違いないでしょうか。"]
        if found:
            return self._save_all(found, out)
        if any(m in text for m in ("ですか", "ますか", "?", "？", "いくら", "何時")):
            res = self._tool("lookup_faq", question=text)
            return [res.get("answer", "")] + self._next_question()
        return ["恐れ入ります。もう一度伺えますか。"]

    def _save_all(self, found: dict, out: list[str]) -> list[str]:
        for field, value in found.items():
            res = self._tool("save_field", field=field, value=value)
            if res.get("reason") == "field_not_storable":
                self.skip.add(field)
        return out + self._after_save(found)

    def _after_save(self, found: dict, exclude: str | None = None) -> list[str]:
        for field in CONFIRM:
            if field in found and field != exclude and field not in self.skip:
                st = (self._values().get(field) or {}).get("status")
                if st in ("heard", "corrected_unconfirmed"):
                    return ["承知しました。"] + self._readback(field)
        return (["承知しました。"] if found else []) + self._next_question()
