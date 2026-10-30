"""The Grok test of 2026-10-30 (record 20261030-112731) and the user's notes on it.

「要件を言ってるのに…」「なんで？」: the AI took the request without saving it, and the server then handed it
「どのようなご用件でしょうか」 three times. 「お客が20基と言ってるなら20基でございますねと復唱すること」.
"""
import unittest

from prototype.concierge.prompts import (ASK_FOR, CLOSING, CLOSING_WORDS, GOODBYE, OPENING_ANNOUNCEMENT, OPENING_REQUEST,
                                         VOICE_RULES)
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


def say_back(svc, cid, res):
    said = (res.get("read_back_now") or {}).get("say") or res.get("say")
    svc.ai_utterance(cid, said)
    return said


class RequestSaidButNotSavedTest(unittest.TestCase):
    def test_a_request_the_caller_said_is_not_asked_for_again(self):
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T11:27")["id"]
        svc.caller_utterance(cid, "すいません、無煙ロースターの清掃お願いしたいんですが。")
        svc.ai_utterance(cid, "かしこまりました。無煙ロースターの清掃ですね。" + ASK_FOR["preferred_datetime"])
        svc.caller_utterance(cid, "来週の火曜日の15時でどうですか？")
        say_back(svc, cid, svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": "2026-11-03T15:00"}))
        svc.caller_utterance(cid, "はい。")
        res = svc.tool(cid, "confirm_field", {"field": "preferred_datetime", "value": "2026-11-03T15:00"})
        self.assertNotIn("ご用件でしょうか", res.get("say", ""))
        self.assertIn("無煙ロースターの清掃お願いしたいんですが", res["then"])
        self.assertIn("save_field", res["then"])
        # the name: the greeting, and no question on the request
        svc.caller_utterance(cid, "焼肉田中の田中です。")
        svc.tool(cid, "save_field", {"field": "shop_name", "value": "焼肉田中"})
        res = svc.tool(cid, "save_field", {"field": "caller_name", "value": "田中"})
        self.assertEqual(res["say"], "田中様でいらっしゃいますね。いつもお世話になっております。")
        self.assertIn("request に保存", res["then"])
        res = svc.tool(cid, "request_summary", {})
        self.assertFalse(res["ok"])
        self.assertNotIn("say", res)
        # saved: the follow-up on the count
        res = svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースターの清掃"})
        self.assertIn("何基", res["say"])

    def test_a_caller_who_said_no_request_is_asked(self):
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T11:27")["id"]
        svc.caller_utterance(cid, "焼肉田中の田中です。")
        svc.tool(cid, "save_field", {"field": "shop_name", "value": "焼肉田中"})
        res = svc.tool(cid, "save_field", {"field": "caller_name", "value": "田中"})
        self.assertIn(ASK_FOR["request"], res["say"])


class CountSaidBackTest(unittest.TestCase):
    def _to_the_count(self):
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T11:27")["id"]
        svc.caller_utterance(cid, "無煙ロースターの清掃お願いしたいんですが。")
        svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースターの清掃"})
        svc.ai_utterance(cid, "清掃の対象は、何基ございますか。")
        svc.caller_utterance(cid, "20基です。")
        return svc, cid

    def test_the_count_is_said_back_before_the_next_question(self):
        svc, cid = self._to_the_count()
        res = svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースターの清掃、20基"})
        self.assertTrue(res["say"].startswith("20基でございますね。"), res)
        self.assertIn("ご希望のにちじはございますか", res["say"])

    def test_the_count_is_said_back_in_the_summary_when_nothing_is_left(self):
        # 11:29:47: save_field and request_summary in one answer; only the summary's words are read as they are
        svc, cid = self._to_the_count()
        svc.tool(cid, "save_fields", {"shop_name": "焼肉田中", "caller_name": "田中"})
        svc.caller_utterance(cid, "08056442050")
        say_back(svc, cid, svc.tool(cid, "save_field", {"field": "callback_number", "value": "08056442050"}))
        svc.caller_utterance(cid, "はい。")
        svc.tool(cid, "confirm_field", {"field": "callback_number", "value": "08056442050"})
        svc.ai_utterance(cid, ASK_FOR["preferred_datetime"])
        svc.caller_utterance(cid, "特にないです。")
        res = svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースターの清掃、20基"})
        self.assertNotIn("say", res)
        res = svc.tool(cid, "request_summary", {})
        self.assertTrue(res["say"].startswith("20基でございますね。それでは、ご用件を確認いたします。"), res)
        self.assertTrue(svc.tool(cid, "request_summary", {})["say"].startswith("それでは"))   # once only


class SaidTwiceTest(unittest.TestCase):
    # record 20261030-131018: 「25基でございますね。」 said by the AI before the tool call, then the say began with it
    # (「なぜ2回言うの？」)
    def test_what_the_ai_has_just_said_is_left_out_of_the_say(self):
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T13:10")["id"]
        svc.caller_utterance(cid, "すいません、ロースターの清掃お願いしたいんですが。")
        svc.ai_utterance(cid, "かしこまりました。")
        svc.tool(cid, "save_field", {"field": "request", "value": "ロースターの清掃"})
        svc.ai_utterance(cid, "清掃の対象は、何基ございますか。")
        svc.caller_utterance(cid, "25基です。")
        svc.ai_utterance(cid, "25基でございますね。")
        res = svc.tool(cid, "save_field", {"field": "request", "value": "ロースターの清掃、25基"})
        self.assertEqual(res["say"], "作業におうかがいするご希望のにちじはございますか。")

    def test_words_said_before_the_callers_last_words_do_not_count(self):
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T13:10")["id"]
        svc.ai_utterance(cid, "清掃の対象は、何基ございますか。")
        svc.caller_utterance(cid, "え？")
        res = {"say": "清掃の対象は、何基ございますか。"}
        ReceptionService._drop_said(svc._session(cid), res)
        self.assertEqual(res["say"], "清掃の対象は、何基ございますか。")

    def test_the_echo_said_by_the_ai_is_not_said_again_in_the_summary(self):
        svc, cid = CountSaidBackTest()._to_the_count()
        svc.tool(cid, "save_fields", {"shop_name": "焼肉田中", "caller_name": "田中"})
        svc.caller_utterance(cid, "08056442050")
        say_back(svc, cid, svc.tool(cid, "save_field", {"field": "callback_number", "value": "08056442050"}))
        svc.caller_utterance(cid, "はい。")
        svc.tool(cid, "confirm_field", {"field": "callback_number", "value": "08056442050"})
        svc.ai_utterance(cid, ASK_FOR["preferred_datetime"])
        svc.caller_utterance(cid, "特にないです。")
        svc.ai_utterance(cid, "20基でございますね。")
        svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースターの清掃、20基"})
        self.assertTrue(svc.tool(cid, "request_summary", {})["say"].startswith("それでは"))


class NoUketamawaruTest(unittest.TestCase):
    # 「しょうりましたと聞こえます」: Grok said 「承りました」 wrong even when given 「うけたまわりました」 in kana
    def test_the_fixed_words_do_not_use_uketamawaru(self):
        for text in (OPENING_ANNOUNCEMENT, OPENING_REQUEST, CLOSING_WORDS, GOODBYE, CLOSING):
            self.assertNotIn("承", text)
            self.assertNotIn("うけたまわ", text)
        self.assertIn("ご用件をお伺いし、担当の者から", OPENING_ANNOUNCEMENT)
        self.assertTrue(CLOSING_WORDS.startswith("お伺いした内容は担当の者に申し伝え、"))
        self.assertIn("AIオペレーターがお受けいたしました。失礼いたします。", GOODBYE)
        self.assertIn("「承る」の言葉すべて", VOICE_RULES)
        self.assertIn("「〜でございますね」", VOICE_RULES)

    def test_the_summary_has_no_uketamawaru(self):
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T11:27")["id"]
        svc.tool(cid, "save_fields", {"request": "無煙ロースターの清掃、20基", "shop_name": "焼肉田中", "caller_name": "田中"})
        svc.caller_utterance(cid, "08056442050")
        say_back(svc, cid, svc.tool(cid, "save_field", {"field": "callback_number", "value": "08056442050"}))
        svc.caller_utterance(cid, "はい。")
        svc.tool(cid, "confirm_field", {"field": "callback_number", "value": "08056442050"})
        svc.ai_utterance(cid, ASK_FOR["preferred_datetime"])
        say = svc.tool(cid, "request_summary", {})["say"]
        self.assertNotIn("うけたまわ", say)
        self.assertIn("おうかがいした内容は", say)


if __name__ == "__main__":
    unittest.main()
