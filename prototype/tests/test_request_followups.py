"""Grok call of 2026-10-26 (call_id 20261026-230215-753d78) and the user's notes on it.

- 「希望時間を午前と言ったのに、言っていない午前九時と認識」「「2026-11-03T午前」というのはおかしいでしょ」
- 「清掃の対象がなんなのかを必ず確認する（…「フードの清掃」というと厨房フードか焼肉屋の上引きフードかわからない）。
  また焼肉屋だと対象のものが何基あるか必ず確認する。」
"""
import unittest

from prototype.concierge.prompts import ASK_FOR, for_speech
from prototype.concierge.readings import normalize_wish, spoken_datetime_mismatch, wish_phrase
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


def call():
    svc = ReceptionService(Store(":memory:"), env={})
    return svc, svc.start_call(at="2026-10-26T13:00")["id"]


class PartOfDayTest(unittest.TestCase):
    def test_a_part_of_the_day_is_not_turned_into_a_clock_time(self):
        self.assertEqual(normalize_wish("2026-11-03T09:00", "えー、午前の方がいいです。"), "2026-11-03 午前中")
        self.assertEqual(normalize_wish("2026-11-03T15:00", "午後3時で"), "2026-11-03T15:00")
        self.assertEqual(normalize_wish("2026-11-03T16:00", "16時お願いしたいです。"), "2026-11-03T16:00")

    def test_words_after_the_date_are_said_as_japanese(self):
        self.assertEqual(wish_phrase(normalize_wish("2026-11-03T午前")), "11月3日、火曜日の、午前中")
        self.assertEqual(wish_phrase("2026-11-03 夕方"), "11月3日、火曜日の、夕方")
        self.assertIsNone(spoken_datetime_mismatch("11月3日、火曜日の、午前中でございますね。", "2026-11-03 午前中"))
        self.assertTrue(spoken_datetime_mismatch("11月4日でございますね。", "2026-11-03 午前中"))

    def test_the_read_back_and_the_summary_say_the_part_of_the_day(self):
        svc, cid = call()
        svc.caller_utterance(cid, "えー、午前の方がいいです。", "u1")
        res = svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": "2026-11-03T09:00"})
        self.assertEqual(res["read_back_now"]["say"], "11月3日、火曜日の、午前中でございますね。")
        res = svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": "2026-11-03T午前"})
        self.assertNotIn("2026", res.get("read_back_now", {}).get("say", ""))


class FollowUpTest(unittest.TestCase):
    def test_an_unclear_hood_is_asked_about(self):
        svc, cid = call()
        res = svc.tool(cid, "save_field", {"field": "request", "value": "フードの清掃"})
        self.assertEqual(res["say"], for_speech(ASK_FOR["request_target"]))
        res = svc.tool(cid, "save_field", {"field": "request", "value": "厨房のフードの清掃"})
        self.assertNotIn("say", res)   # a kitchen hood: no count needed

    def test_uwabiki_is_said_in_kana(self):   # 2026-10-26 (user): 「上引き＝うわびきです」
        self.assertIn("焼肉のうわびきフード", for_speech(ASK_FOR["request_target"]))
        svc, cid = call()
        svc.ai_utterance(cid, for_speech(ASK_FOR["request_target"]))
        res = svc.tool(cid, "save_field", {"field": "request", "value": "うわびきフードの清掃"})
        self.assertEqual(res["say"], for_speech(ASK_FOR["request_count"]))   # clear now; a yakiniku job: how many

    def test_a_yakiniku_job_is_asked_how_many_once(self):
        svc, cid = call()
        res = svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースターの清掃"})
        self.assertEqual(res["say"], for_speech(ASK_FOR["request_count"]))
        svc.ai_utterance(cid, ASK_FOR["request_count"])
        res = svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースターの清掃"})   # 「分かりません」
        self.assertNotIn("say", res)

    def test_a_count_in_the_request_needs_no_question(self):
        svc, cid = call()
        res = svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースター12基の清掃"})
        self.assertNotIn("say", res)

    def test_a_yakiniku_shop_name_asks_the_count_next(self):
        svc, cid = call()
        svc.tool(cid, "save_field", {"field": "request", "value": "ダクトの清掃"})
        res = svc.tool(cid, "save_fields", {"shop_name": "焼肉ほのか博多店", "caller_name": "田中"})
        self.assertTrue(res["say"].endswith(for_speech(ASK_FOR["request_count"])), res["say"])

    def test_the_summary_waits_for_the_count_question(self):
        svc, cid = call()
        svc.tool(cid, "save_fields", {"request": "無煙ロースターの清掃", "shop_name": "焼肉田中", "caller_name": "田中"})
        res = svc.tool(cid, "request_summary", {})
        self.assertFalse(res["ok"])
        self.assertEqual(res["missing"][0], "対象の数（何基か）")


if __name__ == "__main__":
    unittest.main()
