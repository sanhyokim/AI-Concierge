"""The Grok C5 test of 2026-10-30 (record 20261030-094736) and the user's notes on it.

「「株式会社野田のAIオペレーターでございます。」の「株式会社野田、AIオペレーター」に変更」「突然英語を言い出した」
"""
import unittest
from unittest import mock

from prototype.concierge.prompts import GREETING, OPENING_ANNOUNCEMENT, OPENING_NOTE, VOICE_RULES
from prototype.reception import service as service_mod
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


class OpeningTest(unittest.TestCase):
    def test_the_company_and_the_ai_operator_are_said_apart(self):
        for text in (OPENING_ANNOUNCEMENT, GREETING, OPENING_NOTE):
            self.assertIn("株式会社野田、AIオペレーターでございます", text)
            self.assertNotIn("野田のAIオペレーター", text)


class NoEnglishTest(unittest.TestCase):
    def test_a_refusal_that_only_waits_for_the_caller_asks_for_no_new_answer(self):
        # 109.15 s: confirm_field reached the server before the caller's 「はい」 (0.15 s after the wait); the AI
        # then said 「Waiting for the customer's response.」
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T09:47")["id"]
        svc.caller_utterance(cid, "08056442050")
        say = svc.tool(cid, "save_field", {"field": "callback_number", "value": "08056442050"})["read_back_now"]["say"]
        svc.ai_utterance(cid, say)
        with mock.patch.object(service_mod, "CONFIRM_WAIT_S", 0.05):
            res = svc.tool(cid, "confirm_field", {"field": "callback_number", "value": "08056442050"})
        self.assertFalse(res["ok"])
        self.assertTrue(res["wait_silently"])
        svc.caller_utterance(cid, "はい。")
        res = svc.tool(cid, "confirm_field", {"field": "callback_number", "value": "08056442050"})
        self.assertTrue(res["ok"])
        self.assertNotIn("wait_silently", res)

    def test_the_wait_for_the_reply_is_longer(self):
        self.assertGreaterEqual(service_mod.CONFIRM_WAIT_S, 4.0)

    def test_the_voice_model_is_told_to_speak_only_japanese(self):
        self.assertIn("日本語だけで話します", VOICE_RULES)


if __name__ == "__main__":
    unittest.main()
