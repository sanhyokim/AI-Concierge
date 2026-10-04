import datetime as dt
import unittest
import xml.etree.ElementTree as ET

from prototype.concierge.call_flow import CallFlow
from prototype.concierge.confirmation import FieldState, Reply, Status, classify_reply
from prototype.concierge.readings import (count_morae, datetime_phrase, is_valid_jp_number,
                                          phone_reading, resolve_relative_day)
from prototype.concierge.ssml import relay_slow_token, segment_ssml, validate_rate
from prototype.engines.tools import ToolHandler

JST = dt.timezone(dt.timedelta(hours=9))


class ReadingsTest(unittest.TestCase):
    def test_phone_readings(self):
        self.assertEqual(phone_reading("090-1234-5678"), "ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチ")
        self.assertEqual(phone_reading("092-123-4567"), "ゼロキュウニー、イチニーサン、ヨンゴーロクナナ")
        self.assertEqual(phone_reading("0120773408"), "ゼロイチニーゼロ、ナナナナ、サンヨンゼロハチ")
        self.assertEqual(phone_reading("09012345678"), phone_reading("090-1234-5678"))

    def test_validity(self):
        self.assertTrue(is_valid_jp_number("09012345678"))
        self.assertTrue(is_valid_jp_number("0921234567"))
        self.assertFalse(is_valid_jp_number("9012345678"))
        self.assertFalse(is_valid_jp_number("090123"))

    def test_morae(self):
        self.assertEqual(count_morae("ゼロキュウゼロ"), 6)
        self.assertEqual(count_morae("きゃっと"), 3)
        self.assertEqual(count_morae("ろーすたー"), 5)
        self.assertEqual(count_morae("、。"), 0)

    def test_datetime_phrase(self):
        display, reading = datetime_phrase(dt.datetime(2026, 10, 6, 14, 0, tzinfo=JST))
        self.assertEqual(display, "10月6日、火曜日の、午後2時")
        self.assertEqual(reading, "じゅうがつむいか、かようびの、ごごにじ")
        display, reading = datetime_phrase(dt.datetime(2026, 10, 24, 9, 45, tzinfo=JST))
        self.assertEqual(display, "10月24日、土曜日の、午前9時45分")
        self.assertEqual(reading, "じゅうがつにじゅうよっか、どようびの、ごぜんくじよんじゅうごふん")

    def test_relative_days(self):
        mon10 = dt.datetime(2026, 10, 5, 10, 0, tzinfo=JST)
        self.assertEqual(resolve_relative_day("明日の午後", mon10), ([dt.date(2026, 10, 6)], False))   # R-07
        tue0030 = dt.datetime(2026, 10, 6, 0, 30, tzinfo=JST)
        cands, ambiguous = resolve_relative_day("明日の朝", tue0030)                                  # R-08
        self.assertTrue(ambiguous)
        self.assertEqual(cands, [dt.date(2026, 10, 6), dt.date(2026, 10, 7)])
        self.assertEqual(resolve_relative_day("来週の月曜", mon10), ([dt.date(2026, 10, 12)], False))  # R-09
        sun = dt.datetime(2026, 10, 4, 10, 0, tzinfo=JST)
        self.assertTrue(resolve_relative_day("来週の月曜", sun)[1])


class ConfirmationTest(unittest.TestCase):
    def test_reply_classification(self):
        self.assertEqual(classify_reply("はい、合っています。"), Reply.AFFIRMATIVE)
        self.assertEqual(classify_reply("はい、違います。"), Reply.NEGATIVE)
        self.assertEqual(classify_reply("間違いないです"), Reply.AFFIRMATIVE)
        self.assertEqual(classify_reply("いいえ"), Reply.NEGATIVE)
        self.assertEqual(classify_reply("えーと"), Reply.UNCLEAR)
        self.assertEqual(classify_reply("最後は5678じゃなくて5687です"), Reply.NEGATIVE)

    def test_no_confirmation_without_readback(self):
        f = FieldState("callback_number")
        f.hear("09012345678")
        f.answer("はい、合っています。")
        self.assertEqual(f.status, Status.HEARD)

    def test_confirm_guard_requires_matching_readback(self):
        f = FieldState("callback_number")
        f.hear("09012345678")
        f.read_back("09012345678")
        ok, _ = f.confirm("09012345687", "はい、合っています。")
        self.assertFalse(ok)
        self.assertEqual(f.status, Status.AWAITING)


class CallFlowTest(unittest.TestCase):
    def test_no_say_ai_after_refusal(self):
        flow = CallFlow(caller_id=None)
        flow.on_ai_refused()
        out = flow.on_dtmf("0921234567#") + flow.on_dtmf("2") + flow.on_dtmf("09012345678#") + flow.on_dtmf("1")
        self.assertFalse([a for a in out if a[0] == "say_ai"])
        self.assertIn(("save_field", "callback_number", "09012345678", "dtmf", "confirmed_by_caller"), out)

    def test_voicemail_conditions(self):
        flow = CallFlow(voicemail_enabled=True)
        self.assertTrue(flow.can_use_voicemail())
        flow.on_recording_refused()
        flow.on_ai_failure()
        self.assertFalse(flow.can_use_voicemail())
        self.assertEqual(flow.consent.recording, "refused")


class SsmlTest(unittest.TestCase):
    def test_slow_and_normal_versions_share_marks(self):
        n = segment_ssml("確認します。", "ゼロキュウゼロ", "。よろしいですか。", None)
        d = segment_ssml("確認します。", "ゼロキュウゼロ", "。よろしいですか。", "80%")
        for ssml in (n, d):
            root = ET.fromstring(ssml)
            self.assertEqual([m.get("name") for m in root.iter("mark")], ["target_start", "target_end"])
        self.assertIsNone(ET.fromstring(n).find("prosody"))
        self.assertEqual(ET.fromstring(d).find("prosody").get("rate"), "80%")

    def test_escape_and_rate_validation(self):
        self.assertIn("&amp;", segment_ssml("A&B", "1", "", None))
        with self.assertRaises(ValueError):
            validate_rate("250%")
        token = relay_slow_token("ゼロキュウゼロ", "80%")
        self.assertTrue(token.startswith("<prosody") and token.endswith("</prosody>"))


class ToolHandlerTest(unittest.TestCase):
    def test_readback_returns_digit_reading_and_guard(self):
        h = ToolHandler()
        h.handle("save_field", {"field": "callback_number", "value": "090-1234-5678"})
        r = h.handle("request_readback", {"field": "callback_number"})
        self.assertEqual(r["read_this_clearly"], "ゼロキュウゼロ、イチニーサンヨン、ゴーロクナナハチ")
        h.last_caller_utterance = "はい、違います。"
        self.assertFalse(h.handle("confirm_field", {"field": "callback_number", "value": "09012345678"})["ok"])
        h.handle("save_field", {"field": "callback_number", "value": "09012345687"})
        h.handle("request_readback", {"field": "callback_number"})
        h.last_caller_utterance = "はい、合っています。"
        self.assertTrue(h.handle("confirm_field", {"field": "callback_number", "value": "09012345687"})["ok"])


if __name__ == "__main__":
    unittest.main()
