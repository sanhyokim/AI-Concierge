"""The Grok tests of 2026-10-29 (C2-C7, records 20261029-150054 … 150925) and the user's notes on them.

「「さようでございますか。」は変」「何度も聞き返された」「担当のものを重複して言っている。「担当のものにもうしつたえ、
折り返しお電話いたします。」でいいと思う」「C3の会話が変だった」「承って（しょうって）と言ってる」
「受付一覧に記録のエクスポート機能がほしい」
"""
import csv
import io
import unittest

from prototype.concierge.prompts import ASK_FOR, CLOSING_WORDS, SPEECH_MANNERS, VOICE_RULES, for_speech
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


def call():
    svc = ReceptionService(Store(":memory:"))
    return svc, svc.start_call(at="2026-10-29T15:00")["id"]


class AskedOnceTest(unittest.TestCase):
    def test_a_part_of_the_day_confirmed_by_the_reply_is_not_asked_again(self):
        # 150054: 「午後」 saved as 「2026-11-03 午後」, confirmed by 「はい」; the AI's confirm_field with T14:00 was
        # refused and it read the date back three times
        svc, cid = call()
        svc.tool(cid, "save_fields", {"shop_name": "焼肉ほのか博多店", "caller_name": "田中", "request": "無煙ロースター20基の清掃"})
        svc.caller_utterance(cid, "来週の火曜日、午後からどうですか？")
        say = svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": "2026-11-03T14:00"})["read_back_now"]["say"]
        self.assertEqual(say, "11月3日、火曜日の、午後でございますね。")
        svc.ai_utterance(cid, say)
        svc.caller_utterance(cid, "はい。")
        res = svc.tool(cid, "confirm_field", {"field": "preferred_datetime", "value": "2026-11-03T14:00"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["confirmed_value"], "11月3日、火曜日の、午後")
        self.assertEqual(res["say"], for_speech(ASK_FOR["callback_number"]))   # and on to the next question
        self.assertEqual(svc.fields(cid)["preferred_datetime"]["status"], "confirmed_by_caller")

    def test_the_next_question_follows_a_confirmation_only_when_nothing_waits(self):
        svc, cid = call()
        svc.tool(cid, "save_fields", {"request": "無煙ロースター3基の清掃", "preferred_datetime": "2026-11-03T15:00",
                                      "callback_number": "08056442050"})
        svc.caller_utterance(cid, "はい")
        res = svc.tool(cid, "confirm_field", {"field": "preferred_datetime", "value": "2026-11-03T15:00"})
        self.assertNotIn("say", res)   # the number is still to be read back first


class WordsTest(unittest.TestCase):
    def test_no_sayou_and_no_uketamawatte(self):
        self.assertNotIn("「さようでございますか」から", SPEECH_MANNERS)
        self.assertNotIn("「さようでございますか」から", VOICE_RULES)
        self.assertIn("「さようでございますか」", VOICE_RULES)   # in the words not to use
        self.assertEqual(for_speech("承っております"), "うけたまわっております")

    def test_the_closing_names_the_staff_once(self):
        self.assertEqual(CLOSING_WORDS.count("担当の者"), 1)
        self.assertIn("担当の者に申し伝え、折り返しお電話いたします。", CLOSING_WORDS)


class C3Test(unittest.TestCase):
    def test_a_call_with_only_a_question_ends_with_the_goodbye(self):
        # 150734: 「点検は無料ですか」 answered, 「以上です」, then 「どのようなご用件でしょうか」 again
        svc, cid = call()
        svc.caller_utterance(cid, "点検は無料ですか？")
        svc.tool(cid, "lookup_faq", {"question": "点検は無料ですか？"})
        res = svc.tool(cid, "request_summary", {})
        self.assertTrue(res["nothing_to_take"])
        self.assertNotIn("say", res)
        self.assertIn("end_call", res["then"])

    def test_an_inspection_at_a_yakiniku_shop_is_not_asked_how_many(self):
        # 150925: 「点検」 at 「焼肉ほのか」 got 「清掃の対象は、何基ございますか」
        svc, cid = call()
        res = svc.tool(cid, "save_fields", {"shop_name": "焼肉ほのか博多店", "request": "点検"})
        self.assertEqual(res["say"], for_speech(ASK_FOR["preferred_datetime"]))
        res = svc.tool(cid, "save_field", {"field": "request", "value": "無煙ロースターの清掃"})
        self.assertEqual(res["say"], for_speech(ASK_FOR["request_count"]))   # a cleaning still is


class ExportTest(unittest.TestCase):
    def test_one_row_per_call_for_excel(self):
        svc, cid = call()
        svc.caller_utterance(cid, "=HYPERLINK(\"x\") 焼肉ほのか博多店の田中です")
        svc.tool(cid, "save_fields", {"shop_name": "焼肉ほのか博多店", "caller_name": "田中", "request": "無煙ロースター3基の清掃"})
        svc.end_call(cid)
        text = svc.export_calls_csv()
        self.assertTrue(text.startswith("﻿受付ID,着信日時"))   # Excel reads UTF-8 with the BOM
        self.assertIn("\r\n", text)
        rows = list(csv.reader(io.StringIO(text[1:])))
        head, row = rows[0], rows[1]
        got = dict(zip(head, row))
        self.assertEqual((got["受付ID"], got["店舗名"], got["お名前の確認"], got["用件"]),
                         (cid, "焼肉ほのか博多店", "聞き取りのみ・未確認", "無煙ロースター3基の清掃"))
        self.assertEqual(got["折り返し先の確認"], "未取得")
        self.assertTrue(got["会話"].startswith("お客様：=HYPERLINK"))   # the formula is inside the cell, not at its start
        self.assertEqual(got["着信日時"], "2026-10-29 15:00:00")

    def test_a_cell_that_looks_like_a_formula_is_text(self):
        svc, cid = call()
        svc.tool(cid, "save_field", {"field": "shop_name", "value": "=1+1"})
        got = dict(zip(*list(csv.reader(io.StringIO(svc.export_calls_csv()[1:])))[:2]))
        self.assertEqual(got["店舗名"], "'=1+1")


if __name__ == "__main__":
    unittest.main()
