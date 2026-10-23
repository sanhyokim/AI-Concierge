"""The number the phone line delivered (発信者番号), user request 2026-10-14: it goes into the summary and the
notification even when the caller never said a number, and the AI can ask whether to call back on it."""
import json
import pathlib
import tempfile
import unittest

from prototype.browser_lab.server import Lab
from prototype.reception import settings as S
from prototype.reception.fake_agent import RuleAgent
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


def service():
    return ReceptionService(Store(":memory:"), env={})


class SummaryAndNotificationTest(unittest.TestCase):
    def test_no_number_said_the_line_number_is_in_the_summary_and_the_notification(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00", caller_id="090-1111-2222")["id"]
        svc.tool(cid, "save_fields", {"request": "ダクトの清掃"})
        view = svc.end_call(cid)
        text, body = view["summaries"][0]["text"], view["notifications"][0]["body"]
        for t in (text, body):
            self.assertIn("回線の番号：090-1111-2222（回線から届いた番号。お客様の確認なし）", t)
        self.assertIn("折り返し先が未取得（回線の番号へは折り返せる）", text)

    def test_confirmed_callback_on_the_same_number_is_marked_as_such(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00", caller_id="092-504-2185")["id"]
        now = svc.tool(cid, "save_field", {"field": "callback_number", "value": "092-504-2185"})["read_back_now"]
        svc.ai_utterance(cid, now["say"])
        svc.caller_utterance(cid, "はい", "u1")
        text = svc.end_call(cid)["summaries"][0]["text"]
        self.assertIn("折り返し先：092-504-2185（本人確認済み）", text)
        self.assertIn("回線の番号：092-504-2185（折り返し先と同じ）", text)

    def test_withheld_number_says_so(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00")["id"]
        view = svc.end_call(cid)
        self.assertIn("回線の番号：非通知・不明", view["summaries"][0]["text"])
        self.assertIn("回線の番号：非通知・不明", view["notifications"][0]["body"])
        self.assertIn("折り返し先が未取得", view["summaries"][0]["text"])
        self.assertNotIn("回線の番号へは折り返せる", view["summaries"][0]["text"])

    def test_the_callback_number_rules_apply_to_the_line_number(self):
        svc = service()
        cfg = svc.config()[1]
        fields = {**cfg["fields"], "callback_number": {"store": True, "summary": True, "notify": False}}
        svc.save_config({**cfg, "fields": fields}, "tester")
        cid = svc.start_call(at="2026-10-14T13:00", caller_id="090-1111-2222")["id"]
        view = svc.end_call(cid)
        self.assertIn("回線の番号：090-1111-2222", view["summaries"][0]["text"])
        self.assertNotIn("090-1111-2222", view["notifications"][0]["body"])

    def test_a_renotification_keeps_the_line_number(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00", caller_id="090-1111-2222")["id"]
        svc.end_call(cid)
        svc.outbox.process()
        svc.renotify(cid, "tester")
        bodies = {n["body"] for n in svc.call_view(cid)["notifications"]}
        self.assertTrue(all("回線の番号：090-1111-2222" in b for b in bodies), bodies)


class OfferLineNumberTest(unittest.TestCase):
    def test_the_caller_says_yes_and_the_line_number_becomes_the_confirmed_callback(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00", caller_id="092-504-2185")["id"]
        res = svc.tool(cid, "offer_line_number", {})
        self.assertEqual(res["read_back_now"]["say"], "折り返しのお電話は、いまおかけいただいているお電話番号、"
                                                      "ゼロキュウニーの、ゴーゼロヨンの、ニーイチハチゴーで、よろしいでしょうか。")
        self.assertEqual(svc.fields(cid)["callback_number"]["status"], "awaiting_confirmation")   # not before the yes
        svc.ai_utterance(cid, res["read_back_now"]["say"])
        self.assertEqual(svc.caller_utterance(cid, "はい、お願いします", "u1")["confirmed"], "callback_number")
        f = svc.call_view(cid)["fields"]
        cb = next(x for x in f if x["name"] == "callback_number")
        self.assertEqual((cb["value"], cb["status"]), ("0925042185", "confirmed_by_caller"))
        self.assertTrue(svc.tool(cid, "offer_line_number", {})["already_confirmed"])

    def test_no_means_not_confirmed_and_another_number_can_be_saved(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00", caller_id="092-504-2185")["id"]
        res = svc.tool(cid, "offer_line_number", {})
        svc.ai_utterance(cid, res["read_back_now"]["say"])
        self.assertNotIn("confirmed", svc.caller_utterance(cid, "いいえ、携帯にお願いします", "u1"))
        now = svc.tool(cid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})["read_back_now"]
        svc.ai_utterance(cid, now["say"])
        self.assertEqual(svc.caller_utterance(cid, "はい", "u2")["confirmed"], "callback_number")
        self.assertEqual(svc.fields(cid)["callback_number"]["value"], "09012345678")

    def test_a_misspoken_offer_is_not_confirmed(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00", caller_id="092-504-2185")["id"]
        svc.tool(cid, "offer_line_number", {})
        svc.ai_utterance(cid, "おかけのお電話番号、ゼロキュウニーの、ゴーゼロヨンの、ニーイチハチロクでよろしいでしょうか。")
        self.assertNotIn("confirmed", svc.caller_utterance(cid, "はい", "u1"))

    def test_withheld_number_cannot_be_offered(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00")["id"]
        res = svc.tool(cid, "offer_line_number", {})
        self.assertEqual((res["ok"], res["reason"]), (False, "no_line_number"))
        self.assertNotIn("callback_number", svc.fields(cid))

    def test_the_instructions_say_whether_a_line_number_exists_without_its_digits(self):
        svc = service()
        with_line = svc.start_call(at="2026-10-14T13:00", caller_id="092-504-2185")["id"]
        without = svc.start_call(at="2026-10-14T13:00")["id"]
        text = svc.instructions(with_line)
        self.assertIn("まず offer_line_number を呼び", text)
        self.assertNotIn("504", text)
        self.assertNotIn("0925042185", text)
        self.assertIn("offer_line_number は呼びません", svc.instructions(without))

    def test_the_offline_agent_offers_the_line_number_first(self):
        svc = service()
        cid = svc.start_call(at="2026-10-14T13:00", caller_id="090-1111-2222")["id"]
        agent = RuleAgent(svc, cid)
        agent.greeting()
        said = agent.hear("焼肉ほのか博多店の田中です。無煙ロースターの清掃をお願いしたいです。")["say"]
        self.assertIn("ご希望の日時", " ".join(said))   # the date before the number (user decision 2026-10-23)
        said = agent.hear("特に希望はありません。")["say"]
        self.assertIn("いまおかけいただいているお電話番号、ゼロキュウゼロの、イチイチイチイチの、ニーニーニーニー", " ".join(said))
        agent.hear("はい、お願いします。")
        cb = svc.fields(cid)["callback_number"]
        self.assertEqual((cb["value"], cb["status"]), ("09011112222", "confirmed_by_caller"))


class LabLineNumberTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.lab = Lab(env={}, ledger_path=d / "ledger.jsonl", results_dir=d / "results")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_fictitious_line_number_typed_on_the_page_reaches_the_call(self):
        s = self.lab.start("fake", caller_id="090-1111-2222")
        self.assertEqual(s["caller_id"], "09011112222")
        self.assertEqual(self.lab.service.call_view(s["call_id"])["caller_id"], "09011112222")
        self.assertIsNone(self.lab.start("fake")["caller_id"])

    def test_an_invalid_line_number_is_refused_before_anything_starts(self):
        with self.assertRaises(ValueError):
            self.lab.start("fake", caller_id="12345")


if __name__ == "__main__":
    unittest.main()
