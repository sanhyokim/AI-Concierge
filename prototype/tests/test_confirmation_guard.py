"""Regression tests for the confirmation guard (review of fc81edd, items 1, ★1 and ★2)."""
import time
import unittest

from prototype.concierge.confirmation import Reply, classify_reply
from prototype.engines.tools import ToolHandler
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store

NUM = {"field": "callback_number", "value": "09012345678"}


def handler_with_readback():
    h = ToolHandler()
    h.handle("save_field", {"field": "callback_number", "value": "090-1234-5678"})
    h.handle("request_readback", {"field": "callback_number"})
    return h


class ClassifyTest(unittest.TestCase):
    def test_affirmation_followed_by_a_correction_is_not_an_affirmation(self):
        self.assertEqual(classify_reply("はい、間違いありません。でも末尾は5679です"), Reply.NEGATIVE)
        self.assertEqual(classify_reply("合ってますけど、日付を変えてください"), Reply.NEGATIVE)

    def test_plain_affirmations_still_confirm(self):
        for t in ("はい、間違いありません", "間違いないです", "はい、合っています", "はい、それでお願いします",
                  "はい、いつでも大丈夫です"):
            self.assertEqual(classify_reply(t), Reply.AFFIRMATIVE, t)


class GuardTest(unittest.TestCase):
    def test_review_case_1_an_earlier_yes_is_not_reused(self):
        h = ToolHandler()
        h.caller_said("はい", "u1")                       # said before the number was even saved
        h.handle("save_field", {"field": "callback_number", "value": "090-1234-5678"})
        h.handle("request_readback", {"field": "callback_number"})
        r = h.handle("confirm_field", NUM)
        self.assertFalse(r["ok"])
        self.assertIn("no caller reply after the read-back", r["reason"])
        self.assertEqual(h.store.get("callback_number").status.value, "awaiting_confirmation")

    def test_review_case_2_affirmation_with_a_correction_keeps_the_old_number_unconfirmed(self):
        h = handler_with_readback()
        h.caller_said("はい、間違いありません。でも末尾は5679です", "u1")
        self.assertFalse(h.handle("confirm_field", NUM)["ok"])
        # the corrected value is saved, read back again and confirmed by a new reply
        h.handle("save_field", {"field": "callback_number", "value": "090-1234-5679"})
        h.handle("request_readback", {"field": "callback_number"})
        h.caller_said("はい、間違いありません", "u2")
        self.assertTrue(h.handle("confirm_field", {"field": "callback_number", "value": "09012345679"})["ok"])

    def test_different_digits_in_the_reply_block_the_confirmation(self):
        h = handler_with_readback()
        h.caller_said("はい、5679です", "u1")
        r = h.handle("confirm_field", NUM)
        self.assertFalse(r["ok"])
        self.assertIn("different number", r["reason"])

    def test_normal_end_of_the_read_back_then_yes_confirms(self):
        h = handler_with_readback()
        h.caller_said("はい、合っています", "u1")
        self.assertTrue(h.handle("confirm_field", NUM)["ok"])

    def test_vendor_interruption_invalidates_only_unanswered_read_backs(self):
        h = handler_with_readback()
        self.assertEqual(h.readback_interrupted("gemini:interrupted"), ["callback_number"])
        h.caller_said("はい", "u1")                       # the barge-in itself
        r = h.handle("confirm_field", NUM)
        self.assertFalse(r["ok"])
        self.assertIn("interrupted", r["reason"])
        h.handle("request_readback", {"field": "callback_number"})   # read it back again
        h.caller_said("はい、合っています", "u2")
        self.assertTrue(h.handle("confirm_field", NUM)["ok"])

    def test_interruption_after_the_reply_does_not_invalidate(self):
        h = handler_with_readback()
        h.caller_said("はい、合っています", "u1")
        self.assertEqual(h.readback_interrupted("gemini:interrupted"), [])   # a later AI turn was cut off
        self.assertTrue(h.handle("confirm_field", NUM)["ok"])

    def test_duplicate_transcript_event_is_ignored(self):
        h = handler_with_readback()
        self.assertTrue(h.caller_said("はい、合っています", "u1"))
        self.assertFalse(h.caller_said("はい、合っています", "u1"))
        self.assertEqual(h.utterance_seq, 1)

    def test_one_reply_confirms_one_item_only(self):
        h = ToolHandler()
        h.handle("save_field", {"field": "callback_number", "value": "090-1234-5678"})
        h.handle("save_field", {"field": "preferred_datetime", "value": "2026-10-09T15:00"})
        h.handle("request_readback", {"field": "callback_number"})
        h.handle("request_readback", {"field": "preferred_datetime"})
        h.caller_said("はい", "u1")
        self.assertTrue(h.handle("confirm_field", NUM)["ok"])
        r = h.handle("confirm_field", {"field": "preferred_datetime", "value": "2026-10-09T15:00"})
        self.assertFalse(r["ok"])
        self.assertIn("already used", r["reason"])

    def test_late_confirmation_of_the_old_value_after_a_correction(self):
        h = handler_with_readback()
        h.caller_said("違います、5679です", "u1")
        h.handle("save_field", {"field": "callback_number", "value": "090-1234-5679"})
        h.caller_said("はい", "u2")
        self.assertFalse(h.handle("confirm_field", NUM)["ok"])          # old value, no read-back of the new one
        h.handle("request_readback", {"field": "callback_number"})
        h.caller_said("はい", "u3")
        self.assertFalse(h.handle("confirm_field", NUM)["ok"])          # old value against the new read-back
        self.assertTrue(h.handle("confirm_field", {"field": "callback_number", "value": "09012345679"})["ok"])

    def test_read_only_last_utterance_cannot_be_assigned(self):
        h = handler_with_readback()
        with self.assertRaises(AttributeError):
            h.last_caller_utterance = "はい、合っています"


class ServiceGuardTest(unittest.TestCase):
    def setUp(self):
        self.svc = ReceptionService(Store(":memory:"), env={})
        self.cid = self.svc.start_call(at="2026-10-05T18:00")["id"]
        self.svc.tool(self.cid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        self.svc.tool(self.cid, "request_readback", {"field": "callback_number"})

    def test_text_attached_to_a_tool_call_is_not_a_reply(self):
        t0 = time.monotonic()
        r = self.svc.tool(self.cid, "confirm_field", NUM, caller_utterance="はい、合っています")
        self.assertFalse(r["ok"])
        self.assertGreaterEqual(time.monotonic() - t0, 1.0)   # it waited for a transcript event, then refused

    def test_review_case_1_through_the_service(self):
        svc = ReceptionService(Store(":memory:"), env={})
        cid = svc.start_call(at="2026-10-05T18:00")["id"]
        svc.caller_utterance(cid, "はい")
        svc.tool(cid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        svc.tool(cid, "request_readback", {"field": "callback_number"})
        self.assertFalse(svc.tool(cid, "confirm_field", NUM)["ok"])

    def test_reply_arriving_shortly_after_the_tool_call_is_used(self):
        import threading
        threading.Timer(0.3, lambda: self.svc.caller_utterance(self.cid, "はい、合っています", "p1")).start()
        self.assertTrue(self.svc.tool(self.cid, "confirm_field", NUM)["ok"])

    def test_vendor_interruption_through_the_service(self):
        self.assertEqual(self.svc.ai_interrupted(self.cid, "cartesia:audio_output_clear")["invalidated"],
                         ["callback_number"])
        self.svc.caller_utterance(self.cid, "はい", "p1")
        self.assertFalse(self.svc.tool(self.cid, "confirm_field", NUM)["ok"])


if __name__ == "__main__":
    unittest.main()
