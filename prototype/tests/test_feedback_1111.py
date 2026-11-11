"""2026-11-11, user: 「会話のぶん脈をみておかしなところを確認してください。」 (Mac; GPT-Live 1 record 20261111-163139,
ElevenAgents record 20261111-163658).

- GPT-Live: the wish 「日付はいつでもよい、2時から4時の間」 was read back and answered 「そうです」, and stayed unconfirmed
  (only a read-back with a month and day or a weekday counted).
- GPT-Live: after the number was confirmed, the voice model asked 「どのようなご用件でしょうか。」 again; the caller:
  「先ほども言ったんですが」.
- The summary said 「…アドバイスを希望。。作業の…」.
- ElevenAgents: asked 「店舗名とお名前を…」, the caller said 「焼肉さく。」, and the next words were 「清掃の対象は、何基ございますか。」;
  the caller went on with 「ヤマモトです。」 and the count was never answered.
- ElevenAgents: 「十五んちにお店を借りるんで、それ以降だといつでもいい」 got 「具体的にお聞かせいただけますでしょうか」, and the
  wish was never saved.
"""
import unittest

from prototype.concierge.prompts import ASK_FOR, for_speech
from prototype.reception.faq import BACKEND_RULES, TEXT_AGENT_RULES, date_block
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store
import datetime as dt


def call():
    svc = ReceptionService(Store(":memory:"))
    return svc, svc.start_call(at="2026-11-11T16:31")["id"]


class VagueWishTest(unittest.TestCase):
    def test_a_wish_without_a_date_is_confirmed_by_the_reply(self):
        svc, cid = call()
        svc.caller_utterance(cid, "いつでもいいですが、2時から4時の間がいいですね")
        now = svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": "日付はいつでもよい、2時から4時の間"})
        self.assertIn("read_back_now", now)
        svc.ai_utterance(cid, "日付はいつでもよい、二時から四時の間でございますね。")
        self.assertEqual(svc.caller_utterance(cid, "そうです").get("confirmed"), "preferred_datetime")

    def test_a_wish_said_with_other_numbers_is_not_confirmed(self):
        svc, cid = call()
        svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": "日にちの指定なし、午後2時から4時の間"})
        svc.ai_utterance(cid, "日にちのご指定はなく、午後三時から四時の間でございますね。")
        self.assertIsNone(svc.caller_utterance(cid, "はい").get("confirmed"))

    def test_the_rules_say_to_keep_a_range_as_said(self):
        for brief in (True, False):
            text = date_block(dt.datetime(2026, 11, 11, 16, 31), brief=brief)
            self.assertIn("具体的な日にちを伺い直さず", text)
            self.assertIn("「午後」を付けます", text)


class ProgressTest(unittest.TestCase):
    def test_the_voice_model_is_told_what_is_taken_after_a_confirmation(self):
        svc, cid = call()
        svc.tool(cid, "save_fields", {"request": "居酒屋のダクトの清掃", "shop_name": "咲", "caller_name": "山本"})
        svc.caller_utterance(cid, "ゼロキューゼロ、ナナハチキューヨン、ゼロイチイチゼロ")
        say = svc.tool(cid, "save_field", {"field": "callback_number", "value": "09078940110"})["read_back_now"]["say"]
        svc.ai_utterance(cid, say)
        res = svc.caller_utterance(cid, "そうです")
        self.assertEqual(res["confirmed"], "callback_number")
        self.assertIn("用件", res["progress"])
        self.assertIn("もう一度伺いません", res["progress"])
        self.assertIn("次は希望日時を伺います", res["progress"])   # the date was never asked here

    def test_the_page_passes_it_on_without_speaking_it(self):
        import pathlib
        js = pathlib.Path("prototype/browser_lab/static/adapters/gpt-live.js").read_text(encoding="utf-8")
        self.assertIn("session.thinking.append", js)


class SummaryTest(unittest.TestCase):
    def test_no_double_full_stop(self):
        svc, cid = call()
        svc.tool(cid, "save_fields", {"request": "ダクト清掃を希望。", "shop_name": "咲", "caller_name": "山本",
                                      "callback_number": "09078940110"})
        say = svc.tool(cid, "request_summary", {})["say"]
        self.assertNotIn("。。", say)


class ShopThenNameTest(unittest.TestCase):
    def test_the_name_comes_before_the_count_when_only_the_shop_was_said(self):
        svc, cid = call()
        svc.tool(cid, "save_field", {"field": "request", "value": "店舗を借りる前の清掃"})
        svc.caller_utterance(cid, "希望する日はないです")
        res = svc.tool(cid, "save_field", {"field": "shop_name", "value": "焼肉さく"})
        self.assertEqual(res["say"], for_speech(ASK_FOR["caller_name"]))
        res = svc.tool(cid, "save_field", {"field": "caller_name", "value": "ヤマモト"})
        self.assertIn(for_speech(ASK_FOR["request_count"]), res["say"])   # asked after the greeting

    def test_both_at_once_still_go_on_to_the_count(self):
        svc, cid = call()
        svc.tool(cid, "save_field", {"field": "request", "value": "店舗を借りる前の清掃"})
        res = svc.tool(cid, "save_fields", {"shop_name": "焼肉さく", "caller_name": "ヤマモト"})
        self.assertIn(for_speech(ASK_FOR["request_count"]), res["say"])


class RequestFormTest(unittest.TestCase):
    def test_the_request_is_saved_as_a_short_phrase(self):
        for rules in (BACKEND_RULES, TEXT_AGENT_RULES):
            self.assertIn("短い名詞の形", rules)


if __name__ == "__main__":
    unittest.main()
