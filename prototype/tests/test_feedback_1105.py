"""GPT-Live 1 call of 2026-11-05 (record 20261105-123410, backend gpt-6.1-terra), sent by the user with the
ElevenAgents call of the same day.

- The backend saved 「来週の火曜日午後3時ぐらい」 as '2026-11-10T15:00頃'. The server could not read it, so the read-back
  was the raw value and the AI read the year (「2026年11月10日、火曜の午後3時」).
- The AI's read-back of the number reached the server in two pieces (「…ゴーロクヨンヨンの、」 and
  「ニーゼロゴーゼロ。こちらでお間違いないでしょうか」), with the caller's backchannel 「はい」 between them. Each piece was
  taken as a wrong number, so the caller's 「はい」 after it did not confirm the number.
"""
import unittest

from prototype.concierge.confirmation import Status
from prototype.concierge.readings import normalize_wish, wish_phrase
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


def call():
    svc = ReceptionService(Store(":memory:"), env={})
    return svc, svc.start_call(at="2026-11-05T12:34")["id"]


class AboutTheTimeTest(unittest.TestCase):
    def test_a_wish_with_goro_is_read_as_a_date(self):
        self.assertEqual(normalize_wish("2026-11-10T15:00頃", "来週の火曜日午後3時ぐらいどうですか"), "2026-11-10T15:00")
        for v in ("2026-11-10T15:00ごろ", "2026-11-10T15:00くらい", "2026-11-10 15:00 前後", "2026-11-10T15:00〜"):
            self.assertEqual(wish_phrase(v), "11月10日、火曜日の、午後3時", v)
        self.assertEqual(wish_phrase("2026-11-10 夕方ごろ"), "11月10日、火曜日の、夕方")

    def test_the_read_back_says_the_date_not_the_raw_value(self):
        svc, cid = call()
        svc.caller_utterance(cid, "来週の火曜日午後3時ぐらいどうですか")
        res = svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": "2026-11-10T15:00頃"})
        self.assertEqual(res["read_back_now"]["say"], "11月10日、火曜日の、午後3時でございますね。")


class NumberInTwoPiecesTest(unittest.TestCase):
    def _read_back(self):
        svc, cid = call()
        svc.caller_utterance(cid, "ゼロハチゼロゴーロクヨンヨン、ニーゼロゴーゼロです")
        svc.tool(cid, "save_field", {"field": "callback_number", "value": "08056442050"})
        return svc, cid

    def test_two_pieces_of_one_read_back_are_checked_together(self):
        svc, cid = self._read_back()
        svc.ai_utterance(cid, "復唱いたします。ゼロハチゼロの、ゴーロクヨンヨンの、")
        svc.caller_utterance(cid, "はい")   # a backchannel in the middle: nothing is confirmed by it
        self.assertNotEqual(svc._session(cid).tools.store.fields["callback_number"].status, Status.CONFIRMED)
        svc.ai_utterance(cid, "ニーゼロゴーゼロ。こちらでお間違いないでしょうか")
        svc.caller_utterance(cid, "はい")
        self.assertEqual(svc._session(cid).tools.store.fields["callback_number"].status, Status.CONFIRMED)

    def test_a_wrong_second_piece_is_still_a_wrong_number(self):
        svc, cid = self._read_back()
        svc.ai_utterance(cid, "復唱いたします。ゼロハチゼロの、ゴーロクヨンヨンの、")
        svc.ai_utterance(cid, "ニーゼロゴーイチ。こちらでお間違いないでしょうか")
        svc.caller_utterance(cid, "はい")
        f = svc._session(cid).tools.store.fields["callback_number"]
        self.assertNotEqual(f.status, Status.CONFIRMED)
        self.assertEqual(f.readback_mismatch, "08056442051")

    def test_a_read_back_started_again_is_checked_on_its_own(self):
        svc, cid = self._read_back()
        svc.ai_utterance(cid, "復唱いたします。ゼロハチゼロの、")
        svc.ai_utterance(cid, "失礼いたしました。ゼロハチゼロの、ゴーロクヨンヨンの、ニーゼロゴーゼロ。こちらでお間違いないでしょうか")
        svc.caller_utterance(cid, "はい")
        self.assertEqual(svc._session(cid).tools.store.fields["callback_number"].status, Status.CONFIRMED)

    def test_only_half_of_the_number_never_confirms(self):
        svc, cid = self._read_back()
        svc.ai_utterance(cid, "復唱いたします。ゼロハチゼロの、ゴーロクヨンヨンの、")
        svc.caller_utterance(cid, "はい")
        svc.caller_utterance(cid, "はい、合っています")
        self.assertNotEqual(svc._session(cid).tools.store.fields["callback_number"].status, Status.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
