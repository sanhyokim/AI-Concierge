"""Consent heard in the caller's words: AI refusal vs recording refusal (review of fc81edd, item 3 and ★4)."""
import unittest

from prototype.reception.service import ReceptionService
from prototype.reception.speech_consent import detect
from prototype.reception.store import Store


class DetectTest(unittest.TestCase):
    def test_table(self):
        cases = {
            "人と話したいので、AIは使わないでください": (True, False, False),
            "AIは使わないでください": (True, False, False),
            "人と話したい": (False, False, True),
            "AIは嫌ではありません": (False, False, False),
            "AIでも大丈夫です": (False, False, False),
            "AIで結構です": (False, False, False),
            "AIは結構です": (True, False, False),
            "録音はしないでください": (False, True, False),
            "録音は嫌じゃないです": (False, False, False),
            "録音もAIもやめてください": (True, True, False),
            "AIじゃなくて人と話したいです": (False, False, True),
        }
        for text, (ai, rec, human) in cases.items():
            got = detect(text)
            self.assertEqual((got.ai_refused, got.recording_refused, got.human_request), (ai, rec, human), text)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.svc = ReceptionService(Store(":memory:"), env={})
        self.cid = self.svc.start_call(at="2026-10-05T18:00")["id"]

    def test_spoken_ai_refusal_stops_the_ai_and_later_work(self):
        self.svc.caller_utterance(self.cid, "焼肉ほのか博多店です", "p1")
        r = self.svc.caller_utterance(self.cid, "人と話したいので、AIは使わないでください", "p2")
        self.assertEqual((r["forward"], r["stop_ai"]), (False, True))
        self.assertFalse(self.svc.ai_allowed(self.cid))
        view = self.svc.call_view(self.cid)
        self.assertEqual(view["consent"]["ai_processing"], "refused")
        self.assertNotIn("caller", [e["kind"] for e in view["events"]])          # transcript before it deleted
        self.assertEqual(self.svc.tool(self.cid, "save_field", {"field": "request", "value": "x"})["reason"],
                         "ai_refused")
        later = self.svc.caller_utterance(self.cid, "やっぱりAIでいいです", "p3")
        self.assertFalse(later["forward"])
        self.assertNotIn("やっぱり", str(self.svc.call_view(self.cid)["events"]))    # not stored after refusal
        self.assertEqual(view["state"], "dtmf_entry")                              # push buttons take over

    def test_recording_refusal_alone_does_not_stop_the_ai(self):
        r = self.svc.caller_utterance(self.cid, "録音はしないでください", "p1")
        self.assertEqual((r["forward"], r["stop_ai"], r["recording_stopped"]), (True, False, True))
        self.assertTrue(self.svc.ai_allowed(self.cid))
        view = self.svc.call_view(self.cid)
        self.assertEqual(view["consent"], {"recording": "refused", "ai_processing": "allowed",
                                           "recording_announced": True})
        self.assertIn("recording_deleted", [e["kind"] for e in view["events"]])
        # the conversation and the business processing continue
        self.assertTrue(self.svc.caller_utterance(self.cid, "点検は無料ですか", "p2")["forward"])
        self.assertTrue(self.svc.tool(self.cid, "lookup_faq", {"question": "点検は無料ですか"})["found"])
        self.assertTrue(self.svc.tool(self.cid, "save_field", {"field": "request", "value": "清掃"})["ok"])

    def test_human_request_is_not_a_refusal(self):
        r = self.svc.caller_utterance(self.cid, "人と話したいです", "p1")
        self.assertEqual((r["forward"], r["stop_ai"], r["human_request"]), (True, False, True))
        self.assertTrue(self.svc.ai_allowed(self.cid))

    def test_negated_refusal_keeps_the_ai(self):
        r = self.svc.caller_utterance(self.cid, "AIは嫌ではありません", "p1")
        self.assertTrue(r["forward"])
        self.assertTrue(self.svc.ai_allowed(self.cid))

    def test_duplicate_refusal_event_is_ignored_safely(self):
        self.svc.caller_utterance(self.cid, "録音はしないでください", "p1")
        r = self.svc.caller_utterance(self.cid, "録音はしないでください", "p1")
        self.assertTrue(r.get("duplicate"))


if __name__ == "__main__":
    unittest.main()
