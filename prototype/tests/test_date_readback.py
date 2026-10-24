"""The date's read-back, user request 2026-10-24: 「相手が日時を言ってきたら返事に「復唱いたします」はいらない。すぐに相手の
言った日時を復唱する。なお時間の言わなかった場合は希望の時間がないか確認する」."""
import unittest

from prototype.concierge.confirmation import Reply, Status, classify_reply
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


def date_read_back(value):
    svc = ReceptionService(Store(":memory:"), env={})
    cid = svc.start_call(at="2026-10-24T10:00")["id"]
    res = svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": value})
    return svc, cid, res["read_back_now"]


def status(svc, cid):
    return svc._session(cid).tools.store.get("preferred_datetime").status


class DateReadBackTest(unittest.TestCase):
    def test_a_date_and_a_time_are_said_at_once_without_the_lead_in(self):
        _, _, now = date_read_back("2026-10-26T15:00")
        self.assertNotIn("復唱いたします", now["say"])
        self.assertTrue(now["say"].startswith("10月26日、月曜日の、午後3時"), now["say"])
        self.assertTrue(now["say"].endswith("でございますね。"), now["say"])

    def test_a_date_without_a_time_asks_for_the_time(self):
        _, _, now = date_read_back("2026-10-26")
        self.assertEqual(now["say"], "10月26日、月曜日でございますね。お時間のご希望はございますか。")

    def test_no_time_wish_accepts_the_date(self):
        for reply in ("特にありません。", "いいえ、特にないです。", "何時でも大丈夫です。", "はい、大丈夫。"):
            svc, cid, now = date_read_back("2026-10-26")
            svc.ai_utterance(cid, now["say"])
            svc.caller_utterance(cid, reply, "u1")
            self.assertEqual(status(svc, cid), Status.CONFIRMED, reply)

    def test_a_time_in_the_reply_is_not_a_yes(self):
        for reply in ("午後3時でお願いします。", "はい、三時ぐらいで。"):
            svc, cid, now = date_read_back("2026-10-26")
            svc.ai_utterance(cid, now["say"])
            svc.caller_utterance(cid, reply, "u1")
            self.assertEqual(status(svc, cid), Status.AWAITING, reply)

    def test_a_wrong_date_is_still_a_no(self):
        self.assertEqual(classify_reply("いいえ、27日です。", time_question=True), Reply.NEGATIVE)
        self.assertEqual(classify_reply("違います", time_question=True), Reply.NEGATIVE)


if __name__ == "__main__":
    unittest.main()
