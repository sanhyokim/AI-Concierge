"""The AI is told today's date and reads relative days off a table (2026-10-08: it did not know what 「明後日」 was)."""
import datetime as dt
import pathlib
import tempfile
import unittest

from prototype.admin.server import AdminApp
from prototype.concierge.readings import resolve_relative_day
from prototype.reception.faq import date_block
from prototype.reception.store import Store

USER = {"user": "admin", "role": "admin", "csrf": "x"}


class DateBlockTest(unittest.TestCase):
    def test_today_and_the_table(self):
        b = date_block(dt.datetime(2026, 10, 8, 14, 5))
        self.assertIn("【今日】2026年10月8日（木） 14時05分", b)
        self.assertIn("- 10/9（金）：明日・今週の金曜", b)
        self.assertIn("- 10/10（土）：明後日・今週の土曜", b)
        self.assertIn("- 10/13（火）：来週の火曜", b)
        self.assertIn("- 10/21（水）：再来週の水曜", b)
        self.assertNotIn("10/22", b)                                  # 14 days
        self.assertIn("明後日は10月10日、土曜日です", b)               # the example is today's own 明後日
        self.assertIn("2026-10-10", b)

    def test_month_and_year_change(self):
        b = date_block(dt.datetime(2026, 12, 30, 9, 0))
        self.assertIn("- 12/31（木）：明日", b)
        self.assertIn("- 1/1（金）：明後日", b)
        self.assertIn("明後日は1月1日、金曜日です", b)
        self.assertIn("2027-01-01", b)
        self.assertIn("- 1/4（月）：来週の月曜", b)

    def test_relative_days(self):
        thu = dt.datetime(2026, 10, 8, 14, 0)
        self.assertEqual(resolve_relative_day("今日", thu), ([dt.date(2026, 10, 8)], False))
        self.assertEqual(resolve_relative_day("しあさって", thu), ([dt.date(2026, 10, 11)], False))
        self.assertEqual(resolve_relative_day("明後日", thu), ([dt.date(2026, 10, 10)], False))


class InstructionsAndMockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.app = AdminApp(Store(d / "app.db"), env={}, ledger_path=d / "l.jsonl", results_dir=d / "r")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def call(self, at):
        return self.app.post("/api/demo/start", {"dialed": "0120-77-3408", "at": at}, USER)["call"]["id"]

    def say(self, cid, text):
        return "".join(self.app.post("/api/demo/say", {"call_id": cid, "text": text}, USER)["result"]["say"])

    def test_instructions_carry_the_call_date(self):
        cid = self.call("2026-10-10T10:00")
        text = self.app.service.instructions(cid)
        self.assertIn("【今日】2026年10月10日（土） 10時00分", text)
        self.assertIn("- 10/12（月）：明後日・来週の月曜", text)
        self.assertLess(text.index("【今日】"), text.index("【FAQ"))

    def test_mock_reads_back_relative_days_as_dates(self):
        for at, said, expect in (("2026-10-10T10:00", "明後日の午後2時でお願いします", "10月12日、月曜日の、午後2時"),
                                 ("2026-10-10T10:00", "来週の火曜でお願いします", None),   # weekend: which week? ask
                                 ("2026-10-08T19:00", "来週の火曜の午前10時で", "10月13日、火曜日の、午前10時")):
            cid = self.call(at)
            for t in ("ダクトの清掃をお願いしたいです", "焼肉ほのか博多店の田中です", "090-1234-5678です", "はい"):
                self.say(cid, t)
            reply = self.say(cid, said)
            if expect:
                self.assertIn(expect, reply, at)
            else:
                self.assertIn("10月13日（火）と、10月20日（火）の、どちらでしょうか", reply)
                self.assertNotIn("復唱", reply)
                self.assertIn("10月20日、火曜日", self.say(cid, "10月20日です"))


if __name__ == "__main__":
    unittest.main()
