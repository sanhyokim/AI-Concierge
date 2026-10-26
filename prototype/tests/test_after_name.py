"""Two findings of the Grok call of 2026-10-26 (call_id 20261026-221720-82c68d).

- User: 「「田中様でいらっしゃいますね。いつもお世話になっております。」と言われた際にお客側は返事しないこともおおく、
  なにも反応しないのがほとんどだと思います。続けて…そのまましゃべっていいと思う。」 The AI waited 10 s for a reply.
- 「ないです。」 to 「こちらでお間違いないでしょうか。」 means there is no mistake, but the number was asked for again.
"""
import unittest

from prototype.concierge.confirmation import Reply, Status, classify_reply
from prototype.concierge.prompts import ASK_FOR, for_speech
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


def call(**kw):
    svc = ReceptionService(Store(":memory:"), env={})
    return svc, svc.start_call(at="2026-10-26T13:00", **kw)["id"]


class AfterTheNameTest(unittest.TestCase):
    def test_the_greeting_and_the_next_question_come_in_one_go(self):
        svc, cid = call()
        svc.tool(cid, "save_fields", {"request": "無煙ロースターの清掃"})
        res = svc.tool(cid, "save_fields", {"shop_name": "焼肉ほのか博多店", "caller_name": "田中"})
        self.assertEqual(res["say"], for_speech("田中様でいらっしゃいますね。いつもお世話になっております。"
                                                + ASK_FOR["preferred_datetime"]))
        self.assertIn("待たずに", res["how"])

    def test_after_the_date_the_number_is_asked_next(self):
        svc, cid = call()
        svc.ai_utterance(cid, ASK_FOR["preferred_datetime"])
        svc.caller_utterance(cid, "特にありません", "u1")
        res = svc.tool(cid, "save_fields", {"request": "清掃", "shop_name": "焼肉ほのか博多店", "caller_name": "田中"})
        self.assertTrue(res["say"].endswith(for_speech(ASK_FOR["callback_number"])), res["say"])

    def test_a_name_given_first_asks_only_for_the_shop(self):
        svc, cid = call()
        svc.ai_utterance(cid, ASK_FOR["preferred_datetime"])   # the date was asked
        res = svc.tool(cid, "save_fields", {"request": "清掃", "caller_name": "田中"})
        self.assertIn("店舗名をおうかがい", res["say"])
        self.assertNotIn("お名前", res["say"])

    def test_the_name_said_again_is_not_greeted_again(self):
        svc, cid = call()
        svc.tool(cid, "save_fields", {"caller_name": "田中"})
        res = svc.tool(cid, "save_fields", {"caller_name": "田中", "shop_name": "焼肉ほのか博多店"})
        self.assertNotIn("say", res)


class NoMistakeTest(unittest.TestCase):
    def test_nai_desu_is_yes_to_the_number(self):
        for text in ("ないです。", "ないです", "はい、ないです。", "特にないです。", "ありません。", "大丈夫です。"):
            self.assertEqual(classify_reply(text, mistake_question=True), Reply.AFFIRMATIVE, text)
        for text in ("違います。", "ないです、最後は1です。", "いいえ"):
            self.assertNotEqual(classify_reply(text, mistake_question=True), Reply.AFFIRMATIVE, text)
        self.assertEqual(classify_reply("ないです。"), Reply.UNCLEAR)   # without the question it says nothing

    def test_the_number_is_confirmed_by_nai_desu(self):
        svc, cid = call()
        res = svc.tool(cid, "save_field", {"field": "callback_number", "value": "08056442050"})
        svc.ai_utterance(cid, res["read_back_now"]["say"])
        svc.caller_utterance(cid, "ないです。", "u1")
        self.assertEqual(svc._session(cid).tools.store.get("callback_number").status, Status.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
