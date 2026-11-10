"""2026-11-10, user, after a GPT-Live call (record 20261110-164029-d82338):

- 「復唱で「ございます」ではなく「ございますね？」です」: a read-back ends 「…でございますね？」.
- 「お客様は名乗ってるのに情報が記録されてない」「会話でやり取りが止まるシーンがあった」: no business call at all, and
  the record could not say why. The page now reports what the vendor did, and the call record keeps it.
"""
import pathlib
import tempfile
import unittest

from prototype.browser_lab.server import Lab
from prototype.engines.tools import ToolHandler
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


class ReadBackEndTest(unittest.TestCase):
    def test_the_number_ends_with_a_question(self):
        now = ToolHandler().handle("save_field", {"field": "callback_number", "value": "08056442050"})["read_back_now"]
        self.assertTrue(now["say"].endswith("ニーゼロゴーゼロでございますね？"), now["say"])
        self.assertNotIn("お間違いない", now["say"])
        self.assertEqual(now["then_ask"], "")

    def test_the_date_ends_with_a_question(self):
        now = ToolHandler().handle("save_field", {"field": "preferred_datetime", "value": "2026-11-17T15:00"})["read_back_now"]
        self.assertTrue(now["say"].endswith("でございますね？"), now["say"])

    def test_yes_to_the_new_read_back_confirms_the_number(self):
        h = ToolHandler()
        now = h.handle("save_field", {"field": "callback_number", "value": "08056442050"})["read_back_now"]
        self.assertEqual(h.ai_said(now["say"]), {})   # the digits said are the saved number
        h.caller_said("はい", "u1")
        res = h.handle("confirm_field", {"field": "callback_number", "value": "08056442050"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(h.store.get("callback_number").status.value, "confirmed_by_caller")

    def test_daijoubu_also_answers_it(self):
        h = ToolHandler()
        h.handle("save_field", {"field": "callback_number", "value": "08056442050"})
        h.caller_said("大丈夫です", "u1")
        self.assertTrue(h.handle("confirm_field", {"field": "callback_number", "value": "08056442050"})["ok"])


class VendorNoteTest(unittest.TestCase):
    def test_a_note_from_the_page_goes_into_the_call_record(self):
        with tempfile.TemporaryDirectory() as d:
            svc = ReceptionService(Store(pathlib.Path(d) / "t.db"))
            lab = Lab(env={}, ledger_path=pathlib.Path(d) / "ledger.json", results_dir=pathlib.Path(d) / "r",
                      service=svc)
            s = lab.start("fake")
            lab.vendor_event(s["session_id"], "note", {"text": "AIが裏方に頼んだ（1回目）"})
            lab.vendor_event(s["session_id"], "note", {"text": "  "})   # nothing to keep
            call_id = lab.store.q1("SELECT call_id FROM lab_sessions WHERE id = ?", (s["session_id"],))["call_id"]
            notes = [e["data"]["text"] for e in svc.call_view(call_id)["events"] if e["kind"] == "vendor_note"]
            self.assertEqual(notes, ["AIが裏方に頼んだ（1回目）"])


class PageAdapterTest(unittest.TestCase):
    def test_the_gpt_live_page_reports_hand_overs_failures_and_errors(self):
        js = pathlib.Path("prototype/browser_lab/static/adapters/gpt-live.js").read_text(encoding="utf-8")
        for needle in ("session.delegation.created", "response.failed", "session.instructions.append",
                       'ctx.vendorEvent("note"', "OpenAI のエラー"):
            self.assertIn(needle, js)

    def test_the_record_shows_the_note(self):
        app = pathlib.Path("prototype/admin/static/app.js").read_text(encoding="utf-8")
        self.assertIn('case "vendor_note"', app)


if __name__ == "__main__":
    unittest.main()
